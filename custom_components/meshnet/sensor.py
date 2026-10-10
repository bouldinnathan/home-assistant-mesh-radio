"""Sensor platform for MeshNet."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MeshNetCoordinator
from .entities.sensors import (
    SUMMARY_SENSORS,
    MeshNetSummarySensor,
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
    node_marks: dict[str, set[str]] = {}

    def _remember(node_key: str, mark: str) -> None:
        seen.add(mark)
        node_marks.setdefault(node_key, set()).add(mark)

    @callback
    def _forget_removed_nodes(node_keys: frozenset[str]) -> None:
        # The rolling window removed these devices and entities, so a node
        # that is heard again later must be allowed to create entities again.
        for node_key in node_keys:
            seen.difference_update(node_marks.pop(node_key, set()))

    @callback
    def _add_missing_node_sensors() -> None:
        if coordinator.node_cleanup_pending():
            # Home Assistant keeps counting evicted devices' entities until their
            # removal lands, so wait for that cleanup before adding entities.
            return
        entities = []
        for description in SUMMARY_SENSORS:
            summary = MeshNetSummarySensor(coordinator, description)
            if summary.unique_id not in seen:
                seen.add(summary.unique_id)
                entities.append(summary)
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
            _remember(node_key, fingerprint)
            for entity in sensor_entities_for_node(coordinator, node_key):
                if entity.unique_id not in seen:
                    _remember(node_key, entity.unique_id)
                    entities.append(entity)
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_node_removed_listener(_forget_removed_nodes))
    entry.async_on_unload(coordinator.async_add_listener(_add_missing_node_sensors))
    _add_missing_node_sensors()
