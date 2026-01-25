"""Library for interacting with the WyBot MQTT API."""

from __future__ import annotations

import json
import socket
import logging
import time
from typing import Callable, Any

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

MQTT_URL = "mqtt.wybotpool.com"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 60

# Use QoS 1 for commands + subscriptions (the mobile app typically uses QoS1).
MQTT_QOS = 1

# User/Password to authenticate from the iOS/Android app to the MQTT server
MQTT_USERNAME = "wyindustry"
MQTT_PASSWORD = "nwe_GTG4faf2qyx8ugx"


def _device_id_from_topic(topic: str) -> str | None:
    """Best-effort device_id extraction from known topic formats."""
    if not topic:
        return None
    topic = topic.strip()
    if topic.startswith("/will/"):
        # /will/<device_id>
        parts = topic.split("/")
        return parts[-1] if parts else None
    # Most WyBot topics end with /<device_id>
    parts = topic.split("/")
    return parts[-1] if parts else None


class WyBotMQTTClient:
    """Client for interacting with the WyBot MQTT API."""

    _mqtt: mqtt.Client
    _subscriptions: list[str]
    _devices: list[str]
    _on_message: Callable[[str, dict[str, Any]], None]
    _ts_provider: Callable[[str], int]

    # New: state callbacks + last seen tracking
    _on_broker_connection_state: Callable[[bool], None] | None
    _on_device_seen: Callable[[str, str, str, Any], None] | None
    _last_seen: dict[str, float]

    def __init__(
        self,
        on_message: Callable[[str, dict[str, Any]], None],
        ts_provider: Callable[[str], int] | None = None,
        on_broker_connection_state: Callable[[bool], None] | None = None,
        on_device_seen: Callable[[str, str, str, Any], None] | None = None,
    ) -> None:
        """Init the wybot mqtt api."""
        self._mqtt = mqtt.Client()
        self._mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        self._mqtt.on_connect = self.on_connect
        self._mqtt.on_message = self._on_mqtt_message
        self._mqtt.on_connect_fail = self.on_connect_fail
        self._mqtt.on_disconnect = self.on_disconnect

        self._subscriptions = []
        self._devices = []
        self._on_message = on_message
        self._ts_provider = ts_provider or (lambda _device_id: 0)

        # New
        self._on_broker_connection_state = on_broker_connection_state
        self._on_device_seen = on_device_seen
        self._last_seen = {}

    # ----------------------------
    # Public helpers for HA
    # ----------------------------

    def get_last_seen(self, device_id: str) -> float | None:
        """Return monotonic timestamp (seconds) of last seen robot traffic."""
        return self._last_seen.get(str(device_id))

    def connect(self):
        """Connect to the MQTT server."""
        _LOGGER.debug("Connecting to wybot mqtt server %s:%s", MQTT_URL, MQTT_PORT)
        self._mqtt.loop_start()
        try:
            self._mqtt.connect(MQTT_URL, MQTT_PORT, MQTT_KEEPALIVE)
        except socket.gaierror as e:
            _LOGGER.warning("WYBOT: MQTT DNS lookup failed during startup; will retry later: %s", e)
            if self._on_broker_connection_state:
                try:
                    self._on_broker_connection_state(False)
                except Exception:
                    pass
            return
        except Exception as e:
            _LOGGER.warning("WYBOT: MQTT connect failed during startup; will retry later: %s", e)
            if self._on_broker_connection_state:
                try:
                    self._on_broker_connection_state(False)
                except Exception:
                    pass
            return

    def reconnect(self):
        """Re-connect to the MQTT server."""
        _LOGGER.debug("Reconnecting to wybot mqtt server %s:%s", MQTT_URL, MQTT_PORT)
        try:
            self._mqtt.reconnect()
        except Exception as e:
            _LOGGER.warning("WYBOT: MQTT reconnect failed: %s", e)

    def is_connected(self) -> bool:
        """is_connected? to the MQTT server."""
        return self._mqtt.is_connected()

    def disconnect(self):
        """Stop the MQTT client."""
        _LOGGER.info("Stopping MQTT client.")
        try:
            self._mqtt.loop_stop()
        finally:
            try:
                self._mqtt.disconnect()
            except Exception:
                pass

    # ----------------------------
    # MQTT callbacks
    # ----------------------------

    def on_connect(self, client: mqtt.Client, userdata, flags, reasonCode):
        _LOGGER.debug("Connected with result code %s", reasonCode)
        if self._on_broker_connection_state:
            try:
                self._on_broker_connection_state(True)
            except Exception:
                pass

        for subscription in self._subscriptions:
            client.subscribe(subscription, qos=MQTT_QOS)

        # Important: after broker connect, ask each device for current DPs
        for device in self._devices:
            self.ensure_device_sends_statuses(device)

    def on_disconnect(self, client, userdata, rc):
        _LOGGER.debug("Disconnected rc=%s", rc)
        if self._on_broker_connection_state:
            try:
                self._on_broker_connection_state(False)
            except Exception:
                pass

    def on_connect_fail(self, client, userdata):
        _LOGGER.debug("Connect failed")
        if self._on_broker_connection_state:
            try:
                self._on_broker_connection_state(False)
            except Exception:
                pass

    # ----------------------------
    # Subscription management
    # ----------------------------

    def subscribe_for_device(self, device_id: str):
        """Subscribe to a device."""
        if not device_id:
            _LOGGER.debug("subscribe_for_device called with empty device_id; skipping")
            return

        _LOGGER.debug("Subscribing to wybot mqtt for device %s", device_id)

        topics = [
            f"/will/{device_id}",
            f"/device/DATA/send_transparent_data/{device_id}",
            f"/device/DATA/recv_transparent_query_data/{device_id}",
            f"/device/DATA/recv_transparent_cmd_data/{device_id}",
            f"/device/OTA/post_update_progress/{device_id}",
            f"/device/OTA/notify_ready_to_update/{device_id}",
        ]

        for t in topics:
            if t not in self._subscriptions:
                self._subscriptions.append(t)

        if device_id not in self._devices:
            self._devices.append(device_id)

        for subscription in topics:
            try:
                self._mqtt.subscribe(subscription, qos=MQTT_QOS)
            except Exception as e:
                _LOGGER.debug("Subscribe failed topic=%s err=%s", subscription, e)

        # Ask for current status snapshot (even if we subscribed after connect)
        self.ensure_device_sends_statuses(device_id)

    def ensure_device_sends_statuses(self, device_id: str):
        """Ensure that a device sends statuses."""
        _LOGGER.debug("Ensuring device sends statuses %s", device_id)
        self.send_query_command_for_device(
            device_id,
            {
                "ts": 0,  # overwritten
                "cmd": 9,
                "dp": [{"id": 0}, {"id": 1}, {"id": 50}, {"id": 11}, {"id": 41}],
            },
        )

    # ----------------------------
    # Publishing
    # ----------------------------

    def send_query_command_for_device(self, device_id: str, command: dict):
        """Send a query command to a device."""
        if not device_id:
            _LOGGER.debug("send_query_command_for_device called with empty device_id; skipping publish: %s", command)
            return

        command = dict(command)
        command["ts"] = int(self._ts_provider(device_id))

        _LOGGER.debug("SENDING QUERY - %s - %s", device_id, command)
        if self.is_connected() is False:
            self.reconnect()

        topic = f"/device/DATA/recv_transparent_query_data/{device_id}"
        info = self._mqtt.publish(topic, json.dumps(command), qos=MQTT_QOS)
        _LOGGER.debug(
            "PUBLISHED QUERY ---- %s ---- topic=%s rc=%s mid=%s qos=%s",
            device_id,
            topic,
            getattr(info, "rc", None),
            getattr(info, "mid", None),
            MQTT_QOS,
        )

    def send_write_command_for_device(self, device_id: str, command: dict):
        """Send a write command to a device."""
        if not device_id:
            _LOGGER.debug("send_write_command_for_device called with empty device_id; skipping publish: %s", command)
            return

        command = dict(command)
        command["ts"] = int(self._ts_provider(device_id))

        _LOGGER.debug("SENDING CMD - %s - %s", device_id, command)
        if self.is_connected() is False:
            self.reconnect()

        topic = f"/device/DATA/recv_transparent_cmd_data/{device_id}"
        info = self._mqtt.publish(topic, json.dumps(command), qos=MQTT_QOS)
        _LOGGER.debug(
            "PUBLISHED CMD ---- %s ---- topic=%s rc=%s mid=%s qos=%s",
            device_id,
            topic,
            getattr(info, "rc", None),
            getattr(info, "mid", None),
            MQTT_QOS,
        )

    # ----------------------------
    # Incoming messages
    # ----------------------------

    def _on_mqtt_message(self, client, userdata, msg):
        """Handle the incoming message from the MQTT server."""
        topic = getattr(msg, "topic", "") or ""
        device_id = _device_id_from_topic(topic)

        # Special-case /will/<device> which often is NOT JSON.
        if topic.startswith("/will/"):
            # Mark device as "seen" when will arrives too (useful for offline signal),
            # but do not treat as normal DP state.
            if device_id:
                self._last_seen[str(device_id)] = time.monotonic()
                if self._on_device_seen:
                    try:
                        self._on_device_seen(str(device_id), "will", topic, msg.payload)
                    except Exception:
                        pass
            # Nothing else to parse
            return

        # Normal JSON topics
        try:
            payload = json.loads(msg.payload)
        except Exception:
            _LOGGER.debug("WYBOT: Failed to JSON decode payload topic=%s payload=%r", topic, msg.payload)
            return

        if device_id:
            self._last_seen[str(device_id)] = time.monotonic()
            if self._on_device_seen:
                try:
                    self._on_device_seen(str(device_id), "json", topic, payload)
                except Exception:
                    pass

        self._on_message(topic, payload)
