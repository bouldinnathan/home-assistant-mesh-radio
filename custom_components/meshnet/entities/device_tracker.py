"""Device tracker entities for MeshNet."""

from __future__ import annotations

from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType

from ..entity import MeshNetNodeEntity
from ..models import has_valid_location, location_accuracy_meters


class MeshNetDeviceTracker(MeshNetNodeEntity, TrackerEntity):
    """GPS tracker for a mesh node."""

    _attr_name = "Location"

    def __init__(self, coordinator, node_key: str) -> None:
        super().__init__(coordinator, node_key, "location")

    @property
    def source_type(self) -> SourceType:
        """Return source type."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Return latitude."""
        node = self.node
        if node is None or not has_valid_location(
            node.location,
            zero_pair_is_missing=node.protocol == "meshtastic",
        ):
            return None
        return float(node.location["latitude"])

    @property
    def longitude(self) -> float | None:
        """Return longitude."""
        node = self.node
        if node is None or not has_valid_location(
            node.location,
            zero_pair_is_missing=node.protocol == "meshtastic",
        ):
            return None
        return float(node.location["longitude"])

    @property
    def location_accuracy(self) -> float:
        """Return location accuracy in meters."""
        node = self.node
        if not node:
            return 0.0
        return location_accuracy_meters(node.location)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return tracker attributes."""
        attrs = super().extra_state_attributes
        node = self.node
        if node is None:
            return attrs
        attrs.update(
            {
                "altitude": node.location.get("altitude"),
                "speed": node.location.get("speed"),
                "heading": node.location.get("heading"),
                "last_heard": node.last_heard.isoformat() if node.last_heard else None,
            }
        )
        return attrs
