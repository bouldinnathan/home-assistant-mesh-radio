"""Device tracker platform for MeshNet."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MeshNetCoordinator
from .entities.device_tracker import MeshNetDeviceTracker


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MeshNet device trackers."""
    coordinator: MeshNetCoordinator = hass.data[DOMAIN][entry.entry_id]
    seen: set[str] = set()

    @callback
    def _add_missing_trackers() -> None:
        entities = []
        for node_key, node in coordinator.data.nodes.items():
            if node.location.get("latitude") is None or node.location.get("longitude") is None:
                continue
            unique_id = f"{entry.entry_id}_{node_key}_location"
            if unique_id in seen:
                continue
            seen.add(unique_id)
            entities.append(MeshNetDeviceTracker(coordinator, node_key))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_missing_trackers))
    _add_missing_trackers()
