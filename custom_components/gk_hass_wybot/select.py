"""Select platform for WyBot integration (Clean Time + Cleaning Mode)."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .entity import (
    _get_coordinator_from_entry_data,
    display_name_for_group,
    resolve_target_id,
)
from .wybot_coordinator import WyBotCoordinator
from .wybot_dp_models import DP

_LOGGER = logging.getLogger(__name__)

_CLEAN_TIME_PRESETS = {"1h": 60, "2h": 120, "3h": 180, "4h": 240}
_MINUTES_TO_CLEAN_TIME_OPTION = {v: k for k, v in _CLEAN_TIME_PRESETS.items()}

_CLEAN_MODE_PRESETS: dict[str, int] = {
    "Floor Only": 0x00,
    "Wall Only": 0x01,
    "Standard Full Pool": 0x03,
    "Waterline Only": 0x04,
}
_CODE_TO_CLEAN_MODE_OPTION = {v: k for k, v in _CLEAN_MODE_PRESETS.items()}


def _registry_has_unique_id(reg: er.EntityRegistry, unique_id: str) -> bool:
    return any(
        e.platform == DOMAIN and e.unique_id == unique_id
        for e in reg.entities.values()
    )


def _choose_select_unique_id(reg: er.EntityRegistry, prefix: str, group_id: str, target_id: str) -> str:
    """
    Prefer group_id-based unique_id (so duplicates stop),
    but if HA already has an entity with a different ID, reuse it.
    """
    cand_group = f"{prefix}{group_id}"
    cand_target = f"{prefix}{target_id}"

    if _registry_has_unique_id(reg, cand_group):
        return cand_group
    if _registry_has_unique_id(reg, cand_target):
        return cand_target

    # Fresh install: create the group-based one
    return cand_group


def _dp15_minutes_from_hex(raw: str | None) -> int | None:
    if not raw or not isinstance(raw, str) or len(raw) < 2:
        return None
    try:
        return int(raw[0:2], 16)
    except Exception:
        return None


def _dp15_hex_from_minutes(minutes: int) -> str:
    return f"{minutes:02x}000000"


def _dp1_code_from_hex(raw: str | None) -> int | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip().lower()
    if len(raw) < 2:
        return None
    try:
        return int(raw[0:2], 16)
    except Exception:
        return None


def _dp1_hex_from_code(code: int) -> str:
    return f"{int(code) & 0xFF:02x}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyBotCoordinator = _get_coordinator_from_entry_data(entry_data)
    reg = er.async_get(hass)

    entities = []
    for group_id, group in coordinator.data.items():
        target_id = resolve_target_id(group)
        if not target_id:
            continue

        clean_time_uid = _choose_select_unique_id(reg, "wybot_clean_time_", str(group_id), str(target_id))
        cleaning_mode_uid = _choose_select_unique_id(reg, "wybot_cleaning_mode_", str(group_id), str(target_id))

        entities.append(
            WyBotCleanTimeSelect(
                group_id=str(group_id),
                target_id=str(target_id),
                unique_id=clean_time_uid,
                coordinator=coordinator,
            )
        )
        entities.append(
            WyBotCleaningModeSelect(
                group_id=str(group_id),
                target_id=str(target_id),
                unique_id=cleaning_mode_uid,
                coordinator=coordinator,
            )
        )

    async_add_entities(entities, update_before_add=True)


class _WyBotBaseSelect(CoordinatorEntity, SelectEntity):
    """Shared bits for WyBot selects."""

    def __init__(self, group_id: str, target_id: str, unique_id: str, coordinator: WyBotCoordinator) -> None:
        super().__init__(coordinator=coordinator, context=target_id)
        self._group_id = str(group_id)
        self._target_id = str(target_id)
        self._unique_id = str(unique_id)
        self._group = coordinator.data[self._group_id]

    @callback
    def _handle_coordinator_update(self) -> None:
        self._group = self.coordinator.data[str(self._group_id)]
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> DeviceInfo:
        group = self._group
        name = display_name_for_group(group) if group else "WyBot"

        identifiers: set[tuple[str, str]] = {(DOMAIN, self._group_id)}
        if self._target_id:
            identifiers.add((DOMAIN, self._target_id))

        return DeviceInfo(
            identifiers=identifiers,
            name=name,
            manufacturer=MANUFACTURER,
            model=getattr(group.device, "device_type", None),
        )

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "group_id": self._group_id,
            "target_id": self._target_id,
            "model": getattr(self._group.device, "device_type", None),
        }


class WyBotCleanTimeSelect(_WyBotBaseSelect):
    """Select entity that controls WyBot clean time preset via DP15."""

    _attr_name = "Clean Time"
    _attr_options = list(_CLEAN_TIME_PRESETS.keys())

    @property
    def current_option(self) -> str | None:
        raw = None
        try:
            dp15 = self._group.device.dps.get("15") or self._group.device.dps.get(15)
            raw = getattr(dp15, "data", None)
        except Exception:
            raw = None

        minutes = _dp15_minutes_from_hex(raw)
        if minutes is None:
            return None
        return _MINUTES_TO_CLEAN_TIME_OPTION.get(minutes)

    async def async_select_option(self, option: str) -> None:
        minutes = _CLEAN_TIME_PRESETS.get(option)
        if minutes is None:
            raise ValueError(f"Unsupported clean time option: {option}")

        payload = _dp15_hex_from_minutes(minutes)
        dp = DP(id=15, type=2, len=4, data=payload)

        _LOGGER.debug("WYBOT: setting clean time via DP15 minutes=%s payload=%s", minutes, payload)
        self.coordinator.send_write_command(self._group, dp)


class WyBotCleaningModeSelect(_WyBotBaseSelect):
    """Select entity that controls WyBot cleaning mode via DP1."""

    _attr_name = "Cleaning Mode"
    _attr_options = list(_CLEAN_MODE_PRESETS.keys())

    @property
    def current_option(self) -> str | None:
        raw = None
        try:
            dp1 = self._group.device.dps.get("1") or self._group.device.dps.get(1)
            raw = getattr(dp1, "data", None)
        except Exception:
            raw = None

        code = _dp1_code_from_hex(raw)
        if code is None:
            return None
        return _CODE_TO_CLEAN_MODE_OPTION.get(code, f"Mode {code:02X}")

    async def async_select_option(self, option: str) -> None:
        code = _CLEAN_MODE_PRESETS.get(option)
        if code is None:
            raise ValueError(f"Unsupported cleaning mode option: {option}")

        payload = _dp1_hex_from_code(code)
        dp = DP(id=1, type=4, len=1, data=payload)

        _LOGGER.debug("WYBOT: setting cleaning mode via DP1 code=0x%02X payload=%s", code, payload)
        self.coordinator.send_write_command(self._group, dp)
