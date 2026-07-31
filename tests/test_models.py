from __future__ import annotations

from custom_components.meshnet.models import (
    MeshPacket,
    MessageRecord,
    NodeState,
    canonical_node_key,
    coerce_float,
    coerce_int,
    has_valid_location,
    location_accuracy_meters,
    merge_dict,
    parse_timestamp,
)


def test_message_relationship_fields_round_trip_without_provider_raw_data() -> None:
    message = MessageRecord(
        message_id="meshtastic:22",
        protocol="meshtastic",
        gateway_id="gateway-1",
        sender="!12345678",
        receiver="!ffffffff",
        channel="0",
        text="👍",
        reply_to_message_id="meshtastic:11",
        reaction="👍",
    )

    restored = MessageRecord.from_dict(message.as_dict())

    assert restored.reply_to_message_id == "meshtastic:11"
    assert restored.reaction == "👍"
    packet = MeshPacket(
        protocol="meshtastic",
        gateway_id="gateway-1",
        reply_to_message_id="meshtastic:11",
        reaction="👍",
    )
    assert packet.as_dict()["reply_to_message_id"] == "meshtastic:11"
    assert packet.as_dict()["reaction"] == "👍"


def test_numeric_coercion_rejects_booleans_nonfinite_values_and_overflow() -> None:
    assert coerce_float("12.5") == 12.5
    assert coerce_int("12") == 12
    for value in (True, False, float("nan"), float("inf"), float("-inf")):
        assert coerce_float(value) is None
        assert coerce_int(value) is None
    assert coerce_int(1.5) is None


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


def test_meshtastic_hop_evidence_merges_atomically() -> None:
    node = NodeState(
        node_key="meshtastic:1",
        protocol="meshtastic",
        connectivity={
            "hops": 2,
            "hops_gateway_id": "gateway-a",
            "via_mqtt": False,
        },
    )
    node.merge(
        NodeState(
            node_key=node.node_key,
            protocol=node.protocol,
            connectivity={
                "hops": 0,
                "hops_gateway_id": None,
                "via_mqtt": True,
            },
        )
    )

    assert node.connectivity["hops"] == 0
    assert node.connectivity["via_mqtt"] is True
    assert "hops_gateway_id" not in node.connectivity

    node.merge(
        NodeState(
            node_key=node.node_key,
            protocol=node.protocol,
            connectivity={
                "hops": None,
                "hops_gateway_id": None,
                "via_mqtt": False,
            },
        )
    )

    assert node.connectivity["hops"] == 0
    assert node.connectivity["via_mqtt"] is True
    assert "hops_gateway_id" not in node.connectivity


def test_merge_dict_skips_none() -> None:
    assert merge_dict({"a": 1, "b": {"x": 1}}, {"a": None, "b": {"y": 2}}) == {
        "a": 1,
        "b": {"x": 1, "y": 2},
    }


def test_location_requires_finite_in_range_coordinates() -> None:
    assert has_valid_location({"latitude": 41.1, "longitude": -87.6}) is True
    assert has_valid_location({"latitude": "41.1", "longitude": "-87.6"}) is True
    assert has_valid_location({"latitude": 0, "longitude": 0}) is True
    assert (
        has_valid_location(
            {"latitude": 0, "longitude": 0},
            zero_pair_is_missing=True,
        )
        is False
    )
    assert has_valid_location({"latitude": None, "longitude": None}) is False
    assert has_valid_location({"latitude": True, "longitude": 0}) is False
    assert has_valid_location({"latitude": float("nan"), "longitude": 0}) is False
    assert has_valid_location({"latitude": 91, "longitude": 0}) is False
    assert has_valid_location({"latitude": 0, "longitude": 181}) is False


def test_location_accuracy_requires_explicit_finite_meters() -> None:
    assert location_accuracy_meters({"accuracy": 12.5}) == 12.5
    assert location_accuracy_meters({"precision_bits": 14}) == 0.0
    assert location_accuracy_meters({"accuracy": True}) == 0.0
    assert location_accuracy_meters({"accuracy": -1}) == 0.0
    assert location_accuracy_meters({"accuracy": float("inf")}) == 0.0
