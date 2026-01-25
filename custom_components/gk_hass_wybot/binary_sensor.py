from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyBotCoordinator = _get_coordinator_from_entry_data(entry_data)

    entities: list[BinarySensorEntity] = []

    # Per-device "Online" sensors (based on /will + traffic)
    for group_id, group in coordinator.data.items():
        target_id = resolve_target_id(group)
        if not target_id:
            continue

        base_name = display_name_for_group(group)
        entities.append(
            WyBotOnlineBinarySensor(
                coordinator=coordinator,
                ctx=WyBotEntityContext(group_id=group_id, target_id=target_id),
                name=f"{base_name} Online",
                unique_id=f"{target_id}_online",
            )
        )

        entities.append(
            WyBotMQTTConnectedBinarySensor(
                coordinator=coordinator,
                ctx=WyBotEntityContext(group_id=group_id, target_id=target_id),
                name=f"{base_name} MQTT Connected",
                unique_id=f"{target_id}_mqtt_connected",
            )
        )

    async_add_entities(entities)


class WyBotOnlineBinarySensor(WyBotBaseEntity, BinarySensorEntity):
    """True when device is online, None/Unknown when never seen, False when offline."""

    _attr_icon = "mdi:lan-connect"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.is_online(self.ctx.target_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "target_id": self.ctx.target_id,
            "seconds_since_heard": self.coordinator.seconds_since_heard(self.ctx.target_id),
        }


class WyBotMQTTConnectedBinarySensor(WyBotBaseEntity, BinarySensorEntity):
    """
    MQTT broker connection status (per entry, but attached to each device for convenience).
    """

    _attr_icon = "mdi:mqtt"

    @property
    def is_on(self) -> bool:
        # This reads the client connection state; entity 'available' stays True unless device explicitly offline.
        return bool(self.coordinator.wybot_mqtt_client.is_connected())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "target_id": self.ctx.target_id,
        }
