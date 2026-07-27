"""Diagnostics support for MeshNet."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.diagnostics import async_redact_data

from .const import DIAGNOSTIC_REDACT, DOMAIN
from .coordinator import MeshNetCoordinator
from .models import NodeState, timestamp_to_json


def _safe_node_diagnostics(node: NodeState) -> dict[str, Any]:
    """Return useful node health fields without identity, content, or location."""
    return {
        "protocol": node.protocol,
        "hardware_model": node.hardware_model,
        "firmware_version": node.firmware_version,
        "radio_type": node.radio_type,
        "role": node.role,
        "online": node.online,
        "last_heard": timestamp_to_json(node.last_heard),
        "gateway_count": len(node.gateway_ids),
        "has_location": bool(node.location),
        "connectivity_field_count": len(node.connectivity),
        "power_field_count": len(node.power),
        "radio_field_count": len(node.radio),
        "sensor_field_count": len(node.sensors),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: MeshNetCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    diagnostics = await coordinator.async_diagnostics() if coordinator else {}
    return async_redact_data(
        {
            "runtime": diagnostics,
        },
        DIAGNOSTIC_REDACT,
    )


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    coordinator: MeshNetCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return {}
    identifiers = {identifier for domain, identifier in device.identifiers if domain == DOMAIN}
    nodes = [
        _safe_node_diagnostics(node)
        for key, node in coordinator.snapshot.nodes.items()
        if key in identifiers
    ]
    return async_redact_data({"nodes": nodes}, DIAGNOSTIC_REDACT)
