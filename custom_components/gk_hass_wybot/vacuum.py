"""Platform for WyBot vacuum integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import entity_platform

from .const import DOMAIN, MANUFACTURER
from .entity import (
    _get_coordinator_from_entry_data,
    display_name_for_group,
    resolve_target_id,
)
from .wybot_coordinator import WyBotCoordinator
from .wybot_dp_models import (
    build_dp_cleaning_command,
    Battery,
    BatteryState,
    CleaningMode,
    CleaningStatus,
    CleaningStatusMode,
    Dock,
    DockStatus,
    DP,
)

_LOGGER = logging.getLogger(__name__)


def _dp15_clean_time(minutes: int) -> DP:
    """Build DP15 clean-time payload."""
    minutes = int(minutes)
    if minutes < 0:
        minutes = 0
    if minutes > 255:
        minutes = 255
    data = f"{minutes:02x}000000"
    return DP(id=15, type=2, len=4, data=data)


def _snap_clean_time_minutes(minutes: int) -> int:
    """WY460 app supports only 1h/2h/3h/4h."""
    allowed = (60, 120, 180, 240)
    minutes = int(minutes)
    if minutes in allowed:
        return minutes
    return min(allowed, key=lambda x: abs(x - minutes))


def _registry_has_unique_id(reg: er.EntityRegistry, unique_id: str) -> bool:
    return any(
        e.platform == DOMAIN and e.unique_id == unique_id
        for e in reg.entities.values()
    )


def _choose_vacuum_unique_id(reg: er.EntityRegistry, group_id: str, target_id: str) -> str:
    """
    Pick a stable unique_id without creating duplicates.

    Prefer an existing one (to preserve entity_id), otherwise default to the target_id-based one.
    """
    cand_target = f"wybot_vacuum_{target_id}"
    cand_group = f"wybot_vacuum_{group_id}"

    if _registry_has_unique_id(reg, cand_target):
        return cand_target
    if _registry_has_unique_id(reg, cand_group):
        return cand_group

    return cand_target


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the vacuum platform."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyBotCoordinator = _get_coordinator_from_entry_data(entry_data)

    # --- Compatibility for HA device-page "Controls" ---
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("turn_on", {}, "async_start")
    platform.async_register_entity_service("turn_off", {}, "async_stop")

    reg = er.async_get(hass)

    entities: list[WyBotVacuum] = []
    for group_id, group in coordinator.data.items():
        target_id = resolve_target_id(group)
        if not target_id:
            continue

        unique_id = _choose_vacuum_unique_id(reg, str(group_id), str(target_id))
        entities.append(
            WyBotVacuum(
                group_id=str(group_id),
                target_id=str(target_id),
                unique_id=unique_id,
                coordinator=coordinator,
            )
        )

    async_add_entities(entities, update_before_add=True)


class WyBotVacuum(CoordinatorEntity, StateVacuumEntity):
    """A WyBot vacuum entity."""

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
        """
        IMPORTANT: Must match wybot/entity.py so *all* entities converge on the same HA device.
        """
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
    def unique_id(self) -> str | None:
        return self._unique_id

    @property
    def name(self) -> str | None:
        return display_name_for_group(self._group)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "group_id": self._group_id,
            "target_id": self._target_id,
            "model": getattr(self._group.device, "device_type", None),
        }

    @property
    def activity(self) -> VacuumActivity | None:
        # Fallback to raw DP0
        raw_dp0 = None
        try:
            dp0 = self._group.device.dps.get("0") or self._group.device.dps.get(0)
            raw_dp0 = getattr(dp0, "data", None)
        except Exception:
            raw_dp0 = None

        # Best-effort parse
        try:
            battery = self._group.get_dp(Battery)
        except TypeError:
            _LOGGER.debug("WYBOT: Battery DP model mismatch; skipping battery parsing")
            battery = None

        cleaning_status = self._group.get_dp(CleaningStatus)
        dock_status = self._group.get_dp(Dock)

        if cleaning_status is None or dock_status is None:
            if raw_dp0 == "02":
                return VacuumActivity.CLEANING
            if raw_dp0 == "03":
                return VacuumActivity.RETURNING
            if raw_dp0 == "01":
                return VacuumActivity.DOCKED
            if raw_dp0 == "00":
                return VacuumActivity.PAUSED
            return None

        if battery is not None and battery.charge_state in (
            BatteryState.CHARGING,
            BatteryState.CHARGED,
        ):
            return VacuumActivity.DOCKED

        if dock_status.status == DockStatus.RETURNING:
            return VacuumActivity.RETURNING

        if cleaning_status.status == CleaningStatusMode.STOPPED:
            return VacuumActivity.PAUSED

        if cleaning_status.status in (
            CleaningStatusMode.CLEANING,
            CleaningStatusMode.STARTING,
        ):
            return VacuumActivity.CLEANING

        # final fallback
        if raw_dp0 == "02":
            return VacuumActivity.CLEANING
        if raw_dp0 == "03":
            return VacuumActivity.RETURNING
        if raw_dp0 == "01":
            return VacuumActivity.DOCKED
        if raw_dp0 == "00":
            return VacuumActivity.PAUSED

        return None

    @property
    def fan_speed_list(self):
        try:
            modes = getattr(CleaningMode, "CLEANING_MODES", None)
            if modes:
                return list(modes)
            return [getattr(m, "value", str(m)) for m in list(CleaningMode)]
        except Exception:
            return []

    @property
    def fan_speed(self) -> str | None:
        mode = self._group.get_dp(CleaningMode)
        return mode.cleaning_mode if mode is not None else None

    @property
    def supported_features(self) -> VacuumEntityFeature:
        return (
            VacuumEntityFeature.FAN_SPEED
            | VacuumEntityFeature.RETURN_HOME
            | VacuumEntityFeature.START
            | VacuumEntityFeature.STOP
        )

    async def async_set_fan_speed(self, fan_speed: str) -> None:
        cleaning_mode = CleaningMode(mode=fan_speed)
        self.coordinator.send_write_command(self._group, cleaning_mode)

    async def async_stop(self) -> None:
        model = getattr(self._group.device, "device_type", None)
        cmd = "01" if model == "WY460" else "00"
        dp = build_dp_cleaning_command(cmd)
        self.coordinator.send_write_command(self._group, dp)

    async def async_start(self) -> None:
        model = getattr(self._group.device, "device_type", None)

        if model == "WY460":
            if getattr(self.coordinator, "use_clean_time", False):
                minutes = int(getattr(self.coordinator, "clean_time_minutes", 60))
                minutes = _snap_clean_time_minutes(minutes)
                self.coordinator.send_write_command(self._group, _dp15_clean_time(minutes))
                await asyncio.sleep(0.2)

            dp0 = build_dp_cleaning_command("02")
            self.coordinator.send_write_command(self._group, dp0)
            return

        dp = build_dp_cleaning_command("01")
        self.coordinator.send_write_command(self._group, dp)

    async def async_return_to_base(self) -> None:
        dp = build_dp_cleaning_command("03")
        self.coordinator.send_write_command(self._group, dp)
