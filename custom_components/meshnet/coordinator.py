"""Coordinator for the MeshNet integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Coroutine, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aiomeshtastic.errors import MeshtasticNeighborInfoError
from .const import (
    ATTR_CHANNEL,
    ATTR_GATEWAY_ID,
    ATTR_MESSAGE,
    ATTR_MESSAGE_TYPE,
    ATTR_PRIORITY,
    ATTR_TARGET_NODE,
    CONF_GATEWAYS,
    CONF_HISTORY_DAYS,
    CONF_MAINTENANCE_ENABLED,
    CONF_MAINTENANCE_GATEWAY_ID,
    CONF_MAINTENANCE_INTERVAL,
    CONF_MAINTENANCE_MAX_REQUESTS,
    CONF_MAINTENANCE_QUIET_TIME,
    CONF_NODE_TIMEOUT,
    CONF_SCAN_INTERVAL,
    DEFAULT_DATABASE_NAME,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MAINTENANCE_ENABLED,
    DEFAULT_MAINTENANCE_INTERVAL,
    DEFAULT_MAINTENANCE_MAX_REQUESTS,
    DEFAULT_MAINTENANCE_QUIET_TIME,
    DEFAULT_NODE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_GATEWAY_STATUS,
    EVENT_MESSAGE_RECEIVED,
    EVENT_MESSAGE_SENT,
    EVENT_MESSAGE_STATUS,
    EVENT_PACKET,
    MAINTENANCE_MAX_INTERVAL_SECONDS,
    MAINTENANCE_MAX_QUIET_SECONDS,
    MAINTENANCE_MAX_REQUESTS,
    MAINTENANCE_MIN_INTERVAL_SECONDS,
    MAINTENANCE_MIN_QUIET_SECONDS,
    MAINTENANCE_MIN_REQUESTS,
    MAINTENANCE_REQUEST_SPACING_SECONDS,
    MAINTENANCE_SCHEDULER_TICK_SECONDS,
    MANUAL_TRACEROUTE_COOLDOWN_SECONDS,
    MAX_PANEL_NODES,
    MESSAGE_TYPE_BROADCAST,
    MESSAGE_TYPE_DIRECT,
    MESSAGE_TYPE_EMERGENCY,
    MESSAGE_TYPE_GROUP,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_REST,
)
from .dedupe import PacketDeduplicator
from .diagnostic_safety import safe_node_metadata
from .gateway import MeshGateway
from .gateway_settings import GatewaySettingsManager
from .maintenance_scan import MaintenanceScanConfig, MaintenanceScanScheduler
from .meshcore_client import MeshCoreClient
from .meshtastic_client import MeshtasticClient
from .models import (
    GatewayConfig,
    GatewayStatus,
    MeshPacket,
    MeshSnapshot,
    MessageRecord,
    NodeState,
    has_valid_location,
    stable_json,
    timestamp_to_json,
    utcnow,
)
from .node_identity import (
    canonical_meshtastic_mac,
    canonical_meshtastic_node_id,
    meshtastic_identity_is_valid,
    meshtastic_unsafe_identity_keys,
    project_effective_nodes,
)
from .panel_telemetry import PanelTelemetry, safe_error_type
from .rate_limiter import TokenBucket
from .remote_admin import RemoteAdminManager
from .store import MeshStore

_LOGGER = logging.getLogger(__name__)

_RECONNECT_INITIAL_DELAY = 30.0
_RECONNECT_MAX_DELAY = 300.0
_RECONNECT_JITTER_RATIO = 0.2
_GATEWAY_TASK_CANCEL_TIMEOUT = 2.0
_CONNECTION_UPDATE_ACK_TIMEOUT = 5.0
_DIAGNOSTIC_STORE_TIMEOUT = 2.0
_AMBIGUOUS_MESSAGE_ERROR = (
    "The selected Meshtastic identity is ambiguous; direct messaging is disabled until its node records agree"
)
_INVALID_MESSAGE_IDENTITY_ERROR = (
    "The selected Meshtastic identity is malformed or internally inconsistent; direct messaging is disabled"
)
_UNSAFE_MESSAGE_IDENTITY_ERROR_CODE = "unsafe_identity"
_INVALID_MESSAGE_ERROR_CODE = "invalid_message"
_UNSAFE_MESSAGE_ROUTE_ERROR_CODE = "unsafe_route"
_INVALID_MESSAGE_TYPE_ERROR = "Unsupported mesh message type"
_INVALID_MESSAGE_TARGET_ERROR = (
    "Direct messages require exactly one target; broadcast, group, and emergency messages must not specify a target"
)
_MESSAGE_PROTOCOL_MISMATCH_ERROR = "The selected target and gateway use different mesh protocols"
_UNKNOWN_MESSAGE_TARGET_ERROR = "The selected mesh target is not a known node"
_STALE_MESSAGE_TARGET_ERROR = "The selected mesh identity changed before delivery; the message was blocked"
_SUPPORTED_MESSAGE_TYPES = frozenset(
    {
        MESSAGE_TYPE_BROADCAST,
        MESSAGE_TYPE_DIRECT,
        MESSAGE_TYPE_EMERGENCY,
        MESSAGE_TYPE_GROUP,
    }
)
_PUBLIC_MESSAGE_STATUSES = frozenset({"blocked", "queued", "sent"})
_PUBLIC_MESSAGE_ERROR_CODES = frozenset(
    {
        _INVALID_MESSAGE_ERROR_CODE,
        _UNSAFE_MESSAGE_IDENTITY_ERROR_CODE,
        _UNSAFE_MESSAGE_ROUTE_ERROR_CODE,
        "send_failed",
    }
)
_MESHTASTIC_MESSAGE_REFERENCE_RE = re.compile(
    r"^meshtastic:([1-9][0-9]{0,9})$"
)
_PRIVATE_MESHTASTIC_PORTS = frozenset(
    {
        "ADMIN_APP",
        "CONFIG_APP",
        "CHANNEL_CONFIG",
        "SECURITY_CONFIG",
        "SESSION",
    }
)
_GATEWAY_ISSUE_CATEGORIES = (
    "gateway_start",
    "send_failed",
    "unsupported_protocol",
)
_SAFE_GATEWAY_ISSUE_RE = re.compile(
    r"^(?:gateway_start|send_failed|unsupported_protocol)_gateway_"
    r"(?:[0-9]{3}|unknown)$"
)

LOCATION_PROVENANCE_WARNING = (
    "MeshNet does not currently retain an independent location observation "
    "timestamp or source; node last-heard age is only a freshness proxy and "
    "a cached location may be older."
)

_AGE_BUCKETS = ("<15m", "15m-1h", "1-6h", "6-24h", ">=1d", "unknown")
_HOP_BUCKETS = ("0", "1", "2", "3", "4-7", ">=8", "unknown")


def _same_utc_hour(value: str, bucket: datetime) -> bool:
    """Return whether one bounded ISO timestamp belongs to a UTC hour bucket."""
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.astimezone(UTC).replace(
        minute=0,
        second=0,
        microsecond=0,
    ) == bucket


def _bounded_utc_timestamp(value: Any) -> datetime | None:
    """Parse one bounded aware timestamp without accepting provider text."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _maintenance_option_int(
    options: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> tuple[int, bool]:
    """Return one bounded option and whether the stored value was valid."""
    value = options.get(key, default)
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    ):
        return value, True
    return default, False


_TRACEROUTE_PUBLIC_MESSAGES = {
    "traceroute_unavailable": "Manual traceroute is temporarily unavailable",
    "traceroute_gateway_not_found": "The selected traceroute gateway is unavailable",
    "traceroute_requires_bluetooth": "Traceroute requires a connected Meshtastic Bluetooth gateway",
    "traceroute_gateway_disconnected": "The selected traceroute gateway is disconnected",
    "traceroute_target_invalid": "Select one exact Meshtastic node",
    "traceroute_target_unknown": "The selected traceroute target is unavailable in the active radio session",
    "traceroute_target_self": "A gateway cannot traceroute itself",
    "traceroute_cooldown": "The integration-wide traceroute cooldown is active",
    "traceroute_timeout": "The traceroute timed out and was not retried",
    "traceroute_invalid_response": "The traceroute response failed validation",
    "traceroute_lifecycle_changed": "Manual traceroute result was discarded because the integration lifecycle changed",
    "traceroute_failed": "The traceroute could not be completed",
    "traceroute_preflight_failed": "MeshNet could not validate traceroute availability",
}

_NEIGHBOR_INFO_PUBLIC_MESSAGES = {
    "neighbor_info_unavailable": "Manual NeighborInfo is temporarily unavailable",
    "neighbor_info_gateway_not_found": "The selected NeighborInfo gateway is unavailable",
    "neighbor_info_requires_bluetooth": "NeighborInfo requires a connected Meshtastic Bluetooth gateway",
    "neighbor_info_gateway_disconnected": "The selected NeighborInfo gateway is disconnected",
    "neighbor_info_target_invalid": "Select one exact Meshtastic node",
    "neighbor_info_target_unknown": "The selected NeighborInfo target is unavailable in the active radio session",
    "neighbor_info_target_self": "A gateway cannot request its own NeighborInfo",
    "neighbor_info_cooldown": "The NeighborInfo cooldown is active",
    "neighbor_info_preflight_failed": "MeshNet could not validate NeighborInfo availability",
    "neighbor_info_unsupported": "The target does not support remote NeighborInfo or its NeighborInfo module is disabled",
    "neighbor_info_rejected": "The NeighborInfo request was rejected by the mesh and was not retried",
    "neighbor_info_timeout": "The NeighborInfo request timed out and was not retried",
    "neighbor_info_disconnected": "Bluetooth disconnected while waiting for NeighborInfo; the request was not retried",
    "neighbor_info_send_failed": "NeighborInfo transmission could not be confirmed and was not retried",
    "neighbor_info_invalid_response": "The NeighborInfo response failed validation",
    "neighbor_info_lifecycle_changed": "The NeighborInfo result was discarded because the integration lifecycle changed",
    "neighbor_info_failed": "The NeighborInfo request could not be completed",
}


class TracerouteError(HomeAssistantError):
    """A privacy-safe traceroute failure with an explicit RF safety phase."""

    def __init__(
        self,
        code: str,
        *,
        rf_may_have_been_sent: bool = False,
        telemetry_category: str = "validation",
    ) -> None:
        if code not in _TRACEROUTE_PUBLIC_MESSAGES:
            code = "traceroute_failed"
        self.code = code
        self.public_message = _TRACEROUTE_PUBLIC_MESSAGES[code]
        self.rf_may_have_been_sent = rf_may_have_been_sent
        self.telemetry_category = telemetry_category
        super().__init__(self.public_message)


class NeighborInfoError(HomeAssistantError):
    """A privacy-safe NeighborInfo failure with an explicit RF safety phase."""

    def __init__(
        self,
        code: str,
        *,
        rf_may_have_been_sent: bool = False,
        telemetry_category: str = "validation",
    ) -> None:
        if code not in _NEIGHBOR_INFO_PUBLIC_MESSAGES:
            code = "neighbor_info_failed"
        self.code = code
        self.public_message = _NEIGHBOR_INFO_PUBLIC_MESSAGES[code]
        self.rf_may_have_been_sent = rf_may_have_been_sent
        self.telemetry_category = telemetry_category
        super().__init__(self.public_message)


@dataclass(frozen=True, slots=True)
class _ResolvedMessageTarget:
    """A provider destination bound to the protocol that identified it."""

    value: str | None
    protocol: str | None
    binding: str | None


@dataclass(frozen=True, slots=True)
class _ValidatedMessageEnvelope:
    """Normalized user-controlled routing and content fields."""

    target_node: str | None
    message: str
    channel: str | None
    priority: str
    message_type: str
    gateway_id: str | None


def _known_protocol(value: Any) -> str | None:
    """Return a supported protocol name using legacy-safe casing."""
    if not isinstance(value, str):
        return None
    protocol = value.strip().casefold()
    return protocol if protocol in {PROTOCOL_MESHCORE, PROTOCOL_MESHTASTIC} else None


def _canonical_public_key_input(value: Any) -> str | None:
    """Return one strict 32-byte hex public-key input."""
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    compact = text.replace(":", "").replace("-", "")
    if len(compact) != 64 or any(character not in "0123456789abcdef" for character in compact):
        return None
    if text != compact:
        colon = ":".join(compact[index : index + 2] for index in range(0, 64, 2))
        if text not in {colon, colon.replace(":", "-")}:
            return None
    return compact


def _looks_like_bare_identity_input(value: str) -> bool:
    """Return whether malformed bare input still claims identity syntax.

    Valid identity forms are resolved before this check.  This recognizes the
    corresponding malformed shapes so they cannot be reinterpreted as a node
    name after strict parsing fails.
    """
    # Name matching uses NFKC below, so syntax classification must use the
    # same normalization or compatibility characters could bypass this guard.
    text = unicodedata.normalize("NFKC", value).strip()
    lowered = text.casefold()
    if text.startswith("!") or lowered.startswith("0x"):
        return True
    if text.isdecimal():
        return True

    for separator in (":", "-"):
        parts = text.split(separator)
        if len(parts) in {6, 32} and all(len(part) == 2 for part in parts):
            return True

    compact = text.replace(":", "").replace("-", "")
    if len(compact) in {12, 64} and compact.isascii() and compact.isalnum():
        hex_characters = sum(character.casefold() in "0123456789abcdef" for character in compact)
        return hex_characters >= (len(compact) * 3) // 4
    return False


def validated_message_envelope(
    *,
    target_node: Any,
    message: Any,
    channel: Any,
    priority: Any,
    message_type: Any,
    gateway_id: Any,
) -> _ValidatedMessageEnvelope:
    """Validate and normalize message routing fields before persistence."""
    if not isinstance(message_type, str):
        raise HomeAssistantError(_INVALID_MESSAGE_TYPE_ERROR)
    normalized_type = message_type
    if normalized_type not in _SUPPORTED_MESSAGE_TYPES:
        raise HomeAssistantError(_INVALID_MESSAGE_TYPE_ERROR)

    if target_node is None:
        normalized_target = None
    elif isinstance(target_node, str):
        normalized_target = target_node.strip()
        if not normalized_target or len(normalized_target) > 128:
            raise HomeAssistantError(_INVALID_MESSAGE_TARGET_ERROR)
    else:
        raise HomeAssistantError(_INVALID_MESSAGE_TARGET_ERROR)

    if (normalized_type == MESSAGE_TYPE_DIRECT) != (normalized_target is not None):
        raise HomeAssistantError(_INVALID_MESSAGE_TARGET_ERROR)

    if not isinstance(message, str) or not message.strip():
        raise HomeAssistantError("Mesh messages must not be empty")
    try:
        message_bytes = message.encode("utf-8")
    except UnicodeEncodeError as err:
        raise HomeAssistantError("Mesh messages must contain valid UTF-8") from err
    if not 1 <= len(message_bytes) <= 237:
        raise HomeAssistantError("Mesh messages must be 1 to 237 UTF-8 bytes")

    if channel is None:
        normalized_channel = None
    else:
        if isinstance(channel, bool):
            raise HomeAssistantError("Mesh channel must be an integer from 0 to 7")
        if isinstance(channel, int):
            channel_number = channel
        elif isinstance(channel, float) and channel.is_integer():
            channel_number = int(channel)
        elif isinstance(channel, str) and channel in {
            "0",
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
        }:
            channel_number = int(channel, 10)
        else:
            raise HomeAssistantError("Mesh channel must be an integer from 0 to 7")
        if not 0 <= channel_number <= 7:
            raise HomeAssistantError("Mesh channel must be an integer from 0 to 7")
        normalized_channel = str(channel_number)

    if not isinstance(priority, str) or priority not in {
        "normal",
        "high",
        "emergency",
    }:
        raise HomeAssistantError("Unsupported mesh message priority")

    if gateway_id is None:
        normalized_gateway_id = None
    elif isinstance(gateway_id, str) and gateway_id == gateway_id.strip() and 1 <= len(gateway_id) <= 128:
        normalized_gateway_id = gateway_id
    else:
        raise HomeAssistantError("Invalid MeshNet gateway ID")

    return _ValidatedMessageEnvelope(
        target_node=normalized_target,
        message=message,
        channel=normalized_channel,
        priority=priority,
        message_type=normalized_type,
        gateway_id=normalized_gateway_id,
    )


def _diagnostic_task_state(task: asyncio.Future[Any] | None) -> str:
    """Return task state without evaluating or exposing an exception."""
    if task is None:
        return "not_created"
    if task.cancelled():
        return "cancelled"
    if task.done():
        return "finished"
    if isinstance(task, asyncio.Task) and task.cancelling():
        return "cancelling"
    return "pending"


def _diagnostic_error_category(error: str) -> str:
    """Classify an error without returning endpoint or credential-bearing text."""
    lowered = error.casefold()
    categories = (
        ("authentication", ("auth", "credential", "password", "pin", "token")),
        ("bluetooth", ("bluetooth", "bluez", "ble", "dbus")),
        ("configuration", ("config", "invalid", "missing", "required", "unsupported")),
        ("connection", ("connect", "disconnect", "socket", "network", "unreachable")),
        ("data", ("decode", "json", "parse", "payload", "protobuf")),
        ("permission", ("permission", "access denied", "read-only")),
        ("serial", ("serial", "tty", "baud")),
        ("timeout", ("timeout", "timed out")),
    )
    for category, markers in categories:
        if any(marker in lowered for marker in markers):
            return category
    return "other"


def _safe_error_type(error: BaseException) -> str:
    """Return a bounded identifier-like exception class name."""
    name = type(error).__name__
    if not name.isidentifier() or len(name) > 64:
        return "Error"
    return name


def message_api_dict(message: MessageRecord) -> dict[str, Any]:
    """Serialize a message without exposing retained provider metadata."""
    data = message.as_dict()
    data.pop("reply_to_message_id", None)
    data.pop("reaction", None)
    raw: dict[str, str] = {}
    status = message.raw.get("status")
    if isinstance(status, str) and status in _PUBLIC_MESSAGE_STATUSES:
        raw["status"] = status
    error_code = message.raw.get("last_error_code")
    if isinstance(error_code, str) and error_code in _PUBLIC_MESSAGE_ERROR_CODES:
        raw["last_error_code"] = error_code
    data["raw"] = raw
    delivery, peer_node_key = _message_delivery(message)
    channel_index = _message_channel_index(message.channel)
    data["channel"] = (
        str(channel_index)
        if channel_index is not None
        else "0"
        if delivery == "broadcast"
        else None
    )
    data["delivery"] = delivery
    data["peer_node_key"] = peer_node_key
    reply_to_message_id = _validated_message_reference(
        message.reply_to_message_id
    )
    reaction = _validated_message_reaction(message.reaction)
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = reply_to_message_id
        if reaction is not None:
            data["reaction"] = reaction
    mesh_packet_id = _provider_mesh_packet_id(message)
    if mesh_packet_id is not None:
        data["mesh_packet_id"] = mesh_packet_id
    return data


def _validated_message_reference(value: Any) -> str | None:
    """Return one exact stored Meshtastic on-air message reference."""
    if not isinstance(value, str):
        return None
    match = _MESHTASTIC_MESSAGE_REFERENCE_RE.fullmatch(value)
    if match is None:
        return None
    packet_id = int(match.group(1))
    return value if packet_id <= 0xFFFFFFFF else None


def _validated_message_reaction(value: Any) -> str | None:
    """Return one printable Unicode scalar used as a Meshtastic reaction."""
    if not isinstance(value, str) or len(value) != 1:
        return None
    if unicodedata.category(value).startswith("C"):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if 1 <= len(encoded) <= 4 else None


def _provider_mesh_packet_id(message: MessageRecord) -> str | None:
    """Project a sent Meshtastic packet ID without exposing provider metadata."""
    if _known_protocol(message.protocol) != PROTOCOL_MESHTASTIC:
        return None
    value = message.raw.get("provider_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        packet_id = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        packet_id = int(value)
    else:
        return None
    if not 1 <= packet_id <= 0xFFFFFFFF:
        return None
    return f"meshtastic:{packet_id}"


def _message_channel_index(value: Any) -> int | None:
    """Return one exact supported channel index without coercing booleans."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 7 else None
    if isinstance(value, str) and value in {"0", "1", "2", "3", "4", "5", "6", "7"}:
        return int(value)
    return None


def _meshcore_peer_node_key(value: Any) -> str | None:
    """Return one exact MeshCore conversation key without guessing a name."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text.encode("utf-8")) > 256:
        return None
    folded = text.casefold()
    if folded.startswith("pub:"):
        public_key = folded[4:]
        if re.fullmatch(r"[0-9a-f]{2,128}", public_key) is not None:
            return f"pub:{public_key}"
        return None
    if re.fullmatch(r"[0-9a-f]{2,128}", folded) is not None:
        return f"pub:{folded}"
    if folded.startswith("meshcore:"):
        suffix = text[len("meshcore:") :]
        if suffix and all(character.isprintable() for character in suffix):
            return text
    return None


def _message_delivery(message: MessageRecord) -> tuple[str, str | None]:
    """Classify a message only from exact destination/identity evidence."""
    protocol = _known_protocol(message.protocol)
    receiver_text = str(message.receiver or "").strip()
    receiver_folded = receiver_text.casefold()
    receiver_id = canonical_meshtastic_node_id(receiver_text) if protocol == PROTOCOL_MESHTASTIC else None
    if receiver_id is not None:
        peer = message.sender if message.direction == "rx" else message.receiver
        peer_id = canonical_meshtastic_node_id(peer)
        return (
            "direct",
            f"meshtastic:{peer_id}" if peer_id is not None else None,
        )
    if protocol == PROTOCOL_MESHCORE and message.message_type == MESSAGE_TYPE_DIRECT:
        peer = message.sender if message.direction == "rx" else message.receiver
        peer_node_key = _meshcore_peer_node_key(peer)
        if peer_node_key is not None:
            return "direct", peer_node_key
        return "unknown", None
    channel_index = _message_channel_index(message.channel)
    if channel_index is not None and channel_index > 0:
        return "channel", None
    if receiver_folded in {"", "^all", "!ffffffff", "ffffffff"} and channel_index in {None, 0}:
        return "broadcast", None
    return "unknown", None


def message_submission_response(message_id: str, status: str) -> dict[str, Any]:
    """Return the bounded response for a Home Assistant send action."""
    durable_status = status if status in _PUBLIC_MESSAGE_STATUSES else "queued"
    return {
        "schema_version": 1,
        "message_id": str(message_id)[:128],
        "status": durable_status,
    }


def _message_status_event(
    record: MessageRecord,
    status: str,
    *,
    retryable: bool,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Project one message submission outcome without message content."""
    event: dict[str, Any] = {
        "schema_version": 1,
        "message_id": str(record.message_id)[:128],
        "status": status,
        "retryable": retryable,
        "protocol": _known_protocol(record.protocol) or "unknown",
        "gateway_id": str(record.gateway_id)[:128],
        "message_type": (
            record.message_type if record.message_type in _SUPPORTED_MESSAGE_TYPES else MESSAGE_TYPE_BROADCAST
        ),
        "occurred_at": timestamp_to_json(utcnow()),
    }
    if error_code in _PUBLIC_MESSAGE_ERROR_CODES:
        event["error_code"] = error_code
    return event


def _packet_event_dict(packet: MeshPacket) -> dict[str, Any]:
    """Project the deprecated packet event onto strict decoded metadata."""
    event: dict[str, Any] = {
        "schema_version": 1,
        "protocol": _known_protocol(packet.protocol) or "unknown",
        "gateway_id": str(packet.gateway_id)[:128],
        "encrypted": bool(packet.encrypted),
        "timestamp": timestamp_to_json(packet.timestamp),
    }
    fields = {
        "packet_id": packet.packet_id,
        "sender": packet.sender,
        "receiver": packet.receiver,
        "channel": packet.channel,
        "portnum": packet.portnum,
        "rssi": packet.rssi,
        "snr": packet.snr,
        "hops": packet.hops,
        "hop_limit": packet.hop_limit,
    }
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str):
            event[key] = value[:128]
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)) and math.isfinite(float(value)):
            event[key] = value
    return event


def _message_received_event(record: MessageRecord) -> dict[str, Any]:
    """Project one decoded text message for privacy-safe automations."""
    delivery, _peer_node_key = _message_delivery(record)
    event = {
        "schema_version": 1,
        "message_id": str(record.message_id)[:128],
        "protocol": _known_protocol(record.protocol) or "unknown",
        "gateway_id": str(record.gateway_id)[:128],
        "sender": str(record.sender or "")[:128] or None,
        "receiver": str(record.receiver or "")[:128] or None,
        "channel": str(record.channel)[:32] if record.channel is not None else None,
        "text": record.text,
        "delivery": delivery,
        "encrypted": bool(record.encrypted),
        "hops": record.hops,
        "timestamp": timestamp_to_json(record.timestamp),
    }
    reply_to_message_id = _validated_message_reference(
        record.reply_to_message_id
    )
    reaction = _validated_message_reaction(record.reaction)
    if reply_to_message_id is not None:
        event["reply_to_message_id"] = reply_to_message_id
        if reaction is not None:
            event["reaction"] = reaction
    return event


def _safe_inbound_text(value: Any) -> str | None:
    """Return a valid Meshtastic-sized text string or no text."""
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if len(encoded) <= 237 else None


def _packet_port_is_private(packet: MeshPacket) -> bool:
    """Fail closed for admin/config/unknown Meshtastic packet types."""
    if _known_protocol(packet.protocol) != PROTOCOL_MESHTASTIC:
        return False
    portnum = str(packet.portnum or "").strip().upper()
    return portnum in _PRIVATE_MESHTASTIC_PORTS or portnum.startswith("UNKNOWN")


def _packet_is_maintenance_neighbor_info(packet: MeshPacket) -> bool:
    """Identify only a response correlated to MeshNet's own maintenance call."""
    if _known_protocol(packet.protocol) != PROTOCOL_MESHTASTIC:
        return False
    decoded = packet.raw.get("decoded")
    return bool(
        isinstance(decoded, Mapping)
        and decoded.get("neighborInfoProvenance") == "maintenance_scan"
        and str(packet.portnum or "").strip().upper() == "NEIGHBORINFO_APP"
    )


def node_age_bucket(
    last_heard: datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    """Return a privacy-safe age bucket for one cached node timestamp."""
    if not isinstance(last_heard, datetime):
        return "unknown"
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    heard = last_heard.replace(tzinfo=UTC) if last_heard.tzinfo is None else last_heard.astimezone(UTC)
    age_seconds = max(0.0, (current - heard).total_seconds())
    if age_seconds < 15 * 60:
        return "<15m"
    if age_seconds < 60 * 60:
        return "15m-1h"
    if age_seconds < 6 * 60 * 60:
        return "1-6h"
    if age_seconds < 24 * 60 * 60:
        return "6-24h"
    return ">=1d"


def _hop_bucket(value: Any) -> str:
    """Return a bounded hop-count bucket without coercing malformed values."""
    if isinstance(value, bool):
        return "unknown"
    if isinstance(value, float):
        if not value.is_integer():
            return "unknown"
        value = int(value)
    if not isinstance(value, int) or value < 0:
        return "unknown"
    if value <= 3:
        return str(value)
    if value <= 7:
        return "4-7"
    return ">=8"


def _normalized_identity_aliases(node: NodeState) -> set[tuple[str, str]]:
    """Return typed aliases for in-memory collision counting only."""
    aliases: set[tuple[str, str]] = set()

    if node.node_id is not None:
        node_id = str(node.node_id).strip()
        if node_id:
            normalized_node_id = node_id.casefold()
            if node.protocol == PROTOCOL_MESHTASTIC:
                numeric_text = normalized_node_id
                base = 10
                if numeric_text.startswith("!"):
                    numeric_text = numeric_text[1:]
                    base = 16
                elif numeric_text.startswith("0x"):
                    numeric_text = numeric_text[2:]
                    base = 16
                try:
                    normalized_node_id = str(int(numeric_text, base))
                except ValueError:
                    pass
            aliases.add(("node_id", normalized_node_id))

    if node.mac is not None:
        mac = re.sub(r"[^0-9a-z]", "", str(node.mac).casefold())
        if mac:
            aliases.add(("mac", mac))

    if node.public_key is not None:
        public_key = str(node.public_key).strip()
        if public_key:
            # Public keys may use a case-sensitive encoding.
            aliases.add(("public_key", public_key))

    return aliases


def _identity_alias_collision_summary(nodes: list[NodeState]) -> dict[str, int]:
    """Count shared identity aliases without returning alias or node values."""
    aliases: dict[tuple[str, str], set[int]] = {}
    for index, node in enumerate(nodes):
        for alias in _normalized_identity_aliases(node):
            aliases.setdefault(alias, set()).add(index)

    shared_aliases = [indexes for indexes in aliases.values() if len(indexes) > 1]
    parents = list(range(len(nodes)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for indexes in shared_aliases:
        first, *remaining = sorted(indexes)
        for index in remaining:
            union(first, index)

    groups: Counter[int] = Counter()
    collision_nodes = {index for indexes in shared_aliases for index in indexes}
    for index in collision_nodes:
        groups[find(index)] += 1
    return {
        "group_count": sum(size > 1 for size in groups.values()),
        "node_count": sum(size for size in groups.values() if size > 1),
        "shared_alias_count": len(shared_aliases),
    }


def node_observability_aggregate(
    nodes: Iterable[NodeState],
    observed_node_keys: Collection[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return identity-free, side-effect-free node provenance aggregates."""
    node_list = list(nodes)
    observed_keys = set(observed_node_keys)
    current_session_count = sum(node.node_key in observed_keys for node in node_list)
    online_count = sum(bool(node.online) for node in node_list)
    located_flags = [
        has_valid_location(
            node.location,
            zero_pair_is_missing=node.protocol == PROTOCOL_MESHTASTIC,
        )
        for node in node_list
    ]
    via_mqtt_counts = {"true": 0, "false": 0, "unknown": 0}
    hop_counts = dict.fromkeys(_HOP_BUCKETS, 0)
    age_counts = dict.fromkeys(_AGE_BUCKETS, 0)
    for node in node_list:
        via_mqtt = node.connectivity.get("via_mqtt")
        if via_mqtt is True:
            via_mqtt_counts["true"] += 1
        elif via_mqtt is False:
            via_mqtt_counts["false"] += 1
        else:
            via_mqtt_counts["unknown"] += 1
        hop_counts[_hop_bucket(node.connectivity.get("hops"))] += 1
        age_counts[node_age_bucket(node.last_heard, now=now)] += 1

    located_count = sum(located_flags)
    return {
        "node_counts": {
            "total": len(node_list),
            "observed_this_session": current_session_count,
            "cached_only": len(node_list) - current_session_count,
            "online": online_count,
            "offline": len(node_list) - online_count,
            "located": located_count,
            "located_offline": sum(
                located and not node.online for node, located in zip(node_list, located_flags, strict=True)
            ),
        },
        "via_mqtt_counts": via_mqtt_counts,
        "hop_counts": hop_counts,
        "age_counts": age_counts,
        "identity_alias_collisions": _identity_alias_collision_summary(node_list),
        "location_provenance": {
            "independent_timestamp_available": False,
            "source_available": False,
            "freshness_basis": "node_last_heard_proxy",
            "cached_location_may_be_older": True,
            "warning": LOCATION_PROVENANCE_WARNING,
        },
    }


class MeshNetCoordinator(DataUpdateCoordinator[MeshSnapshot]):
    """Single merged coordinator for a MeshNet config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.node_timeout = int(
            entry.options.get(CONF_NODE_TIMEOUT, entry.data.get(CONF_NODE_TIMEOUT, DEFAULT_NODE_TIMEOUT))
        )
        self.history_days = int(
            entry.options.get(CONF_HISTORY_DAYS, entry.data.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
        )
        self.store = MeshStore(Path(hass.config.path(DEFAULT_DATABASE_NAME)), executor=hass.async_add_executor_job)
        self.deduplicator = PacketDeduplicator()
        self.tx_limiter = TokenBucket(rate=0.5, capacity=5)
        self.snapshot = MeshSnapshot()
        self.gateways: dict[str, MeshGateway] = {}
        self._gateway_configs = self._load_gateway_configs(entry)
        self._outbox_lock = asyncio.Lock()
        self._outbox_flush_owner: asyncio.Task[Any] | None = None
        self._gateway_startup_task: asyncio.Task[Any] | None = None
        self._reconnect_tasks: dict[str, asyncio.Task[Any]] = {}
        self._send_tasks: set[asyncio.Task[Any]] = set()
        self._traceroute_tasks: set[asyncio.Task[Any]] = set()
        self._radio_operations_accepting = True
        self._active_send_message_ids: set[str] = set()
        self._gateway_generation = 0
        self._gateway_connected_states: dict[str, bool] = {}
        self._gateway_failure_counts: dict[str, int] = {}
        self._shutting_down = False
        self._reconnect_suspended = False
        self._last_update_attempt_at: datetime | None = None
        self._last_update_success_at: datetime | None = None
        self._last_update_duration_seconds: float | None = None
        self._last_update_error_category: str | None = None
        self._legacy_issue_cleanup_count = 0
        self._raw_nodes: dict[str, NodeState] = {}
        self._node_alias_redirects: dict[str, str] = {}
        self._node_aliases_by_effective: dict[str, tuple[str, ...]] = {}
        empty_projection = project_effective_nodes({})
        self._node_identity_stats = empty_projection.stats
        self._unsafe_meshtastic_node_keys = empty_projection.unsafe_node_keys
        self._effective_observed_node_keys: set[str] = set()
        self._node_update_lock = asyncio.Lock()
        self._session_observed_node_keys: set[str] = set()
        self._topology_history: dict[str, list[dict[str, Any]]] = {}
        self._connection_update_reload_options: dict[str, Any] | None = None
        self._connection_update_reload_waiter: asyncio.Future[bool] | None = None
        self._connection_update_lock = asyncio.Lock()
        self.panel_telemetry = PanelTelemetry(_LOGGER)
        self.gateway_settings = GatewaySettingsManager(self)
        self.remote_admin = RemoteAdminManager(self)
        maintenance_options = dict(entry.options)
        maintenance_enabled = (
            maintenance_options.get(
                CONF_MAINTENANCE_ENABLED,
                DEFAULT_MAINTENANCE_ENABLED,
            )
            is True
        )
        interval, interval_valid = _maintenance_option_int(
            maintenance_options,
            CONF_MAINTENANCE_INTERVAL,
            DEFAULT_MAINTENANCE_INTERVAL,
            MAINTENANCE_MIN_INTERVAL_SECONDS,
            MAINTENANCE_MAX_INTERVAL_SECONDS,
        )
        quiet_time, quiet_valid = _maintenance_option_int(
            maintenance_options,
            CONF_MAINTENANCE_QUIET_TIME,
            DEFAULT_MAINTENANCE_QUIET_TIME,
            MAINTENANCE_MIN_QUIET_SECONDS,
            MAINTENANCE_MAX_QUIET_SECONDS,
        )
        max_requests, max_requests_valid = _maintenance_option_int(
            maintenance_options,
            CONF_MAINTENANCE_MAX_REQUESTS,
            DEFAULT_MAINTENANCE_MAX_REQUESTS,
            MAINTENANCE_MIN_REQUESTS,
            MAINTENANCE_MAX_REQUESTS,
        )
        requested_gateway_id = maintenance_options.get(
            CONF_MAINTENANCE_GATEWAY_ID
        )
        eligible_gateway_ids = {
            config.gateway_id
            for config in self._gateway_configs
            if config.protocol == PROTOCOL_MESHTASTIC
            and config.transport == TRANSPORT_BLUETOOTH
        }
        gateway_valid = (
            isinstance(requested_gateway_id, str)
            and requested_gateway_id in eligible_gateway_ids
        )
        self._maintenance_gateway_id = (
            requested_gateway_id if gateway_valid else None
        )
        self._maintenance_configuration_valid = bool(
            interval_valid
            and quiet_valid
            and max_requests_valid
            and (not maintenance_enabled or gateway_valid)
        )
        self.maintenance = MaintenanceScanScheduler(
            MaintenanceScanConfig(
                enabled=(
                    maintenance_enabled
                    and self._maintenance_configuration_valid
                ),
                interval_seconds=interval,
                quiet_seconds=quiet_time,
                max_requests=max_requests,
                request_spacing_seconds=MAINTENANCE_REQUEST_SPACING_SECONDS,
                tick_seconds=MAINTENANCE_SCHEDULER_TICK_SECONDS,
            ),
            next_candidate=self._async_next_maintenance_candidate,
            request_neighbor_info=self._async_maintenance_neighbor_info,
            is_busy=self._maintenance_foreground_busy,
            task_factory=lambda target, name: self._async_create_background_task(
                target, name
            ),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=30),
            always_update=True,
        )

    def _fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Fire an event when attached to a live Home Assistant bus."""
        bus = getattr(getattr(self, "hass", None), "bus", None)
        fire = getattr(bus, "async_fire", None)
        if callable(fire):
            fire(event_type, data)

    def _record_network_activity(self) -> None:
        """Tell optional maintenance that foreground or received traffic won."""
        maintenance = getattr(self, "maintenance", None)
        record = getattr(maintenance, "record_activity", None)
        if callable(record):
            record()

    def _maintenance_foreground_busy(self) -> bool:
        """Return whether any legitimate or lifecycle radio owner is active."""
        if (
            getattr(self, "_shutting_down", False)
            or getattr(self, "_reconnect_suspended", False)
            or not getattr(self, "_radio_operations_accepting", True)
        ):
            return True

        def task_active(task: Any) -> bool:
            return isinstance(task, asyncio.Task) and not task.done()

        if task_active(getattr(self, "_gateway_startup_task", None)):
            return True
        if task_active(getattr(self, "_outbox_flush_owner", None)):
            return True
        for tasks in (
            getattr(self, "_send_tasks", set()),
            getattr(self, "_traceroute_tasks", set()),
            getattr(self, "_reconnect_tasks", {}).values(),
        ):
            if any(task_active(task) for task in tasks):
                return True
        for manager_name in ("gateway_settings", "remote_admin"):
            manager = getattr(self, manager_name, None)
            active = getattr(manager, "_active_tasks", {})
            tasks = active if not isinstance(active, Mapping) else active.keys()
            if any(task_active(task) for task in tasks):
                return True
        for gateway in getattr(self, "gateways", {}).values():
            if any(
                task_active(task)
                for task in getattr(gateway, "_ble_operation_tasks", set())
            ):
                return True
        return False

    async def _async_next_maintenance_candidate(
        self,
        excluded: frozenset[Any],
    ) -> tuple[str, str] | None:
        """Choose one exact, current-session BLE node using fair rotation."""
        gateway_id = self._maintenance_gateway_id
        if gateway_id is None or self._maintenance_foreground_busy():
            return None
        gateway = self.gateways.get(gateway_id)
        if (
            gateway is None
            or gateway.config.protocol != PROTOCOL_MESHTASTIC
            or gateway.config.transport != TRANSPORT_BLUETOOTH
            or not gateway.status.connected
        ):
            return None
        local_node_id = canonical_meshtastic_node_id(
            getattr(gateway, "local_node_id", None)
        )
        candidates: list[NodeState] = []
        for node in self.snapshot.nodes.values():
            provider_id = canonical_meshtastic_node_id(node.node_id)
            candidate = (gateway_id, node.node_key)
            if (
                candidate in excluded
                or node.protocol != PROTOCOL_MESHTASTIC
                or not node.online
                or node.connectivity.get("via_mqtt") is True
                or node.last_gateway_id != gateway_id
                or gateway_id not in node.gateway_ids
                or not self.node_observed_this_session(node.node_key)
                or node.node_key in self._unsafe_meshtastic_node_keys
                or not meshtastic_identity_is_valid(node.node_key, node)
                or provider_id is None
                or node.node_key != f"meshtastic:{provider_id}"
                or provider_id == local_node_id
            ):
                continue
            candidates.append(node)
        if not candidates:
            return None
        attempts = await self.store.async_neighbor_info_attempt_history(
            limit=1000
        )
        never = datetime.min.replace(tzinfo=UTC)
        candidates.sort(
            key=lambda node: (
                attempts.get(node.node_key) is not None,
                attempts.get(node.node_key, never),
                node.last_heard or never,
                node.node_key,
            )
        )
        return gateway_id, candidates[0].node_key

    async def _async_maintenance_neighbor_info(self, candidate: Any) -> None:
        """Submit one guarded maintenance request through the validated path."""
        if (
            not isinstance(candidate, tuple)
            or len(candidate) != 2
            or not all(isinstance(value, str) for value in candidate)
        ):
            raise NeighborInfoError("neighbor_info_preflight_failed")
        gateway_id, target_node = candidate
        maintenance = self.maintenance
        activity_generation = maintenance.activity_generation

        def still_idle() -> bool:
            return bool(
                maintenance.accepting
                and maintenance.activity_generation == activity_generation
                and not self._maintenance_foreground_busy()
            )

        await self._async_manual_neighbor_info(
            gateway_id=gateway_id,
            target_node=target_node,
            provenance="maintenance_scan",
            pre_submit_guard=still_idle,
        )

    async def _async_setup(self) -> None:
        self._legacy_issue_cleanup_count = self._delete_legacy_gateway_issues()
        await self.store.async_open()
        cached = await self.store.async_load_snapshot(recent_limit=100)
        self._raw_nodes = dict(cached.nodes)
        self._refresh_effective_node_projection()
        self.snapshot.recent_messages = cached.recent_messages
        try:
            self._topology_history = await self.store.async_graph_position_history()
        except Exception as err:
            # Position trails are presentation-only. A corrupt/locked optional
            # history must never prevent Home Assistant or a radio from loading.
            self._topology_history = {}
            _LOGGER.warning(
                "MeshNet could not load the optional graph position history "
                "(error_type=%s)",
                _safe_error_type(err),
            )
        await self._rebuild_gateways()

    def _refresh_effective_node_projection(self, *, changed_raw_key: str | None = None) -> None:
        """Publish a reversible distinct-node view from retained raw records."""
        raw_nodes = getattr(self, "_raw_nodes", self.snapshot.nodes)
        if changed_raw_key is not None:
            effective_key = getattr(self, "_node_alias_redirects", {}).get(changed_raw_key)
            aliases = getattr(self, "_node_aliases_by_effective", {}).get(effective_key or "")
            if effective_key is not None and aliases and all(alias in raw_nodes for alias in aliases):
                subset = project_effective_nodes({alias: raw_nodes[alias] for alias in aliases})
                if set(subset.redirects) == set(aliases) and set(subset.nodes) == {effective_key}:
                    self.snapshot.nodes[effective_key] = subset.nodes[effective_key]
                    self._refresh_effective_observed_node_keys()
                    return

        projection = project_effective_nodes(raw_nodes)
        self.snapshot.nodes = projection.nodes
        self._node_alias_redirects = projection.redirects
        aliases_by_effective: dict[str, list[str]] = {}
        for raw_key, effective_key in projection.redirects.items():
            aliases_by_effective.setdefault(effective_key, []).append(raw_key)
        self._node_aliases_by_effective = {
            effective_key: tuple(sorted(raw_keys)) for effective_key, raw_keys in aliases_by_effective.items()
        }
        self._node_identity_stats = projection.stats
        self._unsafe_meshtastic_node_keys = projection.unsafe_node_keys
        self._refresh_effective_observed_node_keys()

    def _refresh_effective_observed_node_keys(self) -> None:
        """Map live raw observations onto the current distinct-node view."""
        observed = getattr(self, "_session_observed_node_keys", set())
        self._effective_observed_node_keys = {
            getattr(self, "_node_alias_redirects", {}).get(node_key, node_key) for node_key in observed
        }

    async def async_start_gateways(self) -> None:
        """Start radio transports after config-entry setup has completed.

        Meshtastic's synchronous BLE constructor can spend several minutes in
        discovery and configuration waits.  Home Assistant awaits config-entry
        setup before completing a config flow, so radio I/O must run in the
        entry-owned background task created by ``async_setup_entry``.
        """
        if self._shutting_down:
            return
        _LOGGER.debug(
            "Starting %d MeshNet gateway transport(s) in the background",
            len(self.gateways),
        )
        await self._start_gateways()
        if not self._shutting_down:
            await self._flush_outbox()
            self.maintenance.start()
            _LOGGER.debug("MeshNet background gateway startup pass completed")

    def async_start_gateways_background(self) -> None:
        """Start and retain the entry-owned transport startup waiter."""
        if self._shutting_down:
            return
        task = self._gateway_startup_task
        if task is not None and not task.done():
            return
        task = self._async_create_background_task(self.async_start_gateways(), "MeshNet gateway startup")
        self._gateway_startup_task = task

        def clear_startup(done_task: asyncio.Task[Any]) -> None:
            if self._gateway_startup_task is done_task:
                self._gateway_startup_task = None

        task.add_done_callback(clear_startup)

    def _async_create_background_task(self, target: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        """Create long-lived work tied to this config entry when possible."""
        creator = getattr(self.entry, "async_create_background_task", None)
        if callable(creator):
            return creator(self.hass, target, name)
        creator = getattr(self.hass, "async_create_background_task", None)
        if callable(creator):
            return creator(target, name)
        return self.hass.async_create_task(target)

    async def _async_update_data(self) -> MeshSnapshot:
        started = time.monotonic()
        self._last_update_attempt_at = utcnow()
        try:
            await self.store.async_prune(self.history_days)
            self._prune_topology_history_cache()
            self._mark_stale_nodes()
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            self.snapshot.messages_today = await self.store.async_messages_since(midnight)
            self.snapshot.mesh_health_score = self._mesh_health_score()
        except Exception as err:
            self._last_update_duration_seconds = round(time.monotonic() - started, 3)
            error_category = _diagnostic_error_category(str(err))
            self._last_update_error_category = error_category
            raise UpdateFailed(f"MeshNet update failed ({error_category}; {_safe_error_type(err)})") from err
        self._last_update_success_at = utcnow()
        self._last_update_duration_seconds = round(time.monotonic() - started, 3)
        self._last_update_error_category = None
        return self.snapshot

    async def async_shutdown(self) -> None:
        """Stop gateways and close durable storage."""
        self._shutting_down = True
        self._reconnect_suspended = True
        radio_operations_drained = await self.async_quiesce_radio_operations()
        settings_manager = getattr(self, "gateway_settings", None)
        settings_drained = await settings_manager.async_quiesce() if settings_manager is not None else True
        await super().async_shutdown()
        startup_drained = await self._cancel_gateway_startup_task()
        reconnects_drained = await self._cancel_reconnect_tasks()
        outbox_drained = await self._cancel_outbox_flush_owner()
        sends_drained = await self._cancel_send_tasks()
        if not startup_drained:
            _LOGGER.warning(
                "Gateway startup did not stop promptly; continuing bounded shutdown with transport stop fences"
            )
        if not reconnects_drained:
            _LOGGER.warning(
                "Gateway reconnect did not stop promptly; continuing bounded shutdown with transport stop fences"
            )
        if not outbox_drained:
            _LOGGER.warning("Outbox delivery did not stop promptly; continuing bounded shutdown with lifecycle fences")
        if not sends_drained:
            _LOGGER.warning("Message delivery did not stop promptly; continuing bounded shutdown with lifecycle fences")
        if not radio_operations_drained:
            _LOGGER.warning(
                "Remote administration or traceroute work did not stop promptly; "
                "continuing bounded shutdown with lifecycle fences"
            )
        if not settings_drained:
            _LOGGER.warning(
                "Gateway settings work did not stop promptly; transport stop "
                "will rely on its command and lifecycle fences"
            )
        for gateway in list(self.gateways.values()):
            try:
                await gateway.async_stop()
            except Exception as err:
                _LOGGER.debug(
                    "MeshNet gateway adapter %s stop failed (%s; %s)",
                    self._gateway_ordinal(gateway.config.gateway_id),
                    _diagnostic_error_category(str(err)),
                    _safe_error_type(err),
                )
        await self.store.async_close()

    async def async_reload_gateways(self) -> None:
        """Reload gateway configuration from the current config entry."""
        self._reconnect_suspended = True
        radio_operations_drained = await self.async_quiesce_radio_operations()
        settings_manager = getattr(self, "gateway_settings", None)
        settings_drained = await settings_manager.async_quiesce() if settings_manager is not None else True
        reload_completed = False
        try:
            startup_drained = await self._cancel_gateway_startup_task()
            reconnects_drained = await self._cancel_reconnect_tasks()
            outbox_drained = await self._cancel_outbox_flush_owner()
            sends_drained = await self._cancel_send_tasks()
            if (
                not startup_drained
                or not reconnects_drained
                or not outbox_drained
                or not sends_drained
                or not radio_operations_drained
                or not settings_drained
            ):
                _LOGGER.warning("Gateway reload deferred because previous transport work did not stop promptly")
                return
            for gateway in list(self.gateways.values()):
                await gateway.async_stop()
            self._gateway_configs = self._load_gateway_configs(self.entry)
            await self._rebuild_gateways()
            reload_completed = True
        finally:
            self._reconnect_suspended = False
            if settings_manager is not None and not self._shutting_down:
                if not settings_manager.resume():
                    _LOGGER.warning("Gateway settings remain fenced because old work is still stopping")
            if not self._shutting_down:
                if reload_completed:
                    if not self.resume_radio_operations():
                        _LOGGER.warning(
                            "Remote administration and traceroute remain fenced "
                            "because old radio work is still stopping"
                        )
                elif radio_operations_drained:
                    _LOGGER.warning(
                        "Remote administration and traceroute remain fenced until "
                        "the deferred gateway reload succeeds"
                    )
        self.async_start_gateways_background()

    async def async_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None = None,
        priority: str = "normal",
        message_type: str = MESSAGE_TYPE_BROADCAST,
        gateway_id: str | None = None,
    ) -> dict[str, Any]:
        """Send or queue a mesh message."""
        self._record_network_activity()
        task = asyncio.current_task()
        if task is not None:
            self._send_tasks.add(task)
        try:
            return await self._async_send_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
                gateway_id=gateway_id,
            )
        finally:
            if task is not None:
                self._send_tasks.discard(task)

    async def _async_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
        gateway_id: str | None,
    ) -> dict[str, Any]:
        """Send once while fencing storage and events from lifecycle changes."""
        gateway_generation = self._gateway_generation
        if not self._gateway_callback_is_current(gateway_generation):
            raise HomeAssistantError("MeshNet is stopping or reloading")
        envelope = validated_message_envelope(
            target_node=target_node,
            message=message,
            channel=channel,
            priority=priority,
            message_type=message_type,
            gateway_id=gateway_id,
        )
        target_node = envelope.target_node
        message = envelope.message
        channel = envelope.channel
        priority = envelope.priority
        message_type = envelope.message_type
        gateway_id = envelope.gateway_id
        self._validate_requested_gateway_id(gateway_id)
        resolved = self._resolve_message_target(target_node)
        target_node = resolved.value
        gateway = self._select_gateway(
            gateway_id=gateway_id,
            target_node=target_node,
            target_protocol=resolved.protocol,
        )
        target_node = self._validated_gateway_message_target(gateway, target_node)
        message_id = self._message_id(
            target_node=target_node,
            message=message,
            channel=channel,
            gateway_id=gateway.config.gateway_id if gateway else gateway_id,
        )
        active_message_ids = getattr(self, "_active_send_message_ids", None)
        if active_message_ids is None:
            active_message_ids = set()
            self._active_send_message_ids = active_message_ids
        active_message_ids.add(message_id)
        try:
            return await self._async_send_message_record(
                message_id=message_id,
                gateway=gateway,
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
                gateway_id=gateway_id,
                target_protocol=resolved.protocol,
                target_binding=resolved.binding,
                gateway_generation=gateway_generation,
            )
        finally:
            active_message_ids.discard(message_id)

    def _provider_message_target(self, target_node: str | None) -> str | None:
        """Resolve a retained alias to the protocol's stable send identifier."""
        return self._resolve_message_target(target_node).value

    def _resolve_message_target(self, target_node: str | None) -> _ResolvedMessageTarget:
        """Resolve one target without allowing identity syntax to become a name."""
        if target_node is None:
            return _ResolvedMessageTarget(None, None, None)
        requested = target_node.strip()
        # Name comparison is NFKC-normalized below. Parse identity syntax from
        # that same representation so compatibility characters cannot turn a
        # reserved identity prefix into a node-name lookup.
        identity_requested = unicodedata.normalize("NFKC", requested)
        prefix, separator, suffix = identity_requested.partition(":")
        reserved_prefix = prefix.casefold() if separator else None

        if reserved_prefix == PROTOCOL_MESHTASTIC:
            requested_id = canonical_meshtastic_node_id(suffix)
            if requested_id is None:
                raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
            return self._resolved_meshtastic_id(requested_id)

        if reserved_prefix == "mac":
            requested_mac = canonical_meshtastic_mac(suffix)
            if requested_mac is None:
                raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
            return self._resolved_identity_matches(self._message_nodes_matching_mac(requested_mac))

        if reserved_prefix == "pub":
            requested_public_key = _canonical_public_key_input(suffix)
            if requested_public_key is None:
                raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
            return self._resolved_identity_matches(self._message_nodes_matching_public_key(requested_public_key))

        if reserved_prefix == "meshtastic-proof":
            proof = suffix.casefold()
            if len(proof) != 64 or any(character not in "0123456789abcdef" for character in proof):
                raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
            proof_key = f"meshtastic-proof:{proof}"
            redirected_keys = {
                effective_key
                for alias, effective_key in getattr(self, "_node_alias_redirects", {}).items()
                if alias.casefold() == proof_key
            }
            matches = [
                (node_key, candidate)
                for node_key, candidate in self.snapshot.nodes.items()
                if _known_protocol(candidate.protocol) == PROTOCOL_MESHTASTIC
                and (candidate.node_key.casefold() == proof_key or node_key in redirected_keys)
            ]
            return self._resolved_identity_matches(matches)

        requested_meshtastic_id = canonical_meshtastic_node_id(identity_requested)
        if requested_meshtastic_id is not None:
            # ID grammar is authoritative. A numeric-looking cached name can
            # never redirect a direct message to a different node.
            return self._resolved_meshtastic_id(requested_meshtastic_id)

        requested_mac = canonical_meshtastic_mac(identity_requested)
        if requested_mac is not None:
            return self._resolved_identity_matches(self._message_nodes_matching_mac(requested_mac))

        requested_public_key = _canonical_public_key_input(identity_requested)
        if requested_public_key is not None:
            return self._resolved_identity_matches(self._message_nodes_matching_public_key(requested_public_key))

        if _looks_like_bare_identity_input(identity_requested):
            raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)

        effective_key = getattr(self, "_node_alias_redirects", {}).get(requested, requested)
        node = self.snapshot.nodes.get(effective_key)
        if node is None:
            alias_matches = [
                (node_key, candidate)
                for node_key, candidate in self.snapshot.nodes.items()
                if requested
                in {
                    candidate.node_key,
                    candidate.node_id,
                    candidate.public_key,
                    candidate.mac,
                }
            ]
            if len(alias_matches) > 1:
                raise HomeAssistantError(_AMBIGUOUS_MESSAGE_ERROR)
            if alias_matches:
                effective_key, node = alias_matches[0]
        if node is not None:
            return self._resolved_node_target(effective_key, node)

        name_matches: list[tuple[str, NodeState]] = []
        normalized_name = self._normalized_meshtastic_name(requested)
        if normalized_name is not None:
            name_matches = [
                (node_key, candidate)
                for node_key, candidate in self.snapshot.nodes.items()
                if _known_protocol(candidate.protocol) == PROTOCOL_MESHTASTIC
                and normalized_name
                in {
                    self._normalized_meshtastic_name(candidate.short_name),
                    self._normalized_meshtastic_name(candidate.long_name),
                }
            ]
            self._validate_meshtastic_send_candidates([candidate for _, candidate in name_matches])
        if name_matches:
            return self._resolved_node_target(*name_matches[0])
        raise HomeAssistantError(_UNKNOWN_MESSAGE_TARGET_ERROR)

    def _resolved_meshtastic_id(self, canonical_id: str) -> _ResolvedMessageTarget:
        """Validate a canonical ID before returning it as a destination."""
        matches = self._meshtastic_nodes_for_id(canonical_id)
        self._validate_meshtastic_send_candidates(matches)
        if matches:
            return self._resolved_node_target(matches[0].node_key, matches[0])
        return _ResolvedMessageTarget(
            canonical_id,
            PROTOCOL_MESHTASTIC,
            self._canonical_target_binding(canonical_id, PROTOCOL_MESHTASTIC),
        )

    def _resolved_identity_matches(self, matches: list[tuple[str, NodeState]]) -> _ResolvedMessageTarget:
        """Resolve a strong-proof grammar exactly once or fail closed."""
        if not matches:
            raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
        if len(matches) > 1:
            raise HomeAssistantError(_AMBIGUOUS_MESSAGE_ERROR)
        return self._resolved_node_target(*matches[0])

    def _resolved_node_target(self, node_key: str, node: NodeState) -> _ResolvedMessageTarget:
        """Bind a known node's provider target to its normalized protocol."""
        protocol = _known_protocol(node.protocol)
        if protocol == PROTOCOL_MESHTASTIC:
            value = self._meshtastic_send_target(node)
            return _ResolvedMessageTarget(
                value,
                protocol,
                self._node_target_binding(node_key, node, value, protocol),
            )
        if protocol == PROTOCOL_MESHCORE:
            return _ResolvedMessageTarget(
                node_key,
                protocol,
                self._node_target_binding(node_key, node, node_key, protocol),
            )
        raise HomeAssistantError(_MESSAGE_PROTOCOL_MISMATCH_ERROR)

    @staticmethod
    def _canonical_target_binding(value: str, protocol: str) -> str:
        """Bind an intentional canonical target without retaining its value."""
        digest = hashlib.sha256(
            stable_json(
                {
                    "version": 1,
                    "kind": "canonical",
                    "protocol": protocol,
                    "value": value,
                }
            ).encode()
        ).hexdigest()
        return f"canonical:{digest}"

    def _node_target_binding(
        self,
        node_key: str,
        node: NodeState,
        provider_target: str,
        protocol: str,
    ) -> str:
        """Bind a selected cached identity without retaining aliases in raw."""
        digest = hashlib.sha256(
            stable_json(
                {
                    "version": 1,
                    "kind": "node",
                    "protocol": protocol,
                    "provider_target": provider_target,
                    "node_key": node_key,
                    "node_id": (
                        canonical_meshtastic_node_id(node.node_id)
                        if protocol == PROTOCOL_MESHTASTIC
                        else str(node.node_id or "")
                    ),
                    "mac": canonical_meshtastic_mac(node.mac),
                    "public_key": _canonical_public_key_input(node.public_key),
                    "short_name": self._normalized_meshtastic_name(node.short_name),
                    "long_name": self._normalized_meshtastic_name(node.long_name),
                }
            ).encode()
        ).hexdigest()
        return f"node:{digest}"

    def _revalidate_bound_target(
        self,
        target_node: str | None,
        target_protocol: str | None,
        target_binding: Any,
    ) -> _ResolvedMessageTarget:
        """Revalidate the original selection at the provider boundary."""
        protocol = _known_protocol(target_protocol)
        if target_node is None:
            if target_binding is None:
                return _ResolvedMessageTarget(None, protocol, None)
            raise HomeAssistantError(_STALE_MESSAGE_TARGET_ERROR)
        if (
            protocol is None
            or not isinstance(target_binding, str)
            or re.fullmatch(r"(?:canonical|node):[0-9a-f]{64}", target_binding) is None
        ):
            raise HomeAssistantError(_STALE_MESSAGE_TARGET_ERROR)

        if protocol == PROTOCOL_MESHTASTIC:
            canonical_id = canonical_meshtastic_node_id(target_node)
            if canonical_id is None:
                raise HomeAssistantError(_STALE_MESSAGE_TARGET_ERROR)
            if target_binding.startswith("canonical:"):
                self._validate_meshtastic_send_candidates(self._meshtastic_nodes_for_id(canonical_id))
                resolved = _ResolvedMessageTarget(
                    canonical_id,
                    PROTOCOL_MESHTASTIC,
                    self._canonical_target_binding(canonical_id, PROTOCOL_MESHTASTIC),
                )
            else:
                matches = self._meshtastic_nodes_for_id(canonical_id)
                self._validate_meshtastic_send_candidates(matches)
                if not matches:
                    raise HomeAssistantError(_STALE_MESSAGE_TARGET_ERROR)
                resolved = self._resolved_node_target(matches[0].node_key, matches[0])
        else:
            if target_binding.startswith("canonical:"):
                raise HomeAssistantError(_STALE_MESSAGE_TARGET_ERROR)
            resolved = self._resolve_message_target(target_node)

        if resolved.protocol != protocol or resolved.binding != target_binding:
            raise HomeAssistantError(_STALE_MESSAGE_TARGET_ERROR)
        return resolved

    def _message_nodes_matching_mac(self, canonical_mac: str) -> list[tuple[str, NodeState]]:
        """Return every node matching one strict MAC identity."""
        return [
            (node_key, candidate)
            for node_key, candidate in self.snapshot.nodes.items()
            if _known_protocol(candidate.protocol) == PROTOCOL_MESHTASTIC
            and canonical_meshtastic_mac(candidate.mac) == canonical_mac
        ]

    def _message_nodes_matching_public_key(self, canonical_public_key: str) -> list[tuple[str, NodeState]]:
        """Return every node matching one strict public-key identity."""
        return [
            (node_key, candidate)
            for node_key, candidate in self.snapshot.nodes.items()
            if _canonical_public_key_input(candidate.public_key) == canonical_public_key
        ]

    def _meshtastic_send_target(self, node: NodeState) -> str:
        """Return a canonical destination only after all known proof validates."""
        if not meshtastic_identity_is_valid(node.node_key, node):
            raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
        canonical_id = canonical_meshtastic_node_id(node.node_id)
        if canonical_id is None:
            raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
        self._validate_meshtastic_send_candidates(self._meshtastic_nodes_for_id(canonical_id))
        return canonical_id

    @staticmethod
    def _validated_gateway_message_target(
        gateway: MeshGateway | None,
        target_node: str | None,
    ) -> str | None:
        """Keep provider-specific fallback resolution outside the send boundary."""
        if (
            gateway is not None
            and _known_protocol(gateway.config.protocol) == PROTOCOL_MESHTASTIC
            and target_node is not None
        ):
            canonical_id = canonical_meshtastic_node_id(target_node)
            if canonical_id is None:
                raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
            return canonical_id
        return target_node

    @staticmethod
    def _normalized_meshtastic_name(value: Any) -> str | None:
        """Match exact cached names with the provider's Unicode behavior."""
        if not isinstance(value, str):
            return None
        normalized = unicodedata.normalize("NFKC", value).strip().casefold()
        return normalized or None

    def _meshtastic_nodes_for_id(self, canonical_id: str) -> list[NodeState]:
        """Return current Meshtastic nodes for one canonical routing ID."""
        return [
            candidate
            for candidate in self.snapshot.nodes.values()
            if _known_protocol(candidate.protocol) == PROTOCOL_MESHTASTIC
            and canonical_meshtastic_node_id(candidate.node_id) == canonical_id
        ]

    def _validate_meshtastic_send_candidates(
        self,
        candidates: list[NodeState],
    ) -> None:
        """Reject ambiguous or malformed known Meshtastic destinations."""
        if len(candidates) > 1:
            raise HomeAssistantError(_AMBIGUOUS_MESSAGE_ERROR)
        if candidates and not meshtastic_identity_is_valid(candidates[0].node_key, candidates[0]):
            raise HomeAssistantError(_INVALID_MESSAGE_IDENTITY_ERROR)
        if candidates and candidates[0].node_key in meshtastic_unsafe_identity_keys(self.snapshot.nodes):
            raise HomeAssistantError(_AMBIGUOUS_MESSAGE_ERROR)

    @staticmethod
    def _block_message(record: MessageRecord, error_code: str) -> None:
        """Make a malformed or unsafe queue item permanently non-retryable."""
        record.raw["status"] = "blocked"
        record.raw["last_error_code"] = error_code
        record.raw.pop("last_error", None)

    @classmethod
    def _block_unsafe_message(cls, record: MessageRecord) -> None:
        """Make an unsafe queued identity permanently non-retryable."""
        cls._block_message(record, _UNSAFE_MESSAGE_IDENTITY_ERROR_CODE)

    @staticmethod
    def _enforce_target_protocol(
        expected: str | None,
        actual: str | None,
    ) -> str | None:
        """Return the known protocol unless two routing facts conflict."""
        normalized_expected = _known_protocol(expected)
        normalized_actual = _known_protocol(actual)
        if (
            normalized_expected is not None
            and normalized_actual is not None
            and normalized_expected != normalized_actual
        ):
            raise HomeAssistantError(_MESSAGE_PROTOCOL_MISMATCH_ERROR)
        return normalized_expected or normalized_actual

    async def _async_send_message_record(
        self,
        *,
        message_id: str,
        gateway: MeshGateway | None,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
        gateway_id: str | None,
        target_protocol: str | None,
        target_binding: str | None,
        gateway_generation: int,
    ) -> dict[str, Any]:
        """Persist and deliver one direct-send record."""
        queued_protocol = target_protocol
        if queued_protocol is None and gateway_id:
            configured_gateway = self.gateways.get(gateway_id)
            if configured_gateway is not None:
                queued_protocol = _known_protocol(configured_gateway.config.protocol)
        raw: dict[str, Any] = {
            "status": "queued",
            "target_node": target_node,
            "gateway_id": gateway_id,
        }
        if target_binding is not None:
            raw["target_binding"] = target_binding
        record = MessageRecord(
            message_id=message_id,
            protocol=(_known_protocol(gateway.config.protocol) if gateway else queued_protocol) or "unknown",
            gateway_id=gateway.config.gateway_id if gateway else gateway_id or "queued",
            sender="homeassistant",
            receiver=target_node,
            channel=channel,
            text=message,
            message_type=message_type,
            priority=priority,
            direction="tx",
            raw=raw,
        )
        await self.store.async_add_message(record)
        if not self._gateway_callback_is_current(gateway_generation):
            return message_submission_response(message_id, "queued")
        if gateway is None:
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            if not self._gateway_callback_is_current(gateway_generation):
                return message_submission_response(message_id, "queued")
            self.async_set_updated_data(self.snapshot)
            self._fire_event(
                EVENT_MESSAGE_STATUS,
                _message_status_event(record, "queued", retryable=True),
            )
            return message_submission_response(message_id, "queued")

        await self.tx_limiter.acquire()
        if not self._gateway_callback_is_current(gateway_generation):
            return message_submission_response(message_id, "queued")
        try:
            # Identity may change while persistence or the limiter yields.
            # Revalidate immediately before entering the provider coroutine.
            resolved = self._revalidate_bound_target(target_node, target_protocol, target_binding)
            target_protocol = self._enforce_target_protocol(target_protocol, resolved.protocol)
            target_protocol = self._enforce_target_protocol(target_protocol, gateway.config.protocol)
            target_node = resolved.value
            target_node = self._validated_gateway_message_target(gateway, target_node)
        except HomeAssistantError:
            self._block_unsafe_message(record)
            await self.store.async_add_message(record)
            self._fire_event(
                EVENT_MESSAGE_STATUS,
                _message_status_event(
                    record,
                    "blocked",
                    retryable=False,
                    error_code=_UNSAFE_MESSAGE_IDENTITY_ERROR_CODE,
                ),
            )
            if self._gateway_callback_is_current(gateway_generation):
                self.snapshot.recent_messages = await self.store.async_recent_messages(100)
                if self._gateway_callback_is_current(gateway_generation):
                    self.async_set_updated_data(self.snapshot)
            raise
        try:
            provider_id = await gateway.async_send_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
            )
        except Exception as err:
            if not self._gateway_callback_is_current(gateway_generation):
                return message_submission_response(message_id, "queued")
            record.raw["status"] = "queued"
            record.raw["last_error_code"] = "send_failed"
            record.raw.pop("last_error", None)
            await self.store.async_add_message(record)
            if not self._gateway_callback_is_current(gateway_generation):
                return message_id
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            if not self._gateway_callback_is_current(gateway_generation):
                return message_id
            self.async_set_updated_data(self.snapshot)
            self._fire_event(
                EVENT_MESSAGE_STATUS,
                _message_status_event(
                    record,
                    "failed",
                    retryable=True,
                    error_code="send_failed",
                ),
            )
            self._create_issue(
                issue_id=self._gateway_issue_id("send_failed", gateway.config.gateway_id),
                message=(
                    "Message queued after gateway adapter "
                    f"{self._gateway_ordinal(gateway.config.gateway_id)} "
                    "send failure "
                    f"({_diagnostic_error_category(str(err))}; "
                    f"{_safe_error_type(err)})."
                ),
            )
            return message_submission_response(message_id, "queued")
        if not self._gateway_callback_is_current(gateway_generation):
            return message_submission_response(message_id, "queued")
        record.raw["status"] = "sent"
        record.raw["provider_id"] = provider_id
        await self.store.async_add_message(record)
        if not self._gateway_callback_is_current(gateway_generation):
            return message_submission_response(message_id, "sent")
        self._fire_event(EVENT_MESSAGE_SENT, message_api_dict(record))
        self._fire_event(
            EVENT_MESSAGE_STATUS,
            _message_status_event(record, "sent", retryable=False),
        )
        self.snapshot.recent_messages = await self.store.async_recent_messages(100)
        if not self._gateway_callback_is_current(gateway_generation):
            return message_id
        self.async_set_updated_data(self.snapshot)
        return message_submission_response(message_id, "sent")

    async def async_gateway_refresh(self, gateway_id: str | None = None) -> None:
        """Refresh one or all gateways."""
        self._record_network_activity()
        gateways: Iterable[MeshGateway]
        if gateway_id:
            gateway = self.gateways.get(gateway_id)
            if gateway is None:
                raise HomeAssistantError(f"Unknown gateway: {gateway_id}")
            gateways = [gateway]
        else:
            gateways = self.gateways.values()
        for gateway in gateways:
            await gateway.async_refresh()

    async def async_manual_traceroute(
        self,
        *,
        gateway_id: str,
        target_node: str,
    ) -> dict[str, Any]:
        """Own one manual traceroute for bounded reload/unload cancellation."""
        self._record_network_activity()
        if (
            not getattr(self, "_radio_operations_accepting", True)
            or getattr(self, "_shutting_down", False)
            or getattr(self, "_reconnect_suspended", False)
        ):
            raise TracerouteError(
                "traceroute_unavailable",
                telemetry_category="lifecycle",
            )
        task = asyncio.current_task()
        tasks = getattr(self, "_traceroute_tasks", None)
        if tasks is None:
            tasks = set()
            self._traceroute_tasks = tasks
        if task is not None:
            tasks.add(task)
        try:
            if (
                not getattr(self, "_radio_operations_accepting", True)
                or getattr(self, "_shutting_down", False)
                or getattr(self, "_reconnect_suspended", False)
            ):
                raise TracerouteError(
                    "traceroute_unavailable",
                    telemetry_category="lifecycle",
                )
            return await self._async_manual_traceroute(
                gateway_id=gateway_id,
                target_node=target_node,
            )
        finally:
            if task is not None:
                tasks.discard(task)

    async def _async_manual_traceroute(
        self,
        *,
        gateway_id: str,
        target_node: str,
    ) -> dict[str, Any]:
        """Run one explicit, cooldown-protected BLE unicast traceroute."""
        if not isinstance(gateway_id, str) or not gateway_id or len(gateway_id) > 128:
            raise TracerouteError("traceroute_gateway_not_found")
        gateway = self.gateways.get(gateway_id)
        if gateway is None:
            raise TracerouteError(
                "traceroute_gateway_not_found",
                telemetry_category="availability",
            )
        if (
            _known_protocol(gateway.config.protocol) != PROTOCOL_MESHTASTIC
            or gateway.config.transport != TRANSPORT_BLUETOOTH
        ):
            raise TracerouteError("traceroute_requires_bluetooth")
        if not gateway.status.connected:
            raise TracerouteError(
                "traceroute_gateway_disconnected",
                telemetry_category="availability",
            )
        if not isinstance(target_node, str) or target_node != target_node.strip() or len(target_node) > 128:
            raise TracerouteError("traceroute_target_invalid")
        node = self.snapshot.nodes.get(target_node)
        if node is None or node.node_key != target_node:
            raise TracerouteError(
                "traceroute_target_unknown",
                telemetry_category="availability",
            )
        if _known_protocol(node.protocol) != PROTOCOL_MESHTASTIC:
            raise TracerouteError("traceroute_target_invalid")
        provider_target = canonical_meshtastic_node_id(node.node_id)
        if provider_target is None or target_node != f"meshtastic:{provider_target}":
            raise TracerouteError("traceroute_target_invalid")
        local_node_id = canonical_meshtastic_node_id(getattr(gateway, "local_node_id", None))
        if local_node_id is not None and provider_target == local_node_id:
            raise TracerouteError("traceroute_target_self")

        observed_checker = getattr(self, "node_observed_this_session", None)
        if (
            hasattr(self, "_session_observed_node_keys")
            and callable(observed_checker)
            and not observed_checker(target_node)
        ):
            raise TracerouteError(
                "traceroute_target_unknown",
                telemetry_category="availability",
            )

        try:
            reservation = await self.store.async_reserve_traceroute(
                gateway_id,
                target_node,
                cooldown_seconds=MANUAL_TRACEROUTE_COOLDOWN_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TracerouteError(
                "traceroute_preflight_failed",
                telemetry_category="availability",
            ) from None
        reserved = (
            reservation
            if isinstance(reservation, bool)
            else isinstance(reservation, Mapping)
            and (reservation.get("reserved") is True or reservation.get("status") == "reserved")
        )
        if not reserved:
            raise TracerouteError("traceroute_cooldown")

        try:
            provider_result = await gateway.async_manual_traceroute(provider_target)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            category = _diagnostic_error_category(str(err))
            code = "traceroute_timeout" if category == "timeout" else "traceroute_failed"
            raise TracerouteError(
                code,
                rf_may_have_been_sent=True,
                telemetry_category="timeout" if category == "timeout" else "connection",
            ) from None

        if (
            not getattr(self, "_radio_operations_accepting", True)
            or getattr(self, "_shutting_down", False)
            or getattr(self, "_reconnect_suspended", False)
        ):
            raise TracerouteError(
                "traceroute_lifecycle_changed",
                rf_may_have_been_sent=True,
                telemetry_category="lifecycle",
            )

        try:
            result = self._validated_manual_traceroute_result(
                provider_result,
                gateway_id=gateway_id,
                target_node=target_node,
                provider_target=provider_target,
                local_node_id=local_node_id,
            )
        except HomeAssistantError:
            raise TracerouteError(
                "traceroute_invalid_response",
                rf_may_have_been_sent=True,
                telemetry_category="data",
            ) from None
        try:
            await self.store.async_store_traceroute_result(
                gateway_id, target_node, result
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TracerouteError(
                "traceroute_failed",
                rf_may_have_been_sent=True,
                telemetry_category="availability",
            ) from None
        status_getter = getattr(self.store, "async_get_traceroute_status", None)
        try:
            status = (
                await status_getter(gateway_id, target_node)
                if callable(status_getter)
                else None
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # The RF result was already validated and durably stored. Failure
            # of this optional convenience read must not turn a successful
            # one-shot request into an ambiguous failure in the panel.
            _LOGGER.debug(
                "Could not attach traceroute cooldown metadata (%s)",
                _safe_error_type(err),
            )
            status = None
        if isinstance(status, Mapping):
            next_allowed_at = status.get("next_allowed_at")
            if isinstance(next_allowed_at, str):
                result["next_allowed_at"] = next_allowed_at
        return result

    @staticmethod
    def _validated_manual_traceroute_result(
        provider_result: Any,
        *,
        gateway_id: str,
        target_node: str,
        provider_target: str,
        local_node_id: str | None,
    ) -> dict[str, Any]:
        """Validate and bound a correlated provider traceroute response."""
        if not isinstance(provider_result, Mapping):
            raise HomeAssistantError("The traceroute response was invalid")
        correlation_id = provider_result.get("correlation_id")
        source = canonical_meshtastic_node_id(provider_result.get("source"))
        destination = canonical_meshtastic_node_id(provider_result.get("destination"))
        if (
            not isinstance(correlation_id, str)
            or not 1 <= len(correlation_id) <= 128
            or destination != provider_target
            or source is None
            or (local_node_id is not None and source != local_node_id)
        ):
            raise HomeAssistantError("The traceroute response was invalid")
        channel = provider_result.get("channel")
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise HomeAssistantError("The traceroute response was invalid")
        if not 0 <= channel <= 7:
            raise HomeAssistantError("The traceroute response was invalid")

        output: dict[str, Any] = {
            "schema_version": 1,
            "gateway_id": gateway_id,
            "correlation_id": correlation_id,
            "source": f"meshtastic:{source}",
            "destination": target_node,
            "channel": channel,
            "status": "complete",
            "completed_at": timestamp_to_json(utcnow()),
        }
        for provider_key, public_key in (
            ("forward_route", "forward_route"),
            ("reverse_route", "reverse_route"),
        ):
            raw_route = provider_result.get(provider_key)
            if raw_route is None:
                continue
            if not isinstance(raw_route, (list, tuple)) or len(raw_route) > 64:
                raise HomeAssistantError("The traceroute response was invalid")
            route: list[str] = []
            for item in raw_route:
                route_node = canonical_meshtastic_node_id(item)
                if route_node is None:
                    raise HomeAssistantError("The traceroute response was invalid")
                route.append(f"meshtastic:{route_node}")
            output[public_key] = route
        for provider_key in ("snr_towards", "snr_back"):
            raw_snr = provider_result.get(provider_key)
            if raw_snr is None:
                continue
            if not isinstance(raw_snr, (list, tuple)) or len(raw_snr) > 64:
                raise HomeAssistantError("The traceroute response was invalid")
            snr_values: list[float] = []
            for item in raw_snr:
                if (
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                    or not -128 <= float(item) <= 128
                ):
                    raise HomeAssistantError("The traceroute response was invalid")
                snr_values.append(float(item))
            output[provider_key] = snr_values
        return output

    async def async_manual_neighbor_info(
        self,
        *,
        gateway_id: str,
        target_node: str,
    ) -> dict[str, Any]:
        """Own one explicit NeighborInfo request for lifecycle cancellation."""
        self._record_network_activity()
        if (
            not getattr(self, "_radio_operations_accepting", True)
            or getattr(self, "_shutting_down", False)
            or getattr(self, "_reconnect_suspended", False)
        ):
            raise NeighborInfoError(
                "neighbor_info_unavailable",
                telemetry_category="lifecycle",
            )
        task = asyncio.current_task()
        tasks = getattr(self, "_traceroute_tasks", None)
        if tasks is None:
            tasks = set()
            self._traceroute_tasks = tasks
        if task is not None:
            tasks.add(task)
        try:
            if (
                not getattr(self, "_radio_operations_accepting", True)
                or getattr(self, "_shutting_down", False)
                or getattr(self, "_reconnect_suspended", False)
            ):
                raise NeighborInfoError(
                    "neighbor_info_unavailable",
                    telemetry_category="lifecycle",
                )
            return await self._async_manual_neighbor_info(
                gateway_id=gateway_id,
                target_node=target_node,
            )
        finally:
            if task is not None:
                tasks.discard(task)

    async def _async_manual_neighbor_info(
        self,
        *,
        gateway_id: str,
        target_node: str,
        provenance: str = "manual_request",
        pre_submit_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Send one cooldown-protected BLE NeighborInfo request."""
        if provenance not in {"manual_request", "maintenance_scan"}:
            raise NeighborInfoError("neighbor_info_preflight_failed")
        if (
            not isinstance(gateway_id, str)
            or not gateway_id
            or len(gateway_id) > 128
        ):
            raise NeighborInfoError("neighbor_info_gateway_not_found")
        gateway = self.gateways.get(gateway_id)
        if gateway is None:
            raise NeighborInfoError(
                "neighbor_info_gateway_not_found",
                telemetry_category="availability",
            )
        if (
            _known_protocol(gateway.config.protocol) != PROTOCOL_MESHTASTIC
            or gateway.config.transport != TRANSPORT_BLUETOOTH
        ):
            raise NeighborInfoError("neighbor_info_requires_bluetooth")
        if not gateway.status.connected:
            raise NeighborInfoError(
                "neighbor_info_gateway_disconnected",
                telemetry_category="availability",
            )
        if (
            not isinstance(target_node, str)
            or target_node != target_node.strip()
            or len(target_node) > 128
        ):
            raise NeighborInfoError("neighbor_info_target_invalid")
        node = self.snapshot.nodes.get(target_node)
        if node is None or node.node_key != target_node:
            raise NeighborInfoError(
                "neighbor_info_target_unknown",
                telemetry_category="availability",
            )
        if _known_protocol(node.protocol) != PROTOCOL_MESHTASTIC:
            raise NeighborInfoError("neighbor_info_target_invalid")
        provider_target = canonical_meshtastic_node_id(node.node_id)
        if provider_target is None or target_node != f"meshtastic:{provider_target}":
            raise NeighborInfoError("neighbor_info_target_invalid")
        local_node_id = canonical_meshtastic_node_id(
            getattr(gateway, "local_node_id", None)
        )
        if local_node_id is not None and provider_target == local_node_id:
            raise NeighborInfoError("neighbor_info_target_self")

        observed_checker = getattr(self, "node_observed_this_session", None)
        if (
            hasattr(self, "_session_observed_node_keys")
            and callable(observed_checker)
            and not observed_checker(target_node)
        ):
            raise NeighborInfoError(
                "neighbor_info_target_unknown",
                telemetry_category="availability",
            )

        if pre_submit_guard is not None and not pre_submit_guard():
            raise NeighborInfoError(
                "neighbor_info_unavailable",
                telemetry_category="lifecycle",
            )

        try:
            reservation = await self.store.async_reserve_neighbor_info_request(
                gateway_id, target_node
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise NeighborInfoError(
                "neighbor_info_preflight_failed",
                telemetry_category="availability",
            ) from None
        reserved = (
            reservation
            if isinstance(reservation, bool)
            else isinstance(reservation, Mapping)
            and (
                reservation.get("reserved") is True
                or reservation.get("status") == "reserved"
            )
        )
        if not reserved:
            raise NeighborInfoError("neighbor_info_cooldown")
        reserved_next_allowed_at = (
            reservation.get("next_allowed_at")
            if isinstance(reservation, Mapping)
            and isinstance(reservation.get("next_allowed_at"), str)
            else None
        )
        if pre_submit_guard is not None and not pre_submit_guard():
            # The durable reservation is intentionally consumed. A foreground
            # action that raced this maintenance attempt wins without an RF
            # retry or catch-up burst.
            raise NeighborInfoError(
                "neighbor_info_unavailable",
                telemetry_category="lifecycle",
            )
        try:
            if provenance == "manual_request":
                provider_result = await gateway.async_manual_neighbor_info(
                    provider_target
                )
            else:
                provider_result = await gateway.async_manual_neighbor_info(
                    provider_target,
                    provenance=provenance,
                    pre_submit_guard=pre_submit_guard,
                )
        except asyncio.CancelledError:
            raise
        except MeshtasticNeighborInfoError as err:
            category = {
                "neighbor_info_lifecycle_changed": "lifecycle",
                "neighbor_info_timeout": "timeout",
                "neighbor_info_disconnected": "connection",
                "neighbor_info_send_failed": "connection",
                "neighbor_info_rejected": "availability",
                "neighbor_info_unsupported": "availability",
            }.get(err.code, "internal")
            raise NeighborInfoError(
                err.code,
                rf_may_have_been_sent=(
                    err.code != "neighbor_info_lifecycle_changed"
                ),
                telemetry_category=category,
            ) from None
        except TimeoutError:
            raise NeighborInfoError(
                "neighbor_info_timeout",
                rf_may_have_been_sent=True,
                telemetry_category="timeout",
            ) from None
        except Exception:
            raise NeighborInfoError(
                "neighbor_info_failed",
                rf_may_have_been_sent=True,
                telemetry_category="connection",
            ) from None
        if (
            not getattr(self, "_radio_operations_accepting", True)
            or getattr(self, "_shutting_down", False)
            or getattr(self, "_reconnect_suspended", False)
        ):
            raise NeighborInfoError(
                "neighbor_info_lifecycle_changed",
                rf_may_have_been_sent=True,
                telemetry_category="lifecycle",
            )
        try:
            result = self._validated_manual_neighbor_info_result(
                provider_result,
                gateway_id=gateway_id,
                target_node=target_node,
                provider_target=provider_target,
                local_node_id=local_node_id,
            )
        except Exception:
            raise NeighborInfoError(
                "neighbor_info_invalid_response",
                rf_may_have_been_sent=True,
                telemetry_category="data",
            ) from None
        try:
            await self.store.async_store_neighbor_info_result(
                gateway_id, target_node, result
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise NeighborInfoError(
                "neighbor_info_failed",
                rf_may_have_been_sent=True,
                telemetry_category="internal",
            ) from None
        if reserved_next_allowed_at is not None:
            result["next_allowed_at"] = reserved_next_allowed_at
        try:
            status = await self.store.async_get_neighbor_info_request_status(
                target_node
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning(
                "NeighborInfo result stored but cooldown status refresh failed; "
                "returning the validated result (error_type=%s)",
                safe_error_type(type(err).__name__),
            )
            return result
        if isinstance(status, Mapping):
            next_allowed_at = status.get("next_allowed_at")
            if isinstance(next_allowed_at, str):
                result["next_allowed_at"] = next_allowed_at
        return result

    @staticmethod
    def _validated_manual_neighbor_info_result(
        provider_result: Any,
        *,
        gateway_id: str,
        target_node: str,
        provider_target: str,
        local_node_id: str | None,
    ) -> dict[str, Any]:
        """Validate and bound one correlated NeighborInfo response."""
        if not isinstance(provider_result, Mapping):
            raise HomeAssistantError("The NeighborInfo response was invalid")
        correlation_id = provider_result.get("correlation_id")
        source = canonical_meshtastic_node_id(provider_result.get("source"))
        destination = canonical_meshtastic_node_id(
            provider_result.get("destination")
        )
        channel = provider_result.get("channel")
        interval = provider_result.get("node_broadcast_interval_secs")
        if (
            not isinstance(correlation_id, str)
            or not 1 <= len(correlation_id) <= 128
            or source != provider_target
            or destination is None
            or (local_node_id is not None and destination != local_node_id)
            or isinstance(channel, bool)
            or not isinstance(channel, int)
            or not 0 <= channel <= 7
            or isinstance(interval, bool)
            or not isinstance(interval, int)
            or not 0 <= interval <= 31_536_000
        ):
            raise HomeAssistantError("The NeighborInfo response was invalid")
        raw_neighbors = provider_result.get("neighbors")
        if not isinstance(raw_neighbors, (list, tuple)) or len(raw_neighbors) > 10:
            raise HomeAssistantError("The NeighborInfo response was invalid")
        neighbors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_neighbors:
            if not isinstance(item, Mapping) or set(item) - {"node_id", "snr"}:
                raise HomeAssistantError("The NeighborInfo response was invalid")
            neighbor_id = canonical_meshtastic_node_id(item.get("node_id"))
            snr = item.get("snr")
            if (
                neighbor_id is None
                or neighbor_id in {provider_target, local_node_id}
                or neighbor_id in seen
                or isinstance(snr, bool)
                or not isinstance(snr, (int, float))
                or not math.isfinite(float(snr))
                or not -128 <= float(snr) <= 128
            ):
                raise HomeAssistantError("The NeighborInfo response was invalid")
            seen.add(neighbor_id)
            neighbors.append(
                {
                    "node_id": f"meshtastic:{neighbor_id}",
                    "snr": float(snr),
                }
            )
        return {
            "schema_version": 1,
            "gateway_id": gateway_id,
            "correlation_id": correlation_id,
            "source": target_node,
            "destination": f"meshtastic:{destination}",
            "channel": channel,
            "node_broadcast_interval_secs": interval,
            "neighbors": neighbors,
            "status": "complete",
            "completed_at": timestamp_to_json(utcnow()),
        }

    async def async_gateway_settings_get(self, gateway_id: str | None = None) -> dict[str, Any]:
        """Read one local gateway's bounded, privacy-safe settings schema."""
        self._record_network_activity()
        return await self.gateway_settings.async_get(gateway_id)

    async def async_gateway_settings_preview(
        self,
        *,
        gateway_id: str,
        revision: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate changes and retain a single-use in-memory diff."""
        self._record_network_activity()
        return await self.gateway_settings.async_preview(
            gateway_id=gateway_id,
            revision=revision,
            changes=changes,
        )

    async def async_gateway_settings_apply(
        self,
        *,
        gateway_id: str,
        revision: str,
        preview_id: str,
        confirm_critical: bool,
    ) -> dict[str, Any]:
        """Apply one unchanged server preview and verify live readback."""
        self._record_network_activity()
        return await self.gateway_settings.async_apply(
            gateway_id=gateway_id,
            revision=revision,
            preview_id=preview_id,
            confirm_critical=confirm_critical,
        )

    async def async_remote_settings_get(self, *, gateway_id: str, target_node: str) -> dict[str, Any]:
        """Read the reviewed settings projection for one exact remote node."""
        self._record_network_activity()
        return await self.remote_admin.async_get(gateway_id, target_node)

    async def async_remote_settings_preview(
        self,
        *,
        gateway_id: str,
        target_node: str,
        revision: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Retain one short-lived, value-redacted remote write preview."""
        self._record_network_activity()
        return await self.remote_admin.async_preview(
            gateway_id,
            target_node,
            revision,
            changes,
        )

    async def async_remote_settings_apply(
        self,
        *,
        gateway_id: str,
        target_node: str,
        revision: str,
        preview_id: str,
        confirm_remote: bool,
    ) -> dict[str, Any]:
        """Consume and apply one explicitly confirmed remote write preview."""
        self._record_network_activity()
        return await self.remote_admin.async_apply(
            gateway_id,
            target_node,
            revision,
            preview_id,
            confirm_remote=confirm_remote,
        )

    async def async_persist_gateway_connection_updates(
        self,
        gateway_id: str,
        updates: dict[str, str | None],
    ) -> None:
        """Serialize config-entry credential commits through listener ACK."""
        lock = getattr(self, "_connection_update_lock", None)
        if lock is None:
            lock = self._connection_update_lock = asyncio.Lock()
        async with lock:
            await self._async_persist_gateway_connection_updates(gateway_id, updates)

    async def _async_persist_gateway_connection_updates(
        self,
        gateway_id: str,
        updates: dict[str, str | None],
    ) -> None:
        """Persist one verified local connection credential without exposing it.

        Radio adapters may request this only after their own live readback has
        verified the change.  Keeping the config-entry mutation here prevents
        an adapter from writing arbitrary Home Assistant configuration.
        """
        if set(updates) != {"pin"}:
            raise ValueError("Unsupported gateway connection update")
        pin = updates.get("pin")
        if pin is not None and (
            not isinstance(pin, str)
            or len(pin) != 6
            or not pin.isascii()
            or not pin.isdigit()
            or not 100000 <= int(pin) <= 999999
        ):
            raise ValueError("Invalid gateway connection update")

        gateway = self.gateways.get(gateway_id)
        if gateway is None or gateway.config.protocol != PROTOCOL_MESHCORE:
            raise ValueError("Unsupported gateway connection update")

        source_gateways = (
            self.entry.options[CONF_GATEWAYS]
            if CONF_GATEWAYS in self.entry.options
            else self.entry.data.get(CONF_GATEWAYS, [])
        )
        if not isinstance(source_gateways, list):
            raise ValueError("Invalid gateway configuration")
        gateways = deepcopy(source_gateways)
        matches = [item for item in gateways if isinstance(item, dict) and item.get("gateway_id") == gateway_id]
        if len(matches) != 1:
            raise ValueError("Gateway configuration was not found")
        gateway_data = matches[0]
        gateway_options = dict(gateway_data.get("options") or {})
        if pin is None:
            gateway_options.pop("pin", None)
        else:
            gateway_options["pin"] = pin
        if gateway_options:
            gateway_data["options"] = gateway_options
        else:
            gateway_data.pop("options", None)

        entry_options = deepcopy(dict(self.entry.options))
        entry_options[CONF_GATEWAYS] = gateways
        # Home Assistant schedules the update listener from inside
        # async_update_entry. Arm the exact one-shot suppression first so this
        # verified credential persistence cannot tear down its own apply task.
        self._connection_update_reload_options = deepcopy(entry_options)
        waiter = asyncio.get_running_loop().create_future()
        self._connection_update_reload_waiter = waiter
        try:
            changed = self.hass.config_entries.async_update_entry(self.entry, options=entry_options)
        except BaseException:
            self._clear_connection_update_reload(waiter)
            raise
        if changed is False:
            # No listener is scheduled when the entry was already identical.
            self._clear_connection_update_reload(waiter)
        else:
            try:
                acknowledged = await asyncio.wait_for(
                    asyncio.shield(waiter),
                    timeout=_CONNECTION_UPDATE_ACK_TIMEOUT,
                )
            except BaseException:
                self._clear_connection_update_reload(waiter)
                raise
            if acknowledged is not True:
                raise ValueError("Gateway connection update was superseded before acknowledgement")

        # Keep this running session coherent because the matching internal
        # update listener is intentionally suppressed.
        if pin is None:
            gateway.config.options.pop("pin", None)
        else:
            gateway.config.options["pin"] = pin

    def consume_connection_update_reload(self, options: Mapping[str, Any]) -> bool:
        """Consume the next expected internal connection-options update.

        The marker is discarded on every listener invocation, including a
        mismatch. This makes the suppression single-use and ensures an
        unrelated or later options update always follows the normal reload
        path.
        """
        expected = getattr(self, "_connection_update_reload_options", None)
        waiter = getattr(self, "_connection_update_reload_waiter", None)
        self._connection_update_reload_options = None
        self._connection_update_reload_waiter = None
        matched = expected is not None and dict(options) == expected
        if waiter is not None and not waiter.done():
            waiter.set_result(matched)
        return matched

    def _clear_connection_update_reload(self, waiter: asyncio.Future[bool]) -> None:
        """Clear one marker only if it still owns the listener handshake."""
        if getattr(self, "_connection_update_reload_waiter", None) is not waiter:
            return
        self._connection_update_reload_options = None
        self._connection_update_reload_waiter = None
        if not waiter.done():
            waiter.cancel()

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return thorough cached diagnostics without identity or mesh content."""
        gateway_items = sorted(self.gateways.items(), key=lambda item: item[0])
        nodes = list(self.snapshot.nodes.values())
        gateway_diagnostics: list[dict[str, Any]] = []
        for index, (_gateway_id, gateway) in enumerate(gateway_items, start=1):
            status = gateway.status
            error_categories = Counter(_diagnostic_error_category(error) for error in status.errors)
            client_snapshot = getattr(gateway, "diagnostic_snapshot", None)
            try:
                client = client_snapshot() if callable(client_snapshot) else {}
            except Exception as err:
                client = {
                    "available": False,
                    "collection_error": type(err).__name__,
                }
            config = getattr(gateway, "config", None)
            options = getattr(config, "options", {}) or {}
            numeric_options = {
                key: options[key]
                for key in (
                    "baudrate",
                    "message_poll_interval",
                    "scan_interval",
                )
                if isinstance(options.get(key), (int, float))
            }
            gateway_diagnostics.append(
                {
                    "diagnostic_id": f"gateway_{index:03d}",
                    "protocol": status.protocol,
                    "transport": status.transport,
                    "connected": status.connected,
                    "last_connected": timestamp_to_json(status.last_connected),
                    "last_packet": timestamp_to_json(status.last_packet),
                    "packets_received": status.packets_received,
                    "packets_sent": status.packets_sent,
                    "duplicate_packets": status.duplicate_packets,
                    "error_count": len(status.errors),
                    "error_categories": dict(sorted(error_categories.items())),
                    "status_detail_field_count": len(status.detail),
                    "configured": {
                        "host_configured": bool(getattr(config, "host", None)),
                        "port": getattr(config, "port", None),
                        "serial_endpoint_configured": bool(getattr(config, "serial_path", None)),
                        "bluetooth_endpoint_configured": bool(getattr(config, "ble_address", None)),
                        "mqtt_subscription_configured": bool(getattr(config, "mqtt_topic", None)),
                        "rest_endpoint_configured": bool(getattr(config, "api_url", None)),
                        "api_key_configured": bool(getattr(config, "api_key", None)),
                        "publish_endpoint_configured": bool(options.get("publish_topic")),
                        "custom_send_endpoint_configured": bool(options.get("send_url")),
                        "bluetooth_adapter_metadata_configured": bool(
                            options.get("bluetooth_adapter") and options.get("bluetooth_adapter_address")
                        ),
                        "bluetooth_bond_managed": bool(options.get("bluetooth_bond_managed")),
                        "debug": bool(options.get("debug")),
                        **numeric_options,
                    },
                    "client": client,
                }
            )

        reconnect_tasks = getattr(self, "_reconnect_tasks", {})
        send_tasks = getattr(self, "_send_tasks", set())
        reconnect_states = Counter(_diagnostic_task_state(task) for task in reconnect_tasks.values())
        send_states = Counter(_diagnostic_task_state(task) for task in send_tasks)
        update_interval = getattr(self, "update_interval", None)
        last_update_attempt_time = getattr(self, "_last_update_attempt_at", None)
        last_update_success_time = getattr(self, "_last_update_success_at", None)

        try:
            async with asyncio.timeout(_DIAGNOSTIC_STORE_TIMEOUT):
                store_diagnostics = await self.store.async_diagnostics()
        except TimeoutError:
            store_diagnostics = {
                "available": False,
                "collection_error": "timeout",
            }
        except asyncio.CancelledError:
            raise
        except Exception as err:
            store_diagnostics = {
                "available": False,
                "collection_error": type(err).__name__,
            }

        gateway_configs = getattr(self, "_gateway_configs", [])
        protocol_counts = Counter(node.protocol for node in nodes)
        role_counts = Counter(safe_node_metadata(node.role, "role") or "redacted_or_unknown" for node in nodes)
        hardware_counts = Counter(
            safe_node_metadata(node.hardware_model, "hardware_model") or "redacted_or_unknown" for node in nodes
        )
        firmware_counts = Counter(
            safe_node_metadata(node.firmware_version, "firmware_version") or "redacted_or_unknown" for node in nodes
        )
        radio_type_counts = Counter(
            safe_node_metadata(node.radio_type, "radio_type") or "redacted_or_unknown" for node in nodes
        )
        gateway_reachability = Counter(str(len(node.gateway_ids)) for node in nodes)
        last_heard_values = [node.last_heard for node in nodes if node.last_heard]
        settings_manager = getattr(self, "gateway_settings", None)
        settings_diagnostics = (
            settings_manager.diagnostic_snapshot() if settings_manager is not None else {"available": False}
        )
        entry_options = getattr(getattr(self, "entry", None), "options", {})
        if not isinstance(entry_options, Mapping):
            entry_options = {}
        maintenance = getattr(self, "maintenance", None)
        maintenance_snapshot = (
            maintenance.diagnostic_snapshot()
            if callable(getattr(maintenance, "diagnostic_snapshot", None))
            else {"available": False}
        )
        topology_history = getattr(self, "_topology_history", {})
        if not isinstance(topology_history, Mapping):
            topology_history = {}

        return {
            "configuration": {
                "node_timeout": self.node_timeout,
                "history_days": self.history_days,
                "gateway_count": len(self.gateways),
                "maintenance_requested": entry_options.get(
                    CONF_MAINTENANCE_ENABLED,
                    DEFAULT_MAINTENANCE_ENABLED,
                )
                is True,
                "maintenance_configuration_valid": getattr(
                    self,
                    "_maintenance_configuration_valid",
                    False,
                ),
                "maintenance_gateway_configured": getattr(
                    self,
                    "_maintenance_gateway_id",
                    None,
                )
                is not None,
                "protocol_counts": dict(sorted(Counter(config.protocol for config in gateway_configs).items())),
                "transport_counts": dict(sorted(Counter(config.transport for config in gateway_configs).items())),
            },
            "lifecycle": {
                "shutting_down": getattr(self, "_shutting_down", False),
                "reconnect_suspended": getattr(self, "_reconnect_suspended", False),
                "gateway_generation": getattr(self, "_gateway_generation", 0),
                "last_update_success": getattr(self, "last_update_success", None),
                "last_update_attempt_time": timestamp_to_json(last_update_attempt_time),
                "last_update_success_time": timestamp_to_json(last_update_success_time),
                "last_update_duration_seconds": getattr(self, "_last_update_duration_seconds", None),
                "last_update_error_category": getattr(self, "_last_update_error_category", None),
                "update_interval_seconds": (update_interval.total_seconds() if update_interval else None),
                "coordinator_data_available": getattr(self, "data", None) is not None,
            },
            "tasks": {
                "gateway_startup": _diagnostic_task_state(getattr(self, "_gateway_startup_task", None)),
                "reconnect_count": len(reconnect_tasks),
                "reconnect_states": dict(sorted(reconnect_states.items())),
                "outbox_flush": _diagnostic_task_state(getattr(self, "_outbox_flush_owner", None)),
                "outbox_lock_held": bool(getattr(self, "_outbox_lock", None) and self._outbox_lock.locked()),
                "send_count": len(send_tasks),
                "send_states": dict(sorted(send_states.items())),
                "active_send_count": len(getattr(self, "_active_send_message_ids", set())),
                "maintenance": maintenance_snapshot,
            },
            "gateways": gateway_diagnostics,
            "dedupe": self.deduplicator.stats(),
            "rate_limit": self.tx_limiter.snapshot(),
            "snapshot": {
                "node_count": len(nodes),
                "online_node_count": sum(node.online for node in nodes),
                "offline_node_count": sum(not node.online for node in nodes),
                "message_count": len(self.snapshot.recent_messages),
                "messages_today": self.snapshot.messages_today,
                "mesh_health_score": self.snapshot.mesh_health_score,
                "nodes_with_location": sum(
                    has_valid_location(
                        node.location,
                        zero_pair_is_missing=node.protocol == PROTOCOL_MESHTASTIC,
                    )
                    for node in nodes
                ),
                "nodes_with_power": sum(bool(node.power) for node in nodes),
                "nodes_with_radio": sum(bool(node.radio) for node in nodes),
                "nodes_with_routing": sum(bool(node.routing) for node in nodes),
                "nodes_with_sensors": sum(bool(node.sensors) for node in nodes),
                "graph_history_node_count": len(topology_history),
                "graph_history_position_count": sum(
                    len(positions)
                    for positions in topology_history.values()
                    if isinstance(positions, (list, tuple))
                ),
                "protocol_counts": dict(sorted(protocol_counts.items())),
                "role_counts": dict(sorted(role_counts.items())),
                "hardware_model_counts": dict(sorted(hardware_counts.items())),
                "firmware_version_counts": dict(sorted(firmware_counts.items())),
                "radio_type_counts": dict(sorted(radio_type_counts.items())),
                "gateway_reachability_counts": dict(sorted(gateway_reachability.items())),
                "oldest_last_heard": timestamp_to_json(min(last_heard_values) if last_heard_values else None),
                "newest_last_heard": timestamp_to_json(max(last_heard_values) if last_heard_values else None),
            },
            "node_observability": self.node_observability_diagnostics(),
            "panel": self.panel_telemetry.snapshot(),
            "gateway_settings": settings_diagnostics,
            "store": store_diagnostics,
            "repairs": self._repair_diagnostics(),
        }

    def node_observed_this_session(self, node_key: str) -> bool:
        """Return whether a live gateway callback observed this node."""
        if node_key in getattr(self, "_session_observed_node_keys", set()):
            return True
        return node_key in getattr(self, "_effective_observed_node_keys", set())

    def node_alias_keys(self, node_key: str) -> tuple[str, ...]:
        """Return retained raw aliases for one effective node key."""
        aliases = getattr(self, "_node_aliases_by_effective", {}).get(node_key)
        return aliases or (node_key,)

    def node_observability_diagnostics(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return passive node provenance diagnostics without identity values."""
        stored_total = len(self.snapshot.nodes)
        nodes = list(islice(self.snapshot.nodes.values(), MAX_PANEL_NODES))
        result = node_observability_aggregate(
            nodes,
            getattr(
                self,
                "_effective_observed_node_keys",
                getattr(self, "_session_observed_node_keys", set()),
            ),
            now=now,
        )
        analyzed = len(nodes)
        result["node_counts"].update(
            {
                "stored_total": stored_total,
                "analyzed": analyzed,
                "analysis_omitted": stored_total - analyzed,
                "analysis_truncated": stored_total > analyzed,
            }
        )
        identity_stats = dict(getattr(self, "_node_identity_stats", {}))
        if not identity_stats:
            identity_stats = {
                "raw_record_count": stored_total,
                "effective_node_count": stored_total,
                "collapsed_alias_record_count": 0,
                "candidate_identity_group_count": 0,
                "resolved_identity_group_count": 0,
                "unresolved_identity_group_count": result["identity_alias_collisions"]["group_count"],
                "unresolved_identity_record_count": result["identity_alias_collisions"]["node_count"],
                "invalid_identity_record_count": 0,
            }
        result["identity_projection"] = identity_stats
        return result

    def panel_node_provenance(self) -> dict[str, int]:
        """Return the panel's small, identity-free provenance projection."""
        observability = self.node_observability_diagnostics()
        node_counts = observability["node_counts"]
        via_mqtt_counts = observability["via_mqtt_counts"]
        identity_projection = observability["identity_projection"]
        return {
            "total_node_count": node_counts["stored_total"],
            "retained_node_record_count": identity_projection["raw_record_count"],
            "collapsed_alias_record_count": identity_projection["collapsed_alias_record_count"],
            "resolved_identity_group_count": identity_projection["resolved_identity_group_count"],
            "unresolved_identity_group_count": identity_projection["unresolved_identity_group_count"],
            "unresolved_identity_node_count": identity_projection["unresolved_identity_record_count"],
            "invalid_identity_record_count": identity_projection["invalid_identity_record_count"],
            "analyzed_node_count": node_counts["analyzed"],
            "omitted_node_count": node_counts["analysis_omitted"],
            "current_session_node_count": node_counts["observed_this_session"],
            "cached_only_node_count": node_counts["cached_only"],
            "online_node_count": node_counts["online"],
            "located_node_count": node_counts["located"],
            "located_offline_node_count": node_counts["located_offline"],
            "mqtt_node_count": via_mqtt_counts["true"],
            "mqtt_unknown_node_count": via_mqtt_counts["unknown"],
            "identity_collision_group_count": identity_projection["unresolved_identity_group_count"],
            "identity_collision_node_count": identity_projection["unresolved_identity_record_count"],
        }

    def maintenance_panel_status(self) -> dict[str, Any]:
        """Return the scheduler's fixed identity-free status for the admin UI."""
        status = self.maintenance.diagnostic_snapshot()
        return {
            "enabled": status["enabled"],
            "accepting": status["accepting"],
            "task_state": status["task_state"],
            "cycle_active": status["cycle_active"],
            "last_outcome": status["last_outcome"],
            "interval_seconds": status["interval_seconds"],
            "quiet_seconds": status["quiet_seconds"],
            "request_spacing_seconds": status["request_spacing_seconds"],
            "max_requests_per_cycle": status["max_requests_per_cycle"],
            "next_cycle_in_seconds": status["next_cycle_in_seconds"],
            "last_activity_age_seconds": status["last_activity_age_seconds"],
            "request_attempt_count": status["request_attempt_count"],
            "request_success_count": status["request_success_count"],
            "request_failure_count": status["request_failure_count"],
            "traffic_deferral_count": status["traffic_deferral_count"],
            "busy_deferral_count": status["busy_deferral_count"],
            "configuration_valid": self._maintenance_configuration_valid,
            "gateway_configured": self._maintenance_gateway_id is not None,
            "automatic_traceroute_supported": False,
            "automatic_retry_supported": False,
        }

    async def _handle_packet(self, packet: MeshPacket, *, gateway_generation: int | None = None) -> None:
        if not self._gateway_callback_is_current(gateway_generation):
            return
        if not _packet_is_maintenance_neighbor_info(packet):
            self._record_network_activity()
        if _packet_port_is_private(packet):
            return
        packet.text = _safe_inbound_text(packet.text)
        gateway = self.gateways.get(packet.gateway_id)
        if self.deduplicator.is_duplicate(packet):
            if gateway:
                gateway.status.duplicate_packets += 1
            return
        await self.store.async_add_packet(packet)
        if not self._gateway_callback_is_current(gateway_generation):
            return
        self._fire_event(EVENT_PACKET, _packet_event_dict(packet))
        if packet.text:
            record = MessageRecord(
                message_id=packet.fingerprint(),
                protocol=packet.protocol,
                gateway_id=packet.gateway_id,
                sender=packet.sender,
                receiver=packet.receiver,
                channel=packet.channel,
                text=packet.text,
                encrypted=packet.encrypted,
                hops=packet.hops,
                timestamp=packet.timestamp,
                message_type=(
                    MESSAGE_TYPE_DIRECT
                    if _known_protocol(packet.protocol) == PROTOCOL_MESHCORE
                    and str(packet.portnum or "").strip().casefold()
                    == "contact_message"
                    else MESSAGE_TYPE_BROADCAST
                ),
                reply_to_message_id=packet.reply_to_message_id,
                reaction=packet.reaction,
                raw={},
            )
            await self.store.async_add_message(record)
            if not self._gateway_callback_is_current(gateway_generation):
                return
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            if not self._gateway_callback_is_current(gateway_generation):
                return
            self._fire_event(EVENT_MESSAGE_RECEIVED, _message_received_event(record))
        self.async_set_updated_data(self.snapshot)

    async def _handle_node(self, node: NodeState, *, gateway_generation: int | None = None) -> None:
        if not self._gateway_callback_is_current(gateway_generation):
            return
        if node.routing.get("neighbors_provenance") != "maintenance_scan":
            self._record_network_activity()
        await self._async_record_graph_position(node)
        if not self._gateway_callback_is_current(gateway_generation):
            return
        node_update_lock = getattr(self, "_node_update_lock", None)
        if node_update_lock is None:
            node_update_lock = self._node_update_lock = asyncio.Lock()
        async with node_update_lock:
            observed_node_keys = getattr(self, "_session_observed_node_keys", None)
            if observed_node_keys is None:
                observed_node_keys = self._session_observed_node_keys = set()
            observed_node_keys.add(node.node_key)
            raw_nodes = getattr(self, "_raw_nodes", None)
            if raw_nodes is None:
                raw_nodes = self._raw_nodes = dict(self.snapshot.nodes)
            existing = raw_nodes.get(node.node_key)
            identity_before = self._node_identity_signature(existing) if existing is not None else None
            if existing:
                existing.merge(node)
                node = existing
            raw_nodes[node.node_key] = node
            identity_unchanged = identity_before is not None and identity_before == self._node_identity_signature(node)
            await self.store.async_upsert_node(node)
            if not self._gateway_callback_is_current(gateway_generation):
                return
            self._refresh_effective_node_projection(changed_raw_key=node.node_key if identity_unchanged else None)
        if not self._gateway_callback_is_current(gateway_generation):
            return
        self.async_set_updated_data(self.snapshot)

    async def _async_record_graph_position(self, node: NodeState) -> None:
        """Best-effort retain one real node observation for a rolling-day trail."""
        if not has_valid_location(
            node.location,
            zero_pair_is_missing=node.protocol == PROTOCOL_MESHTASTIC,
        ):
            return
        latitude = float(node.location["latitude"])
        longitude = float(node.location["longitude"])
        precision = node.location.get("precision_bits")
        if (
            isinstance(precision, bool)
            or not isinstance(precision, int)
            or not 0 <= precision <= 32
        ):
            precision = None
        observed_at = utcnow()
        try:
            await self.store.async_record_graph_position(
                node.node_key,
                latitude,
                longitude,
                observed_at=observed_at,
                precision_bits=precision,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug(
                "Optional MeshNet graph position sample was not stored "
                "(error_type=%s)",
                _safe_error_type(err),
            )
            return
        bucket = observed_at.replace(minute=0, second=0, microsecond=0)
        sample: dict[str, Any] = {
            "observed_at": timestamp_to_json(observed_at),
            "latitude": latitude,
            "longitude": longitude,
        }
        if precision is not None:
            sample["precision_bits"] = precision
        positions = self._topology_history.setdefault(node.node_key, [])
        positions[:] = [
            item
            for item in positions
            if not (
                isinstance(item.get("observed_at"), str)
                and _same_utc_hour(item["observed_at"], bucket)
            )
        ]
        positions.append(sample)
        positions.sort(key=lambda item: str(item.get("observed_at") or ""))
        del positions[:-25]
        if len(self._topology_history) > 100:
            self._prune_topology_history_cache(now=observed_at)

    def _prune_topology_history_cache(
        self,
        *,
        now: datetime | None = None,
    ) -> None:
        """Keep the optional in-memory graph cache to one day and 100 nodes."""
        current = now or utcnow()
        cutoff = current - timedelta(hours=24)
        history = getattr(self, "_topology_history", {})
        if not isinstance(history, dict):
            self._topology_history = {}
            return
        newest: dict[str, datetime] = {}
        for node_key in tuple(history):
            raw_positions = history.get(node_key)
            if not isinstance(raw_positions, list):
                history.pop(node_key, None)
                continue
            retained = [
                item
                for item in raw_positions[-25:]
                if isinstance(item, Mapping)
                and (observed := _bounded_utc_timestamp(item.get("observed_at")))
                is not None
                and observed >= cutoff
            ]
            if not retained:
                history.pop(node_key, None)
                continue
            retained.sort(key=lambda item: str(item.get("observed_at") or ""))
            history[node_key] = retained
            observed = _bounded_utc_timestamp(retained[-1].get("observed_at"))
            if observed is not None:
                newest[node_key] = observed
        if len(history) <= 100:
            return
        keep = {
            node_key
            for node_key, _observed in sorted(
                newest.items(),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )[:100]
        }
        for node_key in tuple(history):
            if node_key not in keep:
                history.pop(node_key, None)

    def topology_history_for_node(self, node_key: str) -> list[dict[str, Any]]:
        """Return at most 25 detached samples across one effective node's aliases."""
        combined: list[dict[str, Any]] = []
        for alias in self.node_alias_keys(node_key):
            for item in self._topology_history.get(alias, ()):  # local cache only
                if isinstance(item, Mapping):
                    combined.append(dict(item))
        combined.sort(key=lambda item: str(item.get("observed_at") or ""))
        return combined[-25:]

    @staticmethod
    def _node_identity_signature(node: NodeState) -> tuple[Any, ...]:
        """Return fields whose change can alter the identity projection."""
        return (
            node.node_key,
            node.protocol,
            node.node_id,
            node.mac,
            node.public_key,
        )

    async def _handle_gateway_status(self, status: GatewayStatus, *, gateway_generation: int | None = None) -> None:
        if not self._gateway_callback_is_current(gateway_generation):
            return
        gateway = self.gateways.get(status.gateway_id)
        if gateway is None or gateway.status is not status:
            return
        connected_states = getattr(self, "_gateway_connected_states", None)
        if connected_states is None:
            connected_states = self._gateway_connected_states = {}
        failure_counts = getattr(self, "_gateway_failure_counts", None)
        if failure_counts is None:
            failure_counts = self._gateway_failure_counts = {}
        previously_observed = status.gateway_id in connected_states
        previous_connected = connected_states.get(status.gateway_id, bool(status.connected))
        previous_failure_count = failure_counts.get(status.gateway_id, max(0, int(status.failure_count)))
        failure_count = max(previous_failure_count, int(status.failure_count))
        status.failure_count = failure_count
        self.snapshot.gateways[status.gateway_id] = status
        if status.connected:
            self._delete_resolved_issue(self._gateway_issue_id("gateway_start", status.gateway_id))
            reconnect_task = self._reconnect_tasks.get(status.gateway_id)
            if reconnect_task and reconnect_task is not asyncio.current_task():
                reconnect_task.cancel()
            await self._flush_outbox(
                gateway_id=status.gateway_id,
                gateway_generation=gateway_generation,
            )
            if not self._gateway_callback_is_current(gateway_generation):
                return
        else:
            self._schedule_reconnect(status.gateway_id)
        connectivity_changed = previously_observed and previous_connected != bool(status.connected)
        failure_changed = previously_observed and failure_count > previous_failure_count
        connected_states[status.gateway_id] = bool(status.connected)
        failure_counts[status.gateway_id] = failure_count
        if connectivity_changed or failure_changed:
            if connectivity_changed:
                transition = "connected" if status.connected else "disconnected"
            else:
                transition = "failure"
            reason = status.last_failure_category
            if reason not in {
                "authentication",
                "bluetooth",
                "configuration",
                "connection",
                "data",
                "permission",
                "serial",
                "timeout",
                "other",
            }:
                reason = None
            event: dict[str, Any] = {
                "schema_version": 1,
                "gateway_id": status.gateway_id[:128],
                "protocol": _known_protocol(status.protocol) or "unknown",
                "transport": str(status.transport)[:32],
                "previous_connected": previous_connected,
                "connected": bool(status.connected),
                "transition": transition,
                "failure_count": failure_count,
                "reconnect_scheduled": not bool(status.connected),
                "occurred_at": timestamp_to_json(utcnow()),
            }
            if reason is not None:
                event["reason_category"] = reason
            self._fire_event(EVENT_GATEWAY_STATUS, event)
        self.async_set_updated_data(self.snapshot)

    async def _rebuild_gateways(self) -> None:
        self._gateway_generation = getattr(self, "_gateway_generation", 0) + 1
        settings_manager = getattr(self, "gateway_settings", None)
        if settings_manager is not None:
            settings_manager.invalidate()
        gateway_generation = self._gateway_generation
        self.gateways = {}
        self._gateway_connected_states = {}
        self._gateway_failure_counts = {}

        async def handle_packet(packet: MeshPacket) -> None:
            await self._handle_packet(packet, gateway_generation=gateway_generation)

        async def handle_node(node: NodeState) -> None:
            await self._handle_node(node, gateway_generation=gateway_generation)

        async def handle_status(status: GatewayStatus) -> None:
            await self._handle_gateway_status(status, gateway_generation=gateway_generation)

        for config in self._gateway_configs:
            if config.protocol == PROTOCOL_MESHTASTIC:
                gateway = MeshtasticClient(
                    self.hass,
                    config,
                    handle_packet,
                    handle_node,
                    handle_status,
                    _LOGGER,
                )
            elif config.protocol == PROTOCOL_MESHCORE:
                gateway = MeshCoreClient(
                    self.hass,
                    config,
                    handle_packet,
                    handle_node,
                    handle_status,
                    _LOGGER,
                )
            else:
                self._create_issue(
                    issue_id=self._gateway_issue_id("unsupported_protocol", config.gateway_id),
                    message=(
                        f"Gateway adapter {self._gateway_ordinal(config.gateway_id)} uses an unsupported protocol."
                    ),
                )
                continue
            self.gateways[config.gateway_id] = gateway
            self.snapshot.gateways[config.gateway_id] = gateway.status
            self._gateway_connected_states[config.gateway_id] = bool(gateway.status.connected)
            self._gateway_failure_counts[config.gateway_id] = max(0, int(gateway.status.failure_count))

    async def _start_gateways(self) -> None:
        if not self.gateways:
            self._create_issue(
                issue_id="no_gateways",
                message="MeshNet has no configured gateways.",
                severity=ir.IssueSeverity.ERROR,
            )
            return
        self._delete_resolved_issue("no_gateways")
        results = await asyncio.gather(
            *(gateway.async_start() for gateway in self.gateways.values()),
            return_exceptions=True,
        )
        for gateway, result in zip(self.gateways.values(), results, strict=False):
            issue_id = self._gateway_issue_id("gateway_start", gateway.config.gateway_id)
            if isinstance(result, BaseException):
                self._create_issue(
                    issue_id=issue_id,
                    message=(
                        "Gateway adapter "
                        f"{self._gateway_ordinal(gateway.config.gateway_id)} "
                        "failed to start "
                        f"({_diagnostic_error_category(str(result))}; "
                        f"{_safe_error_type(result)})."
                    ),
                    severity=ir.IssueSeverity.WARNING,
                )
                # A pre-active Bluetooth failure never emits a connected-to-
                # disconnected transition, so it cannot reach the normal
                # status callback. Reuse the existing single-flight,
                # stop-before-start retry loop after startup cleanup returns.
                self._schedule_reconnect(gateway.config.gateway_id)
            else:
                self._delete_resolved_issue(issue_id)

    async def _flush_outbox(
        self,
        gateway_id: str | None = None,
        *,
        gateway_generation: int | None = None,
    ) -> None:
        """Scan each queued record once without retrying a failed head row."""
        after: tuple[str, str] | None = None
        while True:
            result = await self._flush_outbox_batch(
                gateway_id=gateway_id,
                gateway_generation=gateway_generation,
                after=after,
            )
            if result is None:
                return
            after, page_is_full = result
            if not page_is_full:
                return

    async def _flush_outbox_batch(
        self,
        gateway_id: str | None = None,
        *,
        gateway_generation: int | None = None,
        after: tuple[str, str] | None = None,
    ) -> tuple[tuple[str, str], bool] | None:
        """Flush one bounded batch and report whether another can be safe."""
        if gateway_generation is None:
            gateway_generation = getattr(self, "_gateway_generation", 0)
        if not self._gateway_callback_is_current(gateway_generation):
            return None
        current_task = asyncio.current_task()
        if current_task is not None and current_task is self._outbox_flush_owner:
            return None
        async with self._outbox_lock:
            if not self._gateway_callback_is_current(gateway_generation):
                return None
            self._outbox_flush_owner = current_task
            try:
                pending = await self.store.async_pending_outbox(limit=100, after=after)
                if not self._gateway_callback_is_current(gateway_generation):
                    return
                if not pending:
                    return None
                self._record_network_activity()
                updated_any = False
                for record in pending:
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    if record.message_id in getattr(self, "_active_send_message_ids", set()):
                        continue
                    raw_gateway_id = record.raw.get("gateway_id")
                    persisted_gateway_id = record.gateway_id
                    if raw_gateway_id is not None:
                        desired_gateway = raw_gateway_id
                        gateway_route_is_persisted = True
                    elif persisted_gateway_id not in {"queued", "unknown"}:
                        desired_gateway = persisted_gateway_id
                        gateway_route_is_persisted = True
                    else:
                        desired_gateway = gateway_id
                        gateway_route_is_persisted = False
                    try:
                        envelope = validated_message_envelope(
                            target_node=record.receiver,
                            message=record.text,
                            channel=record.channel,
                            priority=record.priority,
                            message_type=record.message_type,
                            gateway_id=desired_gateway,
                        )
                        self._validate_requested_gateway_id(envelope.gateway_id)
                    except HomeAssistantError:
                        self._block_message(record, _INVALID_MESSAGE_ERROR_CODE)
                        await self.store.async_add_message(record)
                        if not self._gateway_callback_is_current(gateway_generation):
                            return
                        self._fire_event(
                            EVENT_MESSAGE_STATUS,
                            _message_status_event(
                                record,
                                "blocked",
                                retryable=False,
                                error_code=_INVALID_MESSAGE_ERROR_CODE,
                            ),
                        )
                        updated_any = True
                        continue
                    record.receiver = envelope.target_node
                    record.channel = envelope.channel
                    record.priority = envelope.priority
                    record.message_type = envelope.message_type
                    try:
                        persisted_protocol = _known_protocol(record.protocol)
                        if persisted_protocol is None and str(record.protocol).casefold() != "unknown":
                            raise HomeAssistantError(_MESSAGE_PROTOCOL_MISMATCH_ERROR)
                        resolved = self._revalidate_bound_target(
                            envelope.target_node,
                            persisted_protocol,
                            record.raw.get("target_binding"),
                        )
                        target_protocol = self._enforce_target_protocol(persisted_protocol, resolved.protocol)
                        provider_target = resolved.value
                    except HomeAssistantError:
                        self._block_unsafe_message(record)
                        await self.store.async_add_message(record)
                        if not self._gateway_callback_is_current(gateway_generation):
                            return
                        self._fire_event(
                            EVENT_MESSAGE_STATUS,
                            _message_status_event(
                                record,
                                "blocked",
                                retryable=False,
                                error_code=_UNSAFE_MESSAGE_IDENTITY_ERROR_CODE,
                            ),
                        )
                        updated_any = True
                        continue
                    try:
                        gateway = self._select_gateway(
                            gateway_id=envelope.gateway_id,
                            target_node=provider_target,
                            target_protocol=target_protocol,
                        )
                    except HomeAssistantError:
                        if not gateway_route_is_persisted:
                            # A status callback is only a delivery opportunity;
                            # a wrong-protocol gateway must leave an auto-routed
                            # record queued for a compatible radio.
                            continue
                        self._block_message(record, _UNSAFE_MESSAGE_ROUTE_ERROR_CODE)
                        await self.store.async_add_message(record)
                        if not self._gateway_callback_is_current(gateway_generation):
                            return
                        self._fire_event(
                            EVENT_MESSAGE_STATUS,
                            _message_status_event(
                                record,
                                "blocked",
                                retryable=False,
                                error_code=_UNSAFE_MESSAGE_ROUTE_ERROR_CODE,
                            ),
                        )
                        updated_any = True
                        continue
                    if gateway is None:
                        continue
                    try:
                        target_protocol = self._enforce_target_protocol(target_protocol, gateway.config.protocol)
                        provider_target = self._validated_gateway_message_target(gateway, provider_target)
                    except HomeAssistantError:
                        self._block_unsafe_message(record)
                        await self.store.async_add_message(record)
                        if not self._gateway_callback_is_current(gateway_generation):
                            return
                        self._fire_event(
                            EVENT_MESSAGE_STATUS,
                            _message_status_event(
                                record,
                                "blocked",
                                retryable=False,
                                error_code=_UNSAFE_MESSAGE_IDENTITY_ERROR_CODE,
                            ),
                        )
                        updated_any = True
                        continue
                    await self.tx_limiter.acquire()
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    try:
                        # A node update can arrive while the limiter yields.
                        resolved = self._revalidate_bound_target(
                            envelope.target_node,
                            target_protocol,
                            record.raw.get("target_binding"),
                        )
                        target_protocol = self._enforce_target_protocol(target_protocol, resolved.protocol)
                        target_protocol = self._enforce_target_protocol(target_protocol, gateway.config.protocol)
                        provider_target = resolved.value
                        provider_target = self._validated_gateway_message_target(gateway, provider_target)
                    except HomeAssistantError:
                        self._block_unsafe_message(record)
                        await self.store.async_add_message(record)
                        if not self._gateway_callback_is_current(gateway_generation):
                            return
                        self._fire_event(
                            EVENT_MESSAGE_STATUS,
                            _message_status_event(
                                record,
                                "blocked",
                                retryable=False,
                                error_code=_UNSAFE_MESSAGE_IDENTITY_ERROR_CODE,
                            ),
                        )
                        updated_any = True
                        continue
                    try:
                        provider_id = await gateway.async_send_message(
                            target_node=provider_target,
                            message=record.text,
                            channel=record.channel,
                            priority=record.priority,
                            message_type=record.message_type,
                        )
                    except Exception:
                        if not self._gateway_callback_is_current(gateway_generation):
                            return
                        record.raw["status"] = "queued"
                        record.raw["last_error_code"] = "send_failed"
                        record.raw.pop("last_error", None)
                        await self.store.async_add_message(record)
                        if self._gateway_callback_is_current(gateway_generation):
                            self._fire_event(
                                EVENT_MESSAGE_STATUS,
                                _message_status_event(
                                    record,
                                    "failed",
                                    retryable=True,
                                    error_code="send_failed",
                                ),
                            )
                        continue
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    record.gateway_id = gateway.config.gateway_id
                    record.protocol = gateway.config.protocol
                    record.raw["status"] = "sent"
                    record.raw["provider_id"] = provider_id
                    await self.store.async_add_message(record)
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    self._fire_event(EVENT_MESSAGE_SENT, message_api_dict(record))
                    self._fire_event(
                        EVENT_MESSAGE_STATUS,
                        _message_status_event(record, "sent", retryable=False),
                    )
                    updated_any = True
                if updated_any:
                    self.snapshot.recent_messages = await self.store.async_recent_messages(100)
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    self.async_set_updated_data(self.snapshot)
            finally:
                self._outbox_flush_owner = None
        cursor_timestamp = timestamp_to_json(pending[-1].timestamp)
        if cursor_timestamp is None:
            return None
        return (cursor_timestamp, pending[-1].message_id), len(pending) == 100

    def _gateway_callback_is_current(self, gateway_generation: int | None) -> bool:
        """Return whether provider work still belongs to the active gateways."""
        if getattr(self, "_shutting_down", False) or getattr(self, "_reconnect_suspended", False):
            return False
        return gateway_generation is None or gateway_generation == getattr(
            self, "_gateway_generation", gateway_generation
        )

    def _schedule_reconnect(self, gateway_id: str) -> None:
        if self._shutting_down or self._reconnect_suspended or gateway_id in self._reconnect_tasks:
            return
        task = self._async_create_background_task(self._delayed_reconnect(gateway_id), "MeshNet gateway reconnect")
        self._reconnect_tasks[gateway_id] = task

        def clear_reconnect(done_task: asyncio.Task[Any]) -> None:
            if self._reconnect_tasks.get(gateway_id) is done_task:
                self._reconnect_tasks.pop(gateway_id, None)

        task.add_done_callback(clear_reconnect)

    async def _delayed_reconnect(self, gateway_id: str) -> None:
        attempt = 0
        while not self._shutting_down and not self._reconnect_suspended:
            gateway = self.gateways.get(gateway_id)
            if gateway is None or gateway.status.connected:
                return

            # A Meshtastic BLE constructor is synchronous underneath its async
            # executor wrapper. Join that single-flight start instead of stopping
            # it or queueing another constructor in Home Assistant's executor.
            if getattr(gateway, "start_pending", False):
                try:
                    await gateway.async_start()
                except Exception:
                    pass
                continue

            await asyncio.sleep(self._reconnect_delay(attempt))
            if self._shutting_down or self._reconnect_suspended:
                return

            gateway = self.gateways.get(gateway_id)
            if gateway is None or gateway.status.connected:
                return
            if getattr(gateway, "start_pending", False):
                continue

            try:
                await gateway.async_stop()
                if self._shutting_down or self._reconnect_suspended:
                    return
                await gateway.async_start()
            except Exception as err:
                if self._shutting_down or self._reconnect_suspended:
                    return
                await gateway._emit_error(f"Reconnect failed: {err}")

            if gateway.status.connected:
                return
            attempt += 1

    @staticmethod
    def _reconnect_delay(attempt: int) -> float:
        """Return capped exponential reconnect delay with bounded jitter."""
        exponent = min(max(attempt, 0), 16)
        base_delay = min(_RECONNECT_INITIAL_DELAY * (2**exponent), _RECONNECT_MAX_DELAY)
        jitter = base_delay * _RECONNECT_JITTER_RATIO
        return min(
            _RECONNECT_MAX_DELAY,
            max(0.0, random.uniform(base_delay - jitter, base_delay + jitter)),
        )

    async def _cancel_reconnect_tasks(self) -> bool:
        """Cancel reconnect loops within a bound and retain pending owners."""
        reconnect_tasks = set(self._reconnect_tasks.values())
        if not reconnect_tasks:
            return True
        current_task = asyncio.current_task()
        waitable_tasks = reconnect_tasks - {current_task}
        for task in waitable_tasks:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set()
        if waitable_tasks:
            done, pending = await asyncio.wait(waitable_tasks, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for gateway_id, task in list(self._reconnect_tasks.items()):
            if task in done:
                self._reconnect_tasks.pop(gateway_id, None)
        return not pending and current_task not in reconnect_tasks

    async def _cancel_gateway_startup_task(self) -> bool:
        """Cancel the startup waiter within a bound and report if it drained."""
        task = self._gateway_startup_task
        if task is None:
            return True
        if task is asyncio.current_task():
            return False
        if not task.done() and task.cancelling() == 0:
            task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT)
        if task not in done:
            # Keep the entry-owned task retained. Its original done callback
            # clears this reference if the provider eventually returns.
            return False
        await asyncio.gather(task, return_exceptions=True)
        if self._gateway_startup_task is task:
            self._gateway_startup_task = None
        return True

    async def _cancel_outbox_flush_owner(self) -> bool:
        """Cancel active outbox delivery within the gateway task bound."""
        task = getattr(self, "_outbox_flush_owner", None)
        if task is None:
            return True
        if task is asyncio.current_task():
            return False
        if not task.done() and task.cancelling() == 0:
            task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT)
        if task not in done:
            return False
        await asyncio.gather(task, return_exceptions=True)
        if self._outbox_flush_owner is task:
            self._outbox_flush_owner = None
        return True

    async def _cancel_send_tasks(self) -> bool:
        """Cancel active direct sends within the gateway task bound."""
        send_tasks = set(getattr(self, "_send_tasks", set()))
        if not send_tasks:
            return True
        current_task = asyncio.current_task()
        waitable_tasks = send_tasks - {current_task}
        for task in waitable_tasks:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set()
        if waitable_tasks:
            done, pending = await asyncio.wait(waitable_tasks, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
            self._send_tasks.difference_update(done)
        return not pending and current_task not in send_tasks

    async def _cancel_traceroute_tasks(self) -> bool:
        """Cancel active manual traceroutes within the gateway task bound."""
        trace_tasks = set(getattr(self, "_traceroute_tasks", set()))
        if not trace_tasks:
            return True
        current_task = asyncio.current_task()
        waitable_tasks = trace_tasks - {current_task}
        for task in waitable_tasks:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set()
        if waitable_tasks:
            done, pending = await asyncio.wait(
                waitable_tasks,
                timeout=_GATEWAY_TASK_CANCEL_TIMEOUT,
            )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
            self._traceroute_tasks.difference_update(done)
        return not pending and current_task not in trace_tasks

    async def async_quiesce_radio_operations(self) -> bool:
        """Fence and drain manual RF work before reload or platform unload."""
        self._radio_operations_accepting = False
        maintenance = getattr(self, "maintenance", None)
        maintenance_quiesce = getattr(maintenance, "async_quiesce", None)
        maintenance_drained = (
            await maintenance_quiesce()
            if callable(maintenance_quiesce)
            else True
        )
        remote_admin = getattr(self, "remote_admin", None)
        quiesce = getattr(remote_admin, "async_quiesce", None)
        remote_drained = await quiesce() if callable(quiesce) else True
        traceroutes_drained = await self._cancel_traceroute_tasks()
        return bool(
            maintenance_drained and remote_drained and traceroutes_drained
        )

    def resume_radio_operations(self) -> bool:
        """Resume manual RF work only after every previous owner has drained."""
        tasks = getattr(self, "_traceroute_tasks", set())
        done = {task for task in tasks if task.done()}
        if done:
            tasks.difference_update(done)
        if (
            any(not task.done() for task in tasks)
            or getattr(self, "_shutting_down", False)
            or getattr(self, "_reconnect_suspended", False)
        ):
            return False
        remote_admin = getattr(self, "remote_admin", None)
        resume = getattr(remote_admin, "resume", None)
        if callable(resume) and resume() is not True:
            return False
        self._radio_operations_accepting = True
        maintenance = getattr(self, "maintenance", None)
        maintenance_resume = getattr(maintenance, "resume", None)
        if callable(maintenance_resume) and maintenance_resume() is not True:
            self._radio_operations_accepting = False
            return False
        return True

    def _validate_requested_gateway_id(self, gateway_id: str | None) -> None:
        """Reject an explicit gateway unless it exists exactly."""
        if gateway_id is not None and gateway_id not in self.gateways:
            raise HomeAssistantError("Unknown MeshNet gateway ID")

    def _select_gateway(
        self,
        *,
        gateway_id: str | None,
        target_node: str | None,
        target_protocol: str | None,
    ) -> MeshGateway | None:
        """Select only a connected gateway compatible with target identity."""
        normalized_target_protocol = _known_protocol(target_protocol)
        if gateway_id is not None:
            self._validate_requested_gateway_id(gateway_id)
            gateway = self.gateways.get(gateway_id)
            if gateway is None:
                return None
            gateway_protocol = _known_protocol(gateway.config.protocol)
            if normalized_target_protocol is not None and gateway_protocol != normalized_target_protocol:
                raise HomeAssistantError(_MESSAGE_PROTOCOL_MISMATCH_ERROR)
            if gateway.status.connected:
                return gateway
            return None
        for node in self.snapshot.nodes.values():
            node_protocol = _known_protocol(node.protocol)
            if normalized_target_protocol is not None and node_protocol != normalized_target_protocol:
                continue
            if target_node and target_node in {
                node.node_key,
                node.node_id,
                node.public_key,
                node.mac,
            }:
                if node.last_gateway_id:
                    gateway = self.gateways.get(node.last_gateway_id)
                    if (
                        gateway
                        and gateway.status.connected
                        and (
                            normalized_target_protocol is None
                            or _known_protocol(gateway.config.protocol) == normalized_target_protocol
                        )
                    ):
                        return gateway
        for gateway in self.gateways.values():
            if gateway.status.connected and (
                normalized_target_protocol is None
                or _known_protocol(gateway.config.protocol) == normalized_target_protocol
            ):
                return gateway
        return None

    def _mark_stale_nodes(self) -> None:
        now = utcnow()
        nodes = getattr(self, "_raw_nodes", None)
        source = nodes.values() if nodes is not None else self.snapshot.nodes.values()
        changed = False
        for node in source:
            if node.last_heard and (now - node.last_heard).total_seconds() > self.node_timeout:
                changed = changed or node.online
                node.online = False
        if nodes is not None and changed:
            self._refresh_effective_node_projection()

    def _mesh_health_score(self) -> float:
        nodes = list(self.snapshot.nodes.values())
        if not nodes:
            return 0.0
        online_count = sum(1 for node in nodes if node.online)
        online_score = online_count / len(nodes)
        battery_values = [
            float(node.power["battery_level"])
            for node in nodes
            if isinstance(node.power.get("battery_level"), (int, float))
        ]
        battery_score = (sum(battery_values) / len(battery_values) / 100) if battery_values else 1.0
        duplicate_penalty = min(self.deduplicator.duplicate_ratio, 0.5)
        connected_gateways = sum(1 for gateway in self.snapshot.gateways.values() if gateway.connected)
        gateway_score = 1.0 if connected_gateways else 0.0
        score = (online_score * 0.45) + (battery_score * 0.2) + (gateway_score * 0.25) + ((1 - duplicate_penalty) * 0.1)
        return round(max(0.0, min(score * 100, 100.0)), 1)

    @staticmethod
    def _issue_category(issue_id: str) -> str:
        """Return a fixed repair category without returning its identifier."""
        for category in _GATEWAY_ISSUE_CATEGORIES:
            if issue_id.startswith(f"{category}_"):
                return category
        if issue_id == "no_gateways":
            return "no_gateways"
        return "other"

    def _delete_legacy_gateway_issues(self) -> int:
        """Delete pre-v0.4.2 issue keys that embedded configured identities."""
        registry_getter = getattr(ir, "async_get", None)
        issue_deleter = getattr(ir, "async_delete_issue", None)
        hass = getattr(self, "hass", None)
        if not callable(registry_getter) or not callable(issue_deleter) or hass is None:
            return 0
        try:
            registry = registry_getter(hass)
            issues = getattr(registry, "issues", {})
            issue_keys = list(issues)
        except Exception:
            return 0

        deleted = 0
        for issue_key in issue_keys:
            if not isinstance(issue_key, tuple) or len(issue_key) != 2:
                continue
            domain, issue_id = issue_key
            if domain != DOMAIN or not isinstance(issue_id, str):
                continue
            category = self._issue_category(issue_id)
            if category not in _GATEWAY_ISSUE_CATEGORIES or _SAFE_GATEWAY_ISSUE_RE.fullmatch(issue_id):
                continue
            try:
                issue_deleter(hass, DOMAIN, issue_id)
            except Exception as err:
                _LOGGER.debug(
                    "Could not remove one legacy MeshNet repair key: %s",
                    type(err).__name__,
                )
            else:
                deleted += 1
        return deleted

    def _delete_resolved_issue(self, issue_id: str) -> bool:
        """Delete one identity-free resolved repair without affecting startup."""
        if issue_id != "no_gateways" and _SAFE_GATEWAY_ISSUE_RE.fullmatch(issue_id) is None:
            return False
        issue_deleter = getattr(ir, "async_delete_issue", None)
        hass = getattr(self, "hass", None)
        if not callable(issue_deleter) or hass is None:
            return False
        try:
            issue_deleter(hass, DOMAIN, issue_id)
        except Exception as err:
            _LOGGER.debug(
                "Could not remove one resolved MeshNet repair: %s",
                type(err).__name__,
            )
            return False
        return True

    def _repair_diagnostics(self) -> dict[str, Any]:
        """Return cached repair aggregates without issue identifiers or text."""
        registry_getter = getattr(ir, "async_get", None)
        hass = getattr(self, "hass", None)
        if not callable(registry_getter) or hass is None:
            return {"available": False}
        try:
            issues = getattr(registry_getter(hass), "issues", {})
            entries = [
                (issue_id, issue)
                for (domain, issue_id), issue in issues.items()
                if domain == DOMAIN and isinstance(issue_id, str)
            ]
        except Exception as err:
            return {
                "available": False,
                "collection_error": type(err).__name__,
            }

        categories = Counter(self._issue_category(issue_id) for issue_id, _issue in entries)
        return {
            "available": True,
            "issue_count": len(entries),
            "active_issue_count": sum(bool(getattr(issue, "active", False)) for _issue_id, issue in entries),
            "persistent_issue_count": sum(bool(getattr(issue, "is_persistent", False)) for _issue_id, issue in entries),
            "legacy_identity_issue_count": sum(
                self._issue_category(issue_id) in _GATEWAY_ISSUE_CATEGORIES
                and _SAFE_GATEWAY_ISSUE_RE.fullmatch(issue_id) is None
                for issue_id, _issue in entries
            ),
            "legacy_issues_removed_during_setup": getattr(self, "_legacy_issue_cleanup_count", 0),
            "category_counts": dict(sorted(categories.items())),
        }

    def _create_issue(
        self,
        *,
        issue_id: str,
        message: str,
        severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    ) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=severity,
            translation_key="gateway_issue",
            translation_placeholders={"message": message},
        )

    def _gateway_issue_id(self, category: str, gateway_id: str) -> str:
        """Return a stable issue key that does not expose a gateway identifier."""
        return f"{category}_gateway_{self._gateway_ordinal(gateway_id)}"

    def _gateway_ordinal(self, gateway_id: str) -> str:
        """Return only a stable adapter ordinal, never its configured ID."""
        gateway_configs = list(getattr(self, "_gateway_configs", []))
        for index, config in enumerate(gateway_configs, start=1):
            if config.gateway_id == gateway_id:
                return f"{index:03d}"
        gateway_ids = sorted(getattr(self, "gateways", {}))
        try:
            index = gateway_ids.index(gateway_id) + 1
        except ValueError:
            return "unknown"
        return f"{index:03d}"

    @staticmethod
    def _message_id(
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        gateway_id: str | None,
    ) -> str:
        import hashlib

        return hashlib.sha256(
            stable_json(
                {
                    "target_node": target_node,
                    "message": message,
                    "channel": channel,
                    "gateway_id": gateway_id,
                    "timestamp": utcnow().timestamp(),
                }
            ).encode()
        ).hexdigest()[:20]

    @staticmethod
    def _load_gateway_configs(entry: ConfigEntry) -> list[GatewayConfig]:
        if CONF_GATEWAYS in entry.options:
            gateways = entry.options[CONF_GATEWAYS]
        else:
            gateways = entry.data.get(CONF_GATEWAYS) or []
        scan_interval = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )
        configs: list[GatewayConfig] = []
        for gateway_data in gateways:
            gateway = dict(gateway_data)
            if gateway.get("transport") == TRANSPORT_REST:
                gateway_options = dict(gateway.get("options") or {})
                gateway_options.setdefault(CONF_SCAN_INTERVAL, scan_interval)
                gateway["options"] = gateway_options
            configs.append(GatewayConfig.from_dict(gateway))
        return configs


def service_fields(call_data: dict[str, Any]) -> dict[str, Any]:
    """Return normalized service call fields."""
    envelope = validated_message_envelope(
        target_node=call_data.get(ATTR_TARGET_NODE),
        message=call_data[ATTR_MESSAGE],
        channel=call_data.get(ATTR_CHANNEL),
        priority=call_data.get(ATTR_PRIORITY, "normal"),
        message_type=call_data.get(ATTR_MESSAGE_TYPE, MESSAGE_TYPE_BROADCAST),
        gateway_id=call_data.get(ATTR_GATEWAY_ID),
    )
    return {
        "target_node": envelope.target_node,
        "message": envelope.message,
        "channel": envelope.channel,
        "priority": envelope.priority,
        "message_type": envelope.message_type,
        "gateway_id": envelope.gateway_id,
    }
