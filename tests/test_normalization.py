from __future__ import annotations

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
    assert node is not None
    assert node.power["battery_level"] == 91.0


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
