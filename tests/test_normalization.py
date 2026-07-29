from __future__ import annotations

import pytest

from custom_components.meshnet.meshcore_client import meshcore_payload_to_node, meshcore_payload_to_packet
from custom_components.meshnet.meshtastic_client import (
    meshtastic_node_to_state,
    meshtastic_packet_to_node,
    meshtastic_packet_to_state_packet,
)
from custom_components.meshnet.node_identity import project_effective_nodes


def test_meshtastic_packet_normalization() -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "id": 123,
            "from": 1,
            "to": 2,
            "rxSnr": 8.5,
            "hopStart": 3,
            "hopLimit": 3,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text": "hello",
                "telemetry": {
                    "deviceMetrics": {
                        "batteryLevel": 91,
                        "voltage": 4.1,
                    }
                },
            },
        },
        gateway_id="g1",
    )
    node = meshtastic_packet_to_node(packet)

    assert packet.text == "hello"
    assert packet.snr == 8.5
    assert packet.hops == 0
    assert packet.hop_limit == 3
    assert node is not None
    assert node.connectivity["hops"] == 0
    assert node.connectivity["hops_gateway_id"] == "g1"
    assert node.connectivity["via_mqtt"] is False
    assert node.power["battery_level"] == 91.0


@pytest.mark.parametrize(
    ("hop_fields", "expected"),
    [
        ({"hopsAway": 0, "hops": 5}, 0),
        ({"hops_away": "2"}, 2),
        ({"hopStart": 5, "hopLimit": 3}, 2),
        ({"hop_start": 3, "hop_limit": 3}, 0),
        ({"hopStart": 2, "hopLimit": 3}, None),
        ({"hopStart": 0, "hopLimit": 0}, None),
        ({"hopStart": 3}, None),
        ({"hopsAway": 1.5}, None),
        ({"hopsAway": True}, None),
    ],
)
def test_meshtastic_packet_hops_are_passive_and_zero_safe(
    hop_fields: dict[str, object], expected: int | None
) -> None:
    packet = meshtastic_packet_to_state_packet(
        {"from": 1, "to": 2, **hop_fields},
        gateway_id="g1",
    )

    assert packet.hops == expected


@pytest.mark.parametrize(("hops_away", "expected"), [(0, 0), ("2", 2)])
def test_meshtastic_node_db_hops_away_is_normalized_without_losing_zero(
    hops_away: object, expected: int
) -> None:
    node = meshtastic_node_to_state(
        {
            "num": 0x12345678,
            "user": {"id": "!12345678"},
            "hopsAway": hops_away,
        },
        gateway_id="g1",
    )

    assert node.connectivity["hops"] == expected
    assert node.connectivity["hops_gateway_id"] == "g1"


@pytest.mark.parametrize(
    "user",
    [
        {
            "id": "!12345678",
            "userName": " Radio User ",
            "longName": " Hill Repeater ",
            "shortName": " HR ",
            "hwModel": " RAK4631 ",
        },
        {
            "id": "!12345678",
            "username": " Radio User ",
            "long_name": " Hill Repeater ",
            "short_name": " HR ",
            "hw_model": " RAK4631 ",
        },
        {
            "id": "!12345678",
            "user_name": " Radio User ",
            "longname": " Hill Repeater ",
            "shortname": " HR ",
            "hardware": " RAK4631 ",
        },
    ],
)
def test_meshtastic_node_names_accept_provider_key_variants(
    user: dict[str, object],
) -> None:
    node = meshtastic_node_to_state(
        {"num": 0x12345678, "user": user},
        gateway_id="g1",
    )

    assert node.user_name == "Radio User"
    assert node.long_name == "Hill Repeater"
    assert node.short_name == "HR"
    assert node.hardware_model == "RAK4631"


def test_meshtastic_packet_node_names_accept_snake_case_variants() -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 0x12345678,
            "to": 0xFFFFFFFF,
            "decoded": {
                "user": {
                    "id": "!12345678",
                    "user_name": "Packet User",
                    "long_name": "Packet Long Name",
                    "short_name": "PLN",
                    "hw_model": "T-Echo",
                }
            },
        },
        gateway_id="g1",
    )

    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert node.user_name == "Packet User"
    assert node.long_name == "Packet Long Name"
    assert node.short_name == "PLN"
    assert node.hardware_model == "T-Echo"


def test_mqtt_origin_never_becomes_direct_radio_hop_evidence() -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 1,
            "to": 2,
            "hopsAway": 0,
            "decoded": {"user": {"id": "!00000001"}},
        },
        gateway_id="mqtt-gateway",
        topic="msh/US/2/json/LongFast/!00000001",
    )
    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert node.connectivity["hops"] == 0
    assert node.connectivity["via_mqtt"] is True
    assert node.connectivity["hops_gateway_id"] is None


def test_node_db_mqtt_marker_suppresses_direct_radio_hop_provenance() -> None:
    node = meshtastic_node_to_state(
        {
            "num": 1,
            "user": {"id": "!00000001"},
            "hopsAway": 0,
            "viaMqtt": True,
        },
        gateway_id="g1",
    )

    assert node.connectivity["hops"] == 0
    assert node.connectivity["via_mqtt"] is True
    assert node.connectivity["hops_gateway_id"] is None


def test_meshtastic_zero_position_is_not_treated_as_a_gps_fix() -> None:
    node = meshtastic_node_to_state(
        {
            "num": 1,
            "user": {"id": "!00000001"},
            "position": {
                "latitude": 0,
                "longitude": 0,
                "precisionBits": 14,
            },
        },
        gateway_id="g1",
    )

    assert node.location["latitude"] is None
    assert node.location["longitude"] is None
    assert node.location["precision_bits"] == 14
    assert node.location["accuracy"] is None


def test_meshtastic_precision_bits_are_not_reported_as_meter_accuracy() -> None:
    node = meshtastic_node_to_state(
        {
            "num": 1,
            "user": {"id": "!00000001"},
            "position": {
                "latitude": 41.1,
                "longitude": -87.6,
                "precisionBits": 14,
                "accuracy": 22.5,
            },
        },
        gateway_id="g1",
    )

    assert node.location["precision_bits"] == 14
    assert node.location["accuracy"] == 22.5
    assert "precision" not in node.location


def test_meshtastic_official_mqtt_text_normalization() -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "id": 286331153,
            "from": 572662306,
            "to": 4294967295,
            "channel": 0,
            "type": "text",
            "payload": {"text": "hello from mqtt"},
            "rssi": -60,
            "snr": 13.5,
            "timestamp": 1700000000,
        },
        gateway_id="g1",
        topic="msh/US/2/json/LongFast/!22222222",
    )

    assert packet.text == "hello from mqtt"
    assert packet.channel == "0"
    assert packet.portnum == "text"
    assert packet.sender == "572662306"


def test_meshtastic_official_mqtt_nodeinfo_normalization() -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "id": 858993459,
            "channel": 0,
            "from": 305419896,
            "payload": {
                "hardware": 10,
                "id": "!12345678",
                "longname": "base0",
                "shortname": "BA0",
            },
            "sender": "!12345678",
            "timestamp": 1700000000,
            "to": 4294967295,
            "type": "nodeinfo",
        },
        gateway_id="g1",
    )
    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert node.node_id == "!12345678"
    assert node.long_name == "base0"
    assert node.short_name == "BA0"


def test_meshtastic_protobuf_base64_mac_uses_stable_proof_key() -> None:
    node = meshtastic_node_to_state(
        {
            "num": 0x12345678,
            "user": {
                "id": "!12345678",
                # Protobuf MessageToDict representation of aa:bb:cc:dd:ee:ff.
                "macaddr": "qrvM3e7/",
            },
        },
        gateway_id="g1",
    )

    assert node is not None
    assert node.mac == "aabbccddeeff"
    assert node.node_key.startswith("meshtastic-proof:")
    assert node.mac == "aabbccddeeff"


@pytest.mark.parametrize("malformed_mac", [123456, {"value": "aabbccddeeff"}, [1, 2, 3]])
def test_non_scalar_meshtastic_mac_is_omitted_without_creating_phantom_identity(
    malformed_mac: object,
) -> None:
    node = meshtastic_node_to_state(
        {
            "num": 0x12345678,
            "user": {
                "id": "!12345678",
                "macaddr": malformed_mac,
            },
        },
        gateway_id="g1",
    )

    assert node is not None
    assert node.mac is None
    assert node.node_key == "meshtastic:!12345678"


def test_packet_sender_cannot_be_relabelled_by_conflicting_nodeinfo() -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 0x11111111,
            "decoded": {
                "user": {
                    "id": "!22222222",
                    "longName": "Wrong identity",
                    "shortName": "BAD",
                    "macaddr": "22:22:22:22:22:22",
                    "hwModel": "Spoofed",
                }
            },
        },
        gateway_id="g1",
    )

    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert node.node_id == "!11111111"
    assert node.node_key == "meshtastic:!11111111"
    assert node.mac is None
    assert node.long_name is None
    assert node.short_name is None
    assert node.hardware_model is None


def test_node_db_routing_number_rejects_conflicting_user_identity_fields() -> None:
    node = meshtastic_node_to_state(
        {
            "num": 0x11111111,
            "user": {
                "id": "!22222222",
                "longName": "Wrong identity",
                "shortName": "BAD",
                "macaddr": "22:22:22:22:22:22",
            },
        },
        gateway_id="g1",
        fallback_node_id="!11111111",
    )

    assert node is not None
    assert node.node_id == "!11111111"
    assert node.node_key == "meshtastic:!11111111"
    assert node.mac is None
    assert node.long_name is None
    assert node.short_name is None


def test_node_db_conflicting_authoritative_ids_are_skipped() -> None:
    node = meshtastic_node_to_state(
        {
            "num": 0x11111111,
            "user": {"id": "!11111111", "longName": "Do not trust"},
        },
        gateway_id="g1",
        fallback_node_id="!22222222",
    )

    assert node is None


@pytest.mark.parametrize("sender", [0, 0xFFFFFFFF])
def test_invalid_packet_sender_cannot_be_rescued_by_claimed_user_id(
    sender: int,
) -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "from": sender,
            "decoded": {
                "user": {"id": "!22222222", "longName": "Phantom"}
            },
        },
        gateway_id="g1",
    )

    assert meshtastic_packet_to_node(packet) is None


def test_malformed_claim_with_valid_sender_does_not_create_phantom() -> None:
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 0x11111111,
            "decoded": {
                "user": {
                    "id": "not-a-node-id",
                    "longName": "Phantom",
                    "macaddr": "not-a-mac",
                }
            },
        },
        gateway_id="g1",
    )

    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert node.node_id == "!11111111"
    assert node.node_key == "meshtastic:!11111111"
    assert node.long_name is None
    assert node.mac is None


def test_same_routing_id_with_conflicting_macs_stays_separate() -> None:
    left = meshtastic_node_to_state(
        {
            "num": 0x33333333,
            "user": {
                "id": "!33333333",
                "macaddr": "11:11:11:11:11:11",
            },
        },
        gateway_id="g1",
    )
    right = meshtastic_node_to_state(
        {
            "num": 0x33333333,
            "user": {
                "id": "!33333333",
                "macaddr": "22:22:22:22:22:22",
            },
        },
        gateway_id="g1",
    )

    assert left is not None and right is not None
    assert left.node_key != right.node_key
    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )
    assert len(projection.nodes) == 2
    assert projection.stats["unresolved_identity_group_count"] == 1
    assert projection.stats["collapsed_alias_record_count"] == 0


def test_same_routing_id_with_conflicting_public_keys_stays_separate() -> None:
    left = meshtastic_node_to_state(
        {
            "num": 0x33333333,
            "user": {
                "id": "!33333333",
                "publicKey": bytes.fromhex("11" * 32),
            },
        },
        gateway_id="g1",
    )
    right = meshtastic_node_to_state(
        {
            "num": 0x33333333,
            "user": {
                "id": "!33333333",
                "public_key": bytes.fromhex("22" * 32),
            },
        },
        gateway_id="g1",
    )

    assert left is not None and right is not None
    assert left.node_key.startswith("meshtastic-proof:")
    assert right.node_key.startswith("meshtastic-proof:")
    assert left.node_key != right.node_key
    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )
    assert len(projection.nodes) == 2
    assert projection.stats["unresolved_identity_group_count"] == 1


def test_same_id_and_mac_with_conflicting_public_keys_stays_separate() -> None:
    """A preferred MAC key must not erase a public-key conflict pre-projection."""
    left = meshtastic_node_to_state(
        {
            "num": 0x33333333,
            "user": {
                "id": "!33333333",
                "macaddr": "11:11:11:11:11:11",
                "publicKey": bytes.fromhex("11" * 32),
            },
        },
        gateway_id="g1",
    )
    right = meshtastic_node_to_state(
        {
            "num": 0x33333333,
            "user": {
                "id": "!33333333",
                "macaddr": "11:11:11:11:11:11",
                "publicKey": bytes.fromhex("22" * 32),
            },
        },
        gateway_id="g1",
    )

    assert left is not None and right is not None
    assert left.node_key.startswith("meshtastic-proof:")
    assert right.node_key.startswith("meshtastic-proof:")
    assert left.node_key != right.node_key
    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )
    assert len(projection.nodes) == 2
    assert projection.stats["unresolved_identity_group_count"] == 1
    assert projection.stats["collapsed_alias_record_count"] == 0


def test_equivalent_mac_formats_produce_one_raw_identity() -> None:
    colon = meshtastic_node_to_state(
        {
            "num": 0x44444444,
            "user": {
                "id": "!44444444",
                "macaddr": "AA:BB:CC:DD:EE:FF",
            },
        },
        gateway_id="g1",
    )
    hyphen = meshtastic_node_to_state(
        {
            "num": 0x44444444,
            "user": {
                "id": "!44444444",
                "macaddr": "aa-bb-cc-dd-ee-ff",
            },
        },
        gateway_id="g1",
    )

    assert colon is not None and hyphen is not None
    assert colon.node_key == hyphen.node_key
    assert colon.node_key.startswith("meshtastic-proof:")


@pytest.mark.parametrize("proof", ["mac", "public_key"])
def test_shared_strong_proof_across_different_ids_stays_unresolved(
    proof: str,
) -> None:
    """A routing-ID change or cloned proof must remain visible and non-sendable."""
    shared_user_field = (
        {"macaddr": "11:11:11:11:11:11"}
        if proof == "mac"
        else {"publicKey": bytes.fromhex("11" * 32)}
    )
    left = meshtastic_node_to_state(
        {
            "num": 0x11111111,
            "user": {"id": "!11111111", **shared_user_field},
        },
        gateway_id="g1",
    )
    right = meshtastic_node_to_state(
        {
            "num": 0x22222222,
            "user": {"id": "!22222222", **shared_user_field},
        },
        gateway_id="g1",
    )

    assert left is not None and right is not None
    assert left.node_key != right.node_key
    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )
    assert set(projection.nodes) == {left.node_key, right.node_key}
    assert projection.unsafe_node_keys == frozenset(
        {left.node_key, right.node_key}
    )
    assert projection.stats["resolved_identity_group_count"] == 0
    assert projection.stats["unresolved_identity_group_count"] == 2
    assert projection.stats["unresolved_identity_record_count"] == 2


def test_same_name_on_different_ids_never_merges_nodes() -> None:
    left = meshtastic_node_to_state(
        {
            "num": 0x55555555,
            "user": {"id": "!55555555", "longName": "Shared name"},
        },
        gateway_id="g1",
    )
    right = meshtastic_node_to_state(
        {
            "num": 0x66666666,
            "user": {"id": "!66666666", "longName": "Shared name"},
        },
        gateway_id="g1",
    )

    assert left is not None and right is not None
    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )
    assert len(projection.nodes) == 2
    assert projection.stats["candidate_identity_group_count"] == 0


def test_meshcore_packet_normalization() -> None:
    packet = meshcore_payload_to_packet(
        {
            "type": "PACKET",
            "hash": "abc",
            "payload": {
                "sender": "abcdef",
                "message": "hi",
                "channel": 0,
                "snr": 7,
            },
        },
        gateway_id="g1",
    )
    node = meshcore_payload_to_node({"public_key": "abcdef", "name": "MeshCore Node"}, "g1", packet=packet)

    assert packet.packet_id == "abc"
    assert packet.text == "hi"
    assert node is not None
    assert node.long_name == "MeshCore Node"


@pytest.mark.parametrize("malformed_text", [123, {"text": "hidden"}, ["hidden"]])
def test_meshcore_packet_omits_non_string_message_text(
    malformed_text: object,
) -> None:
    packet = meshcore_payload_to_packet(
        {
            "type": "PACKET",
            "payload": {
                "sender": "abcdef",
                "message": malformed_text,
            },
        },
        gateway_id="g1",
    )

    assert packet.text is None
    assert packet.sender == "abcdef"


@pytest.mark.parametrize("malformed_key", [123, {"key": "abcdef"}, ["abcdef"]])
def test_meshcore_node_rejects_non_string_public_key_without_fallback_identity(
    malformed_key: object,
) -> None:
    assert (
        meshcore_payload_to_node(
            {"public_key": malformed_key},
            "g1",
        )
        is None
    )


def test_meshcore_node_omits_non_scalar_device_fields_and_sensors() -> None:
    node = meshcore_payload_to_node(
        {
            "public_key": "abcdef",
            "name": ["not", "a", "name"],
            "long_name": {"private": "value"},
            "short_name": 123,
            "model": ["bad-model"],
            "firmware": {"version": "bad"},
            "role": ["bad-role"],
            "sensors": {
                "valid": 12.5,
                "nested": {"value": 1},
                "sequence": [1, 2],
                7: "bad-key",
            },
            "telemetry": {
                "finite": 4,
                "not_finite": float("nan"),
            },
        },
        "g1",
    )

    assert node is not None
    assert node.node_id is None
    assert node.user_name is None
    assert node.long_name is None
    assert node.short_name is None
    assert node.hardware_model is None
    assert node.firmware_version is None
    assert node.role is None
    assert node.sensors == {"valid": 12.5, "finite": 4}
