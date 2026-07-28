"""Device tracker entities for MeshNet."""

from __future__ import annotations

try:
    from homeassistant.components.device_tracker import TrackerEntity
except ImportError:  # Home Assistant versions before the public re-export.
    from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType

from ..coordinator import node_age_bucket
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
        observed_lookup = getattr(
            self.coordinator, "node_observed_this_session", None
        )
        observed_this_session = bool(
            observed_lookup(node.node_key)
            if callable(observed_lookup)
            else node.node_key
            in getattr(self.coordinator, "_session_observed_node_keys", set())
        )
        via_mqtt = node.connectivity.get("via_mqtt")
        if via_mqtt is not True and via_mqtt is not False:
            via_mqtt = None
        hops = node.connectivity.get("hops")
        if isinstance(hops, bool) or not isinstance(hops, int) or hops < 0:
            hops = None
        attrs.update(
            {
                "altitude": node.location.get("altitude"),
                "speed": node.location.get("speed"),
                "heading": node.location.get("heading"),
                "last_heard": node.last_heard.isoformat() if node.last_heard else None,
                "observed_this_session": observed_this_session,
                "cached_only": not observed_this_session,
                "via_mqtt": via_mqtt,
                "hops": hops,
                "location_freshness": node_age_bucket(node.last_heard),
                "location_freshness_basis": "node_last_heard_proxy",
                "location_timestamp_available": False,
                "location_source_available": False,
                "location_may_be_older_than_node_activity": True,
            }
        )
        return attrs
