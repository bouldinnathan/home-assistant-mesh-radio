"""Device tracker platform for MeshNet."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MeshNetCoordinator
from .entities.device_tracker import MeshNetDeviceTracker
from .models import has_valid_location


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MeshNet device trackers."""
    coordinator: MeshNetCoordinator = hass.data[DOMAIN][entry.entry_id]
    seen: set[str] = set()
    node_marks: dict[str, str] = {}

    @callback
    def _forget_removed_nodes(node_keys: frozenset[str]) -> None:
        # Evicted by the rolling window; allow re-creation if heard again.
        for node_key in node_keys:
            unique_id = node_marks.pop(node_key, None)
            if unique_id is not None:
                seen.discard(unique_id)

    @callback
    def _add_missing_trackers() -> None:
        if coordinator.node_cleanup_pending():
            # Home Assistant keeps counting evicted devices' entities until their
            # removal lands, so wait for that cleanup before adding entities.
            return
        entities = []
        for node_key, node in coordinator.data.nodes.items():
            if not has_valid_location(
                node.location,
                zero_pair_is_missing=node.protocol == "meshtastic",
            ):
                continue
            unique_id = f"{entry.entry_id}_{node_key}_location"
            if unique_id in seen:
                continue
            seen.add(unique_id)
            node_marks[node_key] = unique_id
            entities.append(MeshNetDeviceTracker(coordinator, node_key))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_node_removed_listener(_forget_removed_nodes))
    entry.async_on_unload(coordinator.async_add_listener(_add_missing_trackers))
    _add_missing_trackers()
