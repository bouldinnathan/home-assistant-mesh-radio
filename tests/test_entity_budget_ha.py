"""Entity budget behaviour that needs the real Home Assistant helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.meshnet import binary_sensor as binary_sensor_platform
from custom_components.meshnet import device_tracker as device_tracker_platform
from custom_components.meshnet import sensor as sensor_platform
from custom_components.meshnet.const import DOMAIN
from custom_components.meshnet.coordinator import MeshNetCoordinator
from custom_components.meshnet.entities.sensors import SUMMARY_SENSORS
from custom_components.meshnet.models import MeshSnapshot


def test_enabled_entity_count_reads_home_assistant_registry_counter(monkeypatch) -> None:
    from homeassistant.helpers import entity_registry as er

    coordinator = object.__new__(MeshNetCoordinator)
    coordinator.hass = SimpleNamespace()
    coordinator.entry = SimpleNamespace(entry_id="entry-1")

    class Entities:
        def get_enabled_count_for_config_entry_id(self, entry_id: str) -> int:
            assert entry_id == "entry-1"
            return 4_321

    monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(entities=Entities()))
    assert coordinator._enabled_entity_count() == 4_321

    # Cores without the counter fall back to counting enabled registry entries.
    entries = [
        SimpleNamespace(disabled_by=None),
        SimpleNamespace(disabled_by="user"),
        SimpleNamespace(disabled_by=None),
    ]
    monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(entities={}))
    monkeypatch.setattr(er, "async_entries_for_config_entry", lambda registry, entry_id: entries)
    assert coordinator._enabled_entity_count() == 2

    def broken(hass):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(er, "async_get", broken)
    assert coordinator._enabled_entity_count() is None

    coordinator.entry = None
    assert coordinator._enabled_entity_count() is None


def _platform_fixture(pending: dict[str, bool]):
    listeners: list = []
    coordinator = SimpleNamespace(
        data=MeshSnapshot(),
        snapshot=MeshSnapshot(),
        entry=SimpleNamespace(entry_id="entry-1", title="Mesh"),
        node_cleanup_pending=lambda: pending["value"],
        async_add_node_removed_listener=lambda listener: (lambda: None),
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    hass = SimpleNamespace(data={DOMAIN: {"entry-1": coordinator}})
    entry = SimpleNamespace(entry_id="entry-1", async_on_unload=lambda unsubscribe: None)
    added: list[list] = []
    return coordinator, hass, entry, listeners, added


@pytest.mark.parametrize(
    ("platform", "expected_after_cleanup"),
    [
        (sensor_platform, len(SUMMARY_SENSORS)),
        (binary_sensor_platform, 0),
        (device_tracker_platform, 0),
    ],
)
def test_platforms_wait_for_node_cleanup_before_adding_entities(platform, expected_after_cleanup) -> None:
    """Home Assistant keeps counting evicted entities until removal lands."""
    pending = {"value": True}
    _coordinator, hass, entry, listeners, added = _platform_fixture(pending)

    asyncio.run(platform.async_setup_entry(hass, entry, lambda entities: added.append(list(entities))))
    assert added == []
    assert len(listeners) == 1

    pending["value"] = False
    listeners[0]()
    assert [len(batch) for batch in added] == ([expected_after_cleanup] if expected_after_cleanup else [])

    listeners[0]()
    assert len(added) == (1 if expected_after_cleanup else 0)
