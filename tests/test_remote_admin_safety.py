"""Safety contract for explicit Meshtastic remote administration.

These tests intentionally describe the reviewed backend boundary before the
runtime implementation exists.  They exercise the Bluetooth protocol owner
directly so browser validation can never become the security boundary.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from types import SimpleNamespace
from typing import Any

import pytest

CONTROLLER_NUM = 0x10203040
TARGET_NUM = 0x50607080
CONTROLLER_ID = "!10203040"
TARGET_ID = "!50607080"
CONTROLLER_PUBLIC_KEY = bytes(range(32))
TARGET_PUBLIC_KEY = bytes(reversed(range(32)))
SESSION_PASSKEY = b"8bytes!!"
PRIVATE_KEY_SENTINEL = "PRIVATE-KEY-MUST-NEVER-LEAVE-THE-RADIO"
CHANNEL_PSK_SENTINEL = "CHANNEL-PSK-MUST-NEVER-LEAVE-THE-RADIO"


def _selected(message: Any) -> str | None:
    """Return the selected protobuf oneof without depending on its name."""
    for oneof in message.DESCRIPTOR.oneofs:
        selected = message.WhichOneof(oneof.name)
        if selected is not None:
            return selected
    return None


def _active_remote_client(
    *,
    target_key: bytes | None = TARGET_PUBLIC_KEY,
    route_error: str | None = None,
    inject_bad_correlations: bool = False,
) -> tuple[Any, Any]:
    """Build an active BLE client and a protocol-aware remote-radio fake."""
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    client = MeshtasticBluetoothClient(
        address="AA:BB:CC:DD:EE:FF",
        device_provider=lambda: None,
        admin_response_timeout=0.05,
    )

    class FakeRemoteRadio:
        is_connected = True
        owns_endpoint = True

        def __init__(self) -> None:
            self.packets: list[Any] = []
            self.operations: list[str] = []
            self.write_operations: list[str] = []
            self.session_requests = 0
            self.max_writes_in_flight = 0
            self._writes_in_flight = 0
            self.short_name = "OLD"

        def _admin_response(
            self,
            request: Any,
            response: Any,
            *,
            source: int = TARGET_NUM,
            channel: int | None = None,
            request_id: int | None = None,
        ) -> None:
            record = mesh_pb2.FromRadio()
            setattr(record.packet, "from", source)
            record.packet.to = CONTROLLER_NUM
            record.packet.channel = (
                int(request.channel) if channel is None else channel
            )
            record.packet.pki_encrypted = True
            record.packet.decoded.portnum = portnums_pb2.ADMIN_APP
            record.packet.decoded.request_id = (
                int(request.id) if request_id is None else request_id
            )
            record.packet.decoded.payload = response.SerializeToString()
            client._handle_from_radio(record.SerializeToString())

        def _routing_response(self, request: Any, error_name: str) -> None:
            routing = mesh_pb2.Routing()
            routing.error_reason = mesh_pb2.Routing.Error.Value(error_name)
            record = mesh_pb2.FromRadio()
            setattr(record.packet, "from", TARGET_NUM)
            record.packet.to = CONTROLLER_NUM
            record.packet.channel = int(request.channel)
            record.packet.decoded.portnum = portnums_pb2.ROUTING_APP
            record.packet.decoded.request_id = int(request.id)
            record.packet.decoded.payload = routing.SerializeToString()
            client._handle_from_radio(record.SerializeToString())

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
            assert packet.to == TARGET_NUM
            assert packet.decoded.portnum == portnums_pb2.ADMIN_APP
            assert packet.decoded.want_response is True
            assert packet.want_ack is True
            assert packet.pki_encrypted is True
            assert bytes(packet.public_key) == TARGET_PUBLIC_KEY

            admin = admin_pb2.AdminMessage()
            admin.ParseFromString(bytes(packet.decoded.payload))
            operation = _selected(admin)
            assert operation is not None
            self.operations.append(operation)
            saved_packet = mesh_pb2.MeshPacket()
            saved_packet.CopyFrom(packet)
            self.packets.append(saved_packet)

            if route_error is not None:
                self._routing_response(packet, route_error)
                return

            if operation.startswith("get_"):
                if operation == "get_config_request":
                    config_name = admin_pb2.AdminMessage.ConfigType.Name(
                        admin.get_config_request
                    )
                    assert config_name != "SECURITY_CONFIG"
                    if config_name == "SESSIONKEY_CONFIG":
                        self.session_requests += 1
                response = admin_pb2.AdminMessage()
                response.session_passkey = SESSION_PASSKEY
                if operation == "get_owner_request":
                    response.get_owner_response.id = TARGET_ID
                    response.get_owner_response.long_name = "Remote Node"
                    response.get_owner_response.short_name = self.short_name
                    response.get_owner_response.public_key = TARGET_PUBLIC_KEY
                elif operation == "get_config_request":
                    section = admin_pb2.AdminMessage.ConfigType.Name(
                        admin.get_config_request
                    ).removesuffix("_CONFIG").lower()
                    getattr(response.get_config_response, section).SetInParent()
                    if section == "device":
                        response.get_config_response.device.serial_enabled = True
                elif operation == "get_module_config_request":
                    fields = response.get_module_config_response.DESCRIPTOR.fields
                    field = next(
                        (
                            item
                            for item in fields
                            if item.index == admin.get_module_config_request
                        ),
                        None,
                    )
                    assert field is not None
                    getattr(response.get_module_config_response, field.name).SetInParent()
                elif operation == "get_channel_request":
                    response.get_channel_response.index = max(
                        0, int(admin.get_channel_request) - 1
                    )

                if inject_bad_correlations:
                    bad_length = admin_pb2.AdminMessage()
                    bad_length.CopyFrom(response)
                    bad_length.session_passkey = b"seven!!"
                    self._admin_response(packet, bad_length)
                    self._admin_response(
                        packet,
                        response,
                        source=TARGET_NUM + 1,
                    )
                    self._admin_response(
                        packet,
                        response,
                        channel=int(packet.channel) + 1,
                    )
                    self._admin_response(
                        packet,
                        response,
                        request_id=int(packet.id) + 1,
                    )
                self._admin_response(packet, response)
                return

            self.write_operations.append(operation)
            assert bytes(admin.session_passkey) == SESSION_PASSKEY
            self._writes_in_flight += 1
            self.max_writes_in_flight = max(
                self.max_writes_in_flight,
                self._writes_in_flight,
            )
            try:
                await asyncio.sleep(0)
                if operation == "set_owner":
                    self.short_name = admin.set_owner.short_name
                self._routing_response(packet, "NONE")
            finally:
                self._writes_in_flight -= 1

    connection = FakeRemoteRadio()
    client._connection = connection  # type: ignore[assignment]
    client._connected = True
    client._my_node_num = CONTROLLER_NUM
    client._nodes = {
        CONTROLLER_NUM: {
            "num": CONTROLLER_NUM,
            "user": {
                "id": CONTROLLER_ID,
                "shortName": "CTRL",
                "publicKey": base64.b64encode(CONTROLLER_PUBLIC_KEY).decode(),
                "privateKey": PRIVATE_KEY_SENTINEL,
            },
        },
        TARGET_NUM: {
            "num": TARGET_NUM,
            "user": {
                "id": TARGET_ID,
                "longName": "Remote Node",
                "shortName": "OLD",
                "publicKey": (
                    base64.b64encode(target_key).decode()
                    if target_key is not None
                    else None
                ),
                "channelPsk": CHANNEL_PSK_SENTINEL,
            },
        },
    }
    return client, connection


def test_remote_admin_exposes_only_explicit_high_level_client_methods() -> None:
    """There must never be a public raw AdminMessage escape hatch."""
    pytest.importorskip("meshtastic")
    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    assert callable(
        MeshtasticBluetoothClient.async_get_remote_settings_snapshot
    )
    assert callable(
        MeshtasticBluetoothClient.async_apply_remote_settings_plan
    )
    for forbidden in (
        "async_send_admin",
        "async_send_admin_message",
        "async_raw_admin",
        "async_import_private_key",
        "async_set_admin_key",
    ):
        assert not hasattr(MeshtasticBluetoothClient, forbidden)


def test_remote_get_copies_controller_public_key_without_secret_projection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The controller public key is copy-only; no other key material escapes."""

    async def run() -> None:
        from meshtastic.protobuf import admin_pb2

        client, connection = _active_remote_client()
        caplog.set_level(logging.DEBUG)

        snapshot = await client.async_get_remote_settings_snapshot(TARGET_ID)

        assert snapshot["controller"] == {
            "node_id": CONTROLLER_ID,
            "short_name": "CTRL",
            "public_key": (
                "base64:" + base64.b64encode(CONTROLLER_PUBLIC_KEY).decode()
            ),
        }
        assert snapshot["target"]["node_id"] == TARGET_ID
        assert snapshot["target"]["public_key_available"] is True
        assert snapshot["target"]["remote_admin_eligible"] is True
        assert "_secret_revision_material" not in snapshot
        rendered = repr(snapshot)
        assert PRIVATE_KEY_SENTINEL not in rendered
        assert CHANNEL_PSK_SENTINEL not in rendered
        assert SESSION_PASSKEY.decode() not in rendered
        assert PRIVATE_KEY_SENTINEL not in caplog.text
        assert CHANNEL_PSK_SENTINEL not in caplog.text
        assert SESSION_PASSKEY.decode() not in caplog.text
        assert "SECURITY_CONFIG" not in connection.operations
        config_requests = []
        for packet in connection.packets:
            admin = admin_pb2.AdminMessage()
            admin.ParseFromString(bytes(packet.decoded.payload))
            if _selected(admin) == "get_config_request":
                config_requests.append(
                    admin_pb2.AdminMessage.ConfigType.Name(
                        admin.get_config_request
                    )
                )
        assert config_requests == ["SESSIONKEY_CONFIG", "DISPLAY_CONFIG"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("target", "target_key", "expected_code"),
    [
        ("Remote Node", TARGET_PUBLIC_KEY, "remote_admin_target_invalid"),
        ("!50607081", TARGET_PUBLIC_KEY, "remote_admin_target_unknown"),
        (TARGET_ID, None, "remote_admin_target_public_key_unavailable"),
        (TARGET_ID, b"short", "remote_admin_target_public_key_unavailable"),
    ],
)
def test_remote_admin_requires_exact_known_identity_and_32_byte_public_key(
    target: str,
    target_key: bytes | None,
    expected_code: str,
) -> None:
    """Names, unknown IDs, and malformed/missing public keys fail before RF."""
    from custom_components.meshnet.aiomeshtastic.errors import (
        MeshtasticConfigurationError,
    )

    async def run() -> None:
        client, connection = _active_remote_client(target_key=target_key)

        with pytest.raises(MeshtasticConfigurationError) as raised:
            await client.async_get_remote_settings_snapshot(target)

        assert getattr(raised.value, "code", None) == expected_code
        assert connection.packets == []

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["serial", "tcp", "mqtt"])
def test_remote_admin_is_rejected_outside_meshtastic_bluetooth(
    transport: str,
) -> None:
    """An adapter cannot silently fall back to the threaded SDK or MQTT."""
    from custom_components.meshnet.meshtastic_client import MeshtasticClient
    from custom_components.meshnet.models import GatewayConfig

    async def noop(*_args: Any) -> None:
        return None

    async def run() -> None:
        gateway = MeshtasticClient(
            SimpleNamespace(async_create_task=asyncio.create_task),
            GatewayConfig(
                gateway_id="unsafe-transport",
                name="Unsafe transport",
                protocol="meshtastic",
                transport=transport,
            ),
            noop,
            noop,
            noop,
            logging.getLogger(__name__),
        )

        with pytest.raises(RuntimeError) as raised:
            await gateway.async_get_remote_settings_snapshot(TARGET_ID)

        assert getattr(raised.value, "code", None) == (
            "remote_admin_requires_bluetooth"
        )

    asyncio.run(run())


def test_remote_session_accepts_only_exactly_correlated_eight_byte_response() -> None:
    """Wrong source/channel/request and malformed passkeys cannot unlock writes."""

    async def run() -> None:
        client, connection = _active_remote_client(
            inject_bad_correlations=True
        )

        snapshot = await client.async_get_remote_settings_snapshot(TARGET_ID)

        assert snapshot["target"]["remote_admin_eligible"] is True
        assert connection.session_requests >= 1
        assert SESSION_PASSKEY.decode() not in repr(snapshot)
        assert SESSION_PASSKEY.decode() not in repr(
            client.diagnostic_snapshot()
        )
        assert SESSION_PASSKEY.decode() not in repr(client.node_snapshot())

    asyncio.run(run())


def test_expired_remote_sessions_are_dropped_before_the_next_operation() -> None:
    """Expired passkeys must not remain reachable for unrelated targets."""
    client, _connection = _active_remote_client()
    client._remote_admin_sessions[TARGET_NUM + 1] = SimpleNamespace(
        passkey=b"expired!",
        expires_monotonic=-1.0,
    )

    client._remote_admin_target(TARGET_ID)

    assert TARGET_NUM + 1 not in client._remote_admin_sessions


@pytest.mark.parametrize(
    "path",
    [
        "config.security.private_key",
        "config.security.public_key",
        "config.security.admin_key",
        "config.security.is_managed",
        "config.bluetooth.fixed_pin",
        "config.bluetooth.enabled",
        "config.device.role",
        "channel.0.settings.psk",
        "admin.factory_reset_device",
        "admin.nodedb_reset",
        "admin.shutdown_seconds",
        "admin.reboot_seconds",
        "admin.ota_request",
        "admin.delete_file_request",
        "raw_admin_message",
    ],
)
def test_remote_write_rejects_security_generic_and_destructive_paths(
    path: str,
) -> None:
    """Bypassing WebSocket validation still cannot reach forbidden commands."""
    from custom_components.meshnet.aiomeshtastic.errors import (
        MeshtasticConfigurationError,
    )

    async def run() -> None:
        client, connection = _active_remote_client()

        with pytest.raises(MeshtasticConfigurationError) as raised:
            await client.async_apply_remote_settings_plan(
                TARGET_ID,
                {path: PRIVATE_KEY_SENTINEL},
            )

        assert getattr(raised.value, "code", None) == (
            "remote_admin_command_forbidden"
        )
        assert connection.packets == []
        assert PRIVATE_KEY_SENTINEL not in repr(raised.value)

    asyncio.run(run())


def test_remote_write_is_serialized_sent_once_and_verified_by_readback() -> None:
    """Concurrent explicit writes serialize and each mutation is sent once."""

    async def run() -> None:
        client, connection = _active_remote_client()
        await client.async_get_remote_settings_snapshot(TARGET_ID)

        first, second = await asyncio.gather(
            client.async_apply_remote_settings_plan(
                TARGET_ID,
                {"owner.short_name": "ONE"},
            ),
            client.async_apply_remote_settings_plan(
                TARGET_ID,
                {"owner.short_name": "TWO"},
            ),
        )

        assert first["status"] == "verified"
        assert second["status"] == "verified"
        assert connection.max_writes_in_flight == 1
        assert connection.write_operations.count("set_owner") == 2
        assert connection.short_name == "TWO"
        assert "get_owner_request" in connection.operations

    asyncio.run(run())


@pytest.mark.parametrize(
    ("routing_error", "expected_code"),
    [
        ("PKI_SEND_FAIL_PUBLIC_KEY", "remote_admin_target_public_key_unavailable"),
        ("ADMIN_PUBLIC_KEY_UNAUTHORIZED", "remote_admin_controller_unauthorized"),
        ("ADMIN_BAD_SESSION_KEY", "remote_admin_session_rejected"),
        ("NO_ROUTE", "remote_admin_no_route"),
        ("NO_RESPONSE", "remote_admin_no_response"),
        ("DUTY_CYCLE_LIMIT", "remote_admin_duty_cycle_limited"),
        ("RATE_LIMIT_EXCEEDED", "remote_admin_rate_limited"),
    ],
)
def test_remote_admin_protocol_failures_keep_stable_actionable_categories(
    routing_error: str,
    expected_code: str,
) -> None:
    """Provider details map to bounded categories without packet/key echoes."""
    from custom_components.meshnet.aiomeshtastic.errors import (
        MeshtasticConfigurationError,
    )

    async def run() -> None:
        client, _connection = _active_remote_client(
            route_error=routing_error
        )

        with pytest.raises(MeshtasticConfigurationError) as raised:
            await client.async_get_remote_settings_snapshot(TARGET_ID)

        assert getattr(raised.value, "code", None) == expected_code
        assert PRIVATE_KEY_SENTINEL not in str(raised.value)
        assert CHANNEL_PSK_SENTINEL not in str(raised.value)

    asyncio.run(run())


def test_cached_reads_diagnostics_and_refresh_never_send_remote_admin() -> None:
    """Only the two explicit remote-admin methods may create ADMIN_APP RF."""

    async def run() -> None:
        client, connection = _active_remote_client()

        client.node_snapshot()
        client.diagnostic_snapshot()
        await client.async_get_settings_snapshot()
        await client.async_node_snapshot()

        assert connection.packets == []

    asyncio.run(run())
