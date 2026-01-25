"""Number entities for WyBot integration (per-device settings)."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_CLEAN_TIME_MINUTES,
    DEFAULT_CLEAN_TIME_MINUTES,
    CLEAN_TIME_MIN,
    CLEAN_TIME_MAX,
    CLEAN_TIME_STEP,
)
from .wybot_coordinator import WyBotCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for WyBot."""
    coordinator: WyBotCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        WyBotCleanTimeNumber(coordinator=coordinator, entry=entry, idx=device_id)
        for device_id in coordinator.vacuums
    ]
    async_add_entities(entities, update_before_add=True)


class WyBotCleanTimeNumber(CoordinatorEntity, NumberEntity):
    """Clean Time (minutes) control stored in config entry options."""

    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "min"

    def __init__(self, coordinator: WyBotCoordinator, entry: ConfigEntry, idx: str) -> None:
        super().__init__(coordinator=coordinator, context=idx)
        self._entry = entry
        self._idx = str(idx)

    @property
    def unique_id(self) -> str:
        return f"wybot_{self._idx}_clean_time_minutes"

    @property
    def name(self) -> str:
        # show the device name in the entity name
        try:
            device_name = self.coordinator.data[self._idx].name
        except Exception:
            device_name = self._idx
        return f"{device_name} Clean Time"

    @property
    def native_min_value(self) -> float:
        return float(CLEAN_TIME_MIN)

    @property
    def native_max_value(self) -> float:
        return float(CLEAN_TIME_MAX)

    @property
    def native_step(self) -> float:
        return float(CLEAN_TIME_STEP)

    @property
    def native_value(self) -> float:
        options = self._entry.options or {}
        per_device = options.get(CONF_CLEAN_TIME_MINUTES, {})
        if isinstance(per_device, dict):
            val = per_device.get(self._idx, DEFAULT_CLEAN_TIME_MINUTES)
        else:
            val = DEFAULT_CLEAN_TIME_MINUTES
        try:
            return float(val)
        except Exception:
            return float(DEFAULT_CLEAN_TIME_MINUTES)

    async def async_set_native_value(self, value: float) -> None:
        """Persist clean time to config entry options (no reload)."""
        minutes = int(round(float(value)))

        # clamp and snap to step
        minutes = max(CLEAN_TIME_MIN, min(CLEAN_TIME_MAX, minutes))
        step = CLEAN_TIME_STEP
        minutes = int(round(minutes / step) * step)

        options = dict(self._entry.options or {})
        per_device = options.get(CONF_CLEAN_TIME_MINUTES)
        if not isinstance(per_device, dict):
            per_device = {}
        else:
            per_device = dict(per_device)

        per_device[self._idx] = minutes
        options[CONF_CLEAN_TIME_MINUTES] = per_device

        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()

    @property
    def icon(self) -> str:
        return "mdi:timer-outline"
