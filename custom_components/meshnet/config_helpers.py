"""Pure configuration helpers shared by the UI flow and tests.

This module intentionally has no Home Assistant imports.  Keeping gateway
normalisation and validation here makes the rules identical for UI, YAML, and
future discovery flows, and lets them be tested without a Home Assistant
runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .const import (
    DEFAULT_MESHCORE_MQTT_TOPIC,
    DEFAULT_MESHTASTIC_MQTT_TOPIC,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    PROTOCOLS,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_NATIVE,
    TRANSPORT_REST,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
    TRANSPORTS,
)


SUPPORTED_TRANSPORTS: dict[str, tuple[str, ...]] = {
    PROTOCOL_MESHTASTIC: (
        TRANSPORT_TCP,
        TRANSPORT_SERIAL,
        TRANSPORT_BLUETOOTH,
        TRANSPORT_MQTT,
    ),
    PROTOCOL_MESHCORE: (
        TRANSPORT_TCP,
        TRANSPORT_SERIAL,
        TRANSPORT_BLUETOOTH,
        TRANSPORT_MQTT,
        TRANSPORT_REST,
        TRANSPORT_NATIVE,
    ),
}

DEFAULT_TCP_PORTS = {
    PROTOCOL_MESHTASTIC: 4403,
}

DEFAULT_MQTT_TOPICS = {
    PROTOCOL_MESHTASTIC: DEFAULT_MESHTASTIC_MQTT_TOPIC,
    PROTOCOL_MESHCORE: DEFAULT_MESHCORE_MQTT_TOPIC,
}

_SAFE_ID = re.compile(r"[^a-z0-9]+")


def supported_transports(protocol: str) -> tuple[str, ...]:
    """Return transports supported by a radio protocol."""
    return SUPPORTED_TRANSPORTS.get(protocol, ())


def default_gateway_name(protocol: str, transport: str) -> str:
    """Return a friendly default gateway name."""
    return f"{protocol.title()} {transport.title()}"


def new_gateway_id(protocol: str, transport: str, name: str) -> str:
    """Build a readable, collision-resistant gateway id."""
    stem = _SAFE_ID.sub("_", name.strip().lower()).strip("_")
    if not stem:
        stem = f"{protocol}_{transport}"
    return f"{stem}_{uuid4().hex[:8]}"


def gateway_from_form(
    protocol: str,
    transport: str,
    user_input: Mapping[str, Any],
    *,
    gateway_id: str | None = None,
) -> dict[str, Any]:
    """Convert one transport-specific form submission into gateway data."""
    name = str(
        user_input.get("name") or default_gateway_name(protocol, transport)
    ).strip()
    data: dict[str, Any] = {
        "gateway_id": gateway_id or new_gateway_id(protocol, transport, name),
        "name": name,
        "protocol": protocol,
        "transport": transport,
    }

    for key in (
        "host",
        "port",
        "serial_path",
        "ble_address",
        "mqtt_topic",
        "api_url",
        "api_key",
    ):
        if key in user_input:
            data[key] = user_input[key]

    options = dict(user_input.get("options") or {})
    for key in (
        "baudrate",
        "debug",
        "pin",
        "publish_topic",
        "send_url",
        "mqtt_node_id",
    ):
        if key in user_input and user_input[key] not in (None, ""):
            options[key] = user_input[key]
    if options:
        data["options"] = options

    if transport == TRANSPORT_MQTT and not data.get("mqtt_topic"):
        data["mqtt_topic"] = DEFAULT_MQTT_TOPICS[protocol]
    if (
        transport == TRANSPORT_TCP
        and data.get("port") in (None, "")
        and protocol in DEFAULT_TCP_PORTS
    ):
        data["port"] = DEFAULT_TCP_PORTS[protocol]
    return validate_gateway_dict(data)


def validate_gateway_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise a gateway mapping."""
    if not isinstance(data, Mapping):
        raise ValueError("gateway must be an object")

    required = {"gateway_id", "name", "protocol", "transport"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"gateway missing required fields: {', '.join(sorted(missing))}")

    cleaned = dict(data)
    for key in (
        "gateway_id",
        "name",
        "protocol",
        "transport",
        "host",
        "serial_path",
        "ble_address",
        "mqtt_topic",
        "api_url",
        "api_key",
    ):
        if isinstance(cleaned.get(key), str):
            cleaned[key] = cleaned[key].strip()

    if not cleaned["gateway_id"] or not cleaned["name"]:
        raise ValueError("gateway_id and name cannot be empty")
    if cleaned["protocol"] not in PROTOCOLS:
        raise ValueError(f"unsupported protocol: {cleaned['protocol']}")
    if cleaned["transport"] not in TRANSPORTS:
        raise ValueError(f"unsupported transport: {cleaned['transport']}")
    if cleaned["transport"] not in supported_transports(cleaned["protocol"]):
        raise ValueError(
            f"{cleaned['protocol']} does not support {cleaned['transport']} transport"
        )

    if (
        cleaned["transport"] == TRANSPORT_TCP
        and cleaned.get("port") in (None, "")
        and cleaned["protocol"] in DEFAULT_TCP_PORTS
    ):
        cleaned["port"] = DEFAULT_TCP_PORTS[cleaned["protocol"]]
    if cleaned["transport"] == TRANSPORT_MQTT and not cleaned.get("mqtt_topic"):
        cleaned["mqtt_topic"] = DEFAULT_MQTT_TOPICS[cleaned["protocol"]]

    if "port" in cleaned and cleaned["port"] not in (None, ""):
        try:
            cleaned["port"] = int(cleaned["port"])
        except (TypeError, ValueError) as err:
            raise ValueError("port must be a number") from err
        if not 1 <= cleaned["port"] <= 65535:
            raise ValueError("port must be between 1 and 65535")

    options = cleaned.get("options")
    if options is not None and not isinstance(options, Mapping):
        raise ValueError("options must be an object")
    if isinstance(options, Mapping):
        cleaned["options"] = dict(options)

    _validate_transport_requirements(cleaned)
    return {
        key: value
        for key, value in cleaned.items()
        if value not in (None, "", {})
    }


def _validate_transport_requirements(data: Mapping[str, Any]) -> None:
    """Validate the fields needed by one transport."""
    transport = data["transport"]
    if transport == TRANSPORT_TCP:
        if not isinstance(data.get("host"), str) or not data["host"]:
            raise ValueError("tcp transport requires host")
        if not data.get("port"):
            raise ValueError("tcp transport requires port")
    elif transport in {TRANSPORT_SERIAL, TRANSPORT_NATIVE}:
        if (
            not isinstance(data.get("serial_path"), str)
            or not data["serial_path"]
        ):
            raise ValueError(f"{transport} transport requires serial_path")
    elif transport == TRANSPORT_BLUETOOTH:
        if (
            not isinstance(data.get("ble_address"), str)
            or not data["ble_address"]
        ):
            raise ValueError("bluetooth transport requires ble_address")
    elif transport == TRANSPORT_REST:
        url = str(data.get("api_url") or "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("rest transport requires a valid http(s) api_url")


def gateway_summary(gateways: list[Mapping[str, Any]]) -> str:
    """Return a compact summary suitable for a config-flow description."""
    return "\n".join(
        f"• {item.get('name', item.get('gateway_id', 'Gateway'))} "
        f"({item.get('protocol', '?')} via {item.get('transport', '?')})"
        for item in gateways
    )
