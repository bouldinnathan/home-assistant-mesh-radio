"""Test-first contract for one-shot Meshtastic BLE RouteDiscovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

LOCAL_NUM = 0x10203040
TARGET_NUM = 0x50607080
TARGET_ID = "!50607080"


def _active_client(*, respond: bool = True, bad_first: bool = False):
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: None,
        admin_response_timeout=0.02,
    )

    class Connection:
        is_connected = True
        owns_endpoint = True

        def __init__(self) -> None:
            self.packets: list[Any] = []

        def reply(
            self,
            request: Any,
            *,
            source: int = TARGET_NUM,
            request_id: int | None = None,
            channel: int | None = None,
        ) -> None:
            route = mesh_pb2.RouteDiscovery()
            route.route.extend([0x11111111, 0x22222222])
            route.route_back.extend([0x33333333])
            route.snr_towards.extend([4, -8])
            route.snr_back.extend([12])
            record = mesh_pb2.FromRadio()
            setattr(record.packet, "from", source)
            record.packet.to = LOCAL_NUM
            record.packet.channel = int(request.channel) if channel is None else channel
            record.packet.decoded.portnum = portnums_pb2.TRACEROUTE_APP
            record.packet.decoded.request_id = int(request.id) if request_id is None else request_id
            record.packet.decoded.payload = route.SerializeToString()
            client._handle_from_radio(record.SerializeToString())

        async def async_send(self, payload: bytes, *, force_read: bool = False) -> None:
            assert force_read is False
            to_radio = mesh_pb2.ToRadio()
            to_radio.ParseFromString(payload)
            assert to_radio.HasField("packet")
            packet = to_radio.packet
            assert packet.to == TARGET_NUM
            assert packet.channel == 0
            assert packet.want_ack is True
            assert packet.decoded.want_response is True
            assert packet.decoded.portnum == portnums_pb2.TRACEROUTE_APP
            route = mesh_pb2.RouteDiscovery()
            route.ParseFromString(bytes(packet.decoded.payload))
            saved = mesh_pb2.MeshPacket()
            saved.CopyFrom(packet)
            self.packets.append(saved)
            if not respond:
                return
            if bad_first:
                self.reply(packet, source=TARGET_NUM + 1)
                self.reply(packet, request_id=int(packet.id) + 1)
                self.reply(packet, channel=1)
            self.reply(packet)

    connection = Connection()
    client._connection = connection  # type: ignore[assignment]
    client._connected = True
    client._my_node_num = LOCAL_NUM
    client._nodes = {
        TARGET_NUM: {
            "num": TARGET_NUM,
            "user": {"id": TARGET_ID},
        }
    }
    return client, connection


def test_ble_traceroute_is_one_exact_correlated_unicast() -> None:
    """Wrong correlation is ignored and one exact response is normalized."""

    async def run() -> None:
        client, connection = _active_client(bad_first=True)

        result = await client.async_manual_traceroute(TARGET_ID)

        assert len(connection.packets) == 1
        assert result["source"] == "!10203040"
        assert result["destination"] == TARGET_ID
        assert result["channel"] == 0
        assert isinstance(result["correlation_id"], str)
        assert result["forward_route"] == [
            "!10203040",
            "!11111111",
            "!22222222",
            TARGET_ID,
        ]
        assert result["reverse_route"] == [
            TARGET_ID,
            "!33333333",
            "!10203040",
        ]
        assert result["snr_towards"] == [1.0, -2.0]
        assert result["snr_back"] == [3.0]

    asyncio.run(run())


def test_ble_traceroute_uses_the_radios_configured_hop_limit() -> None:
    """RouteDiscovery must not silently become a direct-only hop-zero packet."""

    async def run() -> None:
        client, connection = _active_client()
        client._settings._configs["lora"] = SimpleNamespace(hop_limit=5)

        await client.async_manual_traceroute(TARGET_ID)

        assert len(connection.packets) == 1
        assert connection.packets[0].hop_limit == 5

    asyncio.run(run())


def test_ble_traceroute_timeout_is_not_retried() -> None:
    """A timeout has unknown RF outcome and writes exactly once."""

    async def run() -> None:
        client, connection = _active_client(respond=False)

        with pytest.raises(RuntimeError):
            await client.async_manual_traceroute(TARGET_ID)

        assert len(connection.packets) == 1

    asyncio.run(run())


@pytest.mark.parametrize("target", ["^all", "Field Node", "!ffffffff", "!50607081"])
def test_ble_traceroute_rejects_non_exact_or_unknown_target_before_write(
    target: str,
) -> None:
    """The transport independently rejects broadcast, names, and unknown IDs."""

    async def run() -> None:
        client, connection = _active_client()

        with pytest.raises(RuntimeError):
            await client.async_manual_traceroute(target)

        assert connection.packets == []

    asyncio.run(run())
