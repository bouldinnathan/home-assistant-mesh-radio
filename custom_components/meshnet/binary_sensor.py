"""Binary sensor platform for MeshNet."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MeshNetCoordinator
from .entities.binary_sensors import binary_entities_for_node, gateway_binary_entities_for_gateway


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MeshNet binary sensors."""
    coordinator: MeshNetCoordinator = hass.data[DOMAIN][entry.entry_id]
    seen: set[str] = set()
    node_marks: dict[str, set[str]] = {}

    @callback
    def _forget_removed_nodes(node_keys: frozenset[str]) -> None:
        # Evicted by the rolling window; allow re-creation if heard again.
        for node_key in node_keys:
            seen.difference_update(node_marks.pop(node_key, set()))

    @callback
    def _add_missing_node_binary_sensors() -> None:
        if coordinator.node_cleanup_pending():
            # Home Assistant keeps counting evicted devices' entities until their
            # removal lands, so wait for that cleanup before adding entities.
            return
        entities = []
        for gateway_id in coordinator.data.gateways:
            for entity in gateway_binary_entities_for_gateway(coordinator, gateway_id):
                if entity.unique_id not in seen:
                    seen.add(entity.unique_id)
                    entities.append(entity)
        for node_key in coordinator.data.nodes:
            for entity in binary_entities_for_node(coordinator, node_key):
                if entity.unique_id not in seen:
                    seen.add(entity.unique_id)
                    node_marks.setdefault(node_key, set()).add(entity.unique_id)
                    entities.append(entity)
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_node_removed_listener(_forget_removed_nodes))
    entry.async_on_unload(coordinator.async_add_listener(_add_missing_node_binary_sensors))
    _add_missing_node_binary_sensors()
