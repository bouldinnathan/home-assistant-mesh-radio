"""Focused lifecycle tests for the isolated async Meshtastic stack."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.meshnet.aiomeshtastic.bluetooth import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    MESHTASTIC_SERVICE_UUID,
    TORADIO_UUID,
    BluetoothConnection,
)
from custom_components.meshnet.aiomeshtastic.errors import (
    MeshtasticCleanupError,
    MeshtasticConnectionError,
)


class _Services:
    def __init__(self, *, missing: str | None = None) -> None:
        self._characteristics = {
            FROMRADIO_UUID: "from-radio",
            TORADIO_UUID: "to-radio",
            FROMNUM_UUID: "from-num",
        }
        if missing is not None:
            self._characteristics.pop(missing)

    def get_service(self, uuid: str) -> object | None:
        return object() if uuid == MESHTASTIC_SERVICE_UUID else None

    def get_characteristic(self, uuid: str) -> object | None:
        return self._characteristics.get(uuid)


class _GattClient:
    def __init__(self, *, missing: str | None = None) -> None:
        self.services = _Services(missing=missing)
        self.is_connected = True
        self.notifications: dict[object, Any] = {}
        self.read_values: asyncio.Queue[bytes] = asyncio.Queue()
        self.read_count = 0
        self.writes: list[bytes] = []
        self.disconnect_gate = asyncio.Event()
        self.disconnect_gate.set()

    async def start_notify(self, characteristic: object, callback: Any) -> None:
        self.notifications[characteristic] = callback

    async def stop_notify(self, characteristic: object) -> None:
        self.notifications.pop(characteristic, None)

    async def read_gatt_char(self, _characteristic: object) -> bytes:
        self.read_count += 1
        return await self.read_values.get()

    async def write_gatt_char(
        self,
        _characteristic: object,
        payload: bytes,
        *,
        response: bool = True,
    ) -> None:
        assert response is True
        self.writes.append(bytes(payload))

    async def disconnect(self) -> None:
        await self.disconnect_gate.wait()
        self.is_connected = False


def _connection(client: _GattClient, **kwargs: Any) -> BluetoothConnection:
    async def connector(_device: object, _callback: Any) -> _GattClient:
        return client

    return BluetoothConnection(
        address="AA:BB:CC:DD:EE:FF",
        ble_device=object(),
        connector=connector,
        connect_timeout=0.2,
        notify_timeout=0.2,
        io_timeout=0.2,
        disconnect_timeout=kwargs.pop("disconnect_timeout", 0.2),
        idle_read_timeout=0.2,
        **kwargs,
    )


def test_force_read_wakes_reader_only_after_write() -> None:
    async def run() -> None:
        client = _GattClient()
        connection = _connection(client)
        await connection.async_connect()
        await client.read_values.put(b"")
        expected = b"from-radio-record"
        await client.read_values.put(expected)

        stream = connection.packet_stream()
        read = asyncio.create_task(anext(stream))
        while client.read_count < 1:
            await asyncio.sleep(0)
        await connection.async_send(b"want-config", force_read=True)

        assert await read == expected
        assert client.writes == [b"want-config"]
        assert connection.diagnostic_snapshot()["forced_read_count"] == 1
        await stream.aclose()
        await connection.async_disconnect()

    asyncio.run(run())


def test_profile_failure_disconnects_and_drops_ownership() -> None:
    async def run() -> None:
        client = _GattClient(missing=TORADIO_UUID)
        connection = _connection(client)

        with pytest.raises(MeshtasticConnectionError):
            await connection.async_connect()

        assert client.is_connected is False
        assert connection.owns_endpoint is False
        diagnostics = connection.diagnostic_snapshot()
        assert diagnostics["state"] == "failed"
        assert diagnostics["last_failure_phase"] == "validating_profile"

    asyncio.run(run())


def test_read_timeout_retains_from_radio_failure_phase() -> None:
    async def run() -> None:
        client = _GattClient()
        connection = _connection(client)
        await connection.async_connect()
        await connection.async_send(b"want-config", force_read=True)

        stream = connection.packet_stream()
        with pytest.raises(MeshtasticConnectionError, match="read failed"):
            await anext(stream)

        diagnostics = connection.diagnostic_snapshot()
        assert diagnostics["last_error_type"] == "TimeoutError"
        assert diagnostics["last_failure_phase"] == "reading_from_radio"
        await stream.aclose()
        await connection.async_disconnect()

    asyncio.run(run())


def test_failed_session_retains_identity_free_connection_diagnostics() -> None:
    pytest.importorskip("meshtastic")
    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    class FailingConnection:
        is_connected = False
        owns_endpoint = False

        async def async_connect(self) -> None:
            raise MeshtasticConnectionError("test connection failure")

        async def async_disconnect(self) -> None:
            return None

        def diagnostic_snapshot(self) -> dict[str, Any]:
            return {
                "state": "failed",
                "last_error_type": "BleakDBusError",
                "connected": False,
            }

    async def run() -> None:
        client = MeshtasticBluetoothClient(
            address="AA:BB:CC:DD:EE:FF",
            device_provider=lambda: SimpleNamespace(),
            connection_factory=lambda **_kwargs: FailingConnection(),
            connect_timeout=0.2,
            configuration_timeout=0.2,
            io_timeout=0.2,
            disconnect_timeout=0.2,
            stop_timeout=0.5,
            heartbeat_interval=60,
        )

        with pytest.raises(MeshtasticConnectionError, match="test connection failure"):
            await client.async_start()

        diagnostics = client.diagnostic_snapshot()
        assert diagnostics["connect_attempts"] == 1
        assert diagnostics["last_failure_phase"] == "bluetooth_connecting"
        assert diagnostics["last_transport_before_cleanup"] == {
            "state": "failed",
            "last_error_type": "BleakDBusError",
            "connected": False,
        }
        assert diagnostics["last_transport_cleanup_outcome"] == "confirmed"
        assert "AA:BB:CC:DD:EE:FF" not in repr(diagnostics)

    asyncio.run(run())


def test_unconfirmed_disconnect_retains_owner_for_retry() -> None:
    async def run() -> None:
        client = _GattClient()
        client.disconnect_gate.clear()
        connection = _connection(client, disconnect_timeout=0.02)
        await connection.async_connect()

        with pytest.raises(MeshtasticCleanupError):
            await connection.async_disconnect()

        assert client.is_connected is True
        assert connection.owns_endpoint is True
        assert connection.diagnostic_snapshot()["state"] == "cleanup_incomplete"

        client.disconnect_gate.set()
        await connection.async_disconnect()
        assert connection.owns_endpoint is False

    asyncio.run(run())


def test_pending_cleanup_retains_owner_after_raw_link_drops() -> None:
    async def run() -> None:
        client = _GattClient()
        cleanup_can_finish = asyncio.Event()

        async def delayed_disconnect() -> None:
            client.is_connected = False
            while not cleanup_can_finish.is_set():
                try:
                    await cleanup_can_finish.wait()
                except asyncio.CancelledError:
                    # Model a platform backend that drops the raw link but is
                    # slow to return from its native cleanup call.
                    continue

        client.disconnect = delayed_disconnect  # type: ignore[method-assign]
        connection = _connection(client, disconnect_timeout=0.02)
        await connection.async_connect()

        with pytest.raises(MeshtasticCleanupError):
            await connection.async_disconnect()
        assert client.is_connected is False
        assert connection.owns_endpoint is True

        cleanup_can_finish.set()
        await asyncio.sleep(0)
        await connection.async_disconnect()
        assert connection.owns_endpoint is False

    asyncio.run(run())


def test_high_level_handshake_callbacks_send_and_stop() -> None:
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )
    from custom_components.meshnet.meshtastic_client import meshtastic_node_to_state

    class ProtocolConnection:
        def __init__(self, **_kwargs: Any) -> None:
            self.is_connected = False
            self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
            self.sent: list[Any] = []

        @property
        def owns_endpoint(self) -> bool:
            return self.is_connected

        async def async_connect(self) -> None:
            self.is_connected = True

        async def async_disconnect(self) -> None:
            self.is_connected = False
            await self.queue.put(None)

        async def async_send(self, payload: bytes, *, force_read: bool = False) -> None:
            message = mesh_pb2.ToRadio()
            message.ParseFromString(payload)
            self.sent.append((message, force_read))
            if message.want_config_id:
                my_info = mesh_pb2.FromRadio()
                my_info.my_info.my_node_num = 0x12345678
                await self.queue.put(my_info.SerializeToString())
                node_info = mesh_pb2.FromRadio()
                node_info.node_info.num = 0x12345678
                node_info.node_info.user.id = "!12345678"
                node_info.node_info.user.long_name = "Test node"
                node_info.node_info.user.macaddr = bytes.fromhex("aabbccddeeff")
                await self.queue.put(node_info.SerializeToString())
                complete = mesh_pb2.FromRadio()
                complete.config_complete_id = message.want_config_id
                await self.queue.put(complete.SerializeToString())

        async def packet_stream(self):
            while self.is_connected:
                payload = await self.queue.get()
                if payload is None:
                    return
                yield payload

        def diagnostic_snapshot(self) -> dict[str, Any]:
            return {"connected": self.is_connected}

    async def run() -> None:
        protocol_connection = ProtocolConnection()
        phases: list[str] = []
        statuses: list[bool] = []
        packets: list[dict[str, Any]] = []
        packet_ready = asyncio.Event()

        def on_packet(packet: dict[str, Any]) -> None:
            packets.append(packet)
            packet_ready.set()

        client = MeshtasticBluetoothClient(
            address="AA:BB:CC:DD:EE:FF",
            device_provider=lambda: SimpleNamespace(),
            connection_factory=lambda **_kwargs: protocol_connection,
            connect_timeout=0.2,
            configuration_timeout=0.2,
            io_timeout=0.2,
            disconnect_timeout=0.2,
            stop_timeout=0.5,
            heartbeat_interval=60,
            state_callback=phases.append,
        )
        client.add_connection_callback(statuses.append)
        client.add_packet_callback(on_packet)

        await client.async_start()
        assert client.connected is True
        assert statuses == [True]
        assert "bluetooth_resolving_device" in phases
        assert "bluetooth_synchronizing_configuration" in phases
        node_snapshot = client.node_snapshot()[0x12345678]
        assert node_snapshot["user"]["id"] == "!12345678"
        normalized_node = meshtastic_node_to_state(
            node_snapshot,
            gateway_id="bluetooth-test",
        )
        assert normalized_node.mac == "aabbccddeeff"
        assert normalized_node.node_key == "mac:aabbccddeeff"
        assert client._resolve_destination(normalized_node.node_key) == 0x12345678
        assert client._resolve_destination("meshtastic:305419896") == 0x12345678
        assert client._resolve_destination("meshtastic:!12345678") == 0x12345678
        assert client._resolve_destination("mac:aabbccddeeff") == 0x12345678
        client._nodes[99] = {"user": {"mac": "AA:BB:CC:DD:EE:FF"}}
        with pytest.raises(ValueError, match="unknown or ambiguous"):
            client._resolve_destination("mac:aabbccddeeff")
        del client._nodes[99]
        with pytest.raises(ValueError, match="unknown or ambiguous"):
            client._resolve_destination("mac:000000000000")

        packet = mesh_pb2.FromRadio()
        setattr(packet.packet, "from", 0x12345678)
        packet.packet.to = 0xFFFFFFFF
        packet.packet.id = 7
        packet.packet.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
        packet.packet.decoded.payload = b"hello"
        await protocol_connection.queue.put(packet.SerializeToString())
        await asyncio.wait_for(packet_ready.wait(), timeout=0.2)
        assert packets[0]["decoded"]["text"] == "hello"

        packet_ready.clear()
        unknown = mesh_pb2.FromRadio()
        setattr(unknown.packet, "from", 0x12345678)
        unknown.packet.to = 0xFFFFFFFF
        unknown.packet.id = 8
        unknown.packet.decoded.portnum = 65534
        unknown.packet.decoded.payload = b"future-app"
        await protocol_connection.queue.put(unknown.SerializeToString())
        await asyncio.wait_for(packet_ready.wait(), timeout=0.2)
        assert packets[1]["decoded"]["portnum"] == "UNKNOWN_APP_65534"

        packet_id = await client.async_send_text("reply", destination_id="!12345678")
        assert packet_id > 0
        assert protocol_connection.sent[-1][0].packet.decoded.payload == b"reply"

        await client.async_stop()
        assert statuses == [True, False]
        assert client.diagnostic_snapshot()["state"] == "bluetooth_stopped"

    asyncio.run(run())


def test_cleanup_never_disconnects_while_session_io_owners_resist_cancellation() -> None:
    pytest.importorskip("meshtastic")

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    class ProtocolConnection:
        def __init__(self) -> None:
            self.is_connected = True
            self.send_calls = 0
            self.disconnect_calls = 0

        @property
        def owns_endpoint(self) -> bool:
            return self.is_connected

        async def async_send(self, _payload: bytes, *, force_read: bool = False) -> None:
            del force_read
            self.send_calls += 1

        async def async_disconnect(self) -> None:
            self.disconnect_calls += 1
            self.is_connected = False

    async def run() -> None:
        connection = ProtocolConnection()
        release_owners = asyncio.Event()
        owners_started = asyncio.Event()
        started_count = 0

        async def cancellation_resistant_owner() -> None:
            nonlocal started_count
            started_count += 1
            if started_count == 2:
                owners_started.set()
            while not release_owners.is_set():
                try:
                    await release_owners.wait()
                except asyncio.CancelledError:
                    # Model a platform BLE call that delays cancellation until
                    # its native operation has returned.
                    continue

        client = MeshtasticBluetoothClient(
            address="AA:BB:CC:DD:EE:FF",
            device_provider=lambda: SimpleNamespace(),
            connect_timeout=0.1,
            configuration_timeout=0.1,
            io_timeout=0.1,
            disconnect_timeout=0.02,
            stop_timeout=0.04,
            heartbeat_interval=60,
        )
        reader = asyncio.create_task(cancellation_resistant_owner())
        heartbeat = asyncio.create_task(cancellation_resistant_owner())
        client._connection = connection  # type: ignore[assignment]
        client._reader_task = reader
        client._heartbeat_task = heartbeat
        await owners_started.wait()

        with pytest.raises(MeshtasticCleanupError, match="session tasks did not stop"):
            await client._cleanup_session(  # type: ignore[arg-type]
                reader,
                heartbeat,
                connection,
            )

        assert connection.send_calls == 0
        assert connection.disconnect_calls == 0
        assert connection.is_connected is True
        assert client._connection is connection
        assert client._reader_task is reader
        assert client._heartbeat_task is heartbeat

        # A backend may report the physical link down before its native GATT
        # coroutine actually yields. Pending owner tasks, not just the link bit,
        # must fence a replacement supervisor.
        connection.is_connected = False
        with pytest.raises(MeshtasticCleanupError, match="owner tasks are still stopping"):
            await client.async_start()
        assert client._runner_task is None
        connection.is_connected = True

        # The public stop retry must obey the same ownership fence; it may not
        # bypass _cleanup_session and race a live read/write owner.
        with pytest.raises(MeshtasticCleanupError, match="cleanup was not confirmed"):
            await client.async_stop()
        assert connection.send_calls == 0
        assert connection.disconnect_calls == 0
        assert connection.is_connected is True

        release_owners.set()
        await asyncio.gather(reader, heartbeat)
        await client._cleanup_session(  # type: ignore[arg-type]
            reader,
            heartbeat,
            connection,
        )

        assert connection.send_calls == 1
        assert connection.disconnect_calls == 1
        assert connection.is_connected is False
        assert client._connection is None
        assert client._reader_task is None
        assert client._heartbeat_task is None

    asyncio.run(run())
