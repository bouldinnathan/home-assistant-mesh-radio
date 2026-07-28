"""Tests for privacy-safe MeshNet tracker provenance attributes."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.meshnet.models import MeshSnapshot, NodeState


def _load_tracker(monkeypatch):
    """Load the tracker against deterministic HA shims.

    The Home Assistant compatibility jobs import the real package before this
    test runs. Always replacing the narrow module surface keeps this unit test
    independent of Home Assistant's global singleton and import state.
    """
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    device_tracker = types.ModuleType("homeassistant.components.device_tracker")
    device_tracker.__path__ = []
    config_entry = types.ModuleType(
        "homeassistant.components.device_tracker.config_entry"
    )
    tracker_const = types.ModuleType("homeassistant.components.device_tracker.const")
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )
    coordinator = types.ModuleType("custom_components.meshnet.coordinator")

    class TrackerEntity:
        pass

    class CoordinatorEntity:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

    class DeviceInfo(dict):
        pass

    config_entry.TrackerEntity = TrackerEntity
    tracker_const.SourceType = SimpleNamespace(GPS="gps")
    device_registry.DeviceInfo = DeviceInfo
    update_coordinator.CoordinatorEntity = CoordinatorEntity
    coordinator.MeshNetCoordinator = object

    def node_age_bucket(last_heard, *, now=None) -> str:
        if last_heard is None:
            return "unknown"
        current = now or datetime.now(UTC)
        return "<15m" if current - last_heard < timedelta(minutes=15) else ">=1d"

    coordinator.node_age_bucket = node_age_bucket

    homeassistant.components = components
    homeassistant.helpers = helpers
    components.device_tracker = device_tracker
    device_tracker.config_entry = config_entry
    device_tracker.const = tracker_const
    helpers.device_registry = device_registry
    helpers.update_coordinator = update_coordinator

    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.device_tracker": device_tracker,
        "homeassistant.components.device_tracker.config_entry": config_entry,
        "homeassistant.components.device_tracker.const": tracker_const,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.update_coordinator": update_coordinator,
        "custom_components.meshnet.coordinator": coordinator,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    for module_name in (
        "custom_components.meshnet.entity",
        "custom_components.meshnet.entities.device_tracker",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    return importlib.import_module(
        "custom_components.meshnet.entities.device_tracker"
    ).MeshNetDeviceTracker


def test_tracker_exposes_passive_provenance_without_new_identity(monkeypatch) -> None:
    tracker_class = _load_tracker(monkeypatch)
    node = NodeState(
        node_key="private-node-key",
        protocol="meshtastic",
        node_id="private-node-id",
        online=False,
        last_heard=datetime.now(UTC),
        connectivity={"via_mqtt": True, "hops": 4},
        location={
            "latitude": 41.0,
            "longitude": -87.0,
            "altitude": 200,
        },
    )
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="private-entry-id"),
        data=MeshSnapshot(nodes={node.node_key: node}),
        node_observed_this_session=lambda node_key: node_key == node.node_key,
    )
    tracker = tracker_class(coordinator, node.node_key)

    attrs = tracker.extra_state_attributes

    assert attrs["observed_this_session"] is True
    assert attrs["cached_only"] is False
    assert attrs["via_mqtt"] is True
    assert attrs["hops"] == 4
    assert attrs["location_freshness"] == "<15m"
    assert attrs["location_freshness_basis"] == "node_last_heard_proxy"
    assert attrs["location_timestamp_available"] is False
    assert attrs["location_source_available"] is False
    assert attrs["location_may_be_older_than_node_activity"] is True
    # These were already exposed by the common node entity before this change.
    assert attrs["node_key"] == "private-node-key"
    assert attrs["node_id"] == "private-node-id"


def test_tracker_preserves_unknown_origin_and_cached_only_state(monkeypatch) -> None:
    tracker_class = _load_tracker(monkeypatch)
    node = NodeState(
        node_key="private-cached-key",
        protocol="meshtastic",
        connectivity={"via_mqtt": "unknown", "hops": True},
        location={"latitude": 1.0, "longitude": 1.0},
    )
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="private-entry-id"),
        data=MeshSnapshot(nodes={node.node_key: node}),
        node_observed_this_session=lambda _node_key: False,
    )
    tracker = tracker_class(coordinator, node.node_key)

    attrs = tracker.extra_state_attributes

    assert attrs["observed_this_session"] is False
    assert attrs["cached_only"] is True
    assert attrs["via_mqtt"] is None
    assert attrs["hops"] is None
    assert attrs["location_freshness"] == "unknown"
