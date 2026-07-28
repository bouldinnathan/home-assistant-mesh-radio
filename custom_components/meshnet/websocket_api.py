"""WebSocket API for the MeshNet panel and automations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from itertools import islice
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import (
    ATTR_CHANNEL,
    ATTR_GATEWAY_ID,
    ATTR_MESSAGE,
    ATTR_MESSAGE_TYPE,
    ATTR_PRIORITY,
    ATTR_TARGET_NODE,
    DOMAIN,
    MAX_PANEL_GATEWAYS,
    MAX_PANEL_NODES,
    MESSAGE_TYPE_BROADCAST,
)
from .coordinator import MeshNetCoordinator
from .panel_telemetry import (
    PANEL_ERROR_CATEGORIES,
    PANEL_ERROR_CODES,
    PANEL_ERROR_TYPES,
    PANEL_OPERATIONS,
    PanelTelemetry,
    classify_exception,
)

_LOGGER = logging.getLogger(__name__)

_FAVORITE_LABEL_NAME = "MeshNet Favorite"
_PANEL_PROVENANCE_KEYS = (
    "total_node_count",
    "analyzed_node_count",
    "omitted_node_count",
    "current_session_node_count",
    "cached_only_node_count",
    "online_node_count",
    "located_node_count",
    "located_offline_node_count",
    "mqtt_node_count",
    "mqtt_unknown_node_count",
    "identity_collision_group_count",
    "identity_collision_node_count",
)
_MAX_REPORTED_COUNT = 1_000_000


def _bounded_positive_int(value: Any) -> int:
    """Validate a positive report count without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise vol.Invalid("expected a positive integer")
    if not 1 <= value <= _MAX_REPORTED_COUNT:
        raise vol.Invalid("reported count is outside the supported range")
    return value


def _telemetry_for(coordinator: Any) -> PanelTelemetry:
    """Return the coordinator-owned panel telemetry, creating it lazily."""
    telemetry = getattr(coordinator, "panel_telemetry", None)
    if isinstance(telemetry, PanelTelemetry):
        return telemetry
    telemetry = PanelTelemetry(_LOGGER)
    coordinator.panel_telemetry = telemetry
    return telemetry


async def _async_panel_operation(
    coordinator: Any,
    operation: str,
    action: Callable[[], Awaitable[Any]],
) -> Any:
    """Run one panel operation while preserving its original exception behavior."""
    telemetry = _telemetry_for(coordinator)
    telemetry.record_request(operation)
    started = time.monotonic()
    try:
        result = await action()
    except asyncio.CancelledError:
        telemetry.record_failure(
            operation,
            category="lifecycle",
            error_type="CancelledError",
            error_code="operation_cancelled",
            duration_seconds=time.monotonic() - started,
        )
        raise
    except Exception as err:
        category, error_type = classify_exception(err)
        telemetry.record_failure(
            operation,
            category=category,
            error_type=error_type,
            error_code="operation_failed",
            duration_seconds=time.monotonic() - started,
        )
        raise
    telemetry.record_success(
        operation,
        duration_seconds=time.monotonic() - started,
    )
    return result


async def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register MeshNet websocket commands."""
    websocket_api.async_register_command(hass, websocket_snapshot)
    websocket_api.async_register_command(hass, websocket_messages)
    websocket_api.async_register_command(hass, websocket_send_message)
    websocket_api.async_register_command(hass, websocket_panel_log)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshnet/snapshot",
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_snapshot(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the current mesh snapshot."""
    coordinator = _get_coordinator(hass)

    async def send_snapshot() -> None:
        connection.send_result(
            msg["id"], _snapshot_with_panel_metadata(hass, coordinator)
        )

    await _async_panel_operation(coordinator, "snapshot", send_snapshot)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshnet/messages",
        vol.Optional("limit", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_messages(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return recent mesh messages."""
    coordinator = _get_coordinator(hass)

    async def send_messages() -> None:
        messages = await coordinator.store.async_recent_messages(msg["limit"])
        connection.send_result(
            msg["id"], [message.as_dict() for message in messages]
        )

    await _async_panel_operation(coordinator, "messages", send_messages)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshnet/send_message",
        vol.Required(ATTR_MESSAGE): str,
        vol.Optional(ATTR_TARGET_NODE): str,
        vol.Optional(ATTR_GATEWAY_ID): str,
        vol.Optional(ATTR_CHANNEL): str,
        vol.Optional(ATTR_PRIORITY, default="normal"): str,
        vol.Optional(ATTR_MESSAGE_TYPE, default=MESSAGE_TYPE_BROADCAST): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_send_message(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Send a mesh message from the panel."""
    coordinator = _get_coordinator(hass)

    async def send_message() -> None:
        message_id = await coordinator.async_send_message(
            target_node=msg.get(ATTR_TARGET_NODE),
            message=msg[ATTR_MESSAGE],
            channel=msg.get(ATTR_CHANNEL),
            priority=msg.get(ATTR_PRIORITY, "normal"),
            message_type=msg.get(ATTR_MESSAGE_TYPE, MESSAGE_TYPE_BROADCAST),
            gateway_id=msg.get(ATTR_GATEWAY_ID),
        )
        connection.send_result(msg["id"], {"message_id": message_id})

    await _async_panel_operation(coordinator, "send_message", send_message)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshnet/panel_log",
        vol.Required("operation"): vol.In(PANEL_OPERATIONS),
        vol.Required("category"): vol.In(PANEL_ERROR_CATEGORIES),
        vol.Required("error_type"): vol.In(PANEL_ERROR_TYPES),
        vol.Required("error_code"): vol.In(PANEL_ERROR_CODES),
        vol.Optional("occurrence", default=1): _bounded_positive_int,
        vol.Optional("consecutive", default=1): _bounded_positive_int,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_panel_log(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Accept one strictly classified, identity-free browser failure report."""
    coordinator = _get_coordinator(hass)
    telemetry = _telemetry_for(coordinator)
    telemetry.record_request("reporting")
    started = time.monotonic()
    try:
        telemetry.record_failure(
            msg["operation"],
            category=msg["category"],
            error_type=msg["error_type"],
            error_code=msg["error_code"],
            occurrence=msg["occurrence"],
            consecutive=msg["consecutive"],
        )
        connection.send_result(msg["id"], {"accepted": True})
    except asyncio.CancelledError:
        telemetry.record_failure(
            "reporting",
            category="lifecycle",
            error_type="CancelledError",
            error_code="operation_cancelled",
            duration_seconds=time.monotonic() - started,
        )
        raise
    except Exception as err:
        # A reporting failure gets one guarded, fixed classification. Never feed
        # caller data or exception text back into this path, and never recurse.
        try:
            _category, error_type = classify_exception(err)
            telemetry.record_failure(
                "reporting",
                category="internal",
                error_type=error_type,
                error_code="report_failed",
                duration_seconds=time.monotonic() - started,
            )
        except Exception:
            pass
        try:
            connection.send_error(
                msg["id"],
                "reporting_failed",
                "MeshNet could not accept the panel failure report",
            )
        except Exception:
            pass
        return
    telemetry.record_success(
        "reporting",
        duration_seconds=time.monotonic() - started,
    )


def _get_coordinator(hass: HomeAssistant) -> MeshNetCoordinator:
    from homeassistant.exceptions import HomeAssistantError

    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("MeshNet is not configured")
    return next(iter(entries.values()))


def _snapshot_with_panel_metadata(
    hass: HomeAssistant, coordinator: MeshNetCoordinator
) -> dict[str, Any]:
    """Return a detached snapshot with privacy-minimal panel preferences."""
    telemetry = _telemetry_for(coordinator)
    snapshot = _panel_snapshot(coordinator)
    favorite_label_configured, favorite_node_keys = _favorite_nodes(
        hass,
        coordinator.entry.entry_id,
        snapshot.get("nodes", {}),
        telemetry=telemetry,
    )

    nodes = snapshot.get("nodes", {})
    if isinstance(nodes, dict):
        for node_key, node in nodes.items():
            if isinstance(node, dict):
                node["favorite"] = node_key in favorite_node_keys

    panel_metadata: dict[str, Any] = {
        "favorite_label_configured": favorite_label_configured,
        "last_snapshot_generated_at": datetime.now(UTC).isoformat(),
        "projection_schema_version": 1,
    }
    provenance_provider = getattr(coordinator, "panel_node_provenance", None)
    if callable(provenance_provider):
        try:
            provenance = provenance_provider()
        except Exception as err:
            category, error_type = classify_exception(err)
            telemetry.record_failure(
                "snapshot_schema",
                category=category,
                error_type=error_type,
                error_code="provenance_failed",
            )
        else:
            safe_provenance = _safe_panel_provenance(provenance)
            if safe_provenance is None:
                telemetry.record_failure(
                    "snapshot_schema",
                    category="data",
                    error_type="SchemaError",
                    error_code="invalid_schema",
                )
            else:
                panel_metadata.update(safe_provenance)
    panel_metadata["telemetry"] = telemetry.snapshot()
    snapshot["panel_metadata"] = panel_metadata
    return snapshot


def _panel_snapshot(coordinator: MeshNetCoordinator) -> dict[str, Any]:
    """Project only fields used by the panel, excluding raw provider state."""
    source = coordinator.snapshot
    node_items = islice(source.nodes.items(), MAX_PANEL_NODES)
    gateway_items = islice(source.gateways.items(), MAX_PANEL_GATEWAYS)
    return {
        "nodes": {
            node_key: _panel_node(node)
            for node_key, node in node_items
        },
        "gateways": {
            gateway_id: {
                "gateway_id": gateway.gateway_id,
                "name": gateway.name,
                "protocol": gateway.protocol,
                "transport": gateway.transport,
                "connected": gateway.connected,
            }
            for gateway_id, gateway in gateway_items
        },
        "recent_messages": [
            {
                "message_id": message.message_id,
                "sender": message.sender,
                "receiver": message.receiver,
                "channel": message.channel,
                "text": message.text,
                "raw": {
                    "status": status,
                }
                if (status := message.raw.get("status")) in {"queued", "sent"}
                else {},
            }
            for message in source.recent_messages[-100:]
        ],
        "mesh_health_score": source.mesh_health_score,
        "messages_today": source.messages_today,
    }


def _panel_node(node: Any) -> dict[str, Any]:
    """Return the bounded node shape required by the sidebar."""
    connectivity = {
        key: node.connectivity[key]
        for key in ("snr", "rssi", "hops", "hops_gateway_id", "via_mqtt")
        if key in node.connectivity
    }
    location = {
        key: node.location[key]
        for key in ("latitude", "longitude")
        if key in node.location
    }
    routing: dict[str, Any] = {}
    for key in ("route", "path"):
        value = node.routing.get(key)
        if isinstance(value, (list, tuple)):
            routing[key] = list(value[:64])
    return {
        "node_key": node.node_key,
        "protocol": node.protocol,
        "node_id": node.node_id,
        "mac": node.mac,
        "public_key": node.public_key,
        "user_name": node.user_name,
        "long_name": node.long_name,
        "short_name": node.short_name,
        "online": node.online,
        "last_heard": (
            node.last_heard.astimezone(UTC).isoformat()
            if node.last_heard is not None
            else None
        ),
        "last_gateway_id": node.last_gateway_id,
        "gateway_ids": sorted(node.gateway_ids),
        "connectivity": connectivity,
        "location": location,
        "routing": routing,
    }


def _safe_panel_provenance(value: Any) -> dict[str, int] | None:
    """Project only exact non-negative node count fields from the coordinator."""
    if not isinstance(value, Mapping):
        return None
    projected: dict[str, int] = {}
    for key in _PANEL_PROVENANCE_KEYS:
        count = value.get(key)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= _MAX_REPORTED_COUNT
        ):
            return None
        projected[key] = count
    return projected


def _favorite_nodes(
    hass: HomeAssistant,
    config_entry_id: str,
    nodes: Any,
    *,
    telemetry: PanelTelemetry | None = None,
) -> tuple[bool, set[str]]:
    """Read favorite flags from one user-owned Home Assistant device label."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import label_registry as lr

    try:
        label_registry = lr.async_get(hass)
        favorite_label = label_registry.async_get_label_by_name(
            _FAVORITE_LABEL_NAME
        )
    except Exception as err:
        if telemetry is not None:
            category, error_type = classify_exception(err)
            telemetry.record_failure(
                "snapshot_schema",
                category=category,
                error_type=error_type,
                error_code="favorite_registry_failed",
            )
        _LOGGER.debug(
            "Unable to read the optional MeshNet favorite label (%s)",
            type(err).__name__,
        )
        return False, set()
    if favorite_label is None:
        return False, set()

    try:
        device_registry = dr.async_get(hass)
    except Exception as err:
        if telemetry is not None:
            category, error_type = classify_exception(err)
            telemetry.record_failure(
                "snapshot_schema",
                category=category,
                error_type=error_type,
                error_code="favorite_registry_failed",
            )
        _LOGGER.debug(
            "Unable to read MeshNet node favorite state (%s)",
            type(err).__name__,
        )
        return True, set()

    if not isinstance(nodes, dict):
        return True, set()

    favorite_node_keys: set[str] = set()
    lookup_failures = 0
    for node_key in nodes:
        if not isinstance(node_key, str):
            continue
        try:
            device = _get_node_device(
                device_registry,
                config_entry_id=config_entry_id,
                node_key=node_key,
            )
            is_favorite = (
                device is not None
                and favorite_label.label_id
                in (getattr(device, "labels", ()) or ())
            )
        except Exception as err:
            lookup_failures += 1
            if telemetry is not None:
                category, error_type = classify_exception(err)
                telemetry.record_failure(
                    "snapshot_schema",
                    category=category,
                    error_type=error_type,
                    error_code="favorite_device_lookup_failed",
                )
            continue
        if is_favorite:
            favorite_node_keys.add(node_key)

    if lookup_failures:
        _LOGGER.debug(
            "Unable to read favorite state for %d MeshNet node device(s)",
            lookup_failures,
        )
    return True, favorite_node_keys


def _get_node_device(
    device_registry: Any,
    *,
    config_entry_id: str,
    node_key: str,
) -> Any:
    """Look up a node across the old and config-entry-scoped registry APIs."""
    scoped_lookup = getattr(
        device_registry, "async_get_device_by_identifier", None
    )
    if callable(scoped_lookup):
        return scoped_lookup((DOMAIN, node_key), config_entry_id)

    legacy_lookup = getattr(device_registry, "async_get_device", None)
    if callable(legacy_lookup):
        return legacy_lookup(identifiers={(DOMAIN, node_key)})
    return None
