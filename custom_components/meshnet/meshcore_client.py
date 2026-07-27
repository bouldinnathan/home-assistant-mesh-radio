"""MeshCore gateway adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from .const import (
    MESSAGE_TYPE_DIRECT,
    PROTOCOL_MESHCORE,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_NATIVE,
    TRANSPORT_REST,
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


class MeshCoreClient(MeshGateway):
    """Gateway adapter for MeshCore native, MQTT, REST, and JSON serial transports."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._meshcore: Any | None = None
        self._unsub_mqtt: Any | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False
        self._contacts: dict[str, Any] = {}
        self._native_subscribed_events: set[str] = set()

    async def async_start(self) -> None:
        """Start the MeshCore transport."""
        self._stopping = False
        if self.config.transport == TRANSPORT_MQTT:
            await self._start_mqtt()
            return
        if self.config.transport == TRANSPORT_REST:
            await self._start_rest()
            return
        if self.config.transport in {TRANSPORT_SERIAL, TRANSPORT_TCP, TRANSPORT_BLUETOOTH, TRANSPORT_NATIVE}:
            await self._start_native()
            return
        raise RuntimeError(f"Unsupported MeshCore transport: {self.config.transport}")

    async def async_stop(self) -> None:
        """Stop the MeshCore transport."""
        self._stopping = True
        if self._unsub_mqtt:
            self._unsub_mqtt()
            self._unsub_mqtt = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._meshcore is not None:
            meshcore = self._meshcore
            self._meshcore = None
            disconnect = getattr(meshcore, "disconnect", None)
            if disconnect:
                result = disconnect()
                if hasattr(result, "__await__"):
                    await result
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
        """Send a MeshCore message."""
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
        elif self.config.transport == TRANSPORT_REST:
            await self._rest_send_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
                message_id=message_id,
            )
        else:
            await self._native_send_message(
                target_node=target_node,
                message=message,
                channel=channel,
                message_type=message_type,
            )
        self.status.packets_sent += 1
        await self._emit_status()
        return message_id

    async def async_refresh(self) -> None:
        """Refresh native MeshCore state."""
        if self._meshcore is None:
            return
        commands = getattr(self._meshcore, "commands", None)
        if not commands:
            return
        for command_name in ("send_device_query", "get_contacts", "get_bat"):
            command = getattr(commands, command_name, None)
            if command is None:
                continue
            try:
                result = await command()
            except Exception as err:
                await self._emit_error(f"{command_name} failed: {err}")
                continue
            # Command results also pass through the SDK event dispatcher. Avoid
            # processing them twice when the corresponding subscription exists.
            if _event_type_name(result) not in self._native_subscribed_events:
                await self._handle_native_event(result)

    async def _start_mqtt(self) -> None:
        try:
            from homeassistant.components import mqtt
        except ImportError as err:
            await self._emit_error("Home Assistant MQTT integration is unavailable")
            raise err

        topic = self.config.mqtt_topic or "meshcore/+/+/packets"

        async def message_received(msg: Any) -> None:
            try:
                raw_payload = msg.payload
                if isinstance(raw_payload, bytes):
                    raw_payload = raw_payload.decode(errors="replace")
                raw = json.loads(raw_payload)
            except Exception as err:
                await self._emit_error(f"invalid MeshCore MQTT payload on {msg.topic}: {err}")
                return
            packet = meshcore_payload_to_packet(
                raw,
                gateway_id=self.config.gateway_id,
                topic=msg.topic,
            )
            await self._handle_packet(packet)

        self._unsub_mqtt = await mqtt.async_subscribe(self.hass, topic, message_received, 0)
        await self._set_connected(True, mqtt_topic=topic)

    async def _start_rest(self) -> None:
        if not self.config.api_url:
            raise RuntimeError("REST transport requires api_url")
        try:
            await self._rest_fetch()
        except Exception as err:
            await self._set_connected(False, api_url=self.config.api_url)
            await self._emit_error(f"REST initial fetch failed: {err}")
            raise
        await self._set_connected(True, api_url=self.config.api_url)
        task = self.hass.async_create_task(self._rest_poll_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _rest_poll_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(float(self.config.options.get("scan_interval", 30)))
            if self._stopping:
                return
            await self._rest_poll_once()

    async def _rest_poll_once(self) -> None:
        """Poll REST once and reflect endpoint health in gateway status."""
        try:
            await self._rest_fetch()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            await self._set_connected(False, api_url=self.config.api_url)
            await self._emit_error(f"REST poll failed: {err}")
            return
        if not self.status.connected:
            await self._set_connected(True, api_url=self.config.api_url)

    async def _rest_fetch(self) -> None:
        import aiohttp

        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(self.config.api_url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json()
        await self._handle_snapshot_payload(data)

    async def _start_native(self) -> None:
        try:
            from meshcore import EventType, MeshCore
        except ImportError as err:
            await self._emit_error("meshcore Python package is unavailable")
            raise err

        try:
            if self.config.transport == TRANSPORT_TCP:
                if not self.config.host:
                    raise RuntimeError("TCP transport requires host")
                self._meshcore = await MeshCore.create_tcp(
                    self.config.host,
                    self.config.port or 4000,
                )
            elif self.config.transport == TRANSPORT_BLUETOOTH:
                self._meshcore = await MeshCore.create_ble(
                    self.config.ble_address,
                    pin=self.config.options.get("pin"),
                )
            else:
                if not self.config.serial_path:
                    raise RuntimeError("Serial/native transport requires serial_path")
                self._meshcore = await MeshCore.create_serial(
                    self.config.serial_path,
                    int(self.config.options.get("baudrate", 115200)),
                    debug=bool(self.config.options.get("debug")),
                )
        except Exception as err:
            await self._emit_error(err)
            raise

        self._subscribe_native_events(EventType)
        await self._set_connected(True)
        await self.async_refresh()
        task = self.hass.async_create_task(self._native_message_poll_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _subscribe_native_events(self, event_type: Any) -> None:
        self._native_subscribed_events.clear()
        if self._meshcore is None:
            return
        subscribe = getattr(self._meshcore, "subscribe", None)
        if subscribe is None:
            return

        def handler(event: Any) -> None:
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._handle_native_event(event))
            )

        names = [
            "CONNECTED",
            "DISCONNECTED",
            "CONTACT_MSG_RECV",
            "CHANNEL_MSG_RECV",
            "MSG_SENT",
            "MESSAGES_WAITING",
            "ADVERTISEMENT",
            "PATH_UPDATE",
            "BATTERY",
            "STATUS_RESPONSE",
            "SELF_INFO",
            "DEVICE_INFO",
            "CONTACTS",
            "NEW_CONTACT",
        ]
        for name in names:
            value = getattr(event_type, name, None)
            if value is None:
                continue
            try:
                subscribe(value, handler)
            except Exception as err:
                self._logger.debug("Could not subscribe to MeshCore event %s: %s", name, err)
            else:
                self._native_subscribed_events.add(_event_type_name(value))

    async def _native_message_poll_loop(self) -> None:
        while not self._stopping:
            if self._meshcore is None:
                return
            command = getattr(getattr(self._meshcore, "commands", None), "get_msg", None)
            if command is None:
                return
            try:
                event = await command(timeout=5)
                event_name = _event_type_name(event)
                if event_name == "no_more_messages":
                    # Some devices return this immediately. Yield long enough to
                    # avoid a tight command loop that can saturate the transport.
                    poll_delay = max(0.1, float(self.config.options.get("message_poll_interval", 1)))
                    await asyncio.sleep(poll_delay)
                    continue
                # The SDK dispatcher invokes subscribed handlers before the
                # command future resolves. Only handle directly as a fallback.
                if event_name not in self._native_subscribed_events:
                    await self._handle_native_event(event)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._logger.debug("MeshCore message poll timed out")
            except Exception as err:
                await self._emit_error(f"MeshCore message poll failed: {err}")
                await asyncio.sleep(10)

    async def _native_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        message_type: str,
    ) -> None:
        if self._meshcore is None:
            raise RuntimeError("MeshCore interface is not connected")
        commands = getattr(self._meshcore, "commands", None)
        if commands is None:
            raise RuntimeError("MeshCore command interface is unavailable")
        if message_type == MESSAGE_TYPE_DIRECT and target_node:
            result = await commands.send_msg(self._contacts.get(target_node, target_node), message)
        else:
            channel_index = coerce_int(channel) or 0
            result = await commands.send_chan_msg(channel_index, message)
        if str(getattr(getattr(result, "type", ""), "value", getattr(result, "type", ""))).endswith("error"):
            raise RuntimeError(str(getattr(result, "payload", "MeshCore send failed")))

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
        base_topic = str(self.config.options.get("publish_topic") or "").strip()
        if not base_topic:
            raise RuntimeError(
                "MeshCore MQTT sending requires options.publish_topic for a compatible JSON bridge"
            )
        if "#" in base_topic or "+" in base_topic:
            raise RuntimeError("MeshCore MQTT publish_topic cannot contain wildcards")

        from homeassistant.components import mqtt
        payload = {
            "id": message_id,
            "command": "send_message",
            "target_node": target_node,
            "channel": coerce_int(channel) if channel is not None else 0,
            "message": message,
            "priority": priority,
            "message_type": message_type,
            "gateway_id": self.config.gateway_id,
        }
        await mqtt.async_publish(
            self.hass,
            f"{base_topic}/send",
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )

    async def _rest_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
        message_id: str,
    ) -> None:
        import aiohttp

        if not self.config.api_url:
            raise RuntimeError("REST transport requires api_url")
        url = self.config.options.get("send_url") or self.config.api_url.rstrip("/") + "/send"
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload = {
            "id": message_id,
            "target_node": target_node,
            "channel": coerce_int(channel) if channel is not None else 0,
            "message": message,
            "priority": priority,
            "message_type": message_type,
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload, timeout=20) as response:
                response.raise_for_status()

    async def _handle_native_event(self, event: Any) -> None:
        if event is None:
            return
        payload = getattr(event, "payload", None)
        event_type = _event_type_name(event)
        if event_type == "connected":
            await self._set_connected(True, connection_event=payload)
            return
        if event_type == "disconnected":
            await self._set_connected(False, disconnect_event=payload)
            return
        if event_type in {"messages_waiting", "no_more_messages"}:
            return
        if isinstance(payload, dict) and event_type == "contacts":
            await self._handle_contacts(payload)
            return
        raw = {"event_type": event_type, "payload": payload}
        packet = meshcore_payload_to_packet(
            raw,
            gateway_id=self.config.gateway_id,
            topic="native",
        )
        await self._handle_packet(packet)

    async def _handle_contacts(self, contacts: dict[Any, Any]) -> None:
        """Normalize a MeshCore contact mapping into individual nodes."""
        for contact_key, contact_payload in contacts.items():
            if not isinstance(contact_payload, dict):
                continue
            contact = dict(contact_payload)
            public_key = _public_key_text(
                contact.get("public_key")
                or contact.get("pubkey")
                or contact.get("pub_key")
                or contact_key
            )
            if public_key:
                contact["public_key"] = public_key
                self._contacts[public_key] = contact
                self._contacts[public_key.lower()] = contact
                self._contacts[f"pub:{public_key.lower()}"] = contact
            node = meshcore_payload_to_node(contact, self.config.gateway_id)
            if node:
                await self._emit_node(node)

    async def _handle_snapshot_payload(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        for node_payload in data.get("nodes", []) if isinstance(data.get("nodes"), list) else []:
            if isinstance(node_payload, dict):
                await self._emit_node(meshcore_payload_to_node(node_payload, self.config.gateway_id))
        for packet_payload in data.get("packets", []) if isinstance(data.get("packets"), list) else []:
            if isinstance(packet_payload, dict):
                await self._handle_packet(meshcore_payload_to_packet(packet_payload, gateway_id=self.config.gateway_id))
        for message_payload in data.get("messages", []) if isinstance(data.get("messages"), list) else []:
            if isinstance(message_payload, dict):
                await self._handle_packet(meshcore_payload_to_packet(message_payload, gateway_id=self.config.gateway_id))
        if "payload" in data and isinstance(data.get("payload"), dict):
            node = meshcore_payload_to_node(data["payload"], self.config.gateway_id)
            if node:
                await self._emit_node(node)

    async def _handle_packet(self, packet: MeshPacket) -> None:
        await self._emit_packet(packet)
        node = meshcore_payload_to_node(packet.raw.get("payload", packet.raw), packet.gateway_id, packet=packet)
        if node:
            await self._emit_node(node)


def meshcore_payload_to_packet(
    raw: dict[str, Any],
    *,
    gateway_id: str,
    topic: str | None = None,
) -> MeshPacket:
    """Normalize MeshCore packet, event, or REST JSON into MeshPacket."""
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    event_type = raw.get("event_type") or raw.get("type") or raw.get("packet_type")
    text = (
        payload.get("text")
        or payload.get("message")
        or raw.get("text")
        or raw.get("message")
    )
    timestamp = (
        parse_timestamp(payload.get("timestamp"))
        or parse_timestamp(raw.get("timestamp"))
        or parse_timestamp(payload.get("time"))
        or utcnow()
    )
    sender = (
        payload.get("sender")
        or payload.get("from")
        or payload.get("from_id")
        or payload.get("pubkey_prefix")
        or raw.get("origin")
        or raw.get("origin_id")
    )
    receiver = payload.get("receiver") or payload.get("to") or payload.get("dst") or raw.get("dst")
    route = payload.get("route") or raw.get("route")
    hops = coerce_int(payload.get("hops") or payload.get("path_len") or (len(route) if isinstance(route, list) else None))
    return MeshPacket(
        protocol=PROTOCOL_MESHCORE,
        gateway_id=gateway_id,
        packet_id=str(payload.get("hash") or payload.get("id") or raw.get("hash") or raw.get("id") or "") or None,
        sender=str(sender) if sender is not None else None,
        receiver=str(receiver) if receiver is not None else None,
        channel=str(payload.get("channel") or payload.get("channel_index") or raw.get("channel") or "") or None,
        portnum=str(event_type) if event_type is not None else None,
        payload=payload.get("data") or payload.get("payload") or raw.get("data"),
        text=text,
        encrypted=payload.get("encrypted") if "encrypted" in payload else None,
        rssi=coerce_float(payload.get("rssi") or payload.get("last_rssi")),
        snr=coerce_float(payload.get("snr") or payload.get("last_snr")),
        hops=hops,
        hop_limit=coerce_int(payload.get("hop_limit")),
        timestamp=timestamp,
        raw={**raw, **({"topic": topic} if topic else {})},
    )


def meshcore_payload_to_node(
    raw: dict[str, Any],
    gateway_id: str,
    *,
    packet: MeshPacket | None = None,
) -> NodeState | None:
    """Normalize MeshCore contact, advertisement, status, or packet payload into NodeState."""
    if not isinstance(raw, dict):
        return None
    event_type = raw.get("event_type")
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    public_key = _public_key_text(
        payload.get("public_key")
        or payload.get("pubkey")
        or payload.get("pub_key")
        or payload.get("hash")
        or payload.get("contact")
    )
    node_id = payload.get("id") or payload.get("node_id") or payload.get("name") or payload.get("adv_name")
    if packet and not public_key and not node_id:
        node_id = packet.sender
    if not public_key and not node_id:
        return None
    node_key = canonical_node_key(PROTOCOL_MESHCORE, node_id=node_id, public_key=public_key)
    timestamp = (
        parse_timestamp(payload.get("timestamp"))
        or (packet.timestamp if packet else None)
        or utcnow()
    )
    return NodeState(
        node_key=node_key,
        protocol=PROTOCOL_MESHCORE,
        node_id=str(node_id) if node_id is not None else None,
        public_key=public_key,
        user_name=payload.get("name") or payload.get("adv_name"),
        long_name=(
            payload.get("long_name")
            or payload.get("advert_name")
            or payload.get("adv_name")
            or payload.get("name")
        ),
        short_name=payload.get("short_name"),
        hardware_model=payload.get("model") or payload.get("hardware") or payload.get("board"),
        firmware_version=payload.get("firmware_version") or payload.get("firmware"),
        role=payload.get("role") or payload.get("node_type") or payload.get("type"),
        online=True,
        last_heard=timestamp,
        last_gateway_id=gateway_id,
        gateway_ids={gateway_id},
        connectivity={
            "snr": coerce_float(payload.get("snr") or (packet.snr if packet else None)),
            "rssi": coerce_float(payload.get("rssi") or (packet.rssi if packet else None)),
            "noise_floor": coerce_float(payload.get("noise_floor")),
            "packet_rx": coerce_int(payload.get("rx") or payload.get("packet_rx")),
            "packet_tx": coerce_int(payload.get("tx") or payload.get("packet_tx")),
            "hops": packet.hops if packet else coerce_int(payload.get("hops")),
        },
        power={
            "battery_level": coerce_float(payload.get("battery") or payload.get("battery_level")),
            "voltage": coerce_float(payload.get("voltage") or payload.get("battery_voltage")),
            "power_source": payload.get("power_source"),
            "charging": payload.get("charging"),
        },
        radio={
            "frequency": coerce_float(payload.get("frequency") or payload.get("freq")),
            "bandwidth": coerce_float(payload.get("bandwidth") or payload.get("bw")),
            "spreading_factor": coerce_int(payload.get("spreading_factor") or payload.get("sf")),
            "coding_rate": coerce_int(payload.get("coding_rate") or payload.get("cr")),
            "tx_power": coerce_float(payload.get("tx_power")),
            "duty_cycle": coerce_float(payload.get("duty_cycle")),
        },
        location={
            "latitude": coerce_float(payload.get("latitude") or payload.get("lat") or payload.get("adv_lat")),
            "longitude": coerce_float(payload.get("longitude") or payload.get("lon") or payload.get("adv_lon")),
            "altitude": coerce_float(payload.get("altitude") or payload.get("alt")),
        },
        routing={
            "route": payload.get("route"),
            "path": payload.get("path"),
            "event_type": event_type,
        },
        sensors=_extract_meshcore_sensors(payload),
        raw=raw,
    )


def _extract_meshcore_sensors(payload: dict[str, Any]) -> dict[str, Any]:
    sensors = {}
    for key, value in payload.items():
        normalized = str(key).lower()
        if normalized in {
            "temperature",
            "humidity",
            "pressure",
            "co2",
            "air_quality",
            "voltage",
            "current",
            "solar_charging",
            "motion",
            "accelerometer",
            "door",
            "pir",
            "water",
        }:
            sensors[normalized] = value
    nested = payload.get("sensors")
    if isinstance(nested, dict):
        sensors.update(nested)
    telemetry = payload.get("telemetry")
    if isinstance(telemetry, dict):
        sensors.update(telemetry)
    return sensors


def _event_type_name(event_or_type: Any) -> str:
    """Return the stable string value for an SDK Event or EventType."""
    event_type = getattr(event_or_type, "type", event_or_type)
    value = getattr(event_type, "value", event_type)
    return str(value).rsplit(".", 1)[-1].lower()


def _public_key_text(value: Any) -> str | None:
    """Normalize MeshCore public keys used as contact-map keys."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex()
    text = str(value).strip()
    return text or None
