"""MeshNet custom integration for Home Assistant."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from .const import (
    ATTR_CHANNEL,
    ATTR_GATEWAY_ID,
    ATTR_MESSAGE,
    ATTR_MESSAGE_TYPE,
    ATTR_PRIORITY,
    ATTR_TARGET_NODE,
    ATTR_WHEN,
    DOMAIN,
    MESSAGE_TYPE_BROADCAST,
    PLATFORMS,
)


_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_MESSAGE = "send_message"
SERVICE_BROADCAST_MESSAGE = "broadcast_message"
SERVICE_SCHEDULE_MESSAGE = "schedule_message"
SERVICE_REFRESH_GATEWAY = "refresh_gateway"


async def async_setup(hass, config: dict[str, Any]) -> bool:
    """Set up the MeshNet integration."""
    from homeassistant import config_entries

    from .websocket_api import async_register_websocket_api

    hass.data.setdefault(DOMAIN, {})
    await async_register_websocket_api(hass)
    await _async_register_panel(hass)
    _async_register_services(hass)
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )
    return True


async def async_setup_entry(hass, entry) -> bool:
    """Set up MeshNet from a config entry."""
    from .coordinator import MeshNetCoordinator

    coordinator = MeshNetCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload MeshNet."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator:
        await coordinator.async_shutdown()
    return unload_ok


async def _async_update_listener(hass, entry) -> None:
    """Reload on options changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass) -> None:
    import voluptuous as vol

    import homeassistant.helpers.config_validation as cv

    from .coordinator import service_fields

    if hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        return

    service_message_schema = vol.Schema(
        {
            vol.Required(ATTR_MESSAGE): cv.string,
            vol.Optional(ATTR_TARGET_NODE): cv.string,
            vol.Optional(ATTR_GATEWAY_ID): cv.string,
            vol.Optional(ATTR_CHANNEL): cv.string,
            vol.Optional(ATTR_PRIORITY, default="normal"): cv.string,
            vol.Optional(ATTR_MESSAGE_TYPE, default=MESSAGE_TYPE_BROADCAST): cv.string,
        }
    )
    service_schedule_schema = vol.Schema(
        {
            vol.Required(ATTR_MESSAGE): cv.string,
            vol.Required(ATTR_WHEN): cv.string,
            vol.Optional(ATTR_TARGET_NODE): cv.string,
            vol.Optional(ATTR_GATEWAY_ID): cv.string,
            vol.Optional(ATTR_CHANNEL): cv.string,
            vol.Optional(ATTR_PRIORITY, default="normal"): cv.string,
            vol.Optional(ATTR_MESSAGE_TYPE, default=MESSAGE_TYPE_BROADCAST): cv.string,
        }
    )
    service_refresh_schema = vol.Schema({vol.Optional(ATTR_GATEWAY_ID): cv.string})

    async def send_message(call) -> None:
        coordinator = _get_coordinator(hass)
        await coordinator.async_send_message(**service_fields(dict(call.data)))

    async def broadcast_message(call) -> None:
        coordinator = _get_coordinator(hass)
        data = dict(call.data)
        data[ATTR_TARGET_NODE] = None
        data[ATTR_MESSAGE_TYPE] = data.get(ATTR_MESSAGE_TYPE, MESSAGE_TYPE_BROADCAST)
        await coordinator.async_send_message(**service_fields(data))

    async def schedule_message(call) -> None:
        coordinator = _get_coordinator(hass)
        data = dict(call.data)
        when = _parse_when(data.pop(ATTR_WHEN))
        delay = max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())

        def _send(_: Any) -> None:
            hass.async_create_task(coordinator.async_send_message(**service_fields(data)))

        from homeassistant.helpers.event import async_call_later

        async_call_later(hass, delay, _send)

    async def refresh_gateway(call) -> None:
        coordinator = _get_coordinator(hass)
        await coordinator.async_gateway_refresh(call.data.get(ATTR_GATEWAY_ID))

    hass.services.async_register(DOMAIN, SERVICE_SEND_MESSAGE, send_message, schema=service_message_schema)
    hass.services.async_register(DOMAIN, SERVICE_BROADCAST_MESSAGE, broadcast_message, schema=service_message_schema)
    hass.services.async_register(DOMAIN, SERVICE_SCHEDULE_MESSAGE, schedule_message, schema=service_schedule_schema)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_GATEWAY, refresh_gateway, schema=service_refresh_schema)


def _get_coordinator(hass):
    from homeassistant.exceptions import HomeAssistantError

    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("MeshNet is not configured")
    return next(iter(entries.values()))


def _parse_when(value: str) -> datetime:
    from homeassistant.exceptions import HomeAssistantError

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise HomeAssistantError(f"Invalid schedule timestamp: {value}") from err
    if parsed.tzinfo is None:
        raise HomeAssistantError("Scheduled message timestamp must include a timezone")
    return parsed


async def _async_register_panel(hass) -> None:
    from homeassistant.components import panel_custom
    from homeassistant.components.http import StaticPathConfig

    frontend_path = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                "/meshnet_static",
                str(frontend_path),
                cache_headers=False,
            )
        ]
    )
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="meshnet-panel",
        frontend_url_path="meshnet",
        module_url="/meshnet_static/meshnet-panel.js",
        sidebar_title="MeshNet",
        sidebar_icon="mdi:radio-tower",
        require_admin=True,
        config={},
    )


async def async_migrate_entry(hass, entry) -> bool:
    """Migrate old config entries."""
    if entry.version == 1:
        return True
    return True
