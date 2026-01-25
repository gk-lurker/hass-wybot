import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .wybot_dp_models import GenericDP
from .wybot_http_client import WyBotHTTPClient
from .wybot_models import Command, Device, Docker, Group
from .wybot_mqtt_client import WyBotMQTTClient

_LOGGER = logging.getLogger(__name__)

DP0_ID = 0

# If we haven't heard *anything* from the device in this long, consider it offline.
_OFFLINE_TTL_SECONDS = 180.0

# Backoff for MQTT reconnect attempts
_RECONNECT_BACKOFF_SECONDS = 30.0

# Debounce coordinator pushes to HA (prevents update storms)
_PUSH_DEBOUNCE_SECONDS = 0.25


class WyBotCoordinator(DataUpdateCoordinator):
    """Coordinates data between WyBot and Homeassistant."""

    wybot_http_client: WyBotHTTPClient
    wybot_mqtt_client: WyBotMQTTClient
    hass: HomeAssistant
    data: dict[str, Group]
    initial_load = False

    def __init__(
        self,
        hass: HomeAssistant,
        wybot_http_client: WyBotHTTPClient,
        dp0_delay_seconds: float = 6.0,
        ts_offset_seconds: int = 0,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="WyBot Coordinator",
            update_interval=timedelta(seconds=120),
        )
        self.wybot_http_client = wybot_http_client
        self.hass = hass
        self.data = {}

        self.dp0_delay_seconds = float(dp0_delay_seconds)
        self.ts_offset_seconds = int(ts_offset_seconds)

        # Track seen/sent timestamps in "Wybot time" (unix + offset)
        self._last_seen_ts: dict[str, int] = {}
        self._last_sent_ts: dict[str, int] = {}

        # Broker connection tracking
        self._mqtt_connected: bool = False

        # Online / last heard tracking (per *device_id* / docker_id)
        # _online:
        #   True/False when we have an explicit signal,
        #   None means "unknown, fall back to last-heard TTL"
        self._online: dict[str, bool | None] = {}
        self._last_heard_monotonic: dict[str, float] = {}

        # Reconnect rate limiting
        self._last_reconnect_attempt: float = 0.0

        # Debounced coordinator pushes
        self._push_scheduled: bool = False

        self.wybot_mqtt_client = WyBotMQTTClient(
            self.on_message,
            ts_provider=self._next_ts_for_device,
            on_broker_connection_state=self._on_broker_connection_state,
            on_device_seen=self._on_device_seen,
        )
        self.wybot_mqtt_client.connect()

        _LOGGER.debug(
            "WyBotCoordinator initialized: dp0_delay=%.2fs ts_offset=%ss",
            self.dp0_delay_seconds,
            self.ts_offset_seconds,
        )

    # ----------------------------
    # Public helpers for entities
    # ----------------------------

    def mqtt_connected(self) -> bool:
        return bool(self._mqtt_connected)

    def is_online(self, device_id: str) -> bool | None:
        """Return explicit online flag if known, else None."""
        return self._online.get(device_id)

    def seconds_since_heard(self, device_id: str) -> float | None:
        t = self._last_heard_monotonic.get(device_id)
        if t is None:
            return None
        return max(0.0, time.monotonic() - t)

    def computed_online(self, device_id: str) -> bool | None:
        """
        Best-effort online:
        - If explicit online/offline is known, use it.
        - Else fall back to last-heard TTL.
        """
        explicit = self._online.get(device_id)
        if explicit is True:
            return True
        if explicit is False:
            return False

        since = self.seconds_since_heard(device_id)
        if since is None:
            return None
        return since < _OFFLINE_TTL_SECONDS

    def resolve_group_targets(self, group: Group) -> list[str]:
        return self._resolve_target_ids(group)

    # ----------------------------
    # Lifecycle
    # ----------------------------

    async def async_stop(self) -> None:
        _LOGGER.info("Stopping MQTT client")
        self.wybot_mqtt_client.disconnect()

    async def _async_update_data(self) -> dict[str, Group]:
        """
        Keep coordinator stable:
        - Do initial HTTP refresh once.
        - Ensure MQTT connection is alive (with backoff).
        - Never mark whole integration unavailable for a transient error; log and keep last data.
        """
        try:
            async with asyncio.timeout(15):
                if self.initial_load is False:
                    self.initial_load = True
                    await self.http_refresh_data()

                # If MQTT isn't connected, attempt reconnect with backoff
                if self.wybot_mqtt_client.is_connected() is False:
                    now = time.monotonic()
                    if (now - self._last_reconnect_attempt) >= _RECONNECT_BACKOFF_SECONDS:
                        self._last_reconnect_attempt = now
                        self.wybot_mqtt_client.reconnect()

                return self.data
        except Exception as err:
            _LOGGER.warning("WyBotCoordinator update tick failed; keeping last data. err=%s", err)
            return self.data

    async def http_refresh_data(self) -> None:
        self.data = await self.hass.async_add_executor_job(
            self.wybot_http_client.get_indexed_current_grouped_devices
        )

        # Initialize target tracking
        for _, group in self.data.items():
            for tid in self._resolve_target_ids(group):
                self._online.setdefault(tid, None)

        for group_id, group in self.data.items():
            device_ids = {"device_id": getattr(group.device, "device_id", None)} if group and group.device else {}
            docker_ids = {"docker_id": getattr(group.docker, "docker_id", None)} if group and group.docker else {}
            resolved_targets = self._resolve_target_ids(group)
            _LOGGER.debug(
                "http_refresh_data: group_id=%s device_ids=%s docker_ids=%s resolved_targets=%s",
                group_id,
                device_ids,
                docker_ids,
                resolved_targets,
            )

        self.subscribe_mqtt(self.data)
        self._request_push()

    def subscribe_mqtt(self, data: dict[str, Group]) -> None:
        """
        Subscribe immediately, but DO NOT prime/query until broker is connected.
        That avoids the reconnect storm you saw in logs.
        """
        for _, group in data.items():
            targets = self._resolve_target_ids(group)
            for tid in targets:
                self.wybot_mqtt_client.subscribe_for_device(tid)

        # If broker is already connected, prime shortly.
        # Otherwise _on_broker_connection_state will prime on connect.
        if self._mqtt_connected:
            all_targets: list[str] = []
            for _, group in data.items():
                all_targets.extend(self._resolve_target_ids(group))
            if all_targets:
                self.hass.loop.call_soon_threadsafe(
                    lambda: self.hass.async_create_task(self._prime_targets(all_targets))
                )

    async def _prime_targets(self, targets: list[str]) -> None:
        # Only prime if we are connected
        if not self._mqtt_connected:
            return

        # Query after short delays (skip 0s to avoid racing the MQTT connect)
        for delay_s in (2, 8, 20):
            await asyncio.sleep(delay_s)
            if not self._mqtt_connected:
                return
            for tid in targets:
                try:
                    self.wybot_mqtt_client.ensure_device_sends_statuses(tid)
                except Exception as e:
                    _LOGGER.debug("prime_targets failed tid=%s err=%s", tid, e)

    # ----------------------------
    # Time/ts helpers
    # ----------------------------

    def _now_wybot(self) -> int:
        return int(time.time()) + self.ts_offset_seconds

    def _note_seen_ts(self, device_id: str, ts_val: Any) -> None:
        try:
            ts_int = int(ts_val)
        except Exception:
            return
        prev = self._last_seen_ts.get(device_id, 0)
        if ts_int > prev:
            self._last_seen_ts[device_id] = ts_int

    def _next_ts_for_device(self, device_id: str) -> int:
        now = self._now_wybot()
        seen = self._last_seen_ts.get(device_id, 0)
        sent = self._last_sent_ts.get(device_id, 0)
        ts_out = max(now, seen + 1, sent + 1)
        self._last_sent_ts[device_id] = ts_out
        return ts_out

    # ----------------------------
    # Debounced push into HA
    # ----------------------------

    async def _push_soon(self) -> None:
        await asyncio.sleep(_PUSH_DEBOUNCE_SECONDS)
        self._push_scheduled = False
        self.async_set_updated_data(self.data)

    def _request_push(self) -> None:
        """
        Schedule a debounced async_set_updated_data call.
        Safe to call from MQTT callback threads.
        """
        if self._push_scheduled:
            return
        self._push_scheduled = True
        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(self._push_soon()))

    # ----------------------------
    # MQTT callback helpers
    # ----------------------------

    def _mark_heard(self, device_id: str) -> None:
        self._last_heard_monotonic[device_id] = time.monotonic()
        if self._online.get(device_id) is None:
            self._online[device_id] = True

    def set_online(self, device_id: str, online: bool | None) -> None:
        self._online[device_id] = online
        if online:
            self._mark_heard(device_id)

    def _on_broker_connection_state(self, connected: bool) -> None:
        self._mqtt_connected = bool(connected)
        _LOGGER.debug("WYBOT: broker connected=%s", connected)

        # On connect, prime all known targets (once)
        if connected and self.data:
            all_targets: list[str] = []
            for _, group in self.data.items():
                all_targets.extend(self._resolve_target_ids(group))
            if all_targets:
                self.hass.loop.call_soon_threadsafe(
                    lambda: self.hass.async_create_task(self._prime_targets(all_targets))
                )

        self._request_push()

    def _parse_will_payload_online(self, payload: Any) -> bool | None:
        try:
            if payload is None:
                return None
            if isinstance(payload, (bytes, bytearray)):
                s = payload.decode("utf-8", errors="ignore").strip().lower()
            else:
                s = str(payload).strip().lower()

            if s in ("1", "online", "true", "yes", "connected"):
                return True
            if s in ("0", "offline", "false", "no", "disconnected"):
                return False

            if "online" in s and ("1" in s or "true" in s):
                return True
            if "online" in s and ("0" in s or "false" in s):
                return False
            return None
        except Exception:
            return None

    def _on_device_seen(self, device_id: str, kind: str, topic: str, payload: Any) -> None:
        device_id = str(device_id)
        self._mark_heard(device_id)

        if kind == "will":
            online = self._parse_will_payload_online(payload)
            _LOGGER.debug(
                "WYBOT: /will received device_id=%s parsed_online=%s payload=%r",
                device_id,
                online,
                payload,
            )
            self.set_online(device_id, online)

            # If device reports online, query once (but only if broker connected)
            if online is True and self._mqtt_connected:
                try:
                    self.wybot_mqtt_client.ensure_device_sends_statuses(device_id)
                except Exception:
                    pass

        self._request_push()

    # ----------------------------
    # Main on_message (JSON topics)
    # ----------------------------

    def on_message(self, topic: str, data: dict[str, Any]) -> None:
        try:
            if topic.startswith("/device/DATA/send_transparent_data/"):
                device_id = topic[35:]
                self._mark_heard(device_id)

                command_response = Command(**data)

                if getattr(command_response, "ts", None) is not None:
                    self._note_seen_ts(device_id, command_response.ts)

                group = self.get_group(device_id)
                if group is None:
                    return

                dps = command_response.get_dps_as_keyed_dict()
                dps = {k: v for k, v in dps.items() if getattr(v, "data", None) is not None}

                if not dps:
                    _LOGGER.debug("SEND RESPONSE ---- %s ---- (no data fields in payload; ignoring)", device_id)
                    _LOGGER.debug("Send CMD ---- %s ----- %s", device_id, command_response)
                    return

                if group.docker is not None and getattr(group.docker, "docker_id", None) == device_id:
                    group.docker.dps = {**(group.docker.dps or {}), **dps}
                    _LOGGER.debug("SEND RESPONSE ---- docker - %s ---- Current DPs: %s", device_id, group.docker.dps)
                elif group.device is not None and getattr(group.device, "device_id", None) == device_id:
                    group.device.dps = {**(group.device.dps or {}), **dps}
                    _LOGGER.debug("SEND RESPONSE ---- device - %s ---- Current DPs: %s", device_id, group.device.dps)

                self.data[group.id] = group

            if topic.startswith("/device/DATA/recv_transparent_query_data/"):
                device_id = topic[41:]
                self._mark_heard(device_id)

                command_response = Command(**data)
                if getattr(command_response, "ts", None) is not None:
                    self._note_seen_ts(device_id, command_response.ts)
                _LOGGER.debug("Query CMD ---- %s ----- %s", device_id, command_response)

            if topic.startswith("/device/DATA/recv_transparent_cmd_data/"):
                device_id = topic[39:]
                self._mark_heard(device_id)

                command_response = Command(**data)
                if getattr(command_response, "ts", None) is not None:
                    self._note_seen_ts(device_id, command_response.ts)
                _LOGGER.debug("SEND CMD ---- %s ----- %s", device_id, command_response)

        finally:
            self._request_push()

    # ----------------------------
    # Helpers for group/device lookup
    # ----------------------------

    def _resolve_target_ids(self, group: Group) -> list[str]:
        targets: list[str] = []
        if group is None:
            return targets

        if group.device is not None:
            did = getattr(group.device, "device_id", None)
            if did:
                targets.append(did)

        if group.docker is not None:
            dock_id = getattr(group.docker, "docker_id", None)
            if dock_id:
                targets.append(dock_id)

        seen = set()
        out: list[str] = []
        for t in targets:
            if t not in seen:
                out.append(t)
                seen.add(t)
        return out

    def get_device_or_docker(self, device_id: str) -> Device | Docker | None:
        for _, group in self.data.items():
            if group.device is not None and group.device.device_id == device_id:
                return group.device
            if group.docker is not None and group.docker.docker_id == device_id:
                return group.docker
        return None

    def get_group(self, device_id: str) -> Group | None:
        for _, group in self.data.items():
            if group.device is not None and group.device.device_id == device_id:
                return group
            if group.docker is not None and group.docker.docker_id == device_id:
                return group
        return None

    # ----------------------------
    # Publishing helpers
    # ----------------------------

    def _normalize_dp_payloads(self, dp: Any) -> list[dict[str, Any]]:
        if isinstance(dp, (list, tuple)):
            out: list[dict[str, Any]] = []
            for item in dp:
                out.extend(self._normalize_dp_payloads(item))
            return out

        if hasattr(dp, "dict"):
            return [dp.dict()]
        if hasattr(dp, "model_dump"):
            return [dp.model_dump()]

        if isinstance(dp, dict):
            return [dp]

        if hasattr(dp, "__dict__"):
            return [dp.__dict__]

        return [{"value": dp}]

    async def _delayed_publish(self, group: Group, payloads: list[dict[str, Any]], delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        self._publish_now(group, payloads)

    def _publish_now(self, group: Group, payloads: list[dict[str, Any]]) -> None:
        targets = self._resolve_target_ids(group)
        cmd = {"ts": 0, "cmd": 4, "dp": payloads}  # ts overwritten by mqtt client

        _LOGGER.debug(
            "publish_now: group_id=%s targets=%s dp=%s",
            getattr(group, "id", None),
            targets,
            payloads,
        )

        for tid in targets:
            self.wybot_mqtt_client.send_write_command_for_device(tid, cmd)

    def send_write_command(self, group: Group, dp: GenericDP | dict | list) -> None:
        payloads = self._normalize_dp_payloads(dp)

        dp0_payloads = [p for p in payloads if p.get("id") == DP0_ID]
        other_payloads = [p for p in payloads if p.get("id") != DP0_ID]

        if dp0_payloads and other_payloads:
            _LOGGER.debug(
                "send_write_command (split dp0): group_id=%s device_id=%s first=%s second=%s delay=%.2fs",
                getattr(group, "id", None),
                getattr(group.device, "device_id", None) if group and group.device else None,
                other_payloads,
                dp0_payloads,
                self.dp0_delay_seconds,
            )
            self._publish_now(group, other_payloads)
            self.hass.async_create_task(self._delayed_publish(group, dp0_payloads, self.dp0_delay_seconds))
            return

        _LOGGER.debug(
            "send_write_command: group_id=%s device_id=%s docker_id=%s payloads=%s",
            getattr(group, "id", None),
            getattr(group.device, "device_id", None) if group and group.device else None,
            getattr(group.docker, "docker_id", None) if group and group.docker else None,
            payloads,
        )
        self._publish_now(group, payloads)

    @property
    def vacuums(self) -> list[str]:
        # NOTE: this returns your group-ids (keys of self.data)
        return [device_id for [device_id, _device] in self.data.items()]
