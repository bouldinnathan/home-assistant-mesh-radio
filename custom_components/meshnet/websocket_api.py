"""WebSocket API for the MeshNet panel and automations."""

from __future__ import annotations

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
    connection.send_result(msg["id"], coordinator.snapshot.as_dict())


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
