"""Switch entities for WyBot integration (per-device settings)."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_USE_CLEAN_TIME
from .wybot_coordinator import WyBotCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities for WyBot."""
    coordinator: WyBotCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        WyBotUseCleanTimeSwitch(coordinator=coordinator, entry=entry, idx=device_id)
        for device_id in coordinator.vacuums
    ]
    async_add_entities(entities, update_before_add=True)


class WyBotUseCleanTimeSwitch(CoordinatorEntity, SwitchEntity):
    """Enable/disable auto-dock after Clean Time."""

    def __init__(self, coordinator: WyBotCoordinator, entry: ConfigEntry, idx: str) -> None:
        super().__init__(coordinator=coordinator, context=idx)
        self._entry = entry
        self._idx = str(idx)

    @property
    def unique_id(self) -> str:
        return f"wybot_{self._idx}_use_clean_time"

    @property
    def name(self) -> str:
        try:
            device_name = self.coordinator.data[self._idx].name
        except Exception:
            device_name = self._idx
        return f"{device_name} Use Clean Time"

    @property
    def is_on(self) -> bool:
        options = self._entry.options or {}
        per_device = options.get(CONF_USE_CLEAN_TIME, {})
        if isinstance(per_device, dict):
            return bool(per_device.get(self._idx, False))
        return False

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_state(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_state(False)

    async def _set_state(self, state: bool) -> None:
        options = dict(self._entry.options or {})
        per_device = options.get(CONF_USE_CLEAN_TIME)
        if not isinstance(per_device, dict):
            per_device = {}
        else:
            per_device = dict(per_device)

        per_device[self._idx] = bool(state)
        options[CONF_USE_CLEAN_TIME] = per_device

        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()

    @property
    def icon(self) -> str:
        return "mdi:timer-cog-outline"
