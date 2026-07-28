"""MeshNet custom integration for Home Assistant."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
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
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    CONF_BLUETOOTH_BOND_MANAGED,
    CONF_GATEWAYS,
    DOMAIN,
    MESSAGE_TYPE_BROADCAST,
    PLATFORMS,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_MESSAGE = "send_message"
SERVICE_BROADCAST_MESSAGE = "broadcast_message"
SERVICE_SCHEDULE_MESSAGE = "schedule_message"
SERVICE_REFRESH_GATEWAY = "refresh_gateway"
_SCHEDULED_CANCELS_ATTR = "_meshnet_scheduled_message_cancels"
_SCHEDULED_UNLOAD_REGISTERED_ATTR = "_meshnet_scheduled_unload_registered"


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
    forwarding_started = False
    try:
        await coordinator.async_config_entry_first_refresh()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        forwarding_started = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        coordinator.async_start_gateways_background()
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    except BaseException:
        # A config entry that never finishes setup is not guaranteed to receive
        # async_unload_entry. Roll back every resource opened by first refresh,
        # while preserving the original setup exception for Home Assistant.
        domain_data = hass.data.get(DOMAIN)
        owns_registration = (
            domain_data is not None
            and domain_data.get(entry.entry_id) is coordinator
        )
        if forwarding_started and owns_registration:
            try:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            except BaseException as err:
                _LOGGER.warning(
                    "Failed to roll back MeshNet platforms after setup error: %s",
                    err,
                )
        _cancel_scheduled_messages(coordinator)
        try:
            await coordinator.async_shutdown()
        except BaseException as err:
            _LOGGER.warning(
                "Failed to finish MeshNet coordinator rollback after setup error: %s",
                err,
            )
        domain_data = hass.data.get(DOMAIN)
        if (
            domain_data is not None
            and domain_data.get(entry.entry_id) is coordinator
        ):
            domain_data.pop(entry.entry_id, None)
        raise
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload MeshNet."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    domain_data = hass.data.get(DOMAIN, {})
    coordinator = domain_data.get(entry.entry_id)
    if coordinator is not None:
        _cancel_scheduled_messages(coordinator)
        await coordinator.async_shutdown()
        if domain_data.get(entry.entry_id) is coordinator:
            domain_data.pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass, entry) -> None:
    """Preserve external Bluetooth state when a config entry is deleted.

    BlueZ exposes no bond-generation identifier.  An address stored when
    MeshNet paired a radio may later name a bond another client recreated, so
    unattended entry removal must never call ``RemoveDevice``.  Users who
    intentionally want address-scoped BlueZ cleanup can request it in the
    confirmed Configure -> Remove gateway flow before deleting the entry.
    """
    del hass, entry


async def _async_update_listener(hass, entry) -> None:
    """Reload on options changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass) -> None:
    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol

    from .coordinator import service_fields

    if hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        return

    service_message_schema = vol.Schema(
        {
            vol.Required(ATTR_MESSAGE): cv.string,
            vol.Optional(ATTR_TARGET_NODE): _coerce_target_node,
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
            vol.Optional(ATTR_TARGET_NODE): _coerce_target_node,
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
        _schedule_message_call(
            hass,
            coordinator,
            dict(call.data),
            service_fields=service_fields,
        )

    async def refresh_gateway(call) -> None:
        coordinator = _get_coordinator(hass)
        await coordinator.async_gateway_refresh(call.data.get(ATTR_GATEWAY_ID))

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_MESSAGE, send_message, schema=service_message_schema
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BROADCAST_MESSAGE,
        broadcast_message,
        schema=service_message_schema,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCHEDULE_MESSAGE,
        schedule_message,
        schema=service_schedule_schema,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_GATEWAY,
        refresh_gateway,
        schema=service_refresh_schema,
    )


def _coerce_target_node(value: Any) -> str:
    """Normalize YAML node numbers without accepting booleans or fractions."""
    if isinstance(value, bool):
        raise ValueError("target_node must be a node number or non-empty string")
    if isinstance(value, str):
        target = value.strip()
        if not target:
            raise ValueError("target_node must not be empty")
        return target
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    raise ValueError("target_node must be a node number or non-empty string")


def _scheduled_message_cancels(coordinator: Any) -> set[Callable[[], None]]:
    """Return the cancellation callbacks owned by one config entry."""
    callbacks = getattr(coordinator, _SCHEDULED_CANCELS_ATTR, None)
    if callbacks is None:
        callbacks = set()
        setattr(coordinator, _SCHEDULED_CANCELS_ATTR, callbacks)
    return callbacks


def _cancel_scheduled_messages(coordinator: Any) -> None:
    """Synchronously prevent every pending timer from crossing entry unload."""
    callbacks = _scheduled_message_cancels(coordinator)
    failure_count = 0
    for cancel in tuple(callbacks):
        try:
            cancel()
        except Exception:
            # Home Assistant does not isolate individual async_on_unload
            # callbacks. Continue fencing the remaining timers so one broken
            # cancellation cannot leave the entry partially unloaded.
            failure_count += 1
    callbacks.clear()
    if failure_count:
        _LOGGER.warning(
            "%d scheduled MeshNet timer cancellation callback(s) failed",
            failure_count,
        )


def _schedule_message_call(
    hass: Any,
    coordinator: Any,
    data: dict[str, Any],
    *,
    service_fields: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Schedule one entry-owned send and retain its timer cancellation."""
    from homeassistant.helpers.event import async_call_later

    when = _parse_when(data.pop(ATTR_WHEN))
    delay = max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())
    callbacks = _scheduled_message_cancels(coordinator)
    active = True

    def _send(_: Any) -> None:
        nonlocal active
        if not active:
            return
        active = False
        callbacks.discard(cancel)
        coordinator.entry.async_create_background_task(
            hass,
            coordinator.async_send_message(**service_fields(data)),
            "MeshNet scheduled message",
        )

    cancel_timer = async_call_later(hass, delay, _send)

    def cancel() -> None:
        nonlocal active
        if not active:
            return
        active = False
        cancel_timer()
        callbacks.discard(cancel)

    callbacks.add(cancel)
    if not getattr(coordinator, _SCHEDULED_UNLOAD_REGISTERED_ATTR, False):
        try:
            coordinator.entry.async_on_unload(
                lambda: _cancel_scheduled_messages(coordinator)
            )
        except Exception:
            try:
                cancel()
            except Exception:
                callbacks.discard(cancel)
                _LOGGER.warning(
                    "A scheduled MeshNet timer could not be canceled after "
                    "unload registration failed"
                )
            raise
        setattr(coordinator, _SCHEDULED_UNLOAD_REGISTERED_ATTR, True)


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
        module_url=f"/meshnet_static/meshnet-panel.js?v={VERSION}",
        sidebar_title="MeshNet",
        sidebar_icon="mdi:radio-tower",
        require_admin=True,
        config={},
    )


async def async_migrate_entry(hass, entry) -> bool:
    """Remove deletion authority from entries created before version 0.4."""
    if entry.version != 1:
        return False
    if entry.minor_version >= 2:
        return True

    data = _strip_legacy_bluetooth_ownership(entry.data)
    options = _strip_legacy_bluetooth_ownership(entry.options)
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        minor_version=2,
    )
    return True


def _strip_legacy_bluetooth_ownership(values: Any) -> dict[str, Any]:
    """Strip v0.4-only trust metadata from an older entry mapping."""
    migrated = deepcopy(dict(values))
    gateways = migrated.get(CONF_GATEWAYS)
    if not isinstance(gateways, list):
        return migrated
    cleaned_gateways: list[Any] = []
    for gateway in gateways:
        if not isinstance(gateway, dict):
            cleaned_gateways.append(gateway)
            continue
        cleaned = deepcopy(gateway)
        gateway_options = dict(cleaned.get("options") or {})
        gateway_options.pop(CONF_BLUETOOTH_BOND_MANAGED, None)
        gateway_options.pop(CONF_BLUETOOTH_ADAPTER, None)
        gateway_options.pop(CONF_BLUETOOTH_ADAPTER_ADDRESS, None)
        if gateway_options:
            cleaned["options"] = gateway_options
        else:
            cleaned.pop("options", None)
        cleaned_gateways.append(cleaned)
    migrated[CONF_GATEWAYS] = cleaned_gateways
    return migrated
