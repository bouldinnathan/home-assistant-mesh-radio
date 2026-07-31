"""Pure data models and normalization helpers for MeshNet."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

JsonDict = dict[str, Any]


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse common timestamp representations into UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            if value.endswith("Z"):
                value = f"{value[:-1]}+00:00"
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def timestamp_to_json(value: datetime | None) -> str | None:
    """Serialize a timestamp for JSON storage."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def stable_json(data: Any) -> str:
    """Serialize data deterministically for hashing and persistence."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def canonical_node_key(protocol: str, node_id: Any = None, mac: Any = None, public_key: Any = None) -> str:
    """Return a stable cross-gateway node key."""
    if mac_id := _clean_id(mac):
        return f"mac:{mac_id.lower().replace(':', '')}"
    if key_id := _clean_id(public_key):
        return f"pub:{key_id.lower()}"
    if node := _clean_id(node_id):
        return f"{protocol}:{node}"
    digest = hashlib.sha256(f"{protocol}:{utcnow().timestamp()}".encode()).hexdigest()[:12]
    return f"{protocol}:unknown:{digest}"


def coerce_float(value: Any) -> float | None:
    """Return a float or None for noisy radio payload values."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if math.isfinite(converted) else None


def coerce_int(value: Any) -> int | None:
    """Return an int or None for noisy radio payload values."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def has_valid_location(
    location: Mapping[str, Any], *, zero_pair_is_missing: bool = False
) -> bool:
    """Return whether cached coordinates are finite and geographically valid."""
    latitude_raw = location.get("latitude")
    longitude_raw = location.get("longitude")
    if isinstance(latitude_raw, bool) or isinstance(longitude_raw, bool):
        return False
    latitude = coerce_float(latitude_raw)
    longitude = coerce_float(longitude_raw)
    valid = bool(
        latitude is not None
        and longitude is not None
        and math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    )
    return bool(
        valid
        and not (
            zero_pair_is_missing
            and latitude == 0.0
            and longitude == 0.0
        )
    )


def location_accuracy_meters(location: Mapping[str, Any]) -> float:
    """Return an explicit finite meter accuracy, or zero when unavailable."""
    raw_accuracy = location.get("accuracy")
    if isinstance(raw_accuracy, bool):
        return 0.0
    accuracy = coerce_float(raw_accuracy)
    if accuracy is None or not math.isfinite(accuracy) or accuracy < 0:
        return 0.0
    return accuracy


def merge_dict(base: JsonDict, update: JsonDict) -> JsonDict:
    """Merge dictionaries without dropping existing values when updates are empty."""
    merged = dict(base)
    for key, value in update.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(slots=True)
class GatewayConfig:
    """Configuration for a single mesh gateway."""

    gateway_id: str
    name: str
    protocol: str
    transport: str
    host: str | None = None
    port: int | None = None
    serial_path: str | None = None
    ble_address: str | None = None
    mqtt_topic: str | None = None
    api_url: str | None = None
    api_key: str | None = None
    options: JsonDict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: JsonDict) -> GatewayConfig:
        """Build a gateway configuration from config-entry data."""
        return cls(
            gateway_id=str(data["gateway_id"]),
            name=str(data.get("name") or data.get("gateway_name") or data["gateway_id"]),
            protocol=str(data["protocol"]),
            transport=str(data["transport"]),
            host=data.get("host"),
            port=coerce_int(data.get("port")),
            serial_path=data.get("serial_path"),
            ble_address=data.get("ble_address"),
            mqtt_topic=data.get("mqtt_topic"),
            api_url=data.get("api_url"),
            api_key=data.get("api_key"),
            options=dict(data.get("options") or {}),
        )

    def as_dict(self, *, redact: bool = False) -> JsonDict:
        """Serialize the gateway configuration."""
        data = {
            "gateway_id": self.gateway_id,
            "name": self.name,
            "protocol": self.protocol,
            "transport": self.transport,
            "host": self.host,
            "port": self.port,
            "serial_path": self.serial_path,
            "ble_address": self.ble_address,
            "mqtt_topic": self.mqtt_topic,
            "api_url": self.api_url,
            "api_key": "***" if redact and self.api_key else self.api_key,
            "options": dict(self.options),
        }
        return {key: value for key, value in data.items() if value not in (None, {}, "")}


@dataclass(slots=True)
class GatewayStatus:
    """Runtime status for a gateway connection."""

    gateway_id: str
    name: str
    protocol: str
    transport: str
    connected: bool = False
    last_connected: datetime | None = None
    last_packet: datetime | None = None
    packets_received: int = 0
    packets_sent: int = 0
    duplicate_packets: int = 0
    failure_count: int = 0
    last_failure_category: str | None = None
    last_failure_at: datetime | None = None
    errors: list[str] = field(default_factory=list)
    detail: JsonDict = field(default_factory=dict)

    def as_dict(self) -> JsonDict:
        """Serialize the gateway status."""
        return {
            "gateway_id": self.gateway_id,
            "name": self.name,
            "protocol": self.protocol,
            "transport": self.transport,
            "connected": self.connected,
            "last_connected": timestamp_to_json(self.last_connected),
            "last_packet": timestamp_to_json(self.last_packet),
            "packets_received": self.packets_received,
            "packets_sent": self.packets_sent,
            "duplicate_packets": self.duplicate_packets,
            "failure_count": self.failure_count,
            "last_failure_category": self.last_failure_category,
            "last_failure_at": timestamp_to_json(self.last_failure_at),
            "errors": list(self.errors[-10:]),
            "detail": dict(self.detail),
        }


@dataclass(slots=True)
class MessageRecord:
    """A normalized mesh message."""

    message_id: str
    protocol: str
    gateway_id: str
    sender: str | None
    receiver: str | None
    channel: str | None
    text: str
    message_type: str = "broadcast"
    priority: str = "normal"
    encrypted: bool | None = None
    hops: int | None = None
    timestamp: datetime = field(default_factory=utcnow)
    direction: str = "rx"
    reply_to_message_id: str | None = None
    reaction: str | None = None
    raw: JsonDict = field(default_factory=dict)

    def as_dict(self) -> JsonDict:
        """Serialize the message."""
        return {
            "message_id": self.message_id,
            "protocol": self.protocol,
            "gateway_id": self.gateway_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "channel": self.channel,
            "text": self.text,
            "message_type": self.message_type,
            "priority": self.priority,
            "encrypted": self.encrypted,
            "hops": self.hops,
            "timestamp": timestamp_to_json(self.timestamp),
            "direction": self.direction,
            "reply_to_message_id": self.reply_to_message_id,
            "reaction": self.reaction,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> MessageRecord:
        """Deserialize a message."""
        return cls(
            message_id=str(data["message_id"]),
            protocol=str(data["protocol"]),
            gateway_id=str(data["gateway_id"]),
            sender=data.get("sender"),
            receiver=data.get("receiver"),
            channel=data.get("channel"),
            text=str(data.get("text") or ""),
            message_type=str(data.get("message_type") or "broadcast"),
            priority=str(data.get("priority") or "normal"),
            encrypted=data.get("encrypted"),
            hops=coerce_int(data.get("hops")),
            timestamp=parse_timestamp(data.get("timestamp")) or utcnow(),
            direction=str(data.get("direction") or "rx"),
            reply_to_message_id=data.get("reply_to_message_id"),
            reaction=data.get("reaction"),
            raw=dict(data.get("raw") or {}),
        )


@dataclass(slots=True)
class MeshPacket:
    """A normalized packet from any supported mesh gateway."""

    protocol: str
    gateway_id: str
    packet_id: str | None = None
    sender: str | None = None
    receiver: str | None = None
    channel: str | None = None
    portnum: str | None = None
    payload: Any = None
    text: str | None = None
    encrypted: bool | None = None
    rssi: float | None = None
    snr: float | None = None
    hops: int | None = None
    hop_limit: int | None = None
    reply_to_message_id: str | None = None
    reaction: str | None = None
    timestamp: datetime = field(default_factory=utcnow)
    raw: JsonDict = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Return a stable packet fingerprint for deduplication."""
        if self.packet_id:
            return f"{self.protocol}:{self.packet_id}"
        bucket = int(self.timestamp.timestamp() // 5)
        payload = {
            "protocol": self.protocol,
            "sender": self.sender,
            "receiver": self.receiver,
            "channel": self.channel,
            "portnum": self.portnum,
            "text": self.text,
            "payload": self.payload,
            "bucket": bucket,
        }
        return hashlib.sha256(stable_json(payload).encode()).hexdigest()

    def as_dict(self) -> JsonDict:
        """Serialize the packet."""
        return {
            "protocol": self.protocol,
            "gateway_id": self.gateway_id,
            "packet_id": self.packet_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "channel": self.channel,
            "portnum": self.portnum,
            "payload": self.payload,
            "text": self.text,
            "encrypted": self.encrypted,
            "rssi": self.rssi,
            "snr": self.snr,
            "hops": self.hops,
            "hop_limit": self.hop_limit,
            "reply_to_message_id": self.reply_to_message_id,
            "reaction": self.reaction,
            "timestamp": timestamp_to_json(self.timestamp),
            "raw": self.raw,
        }


@dataclass(slots=True)
class NodeState:
    """The current normalized state for one mesh node."""

    node_key: str
    protocol: str
    node_id: str | None = None
    mac: str | None = None
    public_key: str | None = None
    user_name: str | None = None
    long_name: str | None = None
    short_name: str | None = None
    hardware_model: str | None = None
    firmware_version: str | None = None
    radio_type: str | None = None
    role: str | None = None
    online: bool = False
    last_heard: datetime | None = None
    last_gateway_id: str | None = None
    gateway_ids: set[str] = field(default_factory=set)
    connectivity: JsonDict = field(default_factory=dict)
    power: JsonDict = field(default_factory=dict)
    radio: JsonDict = field(default_factory=dict)
    location: JsonDict = field(default_factory=dict)
    routing: JsonDict = field(default_factory=dict)
    sensors: JsonDict = field(default_factory=dict)
    raw: JsonDict = field(default_factory=dict)

    def merge(self, other: NodeState) -> NodeState:
        """Merge newer state into this node and return self."""
        if other.node_id:
            self.node_id = other.node_id
        if other.mac:
            self.mac = other.mac
        if other.public_key:
            self.public_key = other.public_key
        for attr in (
            "user_name",
            "long_name",
            "short_name",
            "hardware_model",
            "firmware_version",
            "radio_type",
            "role",
        ):
            value = getattr(other, attr)
            if value:
                setattr(self, attr, value)
        if other.last_heard and (not self.last_heard or other.last_heard >= self.last_heard):
            self.last_heard = other.last_heard
            self.last_gateway_id = other.last_gateway_id
            self.online = other.online
        self.gateway_ids.update(other.gateway_ids)
        if other.last_gateway_id:
            self.gateway_ids.add(other.last_gateway_id)
        connectivity_update = dict(other.connectivity)
        if self.protocol == "meshtastic" and "hops" in connectivity_update:
            hops = connectivity_update.pop("hops")
            hops_gateway_id = connectivity_update.pop("hops_gateway_id", None)
            via_mqtt = connectivity_update.pop("via_mqtt", None)
            # Hop count, transport origin, and observing gateway describe one
            # measurement. Never merge those fields independently: doing so
            # could combine an MQTT zero-hop packet with stale RF provenance.
            if hops is not None:
                for key in ("hops", "hops_gateway_id", "via_mqtt"):
                    self.connectivity.pop(key, None)
                self.connectivity["hops"] = hops
                if hops_gateway_id is not None:
                    self.connectivity["hops_gateway_id"] = hops_gateway_id
                self.connectivity["via_mqtt"] = via_mqtt is True
        self.connectivity = merge_dict(self.connectivity, connectivity_update)
        self.power = merge_dict(self.power, other.power)
        self.radio = merge_dict(self.radio, other.radio)
        self.location = merge_dict(self.location, other.location)
        self.routing = merge_dict(self.routing, other.routing)
        self.sensors = merge_dict(self.sensors, other.sensors)
        self.raw = merge_dict(self.raw, other.raw)
        return self

    @property
    def display_name(self) -> str:
        """Return a useful display name."""
        return self.long_name or self.user_name or self.short_name or self.node_id or self.node_key

    def as_dict(self) -> JsonDict:
        """Serialize the node."""
        return {
            "node_key": self.node_key,
            "protocol": self.protocol,
            "node_id": self.node_id,
            "mac": self.mac,
            "public_key": self.public_key,
            "user_name": self.user_name,
            "long_name": self.long_name,
            "short_name": self.short_name,
            "hardware_model": self.hardware_model,
            "firmware_version": self.firmware_version,
            "radio_type": self.radio_type,
            "role": self.role,
            "online": self.online,
            "last_heard": timestamp_to_json(self.last_heard),
            "last_gateway_id": self.last_gateway_id,
            "gateway_ids": sorted(self.gateway_ids),
            "connectivity": self.connectivity,
            "power": self.power,
            "radio": self.radio,
            "location": self.location,
            "routing": self.routing,
            "sensors": self.sensors,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> NodeState:
        """Deserialize a node."""
        return cls(
            node_key=str(data["node_key"]),
            protocol=str(data["protocol"]),
            node_id=data.get("node_id"),
            mac=data.get("mac"),
            public_key=data.get("public_key"),
            user_name=data.get("user_name"),
            long_name=data.get("long_name"),
            short_name=data.get("short_name"),
            hardware_model=data.get("hardware_model"),
            firmware_version=data.get("firmware_version"),
            radio_type=data.get("radio_type"),
            role=data.get("role"),
            online=bool(data.get("online")),
            last_heard=parse_timestamp(data.get("last_heard")),
            last_gateway_id=data.get("last_gateway_id"),
            gateway_ids=set(data.get("gateway_ids") or []),
            connectivity=dict(data.get("connectivity") or {}),
            power=dict(data.get("power") or {}),
            radio=dict(data.get("radio") or {}),
            location=dict(data.get("location") or {}),
            routing=dict(data.get("routing") or {}),
            sensors=dict(data.get("sensors") or {}),
            raw=dict(data.get("raw") or {}),
        )


@dataclass(slots=True)
class MeshSnapshot:
    """Current complete mesh view exposed by the coordinator."""

    nodes: dict[str, NodeState] = field(default_factory=dict)
    gateways: dict[str, GatewayStatus] = field(default_factory=dict)
    recent_messages: list[MessageRecord] = field(default_factory=list)
    mesh_health_score: float | None = None
    messages_today: int = 0

    def as_dict(self) -> JsonDict:
        """Serialize the snapshot."""
        return {
            "nodes": {key: node.as_dict() for key, node in self.nodes.items()},
            "gateways": {key: gateway.as_dict() for key, gateway in self.gateways.items()},
            "recent_messages": [message.as_dict() for message in self.recent_messages],
            "mesh_health_score": self.mesh_health_score,
            "messages_today": self.messages_today,
        }
