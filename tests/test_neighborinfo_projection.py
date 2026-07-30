"""Meshtastic passive neighbor-evidence decoding and normalization tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    from custom_components.meshnet.aiomeshtastic.client import MeshtasticBluetoothClient
    from custom_components.meshnet.meshtastic_client import (
        meshtastic_node_to_state,
        meshtastic_packet_to_node,
        meshtastic_packet_to_state_packet,
    )
except ImportError:
    pytest.skip("Meshtastic runtime dependencies are unavailable", allow_module_level=True)


def _client() -> MeshtasticBluetoothClient:
    return MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: SimpleNamespace(),
    )


def test_ble_decoder_projects_neighborinfo_using_the_pinned_protobuf() -> None:
    """The local BLE path decodes documented NeighborInfo, not opaque bytes."""
    neighbor_info = mesh_pb2.NeighborInfo(
        node_id=0x01020304,
        last_sent_by_id=0x05060708,
        node_broadcast_interval_secs=3600,
    )
    neighbor = neighbor_info.neighbors.add()
    neighbor.node_id = 0x11121314
    neighbor.snr = -2.25
    neighbor.last_rx_time = 123

    data = mesh_pb2.Data(
        portnum=portnums_pb2.NEIGHBORINFO_APP,
        payload=neighbor_info.SerializeToString(),
    )
    decoded: dict[str, object] = {}

    _client()._decode_application_payload(data, decoded)

    assert decoded["neighborInfo"] == {
        "nodeId": 0x01020304,
        "lastSentById": 0x05060708,
        "nodeBroadcastIntervalSecs": 3600,
        "neighbors": [
            {
                "nodeId": 0x11121314,
                "snr": -2.25,
                "lastRxTime": 123,
            }
        ],
    }


def test_ble_node_cache_retains_neighborinfo_with_observation_time() -> None:
    """A decoded report remains attached to the exact reporting node."""
    client = _client()
    report = mesh_pb2.NeighborInfo(node_id=0x01020304)
    report.neighbors.add(node_id=0x11121314, snr=4.5)
    packet = mesh_pb2.MeshPacket(
        to=0xFFFFFFFF,
        id=7,
        rx_time=1_700_000_000,
    )
    setattr(packet, "from", 0x01020304)
    packet.decoded.portnum = portnums_pb2.NEIGHBORINFO_APP
    packet.decoded.payload = report.SerializeToString()

    packet_dict = client._packet_to_dict(packet)
    client._update_node_from_packet(packet, packet_dict)

    cached = client.node_snapshot()[0x01020304]
    assert cached["neighborInfo"]["neighbors"] == [
        {"nodeId": 0x11121314, "snr": 4.5}
    ]
    assert cached["neighborInfoUpdatedAt"] == 1_700_000_000


def test_neighborinfo_normalizes_exact_bounded_passive_edges() -> None:
    """Only valid unique neighbor IDs from the matching reporter are projected."""
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 0x01020304,
            "to": 0xFFFFFFFF,
            "rxTime": 1_700_000_000,
            "decoded": {
                "portnum": "NEIGHBORINFO_APP",
                "neighborInfo": {
                    "nodeId": 0x01020304,
                    "neighbors": [
                        {"nodeId": 0x11121314, "snr": -1.25},
                        {"node_id": "!21222324", "snr": 0},
                        {"nodeId": 0x11121314, "snr": 9},
                        {"nodeId": 0},
                        {"nodeId": 0xFFFFFFFF},
                        {"nodeId": 0x01020304},
                        {"nodeId": "not-a-node"},
                    ],
                },
            },
        },
        gateway_id="ble-gateway",
    )

    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert node.routing == {
        "neighbors": ["!11121314", "!21222324"],
        "neighbor_count": 2,
        "neighbors_updated_at": "2023-11-14T22:13:20+00:00",
        "neighbors_via_mqtt": False,
    }


def test_neighborinfo_rejects_a_reporter_identity_mismatch() -> None:
    """A forged or corrupted payload cannot attach edges to another sender."""
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 0x01020304,
            "decoded": {
                "portnum": "NEIGHBORINFO_APP",
                "neighborInfo": {
                    "nodeId": 0xA1A2A3A4,
                    "neighbors": [{"nodeId": 0x11121314}],
                },
            },
        },
        gateway_id="ble-gateway",
    )

    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert node.routing == {}


def test_neighborinfo_is_bounded_and_retains_mqtt_provenance() -> None:
    """Public MQTT observations cannot masquerade as unbounded local evidence."""
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 0x01020304,
            "decoded": {
                "portnum": "NEIGHBORINFO_APP",
                "neighborInfo": {
                    "nodeId": 0x01020304,
                    "neighbors": [
                        {"nodeId": 0x10000000 + index}
                        for index in range(80)
                    ],
                },
            },
        },
        gateway_id="mqtt-gateway",
        topic="msh/US/2/json/LongFast/!01020304",
    )

    node = meshtastic_packet_to_node(packet)

    assert node is not None
    assert len(node.routing["neighbors"]) == 64
    assert node.routing["neighbor_count"] == 64
    assert node.routing["neighbors_via_mqtt"] is True


def test_cached_neighborinfo_uses_its_original_observation_time() -> None:
    """Later node activity must not make retained neighbor evidence look new."""
    node = meshtastic_node_to_state(
        {
            "num": 0x01020304,
            "lastHeard": 1_700_000_100,
            "neighborInfoUpdatedAt": 1_700_000_000,
            "neighborInfo": {
                "nodeId": 0x01020304,
                "neighbors": [{"nodeId": 0x11121314}],
            },
        },
        gateway_id="ble-gateway",
    )

    assert node is not None
    assert node.routing["neighbors"] == ["!11121314"]
    assert node.routing["neighbors_updated_at"] == "2023-11-14T22:13:20+00:00"


def test_numeric_meshtastic_broadcast_destination_is_canonicalized() -> None:
    """Official JSON's uint32 broadcast destination groups as a broadcast."""
    packet = meshtastic_packet_to_state_packet(
        {
            "from": 0x01020304,
            "to": 4_294_967_295,
            "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        },
        gateway_id="mqtt-gateway",
    )

    assert packet.receiver == "^all"
