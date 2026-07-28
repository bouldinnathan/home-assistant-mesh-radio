"""Privacy-preserving diagnostics support for MeshNet."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import metadata
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DIAGNOSTIC_REDACT, DOMAIN, VERSION
from .coordinator import MeshNetCoordinator
from .diagnostic_safety import safe_node_metadata
from .models import NodeState, timestamp_to_json

_DIAGNOSTIC_SCHEMA_VERSION = 2
_REDACTED = "**REDACTED**"
_RUNTIME_TIMEOUT = 3.0
_MAX_DIAGNOSTIC_NODES = 1000

_SENSITIVE_KEYS = {
    *DIAGNOSTIC_REDACT,
    "adapter_address",
    "bluetooth_adapter_address",
    "channel",
    "device_id",
    "device_name",
    "from",
    "from_id",
    "friendly_name",
    "gateway_name",
    "hostname",
    "ip_address",
    "last_gateway_id",
    "long_name",
    "message",
    "message_id",
    "name",
    "network_key",
    "next_hop",
    "node_key",
    "packet_id",
    "partner_ieee",
    "position",
    "provider_id",
    "receiver",
    "sender",
    "short_name",
    "target_node",
    "title",
    "to",
    "to_id",
    "unique_id",
    "user_name",
    "username",
}

_PRIVATE_TELEMETRY_KEYS = {
    "contact",
    "door",
    "latitude",
    "location",
    "longitude",
    "motion",
    "occupancy",
    "payload",
    "position",
    "presence",
    "raw",
    "text",
}

_SAFE_CONNECTIVITY_KEYS = frozenset(
    {
        "air_utilization",
        "channel_utilization",
        "hop_limit",
        "hops",
        "latency",
        "link_quality",
        "lqi",
        "noise_floor",
        "packet_rx",
        "packet_tx",
        "rssi",
        "snr",
    }
)
_SAFE_POWER_KEYS = frozenset(
    {
        "battery_level",
        "charging",
        "current",
        "solar_charging",
        "voltage",
    }
)
_SAFE_RADIO_KEYS = frozenset(
    {
        "air_util_tx",
        "bandwidth",
        "channel_utilization",
        "coding_rate",
        "duty_cycle",
        "frequency",
        "spreading_factor",
        "tx_power",
    }
)
_SAFE_ROUTING_KEYS = frozenset(
    {
        "hop_limit",
        "hops",
        "hops_away",
        "neighbor_count",
        "route_count",
    }
)
_SAFE_SENSOR_KEYS = frozenset(
    {
        "air_quality",
        "barometric_pressure",
        "battery_level",
        "co2",
        "current",
        "gas_resistance",
        "humidity",
        "iaq",
        "illuminance",
        "light",
        "lux",
        "pressure",
        "relative_humidity",
        "solar_charging",
        "temperature",
        "voc",
        "voltage",
        "water",
        "wind_direction",
        "wind_speed",
    }
)

_URL_RE = re.compile(r"(?i)\b(?:https?|mqtt|mqtts|ws|wss)://[^\s]+")
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_RE = re.compile(
    r"(?i)(?<![0-9a-f:])(?:"
    r"(?:[0-9a-f]{1,4}:){3,}[0-9a-f:]{1,}"
    r"|[0-9a-f]{0,4}::[0-9a-f:]*"
    r")(?![0-9a-f:])"
)
_MAC_RE = re.compile(r"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b")
_SERIAL_PATH_RE = re.compile(r"(?i)(?:/dev/[^\s]+|\bCOM\d+\b)")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|password|passphrase|pin|secret|token)\s*[:=]\s*[^\s,;]+"
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_HOSTNAME_RE = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+"
    r"(?:local|lan|home|internal|[a-z]{2,63})\b"
)
_LONG_HEX_RE = re.compile(
    r"(?i)(?<![0-9a-f])(?:!?0x[0-9a-f]{8,}|!?[0-9a-f]{12,})(?![0-9a-f])"
)


def _is_sensitive_key(key: str) -> bool:
    """Return whether a mapping key identifies private diagnostic data."""
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    words = set(normalized.split("_"))
    return bool(
        words
        & {
            "authorization",
            "address",
            "credential",
            "credentials",
            "email",
            "latitude",
            "longitude",
            "password",
            "passphrase",
            "payload",
            "path",
            "secret",
            "ssid",
            "token",
            "topic",
            "url",
        }
    ) or normalized.endswith(("_api_key", "_private_key", "_public_key"))


def _sanitize_string(value: str) -> str:
    """Remove common identifiers and credentials embedded inside text."""
    sanitized = value
    for pattern in (
        _URL_RE,
        _IPV4_RE,
        _IPV6_RE,
        _MAC_RE,
        _SERIAL_PATH_RE,
        _SECRET_ASSIGNMENT_RE,
        _EMAIL_RE,
        _HOSTNAME_RE,
        _LONG_HEX_RE,
    ):
        sanitized = pattern.sub(_REDACTED, sanitized)
    return sanitized


def _sanitize_data(
    value: Any,
    *,
    omit_sensitive_keys: bool = False,
    depth: int = 0,
) -> Any:
    """Return JSON-safe data with defensive key and in-string redaction."""
    if depth > 12:
        return "<maximum diagnostic depth reached>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items(), start=1):
            key = str(raw_key)
            if _is_sensitive_key(key):
                if not omit_sensitive_keys:
                    result[key] = _REDACTED
                continue
            sanitized_key = _sanitize_string(key)
            if sanitized_key != key:
                sanitized_key = f"redacted_key_{index:03d}"
            while sanitized_key in result:
                sanitized_key = f"{sanitized_key}_{index:03d}"
            result[sanitized_key] = _sanitize_data(
                item,
                omit_sensitive_keys=omit_sensitive_keys,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _sanitize_data(
                item,
                omit_sensitive_keys=omit_sensitive_keys,
                depth=depth + 1,
            )
            for item in value
        ]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        return _sanitize_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return _sanitize_data(enum_value, depth=depth + 1)
    return f"<{type(value).__name__}>"


def _safe_telemetry(
    data: Mapping[str, Any],
    allowed_keys: frozenset[str],
) -> dict[str, Any]:
    """Return cached numeric telemetry without identity or activity data."""
    safe: dict[str, Any] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
        if (
            normalized not in allowed_keys
            or _is_sensitive_key(key)
            or normalized in _PRIVATE_TELEMETRY_KEYS
        ):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            safe[normalized] = value
    return safe


def _safe_node_diagnostics(
    node: NodeState,
    *,
    diagnostic_id: str | None = None,
) -> dict[str, Any]:
    """Return detailed cached node health without identity, content, or location."""
    result: dict[str, Any] = {
        "protocol": _sanitize_string(node.protocol),
        "hardware_model": safe_node_metadata(node.hardware_model, "hardware_model"),
        "firmware_version": safe_node_metadata(
            node.firmware_version, "firmware_version"
        ),
        "radio_type": safe_node_metadata(node.radio_type, "radio_type"),
        "role": safe_node_metadata(node.role, "role"),
        "metadata_present": {
            "hardware_model": bool(node.hardware_model),
            "firmware_version": bool(node.firmware_version),
            "radio_type": bool(node.radio_type),
            "role": bool(node.role),
        },
        "online": node.online,
        "last_heard": timestamp_to_json(node.last_heard),
        "gateway_count": len(node.gateway_ids),
        "has_location": bool(node.location),
        "field_counts": {
            "connectivity": len(node.connectivity),
            "power": len(node.power),
            "radio": len(node.radio),
            "routing": len(node.routing),
            "sensors": len(node.sensors),
            "location_fields": len(node.location),
            "raw_fields": len(node.raw),
        },
        "connectivity": _safe_telemetry(
            node.connectivity, _SAFE_CONNECTIVITY_KEYS
        ),
        "power": _safe_telemetry(node.power, _SAFE_POWER_KEYS),
        "radio": _safe_telemetry(node.radio, _SAFE_RADIO_KEYS),
        "routing": _safe_telemetry(node.routing, _SAFE_ROUTING_KEYS),
        "sensors": _safe_telemetry(node.sensors, _SAFE_SENSOR_KEYS),
    }
    if diagnostic_id is not None:
        result["diagnostic_id"] = diagnostic_id
    return result


def _safe_gateway_configuration(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return allowlisted gateway configuration and endpoint-presence flags."""
    options = data.get("options")
    if not isinstance(options, Mapping):
        options = {}
    safe_options = {
        key: options[key]
        for key in (
            "baudrate",
            "debug",
            "message_poll_interval",
            "scan_interval",
        )
        if isinstance(options.get(key), (bool, int, float))
    }
    represented_gateway_keys = {
        "api_key",
        "api_url",
        "ble_address",
        "gateway_id",
        "host",
        "mqtt_topic",
        "name",
        "options",
        "port",
        "protocol",
        "serial_path",
        "transport",
    }
    represented_option_keys = {
        "baudrate",
        "bluetooth_adapter",
        "bluetooth_adapter_address",
        "bluetooth_bond_managed",
        "debug",
        "message_poll_interval",
        "mqtt_node_id",
        "pin",
        "publish_topic",
        "scan_interval",
        "send_url",
    }
    return {
        "protocol": data.get("protocol"),
        "transport": data.get("transport"),
        "port": data.get("port") if isinstance(data.get("port"), int) else None,
        "endpoint_configuration": {
            "host_configured": bool(data.get("host")),
            "serial_endpoint_configured": bool(data.get("serial_path")),
            "bluetooth_endpoint_configured": bool(data.get("ble_address")),
            "mqtt_subscription_configured": bool(data.get("mqtt_topic")),
            "rest_endpoint_configured": bool(data.get("api_url")),
            "api_key_configured": bool(data.get("api_key")),
            "publish_endpoint_configured": bool(options.get("publish_topic")),
            "custom_send_endpoint_configured": bool(options.get("send_url")),
            "mqtt_node_metadata_configured": bool(options.get("mqtt_node_id")),
            "bluetooth_adapter_metadata_configured": bool(
                options.get("bluetooth_adapter")
                and options.get("bluetooth_adapter_address")
            ),
            "bluetooth_bond_managed": bool(
                options.get("bluetooth_bond_managed")
            ),
            "pin_configured": bool(options.get("pin")),
        },
        "safe_options": safe_options,
        "omitted_identity_field_count": sum(
            key in data for key in ("gateway_id", "name")
        ),
        "omitted_unknown_field_count": len(
            set(map(str, data)) - represented_gateway_keys
        ),
        "omitted_unknown_option_count": len(
            set(map(str, options)) - represented_option_keys
        ),
    }


def _safe_entry_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return allowlisted non-identifying global config-entry values."""
    safe = {
        key: data[key]
        for key in (
            "history_days",
            "node_timeout",
            "packet_capture",
            "scan_interval",
        )
        if isinstance(data.get(key), (bool, int, float))
    }
    gateways = data.get("gateways")
    if isinstance(gateways, list):
        safe["gateways"] = [
            _safe_gateway_configuration(gateway)
            for gateway in gateways
            if isinstance(gateway, Mapping)
        ]
    represented_top_level_keys = set(safe)
    safe["omitted_top_level_value_count"] = len(
        set(map(str, data)) - represented_top_level_keys
    )
    return safe


def _safe_config_entry(entry: ConfigEntry) -> dict[str, Any]:
    """Return a ZHA-style config-entry snapshot using an explicit allowlist."""
    raw = entry.as_dict()
    data = raw.get("data")
    options = raw.get("options")
    safe: dict[str, Any] = {
        "entry_id": _REDACTED,
        "version": raw.get("version"),
        "minor_version": raw.get("minor_version"),
        "domain": raw.get("domain"),
        "title": _REDACTED if raw.get("title") else None,
        "source": raw.get("source"),
        "unique_id": _REDACTED if raw.get("unique_id") else None,
        "disabled_by": raw.get("disabled_by"),
        "pref_disable_new_entities": raw.get("pref_disable_new_entities"),
        "pref_disable_polling": raw.get("pref_disable_polling"),
        "data": _safe_entry_values(data) if isinstance(data, Mapping) else {},
        "options": (
            _safe_entry_values(options) if isinstance(options, Mapping) else {}
        ),
    }
    return safe


def _package_version(distribution: str) -> str:
    """Return an installed package version without importing its SDK."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "not installed"
    except Exception:
        return "unknown"


_VERSION_INFO = {
    "meshnet": VERSION,
    "meshtastic": _package_version("meshtastic"),
    "meshcore": _package_version("meshcore"),
    "bluetooth_adapters": _package_version("bluetooth-adapters"),
    "dbus_fast": _package_version("dbus-fast"),
    "pypubsub": _package_version("pypubsub"),
    "sqlite": sqlite3.sqlite_version,
}


def _versions() -> dict[str, str]:
    """Return cached integration and provider dependency versions."""
    return dict(_VERSION_INFO)


async def _async_runtime_diagnostics(
    coordinator: MeshNetCoordinator,
) -> dict[str, Any]:
    """Collect cached runtime data behind a short, fail-safe bound."""
    try:
        async with asyncio.timeout(_RUNTIME_TIMEOUT):
            return await coordinator.async_diagnostics()
    except TimeoutError:
        return {"available": False, "collection_error": "timeout"}
    except asyncio.CancelledError:
        raise
    except Exception as err:
        return {
            "available": False,
            "collection_error": type(err).__name__,
        }


def _safe_gateway_device(
    coordinator: MeshNetCoordinator,
    gateway_id: str,
) -> dict[str, Any] | None:
    """Return a cached gateway snapshot without its configured identity."""
    gateway = coordinator.gateways.get(gateway_id)
    if gateway is None:
        return None
    status = gateway.status
    diagnostic_snapshot = getattr(gateway, "diagnostic_snapshot", None)
    try:
        client = diagnostic_snapshot() if callable(diagnostic_snapshot) else {}
    except Exception as err:
        client = {
            "available": False,
            "collection_error": type(err).__name__,
        }
    return {
        "protocol": status.protocol,
        "transport": status.transport,
        "connected": status.connected,
        "last_connected": timestamp_to_json(status.last_connected),
        "last_packet": timestamp_to_json(status.last_packet),
        "packets_received": status.packets_received,
        "packets_sent": status.packets_sent,
        "duplicate_packets": status.duplicate_packets,
        "error_count": len(status.errors),
        "client": client,
    }


def _privacy_metadata() -> dict[str, Any]:
    """Describe intentionally omitted data so a diagnostic is self-explanatory."""
    return {
        "policy_version": 3,
        "cached_state_only": True,
        "radio_operations_performed": False,
        "scope": "MeshNet-owned data section",
        "native_home_assistant_wrapper": {
            "controlled_by_meshnet": False,
            "may_include_config_entry_id": True,
            "may_include_device_name_and_registry_id": True,
            "may_include_system_versions_and_timezone": True,
            "inspect_and_rename_before_sharing": True,
        },
        "omitted": [
            "credentials and pairing PINs from the MeshNet data section",
            "entry, gateway, node, packet, message, and provider identifiers from the MeshNet data section",
            "gateway and node names",
            "network addresses, serial paths, URLs, and MQTT topics",
            "raw packets, payloads, message text, send targets, and channels",
            "precise locations and occupancy, motion, door, and presence values",
            "contacts, public keys, routes containing node identities, and raw SDK state",
        ],
    }


def _registry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return cached entity/device registry aggregates without registry IDs."""
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        entities = er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        devices = dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        )
    except Exception as err:
        return {
            "available": False,
            "collection_error": type(err).__name__,
        }

    entity_domains = Counter(
        entity.entity_id.partition(".")[0] for entity in entities
    )
    entity_disabled = Counter(
        str(getattr(entity, "disabled_by", None) or "enabled")
        for entity in entities
    )
    entity_categories = Counter(
        str(getattr(entity, "entity_category", None) or "none")
        for entity in entities
    )
    device_disabled = Counter(
        str(getattr(device, "disabled_by", None) or "enabled")
        for device in devices
    )
    state_health: Counter[str] = Counter()
    domain_state_health: dict[str, Counter[str]] = {}
    states = getattr(hass, "states", None)
    if states is not None:
        for entity in entities:
            domain = entity.entity_id.partition(".")[0]
            domain_health = domain_state_health.setdefault(domain, Counter())
            state = states.get(entity.entity_id)
            if state is None:
                state_health["not_registered_in_state_machine"] += 1
                domain_health["not_registered_in_state_machine"] += 1
            elif state.state in {"unknown", "unavailable"}:
                state_health[state.state] += 1
                domain_health[state.state] += 1
            else:
                state_health["available"] += 1
                domain_health["available"] += 1

    return {
        "available": True,
        "entity_count": len(entities),
        "device_count": len(devices),
        "entity_domain_counts": dict(sorted(entity_domains.items())),
        "entity_enabled_counts": dict(sorted(entity_disabled.items())),
        "entity_category_counts": dict(sorted(entity_categories.items())),
        "device_enabled_counts": dict(sorted(device_disabled.items())),
        "entity_state_health": dict(sorted(state_health.items())),
        "entity_domain_state_health": {
            domain: dict(sorted(counts.items()))
            for domain, counts in sorted(domain_state_health.items())
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return thorough, cached, and privacy-preserving entry diagnostics."""
    coordinator: MeshNetCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    nodes = (
        list(coordinator.snapshot.nodes.items()) if coordinator is not None else []
    )
    runtime = (
        await _async_runtime_diagnostics(coordinator)
        if coordinator is not None
        else {"available": False, "collection_error": "entry_not_loaded"}
    )
    sorted_nodes = sorted(nodes, key=lambda item: item[0])
    exported_nodes = sorted_nodes[:_MAX_DIAGNOSTIC_NODES]
    diagnostics = {
        "schema_version": _DIAGNOSTIC_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "config_entry": _safe_config_entry(entry),
        "versions": _versions(),
        "runtime": runtime,
        "registries": _registry_diagnostics(hass, entry),
        "node_export": {
            "total_count": len(sorted_nodes),
            "exported_count": len(exported_nodes),
            "truncated": len(exported_nodes) < len(sorted_nodes),
            "maximum_exported": _MAX_DIAGNOSTIC_NODES,
        },
        "nodes": [
            _safe_node_diagnostics(node, diagnostic_id=f"node_{index:03d}")
            for index, (_node_key, node) in enumerate(exported_nodes, start=1)
        ],
        "privacy": _privacy_metadata(),
    }
    sanitized = _sanitize_data(diagnostics)
    return async_redact_data(sanitized, DIAGNOSTIC_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return detailed cached diagnostics for a MeshNet hub, gateway, or node."""
    coordinator: MeshNetCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    base: dict[str, Any] = {
        "schema_version": _DIAGNOSTIC_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "privacy": _privacy_metadata(),
    }
    if coordinator is None:
        return {
            **base,
            "device_type": "unknown",
            "reason": "entry_not_loaded",
        }

    identifiers = {
        identifier
        for domain, identifier in device.identifiers
        if domain == DOMAIN
    }
    if entry.entry_id in identifiers:
        runtime = await _async_runtime_diagnostics(coordinator)
        runtime_snapshot = runtime.get("snapshot", {})
        result = {
            **base,
            "device_type": "hub",
            "hub": {
                "runtime": runtime,
                "registries": _registry_diagnostics(hass, entry),
                "node_count": len(coordinator.snapshot.nodes),
                "gateway_count": len(coordinator.gateways),
                "mesh_health_score": runtime_snapshot.get("mesh_health_score"),
            },
        }
    else:
        gateway_id = next(
            (identifier for identifier in identifiers if identifier in coordinator.gateways),
            None,
        )
        if gateway_id is not None:
            result = {
                **base,
                "device_type": "gateway",
                "gateway": _safe_gateway_device(coordinator, gateway_id),
            }
        else:
            node = next(
                (
                    coordinator.snapshot.nodes[identifier]
                    for identifier in identifiers
                    if identifier in coordinator.snapshot.nodes
                ),
                None,
            )
            if node is None:
                return {
                    **base,
                    "device_type": "unknown",
                    "reason": "device_not_in_entry_snapshot",
                }
            result = {
                **base,
                "device_type": "node",
                "node": _safe_node_diagnostics(node),
            }

    sanitized = _sanitize_data(result)
    return async_redact_data(sanitized, DIAGNOSTIC_REDACT)
