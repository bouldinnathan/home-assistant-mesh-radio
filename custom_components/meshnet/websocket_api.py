"""WebSocket API for the MeshNet panel and automations."""

from __future__ import annotations

import asyncio
import logging
import re
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
    MESSAGE_TYPE_DIRECT,
    MESSAGE_TYPE_EMERGENCY,
    MESSAGE_TYPE_GROUP,
    PROTOCOL_MESHTASTIC,
)
from .coordinator import MeshNetCoordinator, message_api_dict
from .gateway_settings import (
    GatewaySettingsError,
    GatewaySettingsValidationError,
    validate_changes_payload,
)
from .node_identity import (
    meshtastic_identity_is_valid,
    meshtastic_unsafe_identity_keys,
)
from .panel_telemetry import (
    PANEL_ERROR_CATEGORIES,
    PANEL_ERROR_CODES,
    PANEL_ERROR_TYPES,
    PANEL_OPERATIONS,
    PanelTelemetry,
    classify_exception,
)
from .websocket_redaction import send_sensitive_result

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
_OPTIONAL_PANEL_PROVENANCE_KEYS = (
    "retained_node_record_count",
    "collapsed_alias_record_count",
    "resolved_identity_group_count",
    "unresolved_identity_group_count",
    "unresolved_identity_node_count",
    "invalid_identity_record_count",
)
_MAX_REPORTED_COUNT = 1_000_000
_MAX_FAVORITE_ALIASES_PER_NODE = 16


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
    websocket_api.async_register_command(hass, websocket_settings_get)
    websocket_api.async_register_command(hass, websocket_settings_preview)
    websocket_api.async_register_command(hass, websocket_settings_apply)


_GATEWAY_ID = vol.All(str, vol.Length(min=1, max=128))
_SETTINGS_REVISION = vol.Match(r"^[0-9a-f]{64}$")
_PREVIEW_ID = vol.All(str, vol.Length(min=32, max=128))
_MESSAGE_TYPES = (
    MESSAGE_TYPE_DIRECT,
    MESSAGE_TYPE_GROUP,
    MESSAGE_TYPE_BROADCAST,
    MESSAGE_TYPE_EMERGENCY,
)
_MESSAGE_PRIORITIES = ("normal", "high", "emergency")
_SETTINGS_PREVIEW_KEYS = frozenset(
    {"id", "type", ATTR_GATEWAY_ID, "revision", "changes"}
)
_SETTINGS_PREVIEW_ENVELOPE = vol.All(
    # Secret replacements live below caller-selected setting paths, which are
    # not part of Home Assistant's generic websocket redaction-key list.  Keep
    # decorator validation deliberately permissive so a malformed envelope is
    # never rendered (with its secret) by HA's generic voluptuous error logger.
    # The admin-only handler performs the complete strict validation below and
    # returns fixed error text.
    vol.Schema(
        {vol.Required("type"): "meshnet/settings/preview"},
        extra=vol.ALLOW_EXTRA,
    )
)


def _validated_settings_preview_message(
    msg: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Strictly validate a preview after authorization without echoing it."""
    try:
        if set(msg) != _SETTINGS_PREVIEW_KEYS:
            raise ValueError
        gateway_id = msg.get(ATTR_GATEWAY_ID)
        revision = msg.get("revision")
        if not isinstance(gateway_id, str) or not 1 <= len(gateway_id) <= 128:
            raise ValueError
        if not isinstance(revision, str) or re.fullmatch(
            r"[0-9a-f]{64}", revision
        ) is None:
            raise ValueError
        changes = validate_changes_payload(msg.get("changes"))
    except (TypeError, ValueError):
        raise GatewaySettingsValidationError from None
    return gateway_id, revision, changes


def _send_settings_error(
    connection: websocket_api.ActiveConnection,
    message_id: int,
    error: GatewaySettingsError,
) -> None:
    """Return only stable settings errors, never provider exception text."""
    connection.send_error(message_id, error.code, error.public_message)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshnet/settings/get",
        vol.Optional(ATTR_GATEWAY_ID): _GATEWAY_ID,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_settings_get(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return gateway choices and one live, bounded settings schema."""
    coordinator = _get_coordinator(hass)

    async def get_settings() -> None:
        result = await coordinator.async_gateway_settings_get(
            msg.get(ATTR_GATEWAY_ID)
        )
        send_sensitive_result(connection, msg["id"], result)

    try:
        await _async_panel_operation(coordinator, "settings_get", get_settings)
    except GatewaySettingsError as err:
        _send_settings_error(connection, msg["id"], err)
    except Exception:
        connection.send_error(
            msg["id"],
            "settings_error",
            "MeshNet could not load gateway settings",
        )


@websocket_api.websocket_command(_SETTINGS_PREVIEW_ENVELOPE)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_settings_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create a server-side diff for validated typed changes."""
    coordinator = _get_coordinator(hass)

    async def preview_settings() -> None:
        gateway_id, revision, changes = _validated_settings_preview_message(msg)
        result = await coordinator.async_gateway_settings_preview(
            gateway_id=gateway_id,
            revision=revision,
            changes=changes,
        )
        send_sensitive_result(connection, msg["id"], result)

    try:
        await _async_panel_operation(
            coordinator, "settings_preview", preview_settings
        )
    except GatewaySettingsError as err:
        _send_settings_error(connection, msg["id"], err)
    except Exception:
        connection.send_error(
            msg["id"],
            "settings_error",
            "MeshNet could not preview gateway settings",
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshnet/settings/apply",
        vol.Required(ATTR_GATEWAY_ID): _GATEWAY_ID,
        vol.Required("revision"): _SETTINGS_REVISION,
        vol.Required("preview_id"): _PREVIEW_ID,
        vol.Optional("confirm_critical", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_settings_apply(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Consume and apply one unchanged, single-use settings preview."""
    coordinator = _get_coordinator(hass)

    async def apply_settings() -> None:
        result = await coordinator.async_gateway_settings_apply(
            gateway_id=msg[ATTR_GATEWAY_ID],
            revision=msg["revision"],
            preview_id=msg["preview_id"],
            confirm_critical=msg["confirm_critical"],
        )
        send_sensitive_result(connection, msg["id"], result)

    try:
        await _async_panel_operation(
            coordinator, "settings_apply", apply_settings
        )
    except GatewaySettingsError as err:
        _send_settings_error(connection, msg["id"], err)
    except Exception:
        connection.send_error(
            msg["id"],
            "settings_error",
            "MeshNet could not apply gateway settings",
        )


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
        send_sensitive_result(
            connection,
            msg["id"],
            _snapshot_with_panel_metadata(hass, coordinator),
        )

    try:
        await _async_panel_operation(coordinator, "snapshot", send_snapshot)
    except asyncio.CancelledError:
        raise
    except Exception:
        connection.send_error(
            msg["id"],
            "snapshot_failed",
            "MeshNet could not load the panel snapshot",
        )


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
        send_sensitive_result(
            connection,
            msg["id"],
            [message_api_dict(message) for message in messages],
        )

    try:
        await _async_panel_operation(coordinator, "messages", send_messages)
    except asyncio.CancelledError:
        raise
    except Exception:
        connection.send_error(
            msg["id"],
            "messages_failed",
            "MeshNet could not load message history",
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshnet/send_message",
        vol.Required(ATTR_MESSAGE): str,
        vol.Optional(ATTR_TARGET_NODE): vol.All(
            str, vol.Length(min=1, max=128)
        ),
        vol.Optional(ATTR_GATEWAY_ID): _GATEWAY_ID,
        vol.Optional(ATTR_CHANNEL): vol.Any(str, int, float),
        vol.Optional(ATTR_PRIORITY, default="normal"): vol.In(
            _MESSAGE_PRIORITIES
        ),
        vol.Optional(
            ATTR_MESSAGE_TYPE, default=MESSAGE_TYPE_BROADCAST
        ): vol.In(_MESSAGE_TYPES),
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
        send_sensitive_result(
            connection, msg["id"], {"message_id": message_id}
        )

    try:
        await _async_panel_operation(coordinator, "send_message", send_message)
    except asyncio.CancelledError:
        raise
    except Exception:
        connection.send_error(
            msg["id"],
            "send_failed",
            "MeshNet could not submit the message",
        )


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
        node_aliases={
            node_key: coordinator.node_alias_keys(node_key)
            for node_key in snapshot.get("nodes", {})
        }
        if callable(getattr(coordinator, "node_alias_keys", None))
        else None,
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
        "projection_schema_version": 2,
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
    node_items = list(islice(source.nodes.items(), MAX_PANEL_NODES))
    unsafe_node_keys = getattr(
        coordinator, "_unsafe_meshtastic_node_keys", None
    )
    if unsafe_node_keys is None:
        unsafe_node_keys = meshtastic_unsafe_identity_keys(dict(node_items))
    gateway_items = islice(source.gateways.items(), MAX_PANEL_GATEWAYS)
    return {
        "nodes": {
            node_key: _panel_node(
                node,
                identity_valid=node_key not in unsafe_node_keys,
            )
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
                if isinstance(
                    status := message.raw.get("status"), str
                )
                and status in {"blocked", "queued", "sent"}
                else {},
            }
            for message in source.recent_messages[-100:]
        ],
        "mesh_health_score": source.mesh_health_score,
        "messages_today": source.messages_today,
    }


def _panel_node(node: Any, *, identity_valid: bool) -> dict[str, Any]:
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
        "identity_valid": (
            identity_valid
            and meshtastic_identity_is_valid(node.node_key, node)
            if str(node.protocol).strip().casefold() == PROTOCOL_MESHTASTIC
            else True
        ),
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
    for key in _OPTIONAL_PANEL_PROVENANCE_KEYS:
        if key not in value:
            continue
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
    node_aliases: Mapping[str, Any] | None = None,
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
        candidate_keys = [node_key]
        if isinstance(node_aliases, Mapping):
            aliases = node_aliases.get(node_key)
            if isinstance(aliases, (list, tuple, set, frozenset)):
                candidate_keys.extend(
                    alias
                    for alias in islice(
                        aliases, _MAX_FAVORITE_ALIASES_PER_NODE
                    )
                    if isinstance(alias, str)
                )
        candidate_keys = list(dict.fromkeys(candidate_keys))
        try:
            is_favorite = any(
                device is not None
                and favorite_label.label_id
                in (getattr(device, "labels", ()) or ())
                for device in (
                    _get_node_device(
                        device_registry,
                        config_entry_id=config_entry_id,
                        node_key=candidate_key,
                    )
                    for candidate_key in candidate_keys
                )
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
