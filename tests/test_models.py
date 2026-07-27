from __future__ import annotations

from custom_components.meshnet.models import (
    NodeState,
    canonical_node_key,
    merge_dict,
    parse_timestamp,
)


def test_canonical_node_key_prefers_mac() -> None:
    assert canonical_node_key("meshtastic", node_id="123", mac="AA:BB:CC") == "mac:aabbcc"


def test_parse_timestamp_epoch() -> None:
    assert parse_timestamp(0).isoformat() == "1970-01-01T00:00:00+00:00"


def test_node_merge_preserves_existing_values() -> None:
    base = NodeState(
        node_key="meshtastic:1",
        protocol="meshtastic",
        node_id="1",
        long_name="Base",
        power={"battery_level": 55},
        sensors={"temperature": 20},
    )
    update = NodeState(
        node_key="meshtastic:1",
        protocol="meshtastic",
        node_id="1",
        long_name=None,
        power={"voltage": 3.9},
        sensors={"humidity": 40},
    )
    base.merge(update)
    assert base.long_name == "Base"
    assert base.power == {"battery_level": 55, "voltage": 3.9}
    assert base.sensors == {"temperature": 20, "humidity": 40}


def test_merge_dict_skips_none() -> None:
    assert merge_dict({"a": 1, "b": {"x": 1}}, {"a": None, "b": {"y": 2}}) == {
        "a": 1,
        "b": {"x": 1, "y": 2},
    }
