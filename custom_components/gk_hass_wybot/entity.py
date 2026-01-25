from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .wybot_coordinator import WyBotCoordinator
from .wybot_models import Group


@dataclass(frozen=True)
class WyBotEntityContext:
    """Resolved identifiers for an entity."""
    group_id: str
    target_id: str  # device_id or docker_id


def _get_coordinator_from_entry_data(entry_data: Any) -> WyBotCoordinator:
    """
    Be tolerant of different hass.data layouts:
      - hass.data[DOMAIN][entry_id] = coordinator
      - hass.data[DOMAIN][entry_id] = {"coordinator": coordinator, ...}
    """
    if isinstance(entry_data, WyBotCoordinator):
        return entry_data
    if isinstance(entry_data, dict) and "coordinator" in entry_data:
        return entry_data["coordinator"]
    raise TypeError(f"Unexpected hass.data[{DOMAIN}] entry structure: {type(entry_data)}")


def resolve_target_id(group: Group) -> str | None:
    """Pick the best target id for online + MQTT traffic (device_id preferred)."""
    if group is None:
        return None
    if group.device is not None:
        did = getattr(group.device, "device_id", None)
        if did:
            return did
    if group.docker is not None:
        dock_id = getattr(group.docker, "docker_id", None)
        if dock_id:
            return dock_id
    return None


def display_name_for_group(group: Group) -> str:
    """Best-effort friendly name."""
    for obj in (getattr(group, "device", None), getattr(group, "docker", None), group):
        if obj is None:
            continue
        for attr in ("name", "device_name", "title", "alias"):
            val = getattr(obj, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return "WyBot"


class WyBotBaseEntity(CoordinatorEntity[WyBotCoordinator]):
    """Shared base for WyBot entities."""

    def __init__(self, coordinator: WyBotCoordinator, ctx: WyBotEntityContext, name: str, unique_id: str) -> None:
        super().__init__(coordinator)
        self._ctx = ctx
        self._attr_name = name
        self._attr_unique_id = unique_id

    @property
    def ctx(self) -> WyBotEntityContext:
        return self._ctx

    @property
    def group(self) -> Group | None:
        return self.coordinator.data.get(self._ctx.group_id)

    @property
    def available(self) -> bool:
        """
        Don't mark entities unavailable just because online is Unknown.
        Only mark unavailable when we *know* it's offline (online == False)
        or when coordinator updates are failing.
        """
        if not self.coordinator.last_update_success:
            return False
        online = self.coordinator.is_online(self._ctx.target_id)
        return online is not False

    @property
    def device_info(self) -> DeviceInfo:
        """
        IMPORTANT:
        - HA "devices" are keyed by identifiers.
        - If we change identifiers, HA creates *new* devices (what you're seeing).
        - To avoid that, include the original stable key (group_id) AND also include target_id
          so all entity types converge onto the same device going forward.
        """
        group = self.group
        name = display_name_for_group(group) if group else "WyBot"

        identifiers: set[tuple[str, str]] = {(DOMAIN, self._ctx.group_id)}

        # Always include the device_id identifier so all platforms (vacuum/select/etc)
        # converge onto the same HA device (prevents duplicate devices).
        device_id = None
        try:
            dids = getattr(group, "device_ids", None)
            if isinstance(dids, dict):
                device_id = dids.get("device_id") or dids.get("id")
        except Exception:
            device_id = None
        if device_id:
            identifiers.add((DOMAIN, str(device_id)))

        if self._ctx.target_id:
            identifiers.add((DOMAIN, str(self._ctx.target_id)))

        return DeviceInfo(
            identifiers=identifiers,
            name=name,
            manufacturer="WyBot",
        )
