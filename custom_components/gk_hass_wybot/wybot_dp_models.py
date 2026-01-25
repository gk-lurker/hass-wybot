"""WyBot DP models + parsing helpers.

This module provides:
- DP: a simple datapoint container (id/type/len/data)
- GenericDP: base class for parsed/typed DPs (so wybot_models.get_dp() can do issubclass checks)
- Parsed DP models used by coordinator/entities:
  - CleaningStatus (dp=0)
  - CleaningMode   (dp=1)
  - Battery        (dp=50)   (may be 0/0 on wall-powered models)
  - Dock           (dp=11)
- Helpers/constants for building commands and mapping mode/status codes.

Design goals:
- Defensive parsing (firmwares differ)
- No pydantic models here (avoids pydantic v1/v2 mixing issues)
- Keep representations stable for logging/debug
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Any, ClassVar, Optional

_LOGGER = logging.getLogger(__name__)

# --- Low-level DP container -------------------------------------------------


@dataclass(frozen=True)
class DP:
    """Datapoint container.

    Matches your logs, e.g.: DP(id=1, type=4, len=1, data='03')
    """
    id: int
    type: Optional[int] = None
    len: Optional[int] = None
    data: Optional[str] = None  # hex string without 0x, e.g. "03" or "0e"


def _hex_to_int(hex_str: Optional[str], default: int = 0) -> int:
    if not hex_str:
        return default
    try:
        return int(hex_str, 16)
    except Exception:
        return default


def _hex_to_bytes(hex_str: Optional[str]) -> bytes:
    if not hex_str:
        return b""
    try:
        return bytes.fromhex(hex_str)
    except Exception:
        return b""


def _first_byte(dp: DP, default: int = 0) -> int:
    raw = _hex_to_bytes(dp.data)
    if raw:
        return raw[0]
    return _hex_to_int(dp.data, default=default)


# --- Base typed DP ----------------------------------------------------------


class GenericDP:
    """Base class for typed/parsed DPs.

    Your wybot_models.get_dp() uses:
        issubclass(cls, GenericDP)

    So this must be a real class (not typing.Any).
    """

    DP_ID: ClassVar[int] = -1  # subclasses override

    @classmethod
    def parse(cls, dp: DP) -> "GenericDP":
        """Parse a raw DP into a typed instance."""
        return cls()

    @classmethod
    def dp_id(cls) -> int:
        return int(getattr(cls, "DP_ID", -1))


# --- Enums ------------------------------------------------------------------


class CleaningStatusMode(str, Enum):
    UNKNOWN = "unknown"
    STOPPED = "stopped"
    CLEANING = "cleaning"
    PAUSED = "paused"
    RETURNING = "returning"
    DOCKED = "docked"
    ERROR = "error"


class BatteryState(str, Enum):
    UNKNOWN = "unknown"
    CHARGING = "charging"
    DISCHARGING = "discharging"
    FULL = "full"


class DockStatus(str, Enum):
    UNKNOWN = "unknown"
    GENERAL = "general"
    DOCKED = "docked"
    UNDOCKED = "undocked"
    ERROR = "error"


# Cleaning mode: keep labels exactly as you requested.
class CleaningModeLabel(str, Enum):
    UNKNOWN = "unknown"
    FLOOR_ONLY = "Floor Only"
    WALL_ONLY = "Wall Only"
    STANDARD_FULL_POOL = "Standard Full Pool"
    WATERLINE_ONLY = "Waterline Only"


# --- DP ID constants ---------------------------------------------------------

DP_ID_CLEANING_STATUS = 0
DP_ID_CLEANING_MODE = 1
DP_ID_DOCK = 11
DP_ID_BATTERY = 50

# --- Code maps ---------------------------------------------------------------

# Best-effort status mapping. Adjust later if your unit reports different codes.
_CLEANING_STATUS_CODE_MAP: dict[int, CleaningStatusMode] = {
    0x00: CleaningStatusMode.STOPPED,
    0x01: CleaningStatusMode.DOCKED,     # some firmwares use 01 for "idle/docked"
    0x02: CleaningStatusMode.CLEANING,
    0x03: CleaningStatusMode.RETURNING,
    0x04: CleaningStatusMode.PAUSED,     # observed sometimes
    0x05: CleaningStatusMode.ERROR,
}

# Your observed WYBOT mode codes:
# 00 Floor Only
# 01 Wall Only
# 03 Standard Full Pool
# 04 Waterline Only
#
# Keeping 0x0E as an alias for Standard Full Pool because you previously observed it.
CLEANING_MODE_CODE_TO_LABEL: dict[int, str] = {
    0x00: CleaningModeLabel.FLOOR_ONLY.value,
    0x01: CleaningModeLabel.WALL_ONLY.value,
    0x03: CleaningModeLabel.STANDARD_FULL_POOL.value,
    0x04: CleaningModeLabel.WATERLINE_ONLY.value,
    0x0E: CleaningModeLabel.STANDARD_FULL_POOL.value,  # alias observed earlier
}

CLEANING_MODE_LABEL_TO_CODE: dict[str, int] = {
    v: k for k, v in CLEANING_MODE_CODE_TO_LABEL.items()
    if k in (0x00, 0x01, 0x03, 0x04)  # keep the "real" 4 modes for UI writeback
}

# For UIs (select/options), expose the “nice” labels in a stable order
CLEANING_MODE_OPTIONS: list[str] = [
    CleaningModeLabel.FLOOR_ONLY.value,
    CleaningModeLabel.WALL_ONLY.value,
    CleaningModeLabel.STANDARD_FULL_POOL.value,
    CleaningModeLabel.WATERLINE_ONLY.value,
]

_DOCK_STATUS_CODE_MAP: dict[int, DockStatus] = {
    0x00: DockStatus.UNKNOWN,
    0x01: DockStatus.GENERAL,
    0x02: DockStatus.DOCKED,
    0x03: DockStatus.UNDOCKED,
    0x04: DockStatus.ERROR,
}

_BATTERY_STATE_CODE_MAP: dict[int, BatteryState] = {
    0x00: BatteryState.UNKNOWN,
    0x01: BatteryState.DISCHARGING,
    0x02: BatteryState.CHARGING,
    0x03: BatteryState.FULL,
}


# --- Typed DP models ---------------------------------------------------------


@dataclass
class CleaningStatus(GenericDP):
    DP_ID: ClassVar[int] = DP_ID_CLEANING_STATUS
    status: CleaningStatusMode = CleaningStatusMode.UNKNOWN
    raw_code: Optional[int] = None

    @classmethod
    def parse(cls, dp: DP) -> "CleaningStatus":
        code = _first_byte(dp, default=0)
        status = _CLEANING_STATUS_CODE_MAP.get(code, CleaningStatusMode.UNKNOWN)
        if status is CleaningStatusMode.UNKNOWN and dp.data:
            _LOGGER.debug("WYBOT: unknown cleaning_status code=%s hex=%s", code, dp.data)
        return cls(status=status, raw_code=code)


@dataclass
class CleaningMode(GenericDP):
    DP_ID: ClassVar[int] = DP_ID_CLEANING_MODE
    cleaning_mode: str = CleaningModeLabel.UNKNOWN.value
    raw_code: Optional[int] = None

    @classmethod
    def parse(cls, dp: DP) -> "CleaningMode":
        code = _first_byte(dp, default=0)
        label = CLEANING_MODE_CODE_TO_LABEL.get(code)
        if not label:
            # keep something readable for unknowns
            label = f"Mode {code:02X}"
            if dp.data:
                _LOGGER.debug("WYBOT: unknown cleaning_mode code=%s hex=%s", code, dp.data)
        return cls(cleaning_mode=label, raw_code=code)


@dataclass
class Battery(GenericDP):
    DP_ID: ClassVar[int] = DP_ID_BATTERY
    charge_state: BatteryState = BatteryState.UNKNOWN
    battery_level: Optional[int] = None  # 0-100
    raw_state_code: Optional[int] = None

    @classmethod
    def parse(cls, dp: DP) -> "Battery":
        raw = _hex_to_bytes(dp.data)

        state = BatteryState.UNKNOWN
        level: Optional[int] = None
        state_code: Optional[int] = None

        if len(raw) >= 2:
            state_code = int(raw[0])
            level_code = int(raw[1])
            state = _BATTERY_STATE_CODE_MAP.get(state_code, BatteryState.UNKNOWN)
            if 0 <= level_code <= 100:
                level = level_code
        else:
            # fallback: treat dp.data as percent
            val = _hex_to_int(dp.data, default=-1)
            if 0 <= val <= 100:
                level = val

        return cls(charge_state=state, battery_level=level, raw_state_code=state_code)


@dataclass
class Dock(GenericDP):
    DP_ID: ClassVar[int] = DP_ID_DOCK
    status: DockStatus = DockStatus.UNKNOWN
    raw_code: Optional[int] = None

    @classmethod
    def parse(cls, dp: DP) -> "Dock":
        code = _first_byte(dp, default=0)
        status = _DOCK_STATUS_CODE_MAP.get(code, DockStatus.UNKNOWN)
        if status is DockStatus.UNKNOWN and dp.data:
            _LOGGER.debug("WYBOT: unknown dock_status code=%s hex=%s", code, dp.data)
        return cls(status=status, raw_code=code)


# --- Dispatcher --------------------------------------------------------------


def parse_dp(dp: DP) -> Any:
    """Parse a DP into one of our typed models, based on dp.id."""
    if dp.id == DP_ID_CLEANING_STATUS:
        return CleaningStatus.parse(dp)
    if dp.id == DP_ID_CLEANING_MODE:
        return CleaningMode.parse(dp)
    if dp.id == DP_ID_BATTERY:
        return Battery.parse(dp)
    if dp.id == DP_ID_DOCK:
        return Dock.parse(dp)
    return dp


# --- Helpers for command building -------------------------------------------


def build_dp_cleaning_command(action_hex: str) -> DP:
    """Build a dp0 command payload (cmd=4 is set by coordinator/mqtt wrapper)."""
    action_hex = action_hex.strip().lower()
    if len(action_hex) == 1:
        action_hex = "0" + action_hex
    return DP(id=DP_ID_CLEANING_STATUS, type=4, len=1, data=action_hex)


def build_dp_cleaning_mode_code(code: int) -> DP:
    """Build dp1 write for a raw mode code."""
    code = int(code) & 0xFF
    return DP(id=DP_ID_CLEANING_MODE, type=4, len=1, data=f"{code:02x}")


def build_dp_cleaning_mode_label(label: str) -> DP:
    """Build dp1 write for a UI label (Floor Only, Wall Only, etc)."""
    if label not in CLEANING_MODE_LABEL_TO_CODE:
        raise ValueError(f"Unsupported cleaning mode label: {label}")
    return build_dp_cleaning_mode_code(CLEANING_MODE_LABEL_TO_CODE[label])


# --- Backwards-compat helpers ------------------------------------------------


def wybot_dp_id(obj: Any) -> int:
    """Best-effort DP id extractor for legacy code paths."""
    if isinstance(obj, dict):
        try:
            return int(obj.get("id", -1))
        except Exception:
            return -1
    for attr in ("id", "dp", "dp_id"):
        if hasattr(obj, attr):
            try:
                return int(getattr(obj, attr))
            except Exception:
                pass
    return -1
