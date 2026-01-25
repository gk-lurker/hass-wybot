from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .const import DOMAIN
from .entity import (
    WyBotBaseEntity,
    WyBotEntityContext,
    _get_coordinator_from_entry_data,
    display_name_for_group,
    resolve_target_id,
)
from .wybot_coordinator import WyBotCoordinator


# Confirmed by you (matches app):
# 00 - Floor Only
# 01 - Wall Only
# 03 - Standard Full Pool
# 04 - Waterline Only
_CLEAN_MODE_PRESETS: dict[int, str] = {
    0x00: "Floor Only",
    0x01: "Wall Only",
    0x03: "Standard Full Pool",
    0x04: "Waterline Only",
}


def _extract_dp_payload(group: Any, dp_id: int) -> Any:
    """Return the raw DP payload for dp_id from group.device.dps."""
    device = getattr(group, "device", None)
    dps = getattr(device, "dps", None) or {}

    dp = None
    try:
        dp = dps.get(str(dp_id))
    except Exception:
        dp = None

    if dp is None:
        try:
            dp = dps.get(dp_id)
        except Exception:
            dp = None

    if dp is None:
        return None

    raw = getattr(dp, "data", None)
    if raw is not None:
        return raw

    if isinstance(dp, dict):
        return dp.get("data")

    return dp


def _dp_first_byte_to_int(raw: Any) -> int | None:
    """Convert DP payloads to an int (supports int/bytes/decimal str/hex str)."""
    if raw is None:
        return None

    if isinstance(raw, int):
        return raw

    if isinstance(raw, (bytes, bytearray)):
        return int(raw[0]) if len(raw) > 0 else None

    if isinstance(raw, str):
        s = raw.strip().lower()
        if not s:
            return None

        if s.startswith("0x"):
            s = s[2:]

        # decimal
        if s.isdigit():
            try:
                return int(s, 10)
            except Exception:
                return None

        # hex-ish: strip to hex chars only, take first byte
        s = "".join(ch for ch in s if ch in "0123456789abcdef")
        if not s:
            return None

        if len(s) == 1:
            try:
                return int(s, 16)
            except Exception:
                return None

        try:
            return int(s[0:2], 16)
        except Exception:
            return None

    try:
        return int(raw)
    except Exception:
        return None


def _pick_temperature_from_group(group: Any) -> tuple[float | None, int | None, Any]:
    """Best-effort temperature picker. Returns (temp_c, dp_id_used, raw_payload)."""
    for attr in ("temperature", "temp", "water_temperature", "water_temp"):
        try:
            v = getattr(group, attr, None)
            if v is not None:
                try:
                    return float(v), None, v
                except Exception:
                    pass
        except Exception:
            pass

        if isinstance(group, dict) and attr in group:
            v = group.get(attr)
            if v is not None:
                try:
                    return float(v), None, v
                except Exception:
                    pass

    # DP ids to try first (we'll refine if you confirm a specific dp_id later)
    preferred = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20]
    exclude = {1, 15}  # DP1=mode, DP15=time

    def norm(v: int) -> float | None:
        if 100 <= v <= 800:
            return v / 10.0
        if -10 <= v <= 60:
            return float(v)
        return None

    for dp_id in preferred:
        if dp_id in exclude:
            continue
        raw = _extract_dp_payload(group, dp_id)
        if raw is None:
            continue
        iv = _dp_first_byte_to_int(raw)
        if iv is None:
            continue
        tv = norm(iv)
        if tv is not None:
            return tv, dp_id, raw

    # fallback scan all dps keys
    device = getattr(group, "device", None)
    dps = getattr(device, "dps", None) or {}
    keys: list[int] = []
    for k in dps.keys():
        try:
            keys.append(int(k))
        except Exception:
            continue
    keys = sorted(set(keys))

    for dp_id in keys:
        if dp_id in exclude:
            continue
        raw = _extract_dp_payload(group, dp_id)
        if raw is None:
            continue
        iv = _dp_first_byte_to_int(raw)
        if iv is None:
            continue
        tv = norm(iv)
        if tv is not None:
            return tv, dp_id, raw

    return None, None, None


def _pretty_status(s: str | None) -> str | None:
    if not s:
        return None
    s2 = str(s).strip().replace("_", " ")
    if not s2:
        return None
    return s2[:1].upper() + s2[1:]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyBotCoordinator = _get_coordinator_from_entry_data(entry_data)

    entities: list[SensorEntity] = []

    for group_id, group in coordinator.data.items():
        target_id = resolve_target_id(group)
        if not target_id:
            continue

        base_name = display_name_for_group(group)
        ctx = WyBotEntityContext(group_id=str(group_id), target_id=str(target_id))

        entities.append(
            WyBotSecondsSinceHeardSensor(
                coordinator=coordinator,
                ctx=ctx,
                name=f"{base_name} Seconds Since Heard",
                unique_id=f"{target_id}_seconds_since_heard",
            )
        )

        # DP15 confirmation sensor
        entities.append(
            WyBotCleanTimeMinutesSensor(
                coordinator=coordinator,
                ctx=ctx,
                name="Clean Time (min)",
                unique_id=f"{group_id}_clean_time_minutes",
            )
        )

        # DP1 confirmation sensor
        entities.append(
            WyBotCleaningModeSensor(
                coordinator=coordinator,
                ctx=ctx,
                name="Cleaning Mode",
                unique_id=f"{group_id}_cleaning_mode",
            )
        )

        # revive existing entities (match entity_registry unique_ids)
        entities.append(
            WyBotTemperatureSensor(
                coordinator=coordinator,
                ctx=ctx,
                name="Temperature",
                unique_id=f"{group_id}_temperature",
            )
        )

        entities.append(
            WyBotCleaningStatusSensor(
                coordinator=coordinator,
                ctx=ctx,
                name="Cleaning Status",
                unique_id=f"{group_id}_cleaning_status",
            )
        )

    async_add_entities(entities)


class WyBotSecondsSinceHeardSensor(WyBotBaseEntity, SensorEntity):
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Tick so the UI shows the "age" increasing even when no new MQTT arrives.
        if self.hass:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass,
                    self._tick,
                    timedelta(seconds=5),
                )
            )

    @callback
    def _tick(self, _now) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self.coordinator.seconds_since_heard(self.ctx.target_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "target_id": self.ctx.target_id,
            "online": self.coordinator.is_online(self.ctx.target_id),
        }


class WyBotCleanTimeMinutesSensor(WyBotBaseEntity, SensorEntity):
    """DP15 confirmation sensor: clean time minutes (first byte)."""

    _attr_icon = "mdi:timer"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        raw = _extract_dp_payload(self.group, 15)
        return _dp_first_byte_to_int(raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = _extract_dp_payload(self.group, 15)
        return {
            "target_id": self.ctx.target_id,
            "dp_id": 15,
            "dp_raw": raw,
            "dp_raw_type": type(raw).__name__ if raw is not None else None,
        }


class WyBotCleaningModeSensor(WyBotBaseEntity, SensorEntity):
    """DP1 confirmation sensor: cleaning mode code -> label."""

    _attr_icon = "mdi:format-list-bulleted"

    @property
    def native_value(self) -> str | None:
        raw = _extract_dp_payload(self.group, 1)
        code = _dp_first_byte_to_int(raw)
        if code is None:
            return None
        return _CLEAN_MODE_PRESETS.get(code, f"Mode {code:02X}")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = _extract_dp_payload(self.group, 1)
        code = _dp_first_byte_to_int(raw)
        return {
            "target_id": self.ctx.target_id,
            "dp_id": 1,
            "dp_raw": raw,
            "dp_raw_type": type(raw).__name__ if raw is not None else None,
            "dp_code": code,
        }


class WyBotCleaningStatusSensor(WyBotBaseEntity, SensorEntity):
    """
    Cleaning Status sensor that mirrors the vacuum state.

    Bind to the vacuum entity that is attached to the SAME device as this sensor
    (via entity_registry), so robots won't accidentally bind to each other.
    """

    _attr_icon = "mdi:robot-vacuum"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._vacuum_entity_id: str | None = None
        self._cached_status: str | None = None
        self._cached_source: str | None = None
        self._cached_raw: Any = None
        self._device_id_from_registry: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self._vacuum_entity_id = self._find_vacuum_entity_id_for_my_device()

        if self._vacuum_entity_id and self.hass:
            st = self.hass.states.get(self._vacuum_entity_id)
            self._update_from_vacuum_state(st)

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._vacuum_entity_id],
                    self._handle_vacuum_event,
                )
            )

        self.async_write_ha_state()

    def _find_vacuum_entity_id_for_my_device(self) -> str | None:
        if not self.hass or not self.entity_id:
            return None

        reg = er.async_get(self.hass)

        my_entry = reg.async_get(self.entity_id)
        if not my_entry or not my_entry.device_id:
            return None

        self._device_id_from_registry = my_entry.device_id

        entries = er.async_entries_for_device(
            reg, my_entry.device_id, include_disabled_entities=True
        )
        vacs = [e for e in entries if e.domain == "vacuum"]

        if not vacs:
            return None

        vacs.sort(key=lambda e: 0 if e.platform == DOMAIN else 1)
        return vacs[0].entity_id

    @callback
    def _handle_vacuum_event(self, event) -> None:
        new_state = event.data.get("new_state")
        self._update_from_vacuum_state(new_state)
        self.async_write_ha_state()

    def _update_from_vacuum_state(self, st) -> None:
        if st is None:
            return

        attrs = st.attributes or {}

        for key in ("status", "cleaning_status", "state_text", "activity"):
            v = attrs.get(key)
            if isinstance(v, str) and v.strip():
                self._cached_status = _pretty_status(v)
                self._cached_source = f"vacuum_attr:{key}"
                self._cached_raw = v
                return

        self._cached_status = _pretty_status(st.state)
        self._cached_source = "vacuum_state"
        self._cached_raw = st.state

    @property
    def native_value(self) -> str | None:
        return self._cached_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "target_id": self.ctx.target_id,
            "device_id": self._device_id_from_registry,
            "vacuum_entity_id": self._vacuum_entity_id,
            "status_source": self._cached_source,
            "status_raw": self._cached_raw,
        }


class WyBotTemperatureSensor(WyBotBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    @property
    def native_value(self) -> float | None:
        temp_c, _, _ = _pick_temperature_from_group(self.group)
        return temp_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        temp_c, dp_id, raw = _pick_temperature_from_group(self.group)
        return {
            "target_id": self.ctx.target_id,
            "temp_c": temp_c,
            "dp_used": dp_id,
            "dp_raw": raw,
            "dp_raw_type": type(raw).__name__ if raw is not None else None,
        }
