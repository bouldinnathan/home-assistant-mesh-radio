from __future__ import annotations

import pytest

from custom_components.meshnet.meshcore_client import meshcore_payload_to_node, meshcore_payload_to_packet
from custom_components.meshnet.meshtastic_client import (
    meshtastic_node_to_state,
    meshtastic_packet_to_node,
    meshtastic_packet_to_state_packet,
)


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


def test_meshtastic_protobuf_base64_mac_normalizes_to_hex_node_key() -> None:
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

    assert node.mac == "aabbccddeeff"
    assert node.node_key == "mac:aabbccddeeff"


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
