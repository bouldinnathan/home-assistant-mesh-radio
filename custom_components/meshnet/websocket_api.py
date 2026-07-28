"""WebSocket API for the MeshNet panel and automations."""

from __future__ import annotations

import logging
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
    MESSAGE_TYPE_BROADCAST,
)
from .coordinator import MeshNetCoordinator

_LOGGER = logging.getLogger(__name__)

_FAVORITE_LABEL_NAME = "MeshNet Favorite"


async def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register MeshNet websocket commands."""
    websocket_api.async_register_command(hass, websocket_snapshot)
    websocket_api.async_register_command(hass, websocket_messages)
    websocket_api.async_register_command(hass, websocket_send_message)


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
    connection.send_result(
        msg["id"], _snapshot_with_panel_metadata(hass, coordinator)
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
    messages = await coordinator.store.async_recent_messages(msg["limit"])
    connection.send_result(msg["id"], [message.as_dict() for message in messages])


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
    message_id = await coordinator.async_send_message(
        target_node=msg.get(ATTR_TARGET_NODE),
        message=msg[ATTR_MESSAGE],
        channel=msg.get(ATTR_CHANNEL),
        priority=msg.get(ATTR_PRIORITY, "normal"),
        message_type=msg.get(ATTR_MESSAGE_TYPE, MESSAGE_TYPE_BROADCAST),
        gateway_id=msg.get(ATTR_GATEWAY_ID),
    )
    connection.send_result(msg["id"], {"message_id": message_id})


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
    snapshot = coordinator.snapshot.as_dict()
    favorite_label_configured, favorite_node_keys = _favorite_nodes(
        hass,
        coordinator.entry.entry_id,
        snapshot.get("nodes", {}),
    )

    nodes = snapshot.get("nodes", {})
    if isinstance(nodes, dict):
        for node_key, node in nodes.items():
            if isinstance(node, dict):
                node["favorite"] = node_key in favorite_node_keys

    snapshot["panel_metadata"] = {
        "favorite_label_configured": favorite_label_configured,
    }
    return snapshot


def _favorite_nodes(
    hass: HomeAssistant,
    config_entry_id: str,
    nodes: Any,
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
        except Exception:
            lookup_failures += 1
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
