"""Sensor platform for MeshNet."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MeshNetCoordinator
from .entities.sensors import (
    MeshNetSummarySensor,
    SUMMARY_SENSORS,
    gateway_sensor_entities_for_gateway,
    sensor_entities_for_node,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MeshNet sensors."""
    coordinator: MeshNetCoordinator = hass.data[DOMAIN][entry.entry_id]
    seen: set[str] = set()
    async_add_entities(MeshNetSummarySensor(coordinator, description) for description in SUMMARY_SENSORS)

    @callback
    def _add_missing_node_sensors() -> None:
        entities = []
        for gateway_id in coordinator.data.gateways:
            for entity in gateway_sensor_entities_for_gateway(coordinator, gateway_id):
                if entity.unique_id not in seen:
                    seen.add(entity.unique_id)
                    entities.append(entity)
        for node_key in coordinator.data.nodes:
            node = coordinator.data.nodes[node_key]
            fingerprint = f"{node_key}:{sorted(node.sensors)}:{sorted(node.power)}:{sorted(node.connectivity)}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            for entity in sensor_entities_for_node(coordinator, node_key):
                if entity.unique_id not in seen:
                    seen.add(entity.unique_id)
                    entities.append(entity)
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_missing_node_sensors))
    _add_missing_node_sensors()
