"""Focused lifecycle tests for the isolated async Meshtastic stack."""

from __future__ import annotations

import asyncio
import logging
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
    MeshtasticConfigurationError,
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
        await asyncio.sleep(0)
        assert client.read_count == 0
        await connection.async_send(b"want-config", force_read=True)

        assert await read == expected
        assert client.writes == [b"want-config"]
        diagnostics = connection.diagnostic_snapshot()
        assert diagnostics["forced_read_count"] == 1
        assert diagnostics["read_trigger_count"] == 1
        assert diagnostics["empty_read_retry_count"] == 1
        assert diagnostics["read_count"] == 2
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
        assert diagnostics["read_timeout_count"] == 1
        assert client.read_count == 1
        await stream.aclose()
        await connection.async_disconnect()

    asyncio.run(run())


def test_reads_and_writes_share_one_bounded_gatt_owner() -> None:
    class SerializedGattClient(_GattClient):
        def __init__(self) -> None:
            super().__init__()
            self.read_started = asyncio.Event()
            self.release_read = asyncio.Event()
            self.read_active = False

        async def read_gatt_char(self, _characteristic: object) -> bytes:
            self.read_count += 1
            self.read_active = True
            self.read_started.set()
            try:
                await self.release_read.wait()
                return b"from-radio-record"
            finally:
                self.read_active = False

        async def write_gatt_char(
            self,
            _characteristic: object,
            payload: bytes,
            *,
            response: bool = True,
        ) -> None:
            assert self.read_active is False
            await super().write_gatt_char(
                _characteristic,
                payload,
                response=response,
            )

    async def run() -> None:
        client = SerializedGattClient()
        connection = _connection(client, read_timeout=0.5)
        await connection.async_connect()
        await connection.async_send(b"want-config", force_read=True)

        stream = connection.packet_stream()
        read = asyncio.create_task(anext(stream))
        await client.read_started.wait()
        send = asyncio.create_task(connection.async_send(b"message"))
        await asyncio.sleep(0)

        assert send.done() is False
        assert connection.diagnostic_snapshot()["io_locked"] is True
        client.release_read.set()
        assert await read == b"from-radio-record"
        await send
        assert client.writes == [b"want-config", b"message"]
        await stream.aclose()
        await connection.async_disconnect()

    asyncio.run(run())


def test_configuration_deadline_preempts_individual_gatt_read_timeout() -> None:
    pytest.importorskip("meshtastic")
    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    async def run() -> None:
        gatt_client = _GattClient()
        factory_arguments: dict[str, Any] = {}

        async def connector(_device: object, _callback: Any) -> _GattClient:
            return gatt_client

        def connection_factory(**kwargs: Any) -> BluetoothConnection:
            factory_arguments.update(kwargs)
            return BluetoothConnection(
                connector=connector,
                notify_timeout=0.1,
                idle_read_timeout=0.1,
                empty_read_retry_delay=0,
                **kwargs,
            )

        client = MeshtasticBluetoothClient(
            address="AA:BB:CC:DD:EE:FF",
            device_provider=lambda: SimpleNamespace(),
            connection_factory=connection_factory,
            connect_timeout=0.1,
            configuration_timeout=0.03,
            io_timeout=0.01,
            disconnect_timeout=0.1,
            start_timeout=0.5,
            stop_timeout=0.2,
            heartbeat_interval=60,
        )

        with pytest.raises(
            MeshtasticConfigurationError,
            match="did not complete configuration in time",
        ):
            await client.async_start()

        assert factory_arguments["read_timeout"] > 0.03
        assert gatt_client.read_count == 1
        assert gatt_client.is_connected is False
        diagnostics = client.diagnostic_snapshot()
        assert diagnostics["last_failure_phase"] == (
            "bluetooth_synchronizing_configuration"
        )
        assert diagnostics["last_transport_cleanup_outcome"] == "confirmed"

    asyncio.run(run())


def test_want_config_records_feed_privacy_safe_settings_snapshot() -> None:
    """Use the pinned SDK protos to exercise the real FromRadio field names."""
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import mesh_pb2

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: None,
    )
    client._settings.begin_refresh()
    client._config_id = 7412

    my_info = mesh_pb2.FromRadio()
    my_info.my_info.my_node_num = 123
    client._handle_from_radio(my_info.SerializeToString())

    network = mesh_pb2.FromRadio()
    network.config.network.wifi_ssid = "private-wifi-name"
    network.config.network.wifi_psk = "never-project-wifi-password"
    client._handle_from_radio(network.SerializeToString())

    module = mesh_pb2.FromRadio()
    module.moduleConfig.mqtt.enabled = True
    module.moduleConfig.mqtt.username = "never-project-mqtt-user"
    module.moduleConfig.mqtt.password = "never-project-mqtt-password"
    client._handle_from_radio(module.SerializeToString())

    channel = mesh_pb2.FromRadio()
    channel.channel.index = 0
    channel.channel.settings.name = "Private Channel"
    channel.channel.settings.psk = b"never-project-channel-psk"
    client._handle_from_radio(channel.SerializeToString())

    owner = mesh_pb2.FromRadio()
    owner.node_info.num = 123
    owner.node_info.user.long_name = "Local Owner"
    owner.node_info.user.short_name = "HOME"
    client._handle_from_radio(owner.SerializeToString())

    complete = mesh_pb2.FromRadio()
    complete.config_complete_id = 7412
    client._handle_from_radio(complete.SerializeToString())

    snapshot = asyncio.run(client.async_get_settings_snapshot())
    rendered = repr(snapshot)
    assert snapshot["available"] is True
    assert snapshot["complete"] is True
    assert "never-project-wifi-password" not in rendered
    assert "never-project-mqtt-user" not in rendered
    assert "never-project-mqtt-password" not in rendered
    assert "never-project-channel-psk" not in rendered


def _active_settings_client(
    *,
    behavior: str,
    admin_response_timeout: float = 0.03,
) -> tuple[Any, Any]:
    """Build an active local client with a protocol-aware fake radio."""
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: None,
        admin_response_timeout=admin_response_timeout,
    )

    class FakeSettingsConnection:
        is_connected = True
        owns_endpoint = True

        def __init__(self) -> None:
            self.admin_packets: list[Any] = []
            self.operations: list[str] = []
            self.owner: Any | None = None
            self.bluetooth: Any | None = None
            self.reconnect_tasks: set[asyncio.Task[None]] = set()

        @staticmethod
        def _selected(message: Any) -> str | None:
            for oneof in message.DESCRIPTOR.oneofs:
                selected = message.WhichOneof(oneof.name)
                if selected is not None:
                    return selected
            return None

        def _routing_response(
            self,
            packet: Any,
            *,
            error: int = 0,
            request_id: int | None = None,
            source: int = 123,
            empty: bool = False,
        ) -> None:
            routing = mesh_pb2.Routing()
            if not empty:
                routing.error_reason = error
            record = mesh_pb2.FromRadio()
            setattr(record.packet, "from", source)
            record.packet.channel = packet.channel
            record.packet.decoded.portnum = portnums_pb2.ROUTING_APP
            record.packet.decoded.request_id = (
                int(packet.id) if request_id is None else request_id
            )
            record.packet.decoded.payload = routing.SerializeToString()
            client._handle_from_radio(record.SerializeToString())

        async def _reconnect_with_owner(self) -> None:
            await asyncio.sleep(0)
            client._connection_generation += 1
            client._settings.begin_refresh()
            record = mesh_pb2.FromRadio()
            record.node_info.num = 123
            if self.owner is not None:
                record.node_info.user.CopyFrom(self.owner)
            client._handle_from_radio(record.SerializeToString())
            if self.bluetooth is not None:
                record = mesh_pb2.FromRadio()
                record.config.bluetooth.CopyFrom(self.bluetooth)
                client._handle_from_radio(record.SerializeToString())
            client._config_id = 9001
            complete = mesh_pb2.FromRadio()
            complete.config_complete_id = 9001
            client._handle_from_radio(complete.SerializeToString())

        async def async_send(
            self,
            payload: bytes,
            *,
            force_read: bool = False,
        ) -> None:
            assert force_read is False
            to_radio = mesh_pb2.ToRadio()
            to_radio.ParseFromString(payload)
            assert to_radio.HasField("packet")
            packet = to_radio.packet
            assert getattr(packet, "from") == 0
            assert packet.to == 123
            assert packet.decoded.portnum == portnums_pb2.ADMIN_APP
            assert packet.decoded.want_response is True
            assert packet.want_ack is True
            assert packet.pki_encrypted is True

            admin = admin_pb2.AdminMessage()
            admin.ParseFromString(bytes(packet.decoded.payload))
            assert bytes(admin.session_passkey) == b""
            operation = self._selected(admin)
            assert operation is not None
            self.admin_packets.append(packet)
            self.operations.append(operation)
            if operation == "set_owner":
                self.owner = type(admin.set_owner)()
                self.owner.CopyFrom(admin.set_owner)
            elif operation == "set_config":
                section = self._selected(admin.set_config)
                if section == "bluetooth":
                    self.bluetooth = type(admin.set_config.bluetooth)()
                    self.bluetooth.CopyFrom(admin.set_config.bluetooth)
            if behavior == "secret_logging":
                logging.getLogger("meshtastic.fake").debug(
                    "simulated SDK protobuf fixed_pin: 654321"
                )

            if behavior == "timeout_begin" and operation == "begin_edit_settings":
                return
            if behavior == "nak_set" and operation == "set_owner":
                error_field = mesh_pb2.Routing.DESCRIPTOR.fields_by_name[
                    "error_reason"
                ]
                error = next(
                    value.number
                    for value in error_field.enum_type.values
                    if value.number != 0
                )
                self._routing_response(packet, error=error)
                return
            if operation == "begin_edit_settings":
                # None of these records may satisfy the exact pending request.
                self._routing_response(packet, request_id=int(packet.id) + 1)
                self._routing_response(packet, source=321)
                self._routing_response(packet, empty=True)
            if operation == "commit_edit_settings":
                task = asyncio.create_task(self._reconnect_with_owner())
                self.reconnect_tasks.add(task)
                task.add_done_callback(self.reconnect_tasks.discard)
                if behavior == "lost_commit_ack":
                    return
            self._routing_response(packet)

    connection = FakeSettingsConnection()
    client._connection = connection  # type: ignore[assignment]
    client._connected = True
    client._my_node_num = 123
    client._connection_generation = 1
    client._settings_complete_generation = 1
    client._settings_complete_sequence = 1
    client._settings.begin_refresh()
    owner = mesh_pb2.FromRadio()
    owner.node_info.num = 123
    owner.node_info.user.long_name = "Original Owner"
    owner.node_info.user.short_name = "OLD"
    client._settings.capture_from_radio(owner, my_node_num=123)
    bluetooth = mesh_pb2.FromRadio()
    bluetooth.config.bluetooth.enabled = True
    bluetooth.config.bluetooth.fixed_pin = 123456
    client._settings.capture_from_radio(bluetooth, my_node_num=123)
    client._settings.mark_complete()
    return client, connection


@pytest.mark.parametrize("behavior", ["ack", "lost_commit_ack"])
def test_local_admin_write_is_one_shot_and_requires_reconnect_readback(
    behavior: str,
) -> None:
    async def run() -> None:
        client, connection = _active_settings_client(behavior=behavior)

        result = await client.async_apply_settings_plan(
            {"owner.short_name": "HOME"}
        )

        assert result == {
            "verified": ["owner.short_name"],
            "reconnect_required": True,
            "warning_codes": [],
        }
        assert connection.operations == [
            "begin_edit_settings",
            "set_owner",
            "commit_edit_settings",
        ]
        assert len({packet.id for packet in connection.admin_packets}) == 3

    asyncio.run(run())


@pytest.mark.parametrize("behavior", ["timeout_begin", "nak_set"])
def test_local_admin_failure_stops_without_retry_or_commit(behavior: str) -> None:
    async def run() -> None:
        client, connection = _active_settings_client(
            behavior=behavior,
            admin_response_timeout=0.01,
        )

        with pytest.raises(MeshtasticConfigurationError):
            await client.async_apply_settings_plan(
                {"owner.short_name": "HOME"}
            )

        expected = (
            ["begin_edit_settings"]
            if behavior == "timeout_begin"
            else ["begin_edit_settings", "set_owner"]
        )
        assert connection.operations == expected
        assert "commit_edit_settings" not in connection.operations

    asyncio.run(run())


def test_oversized_admin_payload_is_rejected_before_transport_write() -> None:
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import admin_pb2

    async def run() -> None:
        client, connection = _active_settings_client(behavior="ack")
        message = admin_pb2.AdminMessage()
        message.set_owner.public_key = b"x" * 512

        with pytest.raises(MeshtasticConfigurationError, match="payload limit"):
            async with client._send_lock:
                await client._async_send_admin_locked(
                    message,
                    connection=connection,
                )

        assert connection.operations == []

    asyncio.run(run())


def test_admin_app_payload_is_never_published() -> None:
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: None,
    )
    published: list[dict[str, Any]] = []
    client.add_packet_callback(published.append)
    admin = admin_pb2.AdminMessage()
    admin.session_passkey = b"must-never-leave-the-client"
    record = mesh_pb2.FromRadio()
    record.packet.decoded.portnum = portnums_pb2.ADMIN_APP
    record.packet.decoded.payload = admin.SerializeToString()

    client._handle_from_radio(record.SerializeToString())

    assert published == []
    assert "must-never-leave-the-client" not in repr(client.diagnostic_snapshot())


def test_disconnect_fails_and_clears_admin_response_waiters() -> None:
    pytest.importorskip("meshtastic")
    from custom_components.meshnet.aiomeshtastic.client import (
        _PendingAdminResponse,
    )

    async def run() -> None:
        client, _connection = _active_settings_client(behavior="ack")
        future = asyncio.get_running_loop().create_future()
        client._pending_admin_responses[77] = _PendingAdminResponse(
            future=future,
            source=123,
            channel=0,
        )
        client._internal_admin_request_ids.append(77)

        client._fail_pending_admin_responses()

        with pytest.raises(MeshtasticConnectionError, match="disconnected"):
            await future
        assert client._pending_admin_responses == {}
        assert list(client._internal_admin_request_ids) == []

    asyncio.run(run())


def test_secret_admin_write_is_suppressed_and_never_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        client, _connection = _active_settings_client(
            behavior="secret_logging"
        )
        caplog.set_level(10)

        result = await client.async_apply_settings_plan(
            {
                "config.bluetooth.fixed_pin": {
                    "operation": "replace",
                    "value": "654321",
                }
            }
        )
        snapshot = await client.async_get_settings_snapshot()
        fields = {
            field["path"]: field
            for category in snapshot["categories"]
            for field in category["fields"]
        }

        assert result["verified"] == ["config.bluetooth.fixed_pin"]
        assert fields["config.bluetooth.fixed_pin"]["writable"] is True
        assert fields["config.bluetooth.fixed_pin"]["type"] == "secret"
        assert fields["config.bluetooth.enabled"]["writable"] is False
        assert "654321" not in caplog.text
        assert "654321" not in repr(result)
        assert "654321" not in repr(snapshot)
        assert "654321" not in repr(client.diagnostic_snapshot())

    asyncio.run(run())


def test_write_backend_type_error_is_not_retried() -> None:
    class TypeErrorGattClient(_GattClient):
        def __init__(self) -> None:
            super().__init__()
            self.write_attempts = 0

        async def write_gatt_char(
            self,
            _characteristic: object,
            _payload: bytes,
            *,
            response: bool = True,
        ) -> None:
            assert response is True
            self.write_attempts += 1
            raise TypeError("backend failed after accepting the write")

    async def run() -> None:
        client = TypeErrorGattClient()
        connection = _connection(client)
        await connection.async_connect()

        with pytest.raises(MeshtasticConnectionError, match="write failed"):
            await connection.async_send(b"want-config", force_read=True)

        diagnostics = connection.diagnostic_snapshot()
        assert client.write_attempts == 1
        assert diagnostics["write_count"] == 0
        assert diagnostics["forced_read_count"] == 0
        assert diagnostics["last_error_type"] == "TypeError"
        assert diagnostics["last_failure_phase"] == "writing_to_radio"
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
            self.packet_stream_started = False

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
                # A real GATT write suspends while it waits for the ATT write
                # response. Give a prematurely-created reader a chance to run
                # so this test catches a read-before-want_config regression.
                await asyncio.sleep(0)
                assert self.packet_stream_started is False
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
            self.packet_stream_started = True
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
        assert normalized_node.node_key.startswith("meshtastic-proof:")
        assert normalized_node.mac == "aabbccddeeff"
        with pytest.raises(ValueError, match="not a known Meshtastic node"):
            client._resolve_destination(normalized_node.node_key)
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
        packet.packet.hop_start = 3
        packet.packet.hop_limit = 3
        packet.packet.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
        packet.packet.decoded.payload = b"hello"
        await protocol_connection.queue.put(packet.SerializeToString())
        await asyncio.wait_for(packet_ready.wait(), timeout=0.2)
        assert packets[0]["decoded"]["text"] == "hello"
        assert packets[0]["hopStart"] == 3
        assert packets[0]["hopLimit"] == 3
        assert packets[0]["hopsAway"] == 0
        assert client.node_snapshot()[0x12345678]["hopsAway"] == 0

        packet_ready.clear()
        unknown = mesh_pb2.FromRadio()
        setattr(unknown.packet, "from", 0x12345678)
        unknown.packet.to = 0xFFFFFFFF
        unknown.packet.id = 8
        unknown.packet.hop_start = 5
        unknown.packet.hop_limit = 2
        unknown.packet.decoded.portnum = 65534
        unknown.packet.decoded.payload = b"future-app"
        await protocol_connection.queue.put(unknown.SerializeToString())
        await asyncio.wait_for(packet_ready.wait(), timeout=0.2)
        assert packets[1]["decoded"]["portnum"] == "UNKNOWN_APP_65534"
        assert packets[1]["hopsAway"] == 3

        packet_id = await client.async_send_text("reply", destination_id="!12345678")
        assert packet_id > 0
        assert protocol_connection.sent[-1][0].packet.decoded.payload == b"reply"

        await client.async_stop()
        assert statuses == [True, False]
        assert client.diagnostic_snapshot()["state"] == "bluetooth_stopped"

    asyncio.run(run())


def test_destination_resolution_accepts_only_unique_exact_cached_names() -> None:
    pytest.importorskip("meshtastic")

    from custom_components.meshnet import _coerce_target_node
    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: SimpleNamespace(),
    )
    client._nodes = {
        0x11111111: {
            "user": {
                "id": "!11111111",
                "shortName": "NODE",
                "longName": "Exact Long Name",
            }
        },
        0x22222222: {
            "user": {
                "id": "!22222222",
                "short_name": "1234",
                "long_name": "Variant Name",
            }
        },
        0x33333333: {
            "user": {
                "id": "!33333333",
                "shortname": "SAFE",
                "longname": "Known Variant",
            }
        },
    }

    assert client._resolve_destination(" node ") == 0x11111111
    assert client._resolve_destination("EXACT LONG NAME") == 0x11111111
    assert client._resolve_destination("variant name") == 0x22222222
    assert client._resolve_destination("known variant") == 0x33333333
    assert client._resolve_destination("1234") == 0x22222222
    assert client._resolve_destination(_coerce_target_node(1234)) == 0x22222222
    assert client._resolve_destination("4321") == 4321
    assert client._resolve_destination("!11111111") == 0x11111111
    assert client._resolve_destination("meshtastic:!11111111") == 0x11111111
    assert client._resolve_destination("0x22222222") == 0x22222222

    with pytest.raises(ValueError, match="not a known Meshtastic node"):
        client._resolve_destination("Exact Long")
    with pytest.raises(ValueError, match="not a known Meshtastic node"):
        client._resolve_destination("missing")

    client._nodes[0x44444444] = {
        "user": {"id": "!44444444", "shortName": "node"}
    }
    with pytest.raises(ValueError, match="node name is ambiguous"):
        client._resolve_destination("NODE")


def test_node_cache_drops_user_identity_that_conflicts_with_envelope() -> None:
    pytest.importorskip("meshtastic")

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: SimpleNamespace(),
    )
    client._merge_node(
        0x11111111,
        {
            "num": 0x11111111,
            "user": {
                "id": "!22222222",
                "longName": "Wrong node",
                "macaddr": "22:22:22:22:22:22",
            },
        },
    )

    assert "user" not in client._nodes[0x11111111]
    with pytest.raises(ValueError, match="not a known Meshtastic node"):
        client._resolve_destination("Wrong node")
    with pytest.raises(ValueError, match="unknown or ambiguous"):
        client._resolve_destination("mac:222222222222")

    client._merge_node(
        0x11111111,
        {
            "user": {
                "id": "!11111111",
                "longName": "Right node",
                "macaddr": "11:11:11:11:11:11",
            }
        },
    )
    assert client._resolve_destination("Right node") == 0x11111111
    assert client._resolve_destination("mac:111111111111") == 0x11111111


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
