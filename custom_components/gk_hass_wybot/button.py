"""Button platform for WyBot integration (Press-to-start / Press-to-stop)."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import (
    WyBotBaseEntity,
    WyBotEntityContext,
    _get_coordinator_from_entry_data,
    display_name_for_group,
    resolve_target_id,
)
from .wybot_coordinator import WyBotCoordinator
from .wybot_dp_models import DP, build_dp_cleaning_command


def _dp15_clean_time(minutes: int) -> DP:
    """Build DP15 clean-time payload (first byte minutes, rest zeros)."""
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyBotCoordinator = _get_coordinator_from_entry_data(entry_data)

    entities: list[ButtonEntity] = []

    for group_id, group in coordinator.data.items():
        target_id = resolve_target_id(group)
        if not target_id:
            continue

        base_name = display_name_for_group(group)
        ctx = WyBotEntityContext(group_id=str(group_id), target_id=str(target_id))

        entities.append(
            WyBotStartButton(
                coordinator=coordinator,
                ctx=ctx,
                name=f"{base_name} Start",
                unique_id=f"{group_id}_start",
            )
        )

        entities.append(
            WyBotStopButton(
                coordinator=coordinator,
                ctx=ctx,
                name=f"{base_name} Stop",
                unique_id=f"{group_id}_stop",
            )
        )

    async_add_entities(entities)


class WyBotStartButton(WyBotBaseEntity, ButtonEntity):
    """Press to start cleaning."""

    _attr_icon = "mdi:play-circle"

    async def async_press(self) -> None:
        group = self.group
        model = getattr(group.device, "device_type", None)

        # Match the same behavior as your vacuum async_start()
        if model == "WY460":
            if getattr(self.coordinator, "use_clean_time", False):
                minutes = int(getattr(self.coordinator, "clean_time_minutes", 60))
                minutes = _snap_clean_time_minutes(minutes)
                self.coordinator.send_write_command(group, _dp15_clean_time(minutes))
                await asyncio.sleep(0.2)

            dp0 = build_dp_cleaning_command("02")  # WY460 start/clean
            self.coordinator.send_write_command(group, dp0)
            return

        dp = build_dp_cleaning_command("01")  # legacy models start
        self.coordinator.send_write_command(group, dp)


class WyBotStopButton(WyBotBaseEntity, ButtonEntity):
    """Press to stop cleaning."""

    _attr_icon = "mdi:stop-circle"

    async def async_press(self) -> None:
        group = self.group
        model = getattr(group.device, "device_type", None)

        # Match the same behavior as your vacuum async_stop()
        cmd = "01" if model == "WY460" else "00"
        dp = build_dp_cleaning_command(cmd)
        self.coordinator.send_write_command(group, dp)
