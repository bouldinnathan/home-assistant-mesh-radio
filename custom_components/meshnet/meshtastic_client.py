"""Meshtastic gateway adapter."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import logging
from typing import Any

from .const import (
    DEFAULT_MESHTASTIC_MQTT_TOPIC,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)
from .gateway import MeshGateway
from .models import (
    MeshPacket,
    NodeState,
    canonical_node_key,
    coerce_float,
    coerce_int,
    parse_timestamp,
    utcnow,
)


_LOGGER = logging.getLogger(__name__)


class MeshtasticClient(MeshGateway):
    """Gateway adapter for Meshtastic native and MQTT transports."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._interface: Any | None = None
        self._unsub_mqtt: Any | None = None
        self._stopping = False
        self._pub = None
        self._receive_handler = None
        self._connect_handler = None
        self._disconnect_handler = None

    def _owns_interface(self, interface: Any) -> bool:
        """Return whether a process-global pubsub event belongs to this client."""
        return self._interface is not None and interface is self._interface

    async def async_start(self) -> None:
        """Start the Meshtastic transport."""
        self._stopping = False
        if self.config.transport == TRANSPORT_MQTT:
            await self._start_mqtt()
            return
        await self._start_native()

    async def async_stop(self) -> None:
        """Stop the Meshtastic transport."""
        self._stopping = True
        if self._unsub_mqtt:
            self._unsub_mqtt()
            self._unsub_mqtt = None
        if self._pub and self._receive_handler:
            try:
                self._pub.unsubscribe(self._receive_handler, "meshtastic.receive")
            except Exception as err:
                self._logger.debug("Failed to unsubscribe Meshtastic receive handler: %s", err)
        if self._pub and self._connect_handler:
            try:
                self._pub.unsubscribe(
                    self._connect_handler,
                    "meshtastic.connection.established",
                )
            except Exception as err:
                self._logger.debug("Failed to unsubscribe Meshtastic connect handler: %s", err)
        if self._pub and self._disconnect_handler:
            try:
                self._pub.unsubscribe(
                    self._disconnect_handler,
                    "meshtastic.connection.lost",
                )
            except Exception as err:
                self._logger.debug("Failed to unsubscribe Meshtastic disconnect handler: %s", err)
        if self._interface is not None:
            interface = self._interface
            self._interface = None
            await self.hass.async_add_executor_job(interface.close)
        await self._set_connected(False)

    async def async_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
    ) -> str:
        """Send a Meshtastic text message."""
        message_id = hashlib.sha256(
            f"{self.config.gateway_id}:{target_node}:{channel}:{message}:{utcnow().timestamp()}".encode()
        ).hexdigest()[:16]
        if self.config.transport == TRANSPORT_MQTT:
            await self._mqtt_publish_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
                message_id=message_id,
            )
        else:
            if self._interface is None:
                raise RuntimeError("Meshtastic interface is not connected")
            destination = target_node if target_node else "^all"
            kwargs: dict[str, Any] = {}
            if channel is not None:
                kwargs["channelIndex"] = coerce_int(channel) or 0
            await self.hass.async_add_executor_job(
                lambda: self._interface.sendText(message, destinationId=destination, **kwargs)
            )
        self.status.packets_sent += 1
        await self._emit_status()
        return message_id

    async def async_refresh(self) -> None:
        """Refresh node DB from the native interface."""
        if self._interface is None:
            return
        nodes = await self.hass.async_add_executor_job(lambda: dict(self._interface.nodes))
        for node_id, node in nodes.items():
            normalized = meshtastic_node_to_state(
                node,
                gateway_id=self.config.gateway_id,
                fallback_node_id=node_id,
            )
            await self._emit_node(normalized)

    async def _start_native(self) -> None:
        try:
            from pubsub import pub
        except ImportError as err:
            await self._emit_error(
                "pypubsub is unavailable; Home Assistant must install meshtastic requirements"
            )
            raise err

        self._pub = pub

        def receive_handler(packet: dict[str, Any], interface: Any = None) -> None:
            if not self._owns_interface(interface):
                return
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._handle_native_packet(packet))
            )

        def connect_handler(interface: Any, topic: Any = None) -> None:
            if not self._owns_interface(interface):
                return
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._set_connected(True))
            )

        def disconnect_handler(interface: Any, topic: Any = None) -> None:
            if not self._owns_interface(interface):
                return
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._set_connected(False))
            )

        self._receive_handler = receive_handler
        self._connect_handler = connect_handler
        self._disconnect_handler = disconnect_handler
        pub.subscribe(receive_handler, "meshtastic.receive")
        pub.subscribe(connect_handler, "meshtastic.connection.established")
        pub.subscribe(disconnect_handler, "meshtastic.connection.lost")

        try:
            self._interface = await self.hass.async_add_executor_job(self._make_native_interface)
        except Exception as err:
            await self._emit_error(err)
            raise
        await self._set_connected(True)
        await self.async_refresh()

    def _make_native_interface(self) -> Any:
        if self.config.transport == TRANSPORT_SERIAL:
            import meshtastic.serial_interface

            return meshtastic.serial_interface.SerialInterface(self.config.serial_path)
        if self.config.transport == TRANSPORT_TCP:
            import meshtastic.tcp_interface

            host = self.config.host or self.config.api_url
            if not host:
                raise RuntimeError("TCP transport requires host")
            if self.config.port:
                return meshtastic.tcp_interface.TCPInterface(host, portNumber=self.config.port)
            return meshtastic.tcp_interface.TCPInterface(host)
        if self.config.transport == TRANSPORT_BLUETOOTH:
            import meshtastic.ble_interface

            if not self.config.ble_address:
                raise RuntimeError("Bluetooth transport requires ble_address")
            return meshtastic.ble_interface.BLEInterface(self.config.ble_address)
        raise RuntimeError(f"Unsupported Meshtastic transport: {self.config.transport}")

    async def _start_mqtt(self) -> None:
        try:
            from homeassistant.components import mqtt
        except ImportError as err:
            await self._emit_error("Home Assistant MQTT integration is unavailable")
            raise err

        topic = self.config.mqtt_topic or DEFAULT_MESHTASTIC_MQTT_TOPIC

        async def message_received(msg: Any) -> None:
            try:
                raw_payload = msg.payload
                if isinstance(raw_payload, bytes):
                    raw_payload = raw_payload.decode(errors="replace")
                raw = json.loads(raw_payload)
            except Exception as err:
                await self._emit_error(f"invalid Meshtastic MQTT payload on {msg.topic}: {err}")
                return
            packet = meshtastic_packet_to_state_packet(
                raw,
                gateway_id=self.config.gateway_id,
                topic=msg.topic,
            )
            await self._handle_packet(packet)

        self._unsub_mqtt = await mqtt.async_subscribe(self.hass, topic, message_received, 0)
        await self._set_connected(True, mqtt_topic=topic)

    async def _mqtt_publish_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
        message_id: str,
    ) -> None:
        publish_topic = str(self.config.options.get("publish_topic") or "").strip()
        if not publish_topic:
            raise RuntimeError(
                "Meshtastic MQTT sending requires options.publish_topic "
                "(for example msh/US/2/json/mqtt/)"
            )
        if "#" in publish_topic or "+" in publish_topic:
            raise RuntimeError("Meshtastic MQTT publish_topic cannot contain wildcards")
        mqtt_node_id = _meshtastic_node_number(self.config.options.get("mqtt_node_id"))
        if mqtt_node_id is None:
            raise RuntimeError("Meshtastic MQTT sending requires options.mqtt_node_id")

        from homeassistant.components import mqtt

        payload = {
            "from": mqtt_node_id,
            "type": "sendtext",
            "payload": message,
        }
        if target_node:
            destination = _meshtastic_node_number(target_node)
            if destination is None:
                raise RuntimeError(f"Invalid Meshtastic MQTT target node: {target_node}")
            payload["to"] = destination
        if channel is not None:
            channel_index = coerce_int(channel)
            if channel_index is None or not 0 <= channel_index <= 7:
                raise RuntimeError(f"Invalid Meshtastic MQTT channel: {channel}")
            payload["channel"] = channel_index
        await mqtt.async_publish(
            self.hass,
            publish_topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )

    async def _handle_native_packet(self, raw: dict[str, Any]) -> None:
        packet = meshtastic_packet_to_state_packet(raw, gateway_id=self.config.gateway_id)
        await self._handle_packet(packet)

    async def _handle_packet(self, packet: MeshPacket) -> None:
        await self._emit_packet(packet)
        node = meshtastic_packet_to_node(packet)
        if node:
            await self._emit_node(node)


def meshtastic_packet_to_state_packet(
    raw: dict[str, Any],
    *,
    gateway_id: str,
    topic: str | None = None,
) -> MeshPacket:
    """Normalize a Meshtastic packet dict or MQTT JSON payload."""
    decoded = raw.get("decoded") if isinstance(raw.get("decoded"), dict) else {}
    data = decoded.get("data") if isinstance(decoded.get("data"), dict) else {}
    telemetry = decoded.get("telemetry") if isinstance(decoded.get("telemetry"), dict) else {}
    mqtt_payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    payload = raw.get("payload", decoded.get("payload", data.get("payload")))
    text = (
        raw.get("text")
        or decoded.get("text")
        or data.get("text")
        or telemetry.get("text")
        or mqtt_payload.get("text")
    )
    if isinstance(payload, bytes):
        payload = payload.hex()
    packet_time = parse_timestamp(raw.get("rxTime") or raw.get("timestamp") or raw.get("time")) or utcnow()
    channel_value = raw.get("channel")
    if channel_value is None:
        channel_value = decoded.get("channel")
    return MeshPacket(
        protocol=PROTOCOL_MESHTASTIC,
        gateway_id=gateway_id,
        packet_id=str(raw.get("id") or raw.get("packet_id") or "") or None,
        sender=str(raw.get("fromId") or raw.get("from") or raw.get("from_num") or "") or None,
        receiver=str(raw.get("toId") or raw.get("to") or raw.get("to_num") or "") or None,
        channel=str(channel_value) if channel_value is not None else None,
        portnum=str(
            decoded.get("portnum")
            or decoded.get("portnumName")
            or raw.get("portnum")
            or raw.get("type")
            or ""
        )
        or None,
        payload=payload,
        text=text,
        encrypted=bool(raw.get("encrypted")) if "encrypted" in raw else None,
        rssi=coerce_float(raw.get("rxRssi") or raw.get("rssi")),
        snr=coerce_float(raw.get("rxSnr") or raw.get("snr")),
        hops=coerce_int(raw.get("hopsAway") or raw.get("hops")),
        hop_limit=coerce_int(raw.get("hopLimit") or raw.get("hop_limit")),
        timestamp=packet_time,
        raw={**raw, **({"topic": topic} if topic else {})},
    )


def meshtastic_node_to_state(
    raw: dict[str, Any],
    *,
    gateway_id: str,
    fallback_node_id: str | None = None,
) -> NodeState:
    """Normalize Meshtastic node DB entries into NodeState."""
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    position = raw.get("position") if isinstance(raw.get("position"), dict) else {}
    device_metrics = raw.get("deviceMetrics") if isinstance(raw.get("deviceMetrics"), dict) else {}
    node_id = str(user.get("id") or raw.get("id") or raw.get("num") or fallback_node_id or "") or None
    mac = user.get("macaddr") or user.get("mac")
    node_key = canonical_node_key(PROTOCOL_MESHTASTIC, node_id=node_id, mac=mac)
    last_heard = parse_timestamp(raw.get("lastHeard")) or parse_timestamp(raw.get("last_heard"))
    if last_heard is None and isinstance(raw.get("lastHeard"), (int, float)):
        last_heard = datetime.fromtimestamp(raw["lastHeard"], tz=UTC)
    sensors = {}
    for source_key in ("environmentMetrics", "airQualityMetrics", "powerMetrics"):
        source = raw.get(source_key)
        if isinstance(source, dict):
            sensors.update(_flatten_metrics(source))
    return NodeState(
        node_key=node_key,
        protocol=PROTOCOL_MESHTASTIC,
        node_id=node_id,
        mac=mac,
        user_name=user.get("userName") or user.get("name"),
        long_name=user.get("longName"),
        short_name=user.get("shortName"),
        hardware_model=user.get("hwModel") or raw.get("hwModel"),
        firmware_version=raw.get("firmwareVersion") or raw.get("firmware_version"),
        role=raw.get("role"),
        online=True,
        last_heard=last_heard or utcnow(),
        last_gateway_id=gateway_id,
        gateway_ids={gateway_id},
        connectivity={
            "snr": coerce_float(raw.get("snr")),
            "rssi": coerce_float(raw.get("rssi")),
            "channel_utilization": coerce_float(device_metrics.get("channelUtilization")),
            "air_utilization": coerce_float(device_metrics.get("airUtilTx")),
        },
        power={
            "battery_level": coerce_float(device_metrics.get("batteryLevel")),
            "voltage": coerce_float(device_metrics.get("voltage")),
        },
        location={
            "latitude": coerce_float(position.get("latitude")),
            "longitude": coerce_float(position.get("longitude")),
            "altitude": coerce_float(position.get("altitude")),
            "speed": coerce_float(position.get("groundSpeed")),
            "heading": coerce_float(position.get("groundTrack")),
            "precision": coerce_float(position.get("precisionBits")),
        },
        sensors=sensors,
        raw=raw,
    )


def meshtastic_packet_to_node(packet: MeshPacket) -> NodeState | None:
    """Derive a node update from a Meshtastic packet."""
    raw = packet.raw
    decoded = raw.get("decoded") if isinstance(raw.get("decoded"), dict) else {}
    telemetry = decoded.get("telemetry") if isinstance(decoded.get("telemetry"), dict) else {}
    user = decoded.get("user") if isinstance(decoded.get("user"), dict) else {}
    position = decoded.get("position") if isinstance(decoded.get("position"), dict) else {}
    mqtt_payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    if str(raw.get("type") or "").lower() == "nodeinfo" and mqtt_payload:
        user = {
            "id": mqtt_payload.get("id"),
            "userName": mqtt_payload.get("username"),
            "longName": mqtt_payload.get("longname") or mqtt_payload.get("long_name"),
            "shortName": mqtt_payload.get("shortname") or mqtt_payload.get("short_name"),
            "hwModel": mqtt_payload.get("hardware") or mqtt_payload.get("hw_model"),
        }
    if not packet.sender and not user and not position and not telemetry:
        return None
    mac = user.get("macaddr") or user.get("mac")
    node_id = str(user.get("id") or packet.sender or "") or None
    node_key = canonical_node_key(PROTOCOL_MESHTASTIC, node_id=node_id, mac=mac)
    sensors: dict[str, Any] = {}
    power: dict[str, Any] = {}
    connectivity = {
        "snr": packet.snr,
        "rssi": packet.rssi,
        "hops": packet.hops,
        "hop_limit": packet.hop_limit,
    }
    for key in ("deviceMetrics", "environmentMetrics", "airQualityMetrics", "powerMetrics"):
        metrics = telemetry.get(key)
        if isinstance(metrics, dict):
            flattened = _flatten_metrics(metrics)
            sensors.update(flattened)
            if key == "deviceMetrics":
                power.update(
                    {
                        "battery_level": coerce_float(metrics.get("batteryLevel")),
                        "voltage": coerce_float(metrics.get("voltage")),
                    }
                )
                connectivity.update(
                    {
                        "channel_utilization": coerce_float(metrics.get("channelUtilization")),
                        "air_utilization": coerce_float(metrics.get("airUtilTx")),
                    }
                )
    return NodeState(
        node_key=node_key,
        protocol=PROTOCOL_MESHTASTIC,
        node_id=node_id,
        mac=mac,
        user_name=user.get("userName") or user.get("name"),
        long_name=user.get("longName"),
        short_name=user.get("shortName"),
        hardware_model=user.get("hwModel"),
        online=True,
        last_heard=packet.timestamp,
        last_gateway_id=packet.gateway_id,
        gateway_ids={packet.gateway_id},
        connectivity=connectivity,
        power=power,
        location={
            "latitude": coerce_float(position.get("latitude")),
            "longitude": coerce_float(position.get("longitude")),
            "altitude": coerce_float(position.get("altitude")),
            "speed": coerce_float(position.get("groundSpeed") or position.get("speed")),
            "heading": coerce_float(position.get("groundTrack") or position.get("heading")),
            "precision": coerce_float(position.get("precisionBits") or position.get("precision")),
        },
        sensors=sensors,
        raw=raw,
    )


def _flatten_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            flattened[_snake(key)] = value
    return flattened


def _snake(value: str) -> str:
    out = []
    for char in value:
        if char.isupper() and out:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _meshtastic_node_number(value: Any) -> int | None:
    """Parse decimal, !hex, 0xhex, or canonical Meshtastic node IDs."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip()
        if text.lower().startswith("meshtastic:"):
            text = text.split(":", 1)[1]
        try:
            if text.startswith("!"):
                number = int(text[1:], 16)
            elif text.lower().startswith("0x"):
                number = int(text, 16)
            else:
                number = int(text, 10)
        except (TypeError, ValueError):
            return None
    return number if 0 <= number <= 0xFFFFFFFF else None
