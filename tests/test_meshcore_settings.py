from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.meshnet.const import (
    MESSAGE_TYPE_BROADCAST,
    PROTOCOL_MESHCORE,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_SERIAL,
)
from custom_components.meshnet.meshcore_client import MeshCoreClient
from custom_components.meshnet.meshcore_settings import (
    MeshCoreSettingsUnavailable,
    MeshCoreSettingsUnknownState,
    MeshCoreSettingsValidationError,
    _RawSettings,
)
from custom_components.meshnet.models import GatewayConfig


async def _noop(*_args) -> None:
    return None


def _event(event_type: str, payload=None):
    return SimpleNamespace(
        type=SimpleNamespace(value=event_type), payload=payload if payload is not None else {}
    )


class FakeCommands:
    """Small stateful companion command surface."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.device = {
            "fw ver": 10,
            "max_channels": 2,
            "ble_pin": 123456,
            "fw_build": "build",
            "model": "Test companion",
            "ver": "1.14",
            "repeat": False,
            "path_hash_mode": 1,
        }
        self.self_info = {
            "tx_power": 20,
            "max_tx_power": 22,
            "adv_lat": 41.1,
            "adv_lon": -87.2,
            "multi_acks": 0,
            "adv_loc_policy": 0,
            "telemetry_mode_env": 0,
            "telemetry_mode_loc": 1,
            "telemetry_mode_base": 1,
            "manual_add_contacts": False,
            "radio_freq": 915.0,
            "radio_bw": 250.0,
            "radio_sf": 10,
            "radio_cr": 5,
            "name": "Gateway",
            # This must never be projected.
            "public_key": "ab" * 32,
        }
        self.channels = {
            0: {
                "channel_idx": 0,
                "channel_name": "Ops",
                "channel_secret": bytes.fromhex("11" * 16),
            },
            1: {
                "channel_idx": 1,
                "channel_name": "",
                "channel_secret": bytes(16),
            },
        }

    async def send_device_query(self):
        self.calls.append("get_device")
        return _event("device_info", deepcopy(self.device))

    async def send_appstart(self):
        self.calls.append("get_self")
        return _event("self_info", deepcopy(self.self_info))

    async def get_channel(self, index: int):
        self.calls.append(f"get_channel:{index}")
        return _event("channel_info", deepcopy(self.channels[index]))

    async def set_name(self, value: str):
        self.calls.append("set_name")
        self.self_info["name"] = value
        return _event("command_ok")

    async def set_coords(self, latitude: float, longitude: float):
        self.calls.append("set_coords")
        self.self_info["adv_lat"] = round(latitude, 6)
        self.self_info["adv_lon"] = round(longitude, 6)
        return _event("command_ok")

    async def set_radio(
        self,
        frequency: float,
        bandwidth: float,
        spreading_factor: int,
        coding_rate: int,
        repeat: bool,
    ):
        self.calls.append("set_radio")
        assert repeat is False
        self.self_info.update(
            radio_freq=frequency,
            radio_bw=bandwidth,
            radio_sf=spreading_factor,
            radio_cr=coding_rate,
        )
        return _event("command_ok")

    async def set_tx_power(self, value: int):
        self.calls.append("set_tx_power")
        self.self_info["tx_power"] = value
        return _event("command_ok")

    async def set_other_params_from_infos(self, infos):
        self.calls.append("set_other")
        self.self_info.update(deepcopy(infos))
        return _event("command_ok")

    async def set_channel(self, index: int, name: str, secret: bytes):
        self.calls.append(f"set_channel:{index}")
        self.channels[index] = {
            "channel_idx": index,
            "channel_name": name,
            "channel_secret": bytes(secret),
        }
        return _event("command_ok")

    async def set_devicepin(self, pin: int):
        self.calls.append("set_pin")
        self.device["ble_pin"] = pin
        return _event("command_ok")

    async def send_chan_msg(self, _channel: int, _message: str):
        self.calls.append("send_message")
        return _event("message_sent")


def _client(
    commands: FakeCommands | None = None,
    *,
    transport: str = TRANSPORT_SERIAL,
    options: dict | None = None,
) -> tuple[MeshCoreClient, FakeCommands]:
    commands = commands or FakeCommands()
    config = GatewayConfig(
        gateway_id="gateway",
        name="Gateway",
        protocol=PROTOCOL_MESHCORE,
        transport=transport,
        serial_path="/dev/ttyUSB0",
        options=dict(options or {}),
    )
    client = MeshCoreClient(
        None,
        config,
        _noop,
        _noop,
        _noop,
        logging.getLogger(__name__),
    )
    client._meshcore = SimpleNamespace(commands=commands)
    client.status.connected = True
    return client, commands


def _fields(snapshot: dict) -> dict[str, dict]:
    return {
        field["path"]: field
        for category in snapshot["categories"]
        for field in category["fields"]
    }


def test_meshcore_snapshot_is_live_bounded_and_secret_free() -> None:
    async def run() -> None:
        client, _commands = _client(options={"pin": "123456"})

        snapshot = await client.async_get_settings_snapshot()
        fields = _fields(snapshot)

        assert snapshot["writable"] is True
        assert fields["identity.name"]["value"] == "Gateway"
        assert fields["security.pin"]["configured"] is True
        assert "value" not in fields["security.pin"]
        assert fields["channels.0.secret"]["configured"] is True
        assert "value" not in fields["channels.0.secret"]
        for path in (
            "radio.frequency_mhz",
            "radio.bandwidth_khz",
            "radio.spreading_factor",
            "radio.coding_rate",
        ):
            assert fields[path]["writable"] is False
        assert fields["radio.repeat"]["writable"] is False
        assert fields["radio.tuning"]["writable"] is False
        rendered = repr(snapshot)
        assert "123456" not in rendered
        assert "11" * 16 not in rendered
        assert "ab" * 32 not in rendered
        private_material = snapshot["_secret_revision_material"]
        assert private_material["security.pin"] == 123456
        assert private_material["channels.0.secret"] == bytes.fromhex(
            "11" * 16
        )
        assert "123456" not in repr(private_material)

    asyncio.run(run())


def test_meshcore_raw_settings_repr_hides_sensitive_payloads() -> None:
    raw = _RawSettings(
        device={"ble_pin": 123456},
        self_info={"private_key": "private-sentinel"},
        channels={
            0: {
                "channel_name": "Ops",
                "channel_secret": bytes.fromhex("ab" * 16),
            }
        },
    )

    rendered = repr(raw)
    assert "123456" not in rendered
    assert "private-sentinel" not in rendered
    assert "ab" * 16 not in rendered


def test_meshcore_configuration_events_never_enter_packet_history() -> None:
    async def run() -> None:
        client, _commands = _client()
        emitted = []

        async def emit(packet) -> None:
            emitted.append(packet)

        client._emit_packet = emit
        await client._handle_native_event(
            _event("device_info", {"ble_pin": 123456, "model": "radio"})
        )
        await client._handle_native_event(
            _event(
                "channel_info",
                {"channel_name": "Ops", "channel_secret": bytes.fromhex("11" * 16)},
            )
        )
        await client._handle_native_event(
            _event("self_info", {"public_key": "ab" * 32, "name": "radio"})
        )

        assert emitted == []

    asyncio.run(run())


def test_meshcore_bridge_settings_are_explicitly_read_only() -> None:
    async def run() -> None:
        client, commands = _client(transport=TRANSPORT_MQTT)
        client._meshcore = None

        snapshot = await client.async_get_settings_snapshot()

        assert snapshot["writable"] is False
        assert "standardized" in snapshot["read_only_reason"]
        assert commands.calls == []

    asyncio.run(run())


def test_meshcore_coordinates_require_a_complete_finite_in_range_pair() -> None:
    async def run() -> None:
        coordinate_states = [
            {"adv_lat": 41.1},
            {"adv_lat": float("nan"), "adv_lon": -87.2},
            {"adv_lat": 91.0, "adv_lon": -87.2},
            {"adv_lat": 41.1, "adv_lon": -181.0},
        ]
        for coordinates in coordinate_states:
            commands = FakeCommands()
            commands.self_info.pop("adv_lat")
            commands.self_info.pop("adv_lon")
            commands.self_info.update(coordinates)
            client, _commands = _client(commands)

            snapshot = await client.async_get_settings_snapshot()
            fields = _fields(snapshot)
            assert fields["position.latitude"]["writable"] is False
            assert fields["position.longitude"]["writable"] is False

            with pytest.raises(MeshCoreSettingsValidationError):
                await client.async_apply_settings_plan(
                    {"position.latitude": 40.0}
                )
            assert not any(call.startswith("set_") for call in commands.calls)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("adv_loc_policy", 2),
        ("telemetry_mode_base", 3),
        ("telemetry_mode_loc", -1),
        ("telemetry_mode_env", 1.0),
        ("manual_add_contacts", 2),
        ("multi_acks", 2),
    ],
)
def test_meshcore_other_group_requires_exact_preserved_domains(
    key: str, value: object
) -> None:
    async def run() -> None:
        commands = FakeCommands()
        commands.self_info[key] = value
        client, _commands = _client(commands)

        snapshot = await client.async_get_settings_snapshot()
        fields = _fields(snapshot)
        for path in (
            "position.advertise",
            "telemetry.base_mode",
            "telemetry.location_mode",
            "telemetry.environment_mode",
            "contacts.manual_add",
        ):
            assert fields[path]["writable"] is False

        with pytest.raises(MeshCoreSettingsValidationError):
            await client.async_apply_settings_plan({"position.advertise": True})
        assert "set_other" not in commands.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "existing_name",
    ["", "#derived", "é" * 17, "\ud800", b"Ops"],
    ids=["empty", "hash-derived", "over-32-utf8-bytes", "invalid-unicode", "not-text"],
)
def test_meshcore_channel_writes_require_a_safe_existing_name(
    existing_name: object,
) -> None:
    async def run() -> None:
        commands = FakeCommands()
        commands.channels[0]["channel_name"] = existing_name
        client, _commands = _client(commands)

        snapshot = await client.async_get_settings_snapshot()
        fields = _fields(snapshot)
        assert fields["channels.0.name"]["writable"] is False
        assert fields["channels.0.secret"]["writable"] is False

        with pytest.raises(MeshCoreSettingsValidationError):
            await client.async_apply_settings_plan({"channels.0.name": "Safe"})
        assert "set_channel:0" not in commands.calls

    asyncio.run(run())


def test_meshcore_apply_groups_writes_verifies_and_changes_pin_last() -> None:
    async def run() -> None:
        client, commands = _client(
            transport=TRANSPORT_BLUETOOTH, options={"pin": "123456"}
        )
        replacement_secret = "7f" * 16

        result = await client.async_apply_settings_plan(
            {
                "identity.name": "Home base",
                "position.latitude": 40.123456,
                "telemetry.base_mode": 2,
                "radio.tx_power_dbm": 21,
                "channels.0.secret": {
                    "operation": "replace",
                    "value": replacement_secret,
                },
                "security.pin": {"operation": "replace", "value": "654321"},
            }
        )

        setters = [call for call in commands.calls if call.startswith("set_")]
        assert setters == [
            "set_name",
            "set_coords",
            "set_other",
            "set_tx_power",
            "set_channel:0",
            "set_pin",
        ]
        assert result["verified"] == sorted(
            {
                "identity.name",
                "position.latitude",
                "telemetry.base_mode",
                "radio.tx_power_dbm",
                "channels.0.secret",
                "security.pin",
            }
        )
        assert result["reconnect_required"] is True
        assert client.config.options["pin"] == "123456"
        assert result["connection_updates"]["pin"] == "654321"
        assert "654321" not in repr(result)
        assert replacement_secret not in repr(result)

    asyncio.run(run())


def test_meshcore_secret_writes_suppress_sdk_logs(caplog) -> None:
    class LoggingCommands(FakeCommands):
        async def send_device_query(self):
            logging.getLogger("meshcore").debug(
                "raw device info includes pin %s", self.device["ble_pin"]
            )
            return await super().send_device_query()

        async def get_channel(self, index: int):
            logging.getLogger("meshcore.reader").debug(
                "raw channel %s", self.channels[index]["channel_secret"].hex()
            )
            return await super().get_channel(index)

        async def set_channel(self, index: int, name: str, secret: bytes):
            logging.getLogger("meshcore").debug("raw write %s", secret.hex())
            return await super().set_channel(index, name, secret)

        async def set_devicepin(self, pin: int):
            logging.getLogger("meshcore.commands").debug("setting pin %s", pin)
            return await super().set_devicepin(pin)

    async def run() -> None:
        client, _commands = _client(LoggingCommands())
        await client.async_apply_settings_plan(
            {
                "channels.0.secret": {
                    "operation": "replace",
                    "value": "de" * 16,
                },
                "security.pin": {"operation": "replace", "value": "654321"},
            }
        )

    caplog.set_level(logging.DEBUG)
    asyncio.run(run())
    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "123456" not in rendered
    assert "654321" not in rendered
    assert "11" * 16 not in rendered
    assert "de" * 16 not in rendered


def test_meshcore_timeout_is_reread_without_write_retry() -> None:
    class AckLostCommands(FakeCommands):
        async def set_name(self, value: str):
            self.calls.append("set_name")
            self.self_info["name"] = value
            return _event("command_error", {"reason": "timeout"})

    async def run() -> None:
        client, commands = _client(AckLostCommands())

        result = await client.async_apply_settings_plan(
            {"identity.name": "Confirmed after timeout"}
        )

        assert commands.calls.count("set_name") == 1
        assert result["verified"] == ["identity.name"]
        assert result["warnings"] == [
            "write_confirmed_after_timeout_without_retry"
        ]

    asyncio.run(run())


def test_meshcore_mismatched_readback_stops_all_later_writes() -> None:
    class AckButIgnoreNameCommands(FakeCommands):
        async def set_name(self, _value: str):
            self.calls.append("set_name")
            return _event("command_ok")

    async def run() -> None:
        client, commands = _client(AckButIgnoreNameCommands())

        result = await client.async_apply_settings_plan(
            {
                "identity.name": "Not actually changed",
                "radio.tx_power_dbm": 21,
            }
        )

        assert [call for call in commands.calls if call.startswith("set_")] == [
            "set_name"
        ]
        assert result["applied"] == ["identity.name"]
        assert result["verified"] == []
        assert result["warnings"] == [
            "write_acknowledged_readback_mismatch",
            "plan_stopped_after_unverified_write",
        ]

    asyncio.run(run())


def test_meshcore_unavailable_readback_stops_all_later_writes() -> None:
    class UnavailableNameReadbackCommands(FakeCommands):
        self_reads = 0

        async def send_appstart(self):
            self.self_reads += 1
            if self.self_reads > 1:
                raise TimeoutError
            return await super().send_appstart()

    async def run() -> None:
        client, commands = _client(UnavailableNameReadbackCommands())

        result = await client.async_apply_settings_plan(
            {
                "identity.name": "Written but unreadable",
                "radio.tx_power_dbm": 21,
            }
        )

        assert [call for call in commands.calls if call.startswith("set_")] == [
            "set_name"
        ]
        assert result["applied"] == ["identity.name"]
        assert result["verified"] == []
        assert result["warnings"] == [
            "write_acknowledged_readback_unavailable",
            "plan_stopped_after_unverified_write",
        ]

    asyncio.run(run())


def test_meshcore_unconfirmed_timeout_is_unknown_and_pin_is_not_saved() -> None:
    class LostPinCommands(FakeCommands):
        async def set_devicepin(self, _pin: int):
            self.calls.append("set_pin")
            return _event("command_error", {"reason": "no_event_received"})

    async def run() -> None:
        client, commands = _client(LostPinCommands(), options={"pin": "123456"})

        with pytest.raises(MeshCoreSettingsUnknownState):
            await client.async_apply_settings_plan(
                {
                    "security.pin": {
                        "operation": "replace",
                        "value": "654321",
                    }
                }
            )

        assert commands.calls.count("set_pin") == 1
        assert client.config.options["pin"] == "123456"

    asyncio.run(run())


def test_meshcore_adapter_rejects_unsafe_radio_channel_and_pin_values() -> None:
    async def run() -> None:
        client, commands = _client()
        invalid_plans = [
            {"radio.frequency_mhz": 5000.0},
            {"channels.0.name": "#implicit-key-change"},
            {
                "channels.0.secret": {
                    "operation": "replace",
                    "value": "not-hex",
                }
            },
            {"security.pin": {"operation": "replace", "value": "012345"}},
        ]
        for plan in invalid_plans:
            with pytest.raises(MeshCoreSettingsValidationError):
                await client.async_apply_settings_plan(plan)

        assert not any(call.startswith("set_") for call in commands.calls)

    asyncio.run(run())


def test_meshcore_validates_full_plan_before_any_partial_write() -> None:
    async def run() -> None:
        client, commands = _client()

        with pytest.raises(MeshCoreSettingsValidationError):
            await client.async_apply_settings_plan(
                {
                    "identity.name": "Must not be written",
                    "channels.0.name": "Also not written",
                    "channels.0.secret": {"operation": "clear"},
                }
            )

        assert not any(call.startswith("set_") for call in commands.calls)
        assert commands.self_info["name"] == "Gateway"

    asyncio.run(run())


def test_meshcore_verified_pin_clear_returns_internal_persistence_update() -> None:
    async def run() -> None:
        client, _commands = _client(options={"pin": "123456"})

        result = await client.async_apply_settings_plan(
            {"security.pin": {"operation": "clear"}}
        )

        assert client.config.options["pin"] == "123456"
        assert result["connection_updates"]["pin"] is None
        assert repr(result["connection_updates"]) == "{'pin': None}"

    asyncio.run(run())


def test_meshcore_settings_share_command_lock_with_message_send() -> None:
    class BlockingCommands(FakeCommands):
        def __init__(self) -> None:
            super().__init__()
            self.query_started = asyncio.Event()
            self.release_query = asyncio.Event()

        async def send_device_query(self):
            self.calls.append("get_device")
            self.query_started.set()
            await self.release_query.wait()
            return _event("device_info", deepcopy(self.device))

    async def run() -> None:
        commands = BlockingCommands()
        client, _commands = _client(commands)

        snapshot_task = asyncio.create_task(client.async_get_settings_snapshot())
        await commands.query_started.wait()
        send_task = asyncio.create_task(
            client._native_send_message(
                target_node=None,
                message="hello",
                channel="0",
                message_type=MESSAGE_TYPE_BROADCAST,
            )
        )
        await asyncio.sleep(0)
        assert "send_message" not in commands.calls

        commands.release_query.set()
        await snapshot_task
        await send_task
        assert commands.calls[-1] == "send_message"

    asyncio.run(run())


def test_meshcore_lifecycle_change_during_write_stops_later_commands() -> None:
    class DisconnectingCommands(FakeCommands):
        client: MeshCoreClient

        async def set_name(self, value: str):
            self.calls.append("set_name")
            self.self_info["name"] = value
            self.client._lifecycle_epoch += 1
            raise RuntimeError("provider failure must not mask disconnect")

    async def run() -> None:
        commands = DisconnectingCommands()
        client, _commands = _client(commands)
        commands.client = client

        with pytest.raises(MeshCoreSettingsUnavailable, match="connection changed"):
            await client.async_apply_settings_plan(
                {
                    "identity.name": "Written before disconnect",
                    "radio.tx_power_dbm": 21,
                }
            )

        assert [call for call in commands.calls if call.startswith("set_")] == [
            "set_name"
        ]

    asyncio.run(run())


def test_meshcore_lifecycle_change_after_readback_stops_later_commands() -> None:
    class DisconnectingReadbackCommands(FakeCommands):
        client: MeshCoreClient
        self_reads = 0

        async def send_appstart(self):
            result = await super().send_appstart()
            self.self_reads += 1
            if self.self_reads == 2:
                self.client._lifecycle_epoch += 1
            return result

    async def run() -> None:
        commands = DisconnectingReadbackCommands()
        client, _commands = _client(commands)
        commands.client = client

        with pytest.raises(MeshCoreSettingsUnavailable, match="connection changed"):
            await client.async_apply_settings_plan(
                {
                    "identity.name": "Verified as connection changes",
                    "radio.tx_power_dbm": 21,
                }
            )

        assert [call for call in commands.calls if call.startswith("set_")] == [
            "set_name"
        ]

    asyncio.run(run())


def test_meshcore_cancelled_write_never_starts_later_commands() -> None:
    class BlockingCommands(FakeCommands):
        def __init__(self) -> None:
            super().__init__()
            self.write_started = asyncio.Event()
            self.release_write = asyncio.Event()

        async def set_name(self, value: str):
            self.calls.append("set_name")
            self.write_started.set()
            await self.release_write.wait()
            self.self_info["name"] = value
            return _event("command_ok")

    async def run() -> None:
        commands = BlockingCommands()
        client, _commands = _client(commands)
        apply_task = asyncio.create_task(
            client.async_apply_settings_plan(
                {
                    "identity.name": "Cancelled",
                    "radio.tx_power_dbm": 21,
                }
            )
        )
        await commands.write_started.wait()

        apply_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await apply_task

        assert [call for call in commands.calls if call.startswith("set_")] == [
            "set_name"
        ]

    asyncio.run(run())
