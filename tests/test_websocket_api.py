"""Home Assistant-backed tests for MeshNet's privacy-minimal panel snapshot."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402

from custom_components.meshnet.models import MeshSnapshot, NodeState  # noqa: E402
from custom_components.meshnet.websocket_api import (  # noqa: E402
    _FAVORITE_LABEL_NAME,
    _snapshot_with_panel_metadata,
)

ENTRY_ID = "entry-id"
FAVORITE_NODE = "meshtastic:1"
OTHER_NODE = "meshtastic:2"


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id=ENTRY_ID),
        snapshot=MeshSnapshot(
            nodes={
                FAVORITE_NODE: NodeState(
                    node_key=FAVORITE_NODE,
                    protocol="meshtastic",
                ),
                OTHER_NODE: NodeState(
                    node_key=OTHER_NODE,
                    protocol="meshtastic",
                ),
            }
        ),
    )


def test_snapshot_marks_every_node_false_when_favorite_label_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An optional missing label must not hide nodes or access device state."""
    label_lookup = MagicMock(return_value=None)
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=label_lookup,
        ),
    )
    device_registry_get = MagicMock(
        side_effect=AssertionError("device registry must not be read")
    )
    monkeypatch.setattr(dr, "async_get", device_registry_get)
    coordinator = _coordinator()

    result = _snapshot_with_panel_metadata(object(), coordinator)

    assert label_lookup.call_args.args == (_FAVORITE_LABEL_NAME,)
    device_registry_get.assert_not_called()
    assert result["panel_metadata"] == {
        "favorite_label_configured": False,
    }
    assert result["nodes"][FAVORITE_NODE]["favorite"] is False
    assert result["nodes"][OTHER_NODE]["favorite"] is False
    assert not hasattr(coordinator.snapshot.nodes[FAVORITE_NODE], "favorite")


def test_snapshot_reads_favorites_with_legacy_device_lookup_without_leaking_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA 2025.1 through 2026.7 use the legacy identifier lookup."""
    private_label_id = "private-ha-label-id"
    private_device_id = "private-ha-device-id"
    favorite_label = SimpleNamespace(
        label_id=private_label_id,
        name=_FAVORITE_LABEL_NAME,
    )
    label_lookup = MagicMock(return_value=favorite_label)
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=label_lookup,
        ),
    )

    lookup_calls: list[set[tuple[str, str]]] = []

    class LegacyDeviceRegistry:
        def async_get_device(self, *, identifiers):
            lookup_calls.append(identifiers)
            node_key = next(iter(identifiers))[1]
            if node_key != FAVORITE_NODE:
                return None
            return SimpleNamespace(
                id=private_device_id,
                labels={private_label_id},
            )

        def async_update_device(self, *_args, **_kwargs):
            raise AssertionError("favorite projection must remain read-only")

    monkeypatch.setattr(dr, "async_get", lambda _hass: LegacyDeviceRegistry())

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    assert result["panel_metadata"] == {
        "favorite_label_configured": True,
    }
    assert result["nodes"][FAVORITE_NODE]["favorite"] is True
    assert result["nodes"][OTHER_NODE]["favorite"] is False
    assert lookup_calls == [
        {("meshnet", FAVORITE_NODE)},
        {("meshnet", OTHER_NODE)},
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert private_label_id not in serialized
    assert private_device_id not in serialized
    assert _FAVORITE_LABEL_NAME not in serialized


def test_snapshot_prefers_config_entry_scoped_device_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use Home Assistant's scoped identifier API as soon as it is available."""
    favorite_label = SimpleNamespace(label_id="favorite-label")
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=MagicMock(return_value=favorite_label),
        ),
    )

    scoped_calls: list[tuple[tuple[str, str], str]] = []

    class ScopedDeviceRegistry:
        def async_get_device_by_identifier(self, identifier, config_entry_id):
            scoped_calls.append((identifier, config_entry_id))
            labels = (
                {favorite_label.label_id}
                if identifier == ("meshnet", FAVORITE_NODE)
                else set()
            )
            return SimpleNamespace(labels=labels)

        def async_get_device(self, **_kwargs):
            raise AssertionError("legacy lookup must not run when scoped API exists")

        def async_update_device(self, *_args, **_kwargs):
            raise AssertionError("favorite projection must remain read-only")

    monkeypatch.setattr(dr, "async_get", lambda _hass: ScopedDeviceRegistry())

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    assert result["nodes"][FAVORITE_NODE]["favorite"] is True
    assert result["nodes"][OTHER_NODE]["favorite"] is False
    assert scoped_calls == [
        (("meshnet", FAVORITE_NODE), ENTRY_ID),
        (("meshnet", OTHER_NODE), ENTRY_ID),
    ]


def test_snapshot_degrades_safely_when_optional_registry_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A favorites problem must never make the core mesh snapshot unavailable."""
    monkeypatch.setattr(
        lr,
        "async_get",
        MagicMock(side_effect=RuntimeError("private registry detail")),
    )

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    assert result["panel_metadata"] == {
        "favorite_label_configured": False,
    }
    assert all(not node["favorite"] for node in result["nodes"].values())


def test_snapshot_retains_configured_flag_when_device_registry_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient device read failure must not misreport the label as absent."""
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=MagicMock(
                return_value=SimpleNamespace(label_id="favorite-label")
            ),
        ),
    )
    monkeypatch.setattr(
        dr,
        "async_get",
        MagicMock(side_effect=RuntimeError("private device registry detail")),
    )

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    assert result["panel_metadata"] == {
        "favorite_label_configured": True,
    }
    assert all(not node["favorite"] for node in result["nodes"].values())
