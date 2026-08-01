"""Meshtastic passive neighbor-evidence decoding and normalization tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

try:
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    from custom_components.meshnet.aiomeshtastic.client import MeshtasticBluetoothClient
    from custom_components.meshnet.aiomeshtastic.errors import (
        MeshtasticNeighborInfoError,
    )
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
        "neighbors_provenance": "passive",
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
    assert len(node.routing["neighbors"]) == 10
    assert node.routing["neighbor_count"] == 10
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


def _active_neighbor_request_client(
    *,
    respond: bool = True,
    via_mqtt: bool = False,
    neighbor_count: int = 1,
    bad_correlations_first: bool = False,
    routing_error: str | None = None,
    response_timeout: float = 0.02,
):
    local_num = 0x10203040
    target_num = 0x50607080
    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: None,
        admin_response_timeout=response_timeout,
    )

    class Connection:
        is_connected = True
        owns_endpoint = True

        def __init__(self) -> None:
            self.packets: list[Any] = []

        async def async_send(self, payload: bytes, *, force_read: bool = False) -> None:
            assert force_read is False
            request = mesh_pb2.ToRadio()
            request.ParseFromString(payload)
            packet = request.packet
            self.packets.append(packet)
            assert packet.to == target_num
            assert getattr(packet, "from") == local_num
            assert packet.channel == 0
            assert packet.want_ack is True
            assert packet.priority == mesh_pb2.MeshPacket.Priority.RELIABLE
            assert packet.hop_limit == 3
            assert packet.decoded.portnum == portnums_pb2.NEIGHBORINFO_APP
            assert packet.decoded.want_response is True
            requested = mesh_pb2.NeighborInfo()
            requested.ParseFromString(bytes(packet.decoded.payload))
            assert len(requested.neighbors) == 1
            assert requested.neighbors[0].node_id == 0
            assert requested.neighbors[0].snr == 0
            if not respond:
                return

            def deliver_neighbor(
                *,
                source: int = target_num,
                destination: int = local_num,
                channel: int = 0,
                request_id: int | None = None,
                reporter: int = target_num,
                mqtt: bool = False,
            ) -> None:
                report = mesh_pb2.NeighborInfo(
                    node_id=reporter,
                    last_sent_by_id=reporter,
                    node_broadcast_interval_secs=3600,
                )
                for index in range(neighbor_count):
                    report.neighbors.add(
                        node_id=0x11121314 + index,
                        snr=-2.25 + index,
                    )
                response = mesh_pb2.FromRadio()
                setattr(response.packet, "from", source)
                response.packet.to = destination
                response.packet.channel = channel
                response.packet.id = 44
                response.packet.rx_time = 1_700_000_000
                response.packet.via_mqtt = mqtt
                response.packet.decoded.portnum = portnums_pb2.NEIGHBORINFO_APP
                response.packet.decoded.request_id = (
                    int(packet.id) if request_id is None else request_id
                )
                response.packet.decoded.payload = report.SerializeToString()
                client._handle_from_radio(response.SerializeToString())

            def deliver_routing_error(
                *,
                reason: str = "NO_ROUTE",
                destination: int = local_num,
                mqtt: bool = False,
            ) -> None:
                routing = mesh_pb2.Routing()
                routing.error_reason = mesh_pb2.Routing.Error.Value(reason)
                response = mesh_pb2.FromRadio()
                setattr(response.packet, "from", target_num)
                response.packet.to = destination
                response.packet.channel = 0
                response.packet.via_mqtt = mqtt
                response.packet.decoded.portnum = portnums_pb2.ROUTING_APP
                response.packet.decoded.request_id = int(packet.id)
                response.packet.decoded.payload = routing.SerializeToString()
                client._handle_from_radio(response.SerializeToString())

            if bad_correlations_first:
                deliver_neighbor(request_id=int(packet.id) + 1)
                deliver_neighbor(source=target_num + 1)
                deliver_neighbor(destination=local_num + 1)
                deliver_neighbor(channel=1)
                deliver_neighbor(reporter=target_num + 1)
                deliver_routing_error(destination=local_num + 1)
                deliver_routing_error(mqtt=True)
            if routing_error is not None:
                deliver_routing_error(reason=routing_error)
                return
            deliver_neighbor(mqtt=via_mqtt)

    connection = Connection()
    client._connection = connection  # type: ignore[assignment]
    client._connected = True
    client._my_node_num = local_num
    client._settings._configs["lora"] = SimpleNamespace(hop_limit=3)
    client._nodes = {
        local_num: {"num": local_num},
        target_num: {
            "num": target_num,
            "user": {"id": "!50607080", "shortName": "TEST"},
        },
    }
    return client, connection


def test_manual_neighbor_info_request_is_one_correlated_exact_unicast() -> None:
    """One explicit request returns and passively projects one bounded report."""

    async def run() -> None:
        client, connection = _active_neighbor_request_client()

        result = await client.async_manual_neighbor_info("!50607080")

        assert len(connection.packets) == 1
        assert result == {
            "correlation_id": str(connection.packets[0].id),
            "source": "!50607080",
            "destination": "!10203040",
            "channel": 0,
            "node_broadcast_interval_secs": 3600,
            "neighbors": [{"node_id": "!11121314", "snr": -2.25}],
        }
        assert client.node_snapshot()[0x50607080]["neighborInfo"] == {
            "nodeId": 0x50607080,
            "lastSentById": 0x50607080,
            "nodeBroadcastIntervalSecs": 3600,
            "neighbors": [{"nodeId": 0x11121314, "snr": -2.25}],
        }
        assert (
            client.node_snapshot()[0x50607080]["neighborInfoProvenance"]
            == "manual_request"
        )
        projected = meshtastic_node_to_state(
            client.node_snapshot()[0x50607080], gateway_id="ble-gateway"
        )
        assert projected is not None
        assert projected.routing["neighbors_provenance"] == "manual_request"

    asyncio.run(run())


def test_manual_neighbor_info_rejects_mqtt_response_and_inconsistent_target() -> None:
    """MQTT cannot satisfy a local request and cached identity must agree."""

    async def run() -> None:
        mqtt_client, mqtt_connection = _active_neighbor_request_client(
            via_mqtt=True
        )
        with pytest.raises(MeshtasticNeighborInfoError) as timed_out:
            await mqtt_client.async_manual_neighbor_info("!50607080")
        assert timed_out.value.code == "neighbor_info_timeout"
        assert len(mqtt_connection.packets) == 1

        bad_client, bad_connection = _active_neighbor_request_client()
        bad_client._nodes[0x50607080]["user"]["id"] = "!99999999"
        with pytest.raises(RuntimeError, match="identity is inconsistent"):
            await bad_client.async_manual_neighbor_info("!50607080")
        assert bad_connection.packets == []

        missing_client, missing_connection = _active_neighbor_request_client()
        missing_client._nodes[0x50607080]["user"].pop("id")
        with pytest.raises(RuntimeError, match="identity is inconsistent"):
            await missing_client.async_manual_neighbor_info("!50607080")
        assert missing_connection.packets == []

    asyncio.run(run())


def test_manual_neighbor_info_response_is_capped_at_firmware_maximum() -> None:
    """Even oversized provider protobufs expose at most ten neighbors."""

    async def run() -> None:
        client, _connection = _active_neighbor_request_client(neighbor_count=12)
        result = await client.async_manual_neighbor_info("!50607080")
        assert len(result["neighbors"]) == 10

    asyncio.run(run())


def test_manual_neighbor_info_ignores_every_mismatched_correlation() -> None:
    """Only exact local RF response and routing envelopes settle the waiter."""

    async def run() -> None:
        client, connection = _active_neighbor_request_client(
            bad_correlations_first=True
        )
        result = await client.async_manual_neighbor_info("!50607080")
        assert result["source"] == "!50607080"
        assert result["correlation_id"] == str(connection.packets[0].id)

    asyncio.run(run())


def test_unsolicited_neighbor_info_replaces_manual_provenance_with_passive() -> None:
    """A later passive report cannot inherit a previous manual correlation."""

    async def run() -> None:
        client, _connection = _active_neighbor_request_client()
        await client.async_manual_neighbor_info("!50607080")
        assert (
            client.node_snapshot()[0x50607080]["neighborInfoProvenance"]
            == "manual_request"
        )

        passive_report = mesh_pb2.NeighborInfo(
            node_id=0x50607080,
            last_sent_by_id=0x50607080,
            node_broadcast_interval_secs=3600,
        )
        passive_report.neighbors.add(node_id=0x21222324, snr=-1.5)
        response = mesh_pb2.FromRadio()
        setattr(response.packet, "from", 0x50607080)
        response.packet.to = 0xFFFFFFFF
        response.packet.channel = 0
        response.packet.id = 45
        response.packet.rx_time = 1_700_000_001
        response.packet.decoded.portnum = portnums_pb2.NEIGHBORINFO_APP
        response.packet.decoded.payload = passive_report.SerializeToString()
        client._handle_from_radio(response.SerializeToString())

        snapshot = client.node_snapshot()[0x50607080]
        assert snapshot["neighborInfoProvenance"] == "passive"
        projected = meshtastic_node_to_state(
            snapshot, gateway_id="ble-gateway"
        )
        assert projected is not None
        assert projected.routing["neighbors_provenance"] == "passive"
        assert projected.routing["neighbors"] == ["!21222324"]

    asyncio.run(run())


def test_manual_neighbor_info_timeout_is_never_retried() -> None:
    """An unanswered request sends once and leaves no pending response owner."""

    async def run() -> None:
        client, connection = _active_neighbor_request_client(respond=False)

        with pytest.raises(MeshtasticNeighborInfoError) as timed_out:
            await client.async_manual_neighbor_info("!50607080")
        assert timed_out.value.code == "neighbor_info_timeout"

        assert len(connection.packets) == 1
        assert client._pending_neighbor_info_responses == {}
        diagnostics = client.diagnostic_snapshot()
        assert diagnostics["neighbor_info_timeout_count"] == 1
        assert diagnostics["neighbor_info_rejection_count"] == 0
        assert diagnostics["neighbor_info_cancellation_count"] == 0
        assert diagnostics["last_neighbor_info_outcome"] == "timed_out"

    asyncio.run(run())


def test_manual_neighbor_info_routing_rejection_is_exact_and_diagnostic() -> None:
    """A correlated firmware NAK exposes only its stable enum and is not retried."""

    async def run() -> None:
        client, connection = _active_neighbor_request_client(
            routing_error="BAD_REQUEST"
        )

        with pytest.raises(MeshtasticNeighborInfoError) as rejected:
            await client.async_manual_neighbor_info("!50607080")
        assert rejected.value.code == "neighbor_info_unsupported"

        assert len(connection.packets) == 1
        assert client._pending_neighbor_info_responses == {}
        diagnostics = client.diagnostic_snapshot()
        assert diagnostics["neighbor_info_rejection_count"] == 1
        assert diagnostics["neighbor_info_timeout_count"] == 0
        assert diagnostics["neighbor_info_cancellation_count"] == 0
        assert diagnostics["last_neighbor_info_outcome"] == "rejected"
        assert diagnostics["last_neighbor_info_routing_error"] == "BAD_REQUEST"
        assert diagnostics["neighbor_info_routing_error_counts"] == {
            "BAD_REQUEST": 1
        }

    asyncio.run(run())


def test_manual_neighbor_info_cancellation_is_distinct_and_never_retried() -> None:
    """Caller cancellation tears down the sole waiter without another RF send."""

    async def run() -> None:
        client, connection = _active_neighbor_request_client(
            respond=False,
            response_timeout=30.0,
        )
        task = asyncio.create_task(
            client.async_manual_neighbor_info("!50607080")
        )
        for _ in range(10):
            if connection.packets:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(connection.packets) == 1
        assert client._pending_neighbor_info_responses == {}
        diagnostics = client.diagnostic_snapshot()
        assert diagnostics["neighbor_info_cancellation_count"] == 1
        assert diagnostics["neighbor_info_timeout_count"] == 0
        assert diagnostics["neighbor_info_rejection_count"] == 0
        assert diagnostics["last_neighbor_info_outcome"] == "cancelled"

    asyncio.run(run())


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
