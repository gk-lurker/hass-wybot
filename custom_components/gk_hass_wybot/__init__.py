"""The WyBot integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    OPT_DP0_DELAY_SECONDS,
    OPT_TS_OFFSET_SECONDS,
    DEFAULT_DP0_DELAY_SECONDS,
    DEFAULT_TS_OFFSET_SECONDS,
)
from .wybot_http_client import WyBotHTTPClient
from .wybot_coordinator import WyBotCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,  # <-- Press-to-start button
]


def _register_vacuum_turn_on_off_aliases(hass: HomeAssistant) -> None:
    """Create vacuum.turn_on/off as aliases for vacuum.start/stop."""
    schema = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})

    async def _turn_on(call: ServiceCall) -> None:
        await hass.services.async_call(
            "vacuum",
            "start",
            {ATTR_ENTITY_ID: call.data[ATTR_ENTITY_ID]},
            blocking=True,
        )

    async def _turn_off(call: ServiceCall) -> None:
        await hass.services.async_call(
            "vacuum",
            "stop",
            {ATTR_ENTITY_ID: call.data[ATTR_ENTITY_ID]},
            blocking=True,
        )

    if not hass.services.has_service("vacuum", "turn_on"):
        hass.services.async_register("vacuum", "turn_on", _turn_on, schema=schema)
        _LOGGER.info("Registered compatibility service vacuum.turn_on -> vacuum.start")

    if not hass.services.has_service("vacuum", "turn_off"):
        hass.services.async_register("vacuum", "turn_off", _turn_off, schema=schema)
        _LOGGER.info("Registered compatibility service vacuum.turn_off -> vacuum.stop")


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options/data change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WyBot from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    _register_vacuum_turn_on_off_aliases(hass)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)

    wybot_http_client = WyBotHTTPClient(username, password)

    authed = await hass.async_add_executor_job(wybot_http_client.authenticate)
    if not authed:
        _LOGGER.error("WyBot authentication failed")
        return False

    dp0_delay = float(entry.options.get(OPT_DP0_DELAY_SECONDS, DEFAULT_DP0_DELAY_SECONDS))
    ts_offset = int(entry.options.get(OPT_TS_OFFSET_SECONDS, DEFAULT_TS_OFFSET_SECONDS))

    coordinator = WyBotCoordinator(
        hass,
        wybot_http_client=wybot_http_client,
        dp0_delay_seconds=dp0_delay,
        ts_offset_seconds=ts_offset,
    )
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # First refresh populates devices and starts MQTT subscriptions/priming
    await coordinator.async_config_entry_first_refresh()

    # Load entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _stop(event) -> None:
        """Stop MQTT client on Home Assistant stop."""
        await coordinator.async_stop()

    hass.bus.async_listen_once("homeassistant_stop", _stop)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: WyBotCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
