from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from typing import Any

import pytest

from custom_components.meshnet import meshtastic_client as meshtastic_client_module
from custom_components.meshnet.const import (
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_TCP,
)
from custom_components.meshnet.meshtastic_ble import MeshtasticBluetoothTransport
from custom_components.meshnet.meshtastic_client import MeshtasticClient
from custom_components.meshnet.models import GatewayConfig

ADAPTER_ADDRESS = "00:11:22:33:44:55"
OTHER_ADAPTER_ADDRESS = "00:11:22:33:44:66"


class _FakeHass:
    """Small Home Assistant shim that records accidental executor use."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.background_task_names: list[str] = []
        self.executor_targets: list[Any] = []

    @property
    def executor_calls(self) -> int:
        return len(self.executor_targets)

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    def async_create_background_task(self, coroutine, name):
        self.background_task_names.append(name)
        return asyncio.create_task(coroutine, name=name)

    async def async_add_executor_job(self, target, *args):
        self.executor_targets.append(target)
        return target(*args)


class _FakeBluetoothTransport:
    """Controllable implementation of the async Bluetooth transport contract."""

    def __init__(
        self,
        *,
        block_start: bool = False,
        block_stop: bool = False,
        block_send: bool = False,
        block_refresh: bool = False,
        resist_start_cancel: bool = False,
        resist_send_cancel: bool = False,
        fail_start: Exception | None = None,
        fail_stop: Exception | None = None,
        nodes: Mapping[Any, Mapping[str, Any]] | None = None,
    ) -> None:
        self.start_gate = asyncio.Event()
        self.stop_gate = asyncio.Event()
        self.send_gate = asyncio.Event()
        self.refresh_gate = asyncio.Event()
        if not block_start:
            self.start_gate.set()
        if not block_stop:
            self.stop_gate.set()
        if not block_send:
            self.send_gate.set()
        if not block_refresh:
            self.refresh_gate.set()

        self.start_entered = asyncio.Event()
        self.start_cancelled = asyncio.Event()
        self.stop_entered = asyncio.Event()
        self.send_entered = asyncio.Event()
        self.send_cancelled = asyncio.Event()
        self.refresh_entered = asyncio.Event()
        self.refresh_cancelled = asyncio.Event()

        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.resist_start_cancel = resist_start_cancel
        self.resist_send_cancel = resist_send_cancel
        self.nodes = dict(nodes or {})
        self.phase_callback = None
        self.state = "idle"
        self.active = False
        self.send_active = False
        self.refresh_active = False
        self.stop_overlapped_send = False
        self.stop_overlapped_refresh = False
        self.start_calls = 0
        self.stop_calls = 0
        self.send_calls: list[dict[str, Any]] = []
        self.refresh_calls = 0
        self.packet_callbacks: list[Any] = []
        self.connection_callbacks: list[Any] = []
        self.packet_unsubscribe_calls = 0
        self.connection_unsubscribe_calls = 0

    @property
    def connected(self) -> bool:
        return self.active

    def add_packet_callback(self, callback) -> Any:
        self.packet_callbacks.append(callback)

        def unsubscribe() -> None:
            self.packet_unsubscribe_calls += 1
            if callback in self.packet_callbacks:
                self.packet_callbacks.remove(callback)

        return unsubscribe

    def add_connection_callback(self, callback) -> Any:
        self.connection_callbacks.append(callback)

        def unsubscribe() -> None:
            self.connection_unsubscribe_calls += 1
            if callback in self.connection_callbacks:
                self.connection_callbacks.remove(callback)

        return unsubscribe

    async def emit_packet(self, packet: dict[str, Any]) -> None:
        for callback in tuple(self.packet_callbacks):
            result = callback(packet)
            if inspect.isawaitable(result):
                await result

    async def emit_connection(self, connected: bool) -> None:
        for callback in tuple(self.connection_callbacks):
            result = callback(connected)
            if inspect.isawaitable(result):
                await result

    async def async_start(self) -> None:
        self.start_calls += 1
        self.state = "connecting"
        if self.phase_callback is not None:
            self.phase_callback("bluetooth_connecting")
        self.start_entered.set()
        try:
            await self.start_gate.wait()
        except asyncio.CancelledError:
            self.state = "cancelled"
            self.start_cancelled.set()
            if not self.resist_start_cancel:
                raise
            await self.start_gate.wait()
        if self.fail_start is not None:
            self.state = "failed"
            raise self.fail_start
        self.active = True
        self.state = "active"
        if self.phase_callback is not None:
            self.phase_callback("bluetooth_active")

    async def async_stop(self) -> None:
        self.stop_calls += 1
        self.stop_overlapped_send = self.send_active
        self.stop_overlapped_refresh = self.refresh_active
        self.state = "stopping"
        self.stop_entered.set()
        await self.stop_gate.wait()
        if self.fail_stop is not None:
            self.state = "stop_failed"
            raise self.fail_stop
        self.active = False
        self.state = "stopped"

    async def async_send_text(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
    ) -> int:
        self.send_calls.append(
            {
                "target_node": target_node,
                "message": message,
                "channel": channel,
                "priority": priority,
                "message_type": message_type,
            }
        )
        self.send_active = True
        self.send_entered.set()
        try:
            await self.send_gate.wait()
        except asyncio.CancelledError:
            self.send_cancelled.set()
            if not self.resist_send_cancel:
                raise
            await self.send_gate.wait()
        finally:
            self.send_active = False
        return 0x12345678

    async def async_node_snapshot(self) -> dict[Any, dict[str, Any]]:
        self.refresh_calls += 1
        self.refresh_active = True
        self.refresh_entered.set()
        try:
            await self.refresh_gate.wait()
        except asyncio.CancelledError:
            self.refresh_cancelled.set()
            raise
        finally:
            self.refresh_active = False
        return {node_id: dict(node) for node_id, node in self.nodes.items()}

    def diagnostic_snapshot(self) -> dict[str, Any]:
        return {
            "implementation": type(self).__name__,
            "state": self.state,
            "active": self.active,
            "start_calls": self.start_calls,
            "stop_calls": self.stop_calls,
            "send_active": self.send_active,
            "refresh_active": self.refresh_active,
        }


@pytest.fixture(autouse=True)
def _verified_powered_local_adapter(monkeypatch) -> None:
    """Provide one verified local controller unless a test overrides it."""

    async def adapter_details():
        return {
            "hci0": {
                "org.bluez.Adapter1": {
                    "Address": ADAPTER_ADDRESS,
                    "Powered": True,
                }
            },
            "hci1": {
                "org.bluez.Adapter1": {
                    "Address": OTHER_ADAPTER_ADDRESS,
                    "Powered": False,
                }
            },
        }

    monkeypatch.setattr(
        meshtastic_client_module,
        "_async_get_local_bluetooth_adapter_details",
        adapter_details,
    )


def _client(
    hass: _FakeHass,
    transports: list[_FakeBluetoothTransport],
    *,
    statuses: list[bool] | None = None,
    nodes: list[Any] | None = None,
    packets: list[Any] | None = None,
) -> tuple[MeshtasticClient, list[str]]:
    status_updates = statuses if statuses is not None else []
    node_updates = nodes if nodes is not None else []
    packet_updates = packets if packets is not None else []

    async def on_packet(packet) -> None:
        packet_updates.append(packet)

    async def on_node(node) -> None:
        node_updates.append(node)

    async def on_status(status) -> None:
        status_updates.append(status.connected)

    client = MeshtasticClient(
        hass,
        GatewayConfig(
            gateway_id="ble-gateway",
            name="BLE Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_BLUETOOTH,
            ble_address="AA:BB:CC:DD:EE:FF",
            options={
                CONF_BLUETOOTH_ADAPTER: "hci0",
                CONF_BLUETOOTH_ADAPTER_ADDRESS: ADAPTER_ADDRESS,
            },
        ),
        on_packet,
        on_node,
        on_status,
        logging.getLogger(__name__),
    )
    adapters: list[str] = []
    remaining = list(transports)

    def make_transport(adapter: str) -> _FakeBluetoothTransport:
        adapters.append(adapter)
        if not remaining:
            raise AssertionError("unexpected Bluetooth transport construction")
        transport = remaining.pop(0)
        transport.phase_callback = client._set_startup_phase
        return transport

    def forbidden_native_constructor() -> Any:
        raise AssertionError("Bluetooth must not use _make_native_interface")

    client._make_bluetooth_transport = make_transport
    client._make_native_interface = forbidden_native_constructor
    return client, adapters


def test_meshtastic_ble_start_is_single_flight() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_start=True)
        statuses: list[bool] = []
        client, adapters = _client(hass, [transport], statuses=statuses)

        first_waiter = asyncio.create_task(client.async_start())
        await transport.start_entered.wait()
        second_waiter = asyncio.create_task(client.async_start())
        await asyncio.sleep(0)

        assert client.start_pending is True
        assert transport.start_calls == 1
        assert adapters == ["hci0"]
        assert hass.executor_calls == 0
        assert client._native_constructor_future is None

        transport.start_gate.set()
        await asyncio.gather(first_waiter, second_waiter)

        assert client._ble_transport is transport
        assert client._interface is None
        assert transport.refresh_calls == 1
        assert statuses == [True]
        assert hass.executor_calls == 0

        await client.async_stop()
        assert transport.stop_calls == 1
        assert client._native_lock is None

    asyncio.run(run())


def test_meshtastic_ble_diagnostics_track_async_connect_without_identity() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_start=True)
        client, _adapters = _client(hass, [transport])

        start_waiter = asyncio.create_task(client.async_start())
        await transport.start_entered.wait()

        diagnostics = client.diagnostic_snapshot()

        assert diagnostics["startup_phase"] == "bluetooth_connecting"
        assert diagnostics["startup_elapsed_seconds"] is not None
        assert diagnostics["startup_phase_elapsed_seconds"] is not None
        assert diagnostics["native_constructor_state"] == "not_created"
        assert diagnostics["native_constructor_pending"] is False
        assert diagnostics["native_executor_operation_count"] == 0
        assert diagnostics["native_subscription_count"] == 0
        assert diagnostics["last_start_outcome"] == "pending"
        assert diagnostics["bluetooth_adapter_validation"] == {
            "status": "passed",
            "adapter_count": 2,
            "powered_adapter_count": 1,
            "saved_adapter_is_powered": True,
            "selected_adapter_path_count": 1,
        }
        assert diagnostics["bluetooth_transport"]["state"] == "connecting"
        serialized = repr(diagnostics)
        assert "AA:BB:CC:DD:EE:FF" not in serialized
        assert ADAPTER_ADDRESS not in serialized

        transport.start_gate.set()
        await start_waiter

        completed = client.diagnostic_snapshot()
        assert completed["startup_phase"] == "ready"
        assert completed["last_start_outcome"] == "succeeded"
        assert completed["last_start_duration_seconds"] is not None
        assert completed["bluetooth_transport"]["state"] == "active"

        await client.async_stop()

    asyncio.run(run())


def test_ble_runtime_follows_stable_adapter_mac_after_hci_renumber(
    monkeypatch,
) -> None:
    async def run() -> None:
        async def adapter_details():
            return {
                "hci7": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                }
            }

        monkeypatch.setattr(
            meshtastic_client_module,
            "_async_get_local_bluetooth_adapter_details",
            adapter_details,
        )
        hass = _FakeHass()
        transport = _FakeBluetoothTransport()
        client, adapters = _client(hass, [transport])

        await client.async_start()

        assert adapters == ["hci7"]
        assert transport.active is True
        assert client.diagnostic_snapshot()["bluetooth_adapter_validation"] == {
            "status": "passed",
            "adapter_count": 1,
            "powered_adapter_count": 1,
            "saved_adapter_is_powered": True,
            "selected_adapter_path_count": 1,
        }
        await client.async_stop()

    asyncio.run(run())


def test_ble_allows_other_powered_adapters_when_verified_path_is_unique(
    monkeypatch,
) -> None:
    async def run() -> None:
        async def adapter_details():
            return {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
                "hci1": {
                    "org.bluez.Adapter1": {
                        "Address": OTHER_ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
            }

        monkeypatch.setattr(
            meshtastic_client_module,
            "_async_get_local_bluetooth_adapter_details",
            adapter_details,
        )
        hass = _FakeHass()
        transport = _FakeBluetoothTransport()
        client, adapters = _client(hass, [transport])

        await client.async_start()

        assert adapters == ["hci0"]
        validation = client.diagnostic_snapshot()["bluetooth_adapter_validation"]
        assert validation["powered_adapter_count"] == 2
        assert validation["selected_adapter_path_count"] == 1
        await client.async_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("clear_options", "details"),
    [
        (
            True,
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                }
            },
        ),
        (
            False,
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": False,
                    }
                }
            },
        ),
        (
            False,
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": OTHER_ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                }
            },
        ),
        (
            False,
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
                "hci7": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
            },
        ),
        (
            False,
            {
                "not-an-adapter": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                }
            },
        ),
        (False, {"hci0": {"org.bluez.Adapter1": {}}}),
    ],
)
def test_invalid_ble_adapter_fails_before_backend_construction(
    monkeypatch,
    clear_options: bool,
    details: dict[str, Any],
) -> None:
    async def run() -> None:
        async def adapter_details():
            return details

        monkeypatch.setattr(
            meshtastic_client_module,
            "_async_get_local_bluetooth_adapter_details",
            adapter_details,
        )
        hass = _FakeHass()
        transport = _FakeBluetoothTransport()
        client, adapters = _client(hass, [transport])
        if clear_options:
            client.config.options.clear()

        with pytest.raises(RuntimeError):
            await client.async_start()

        assert adapters == []
        assert transport.start_calls == 0
        assert client._ble_transport is None
        assert client._interface is None
        assert client._native_lock is None
        assert hass.executor_calls == 0

    asyncio.run(run())


def test_ble_stop_during_start_cancels_and_awaits_transport(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(meshtastic_client_module, "_STOP_WAIT_TIMEOUT", 0.01)
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_start=True)
        statuses: list[bool] = []
        client, _adapters = _client(hass, [transport], statuses=statuses)

        start_waiter = asyncio.create_task(client.async_start())
        await transport.start_entered.wait()

        await asyncio.wait_for(client.async_stop(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await start_waiter

        assert transport.start_cancelled.is_set()
        assert transport.stop_calls == 1
        assert transport.active is False
        assert client._ble_transport is None
        assert client._interface is None
        assert client._native_lock is None
        assert client.status.connected is False
        assert True not in statuses
        assert hass.executor_calls == 0

    asyncio.run(run())


def test_ble_stop_is_bounded_when_start_resists_cancellation(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(meshtastic_client_module, "_STOP_WAIT_TIMEOUT", 0.01)
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(
            block_start=True,
            resist_start_cancel=True,
        )
        client, _adapters = _client(hass, [transport])

        start_waiter = asyncio.create_task(client.async_start())
        await transport.start_entered.wait()

        with pytest.raises(RuntimeError, match="startup did not stop within"):
            await asyncio.wait_for(client.async_stop(), timeout=0.5)

        assert transport.start_cancelled.is_set()
        assert transport.stop_calls == 0
        assert client._ble_transport is transport
        assert client._native_lock is not None
        assert client._native_lock.locked()

        # The original start path observes _stopping and owns teardown once the
        # cancellation-resistant platform call finally yields.
        transport.start_gate.set()
        await asyncio.wait_for(start_waiter, timeout=0.5)
        await asyncio.wait_for(transport.stop_entered.wait(), timeout=0.5)
        for _ in range(10):
            if client._ble_transport is None:
                break
            await asyncio.sleep(0)

        assert transport.stop_calls >= 1
        assert client._ble_transport is None
        assert client._native_lock is None

    asyncio.run(run())


def test_cancelled_public_waiter_does_not_duplicate_ble_start() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_start=True)
        client, adapters = _client(hass, [transport])

        cancelled_waiter = asyncio.create_task(client.async_start())
        await transport.start_entered.wait()
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        surviving_waiter = asyncio.create_task(client.async_start())
        await asyncio.sleep(0)
        assert transport.start_calls == 1
        assert adapters == ["hci0"]

        transport.start_gate.set()
        await surviving_waiter
        assert client._ble_transport is transport
        await client.async_stop()

    asyncio.run(run())


def test_failed_ble_start_releases_transport_and_lock_before_retry() -> None:
    async def run() -> None:
        hass = _FakeHass()
        failed = _FakeBluetoothTransport(fail_start=RuntimeError("cannot connect"))
        replacement = _FakeBluetoothTransport(block_start=True)
        client, adapters = _client(hass, [failed, replacement])

        with pytest.raises(RuntimeError, match="cannot connect"):
            await client.async_start()
        await asyncio.sleep(0)

        diagnostics = client.diagnostic_snapshot()
        assert diagnostics["last_start_failed_phase"] == "bluetooth_connecting"
        assert diagnostics["last_start_error_subtype"] == "RuntimeError"
        assert diagnostics["last_bluetooth_failure"] == {
            "exception_type": "RuntimeError",
            "error_subtype": "RuntimeError",
            "phase": "bluetooth_connecting",
            "cleanup_outcome": "confirmed",
            "cleanup_exception_type": None,
            "transport": {
                "implementation": "_FakeBluetoothTransport",
                "state": "failed",
                "active": False,
                "start_calls": 1,
                "stop_calls": 0,
                "send_active": False,
                "refresh_active": False,
            },
        }
        assert "AA:BB:CC:DD:EE:FF" not in repr(diagnostics)
        assert ADAPTER_ADDRESS not in repr(diagnostics)
        assert failed.stop_calls == 1
        assert client._ble_transport is None
        assert client._native_lock is None
        assert client.diagnostic_snapshot()["native_subscription_count"] == 0

        retry = asyncio.create_task(client.async_start())
        await replacement.start_entered.wait()

        # A pending retry must not erase the only evidence from the completed
        # failure. It is cleared only after a confirmed successful start.
        assert client.diagnostic_snapshot()["last_bluetooth_failure"] is not None
        replacement.start_gate.set()
        await retry

        assert adapters == ["hci0", "hci0"]
        assert replacement.start_calls == 1
        assert client._ble_transport is replacement
        assert client.diagnostic_snapshot()["last_bluetooth_failure"] is None
        await client.async_stop()

    asyncio.run(run())


def test_ble_failure_diagnostic_projection_rejects_identity_and_content() -> None:
    source = {
        "implementation": "SafeTransport",
        "state": "bluetooth_failed",
        "connect_attempts": 1,
        "address": "AA:BB:CC:DD:EE:FF",
        "message": "private mesh message",
        "device": object(),
        "last_transport_before_cleanup": {
            "state": "failed",
            "last_error_type": "BleakDBusError",
            "address": "AA:BB:CC:DD:EE:FF",
            "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
            "payload": b"private",
        },
    }

    class PollutedTransport:
        def diagnostic_snapshot(self) -> dict[str, Any]:
            return source

    projected = MeshtasticClient._safe_bluetooth_diagnostics(PollutedTransport())
    serialized = repr(projected)

    assert projected == {
        "implementation": "SafeTransport",
        "state": "bluetooth_failed",
        "connect_attempts": 1,
        "last_transport_before_cleanup": {
            "state": "failed",
            "last_error_type": "BleakDBusError",
        },
    }
    assert "AA:BB:CC:DD:EE:FF" not in serialized
    assert "private" not in serialized
    assert "/org/bluez" not in serialized

    # The retained projection is detached from the source mapping.
    source["state"] = "changed"
    source["last_transport_before_cleanup"]["state"] = "changed"
    assert projected["state"] == "bluetooth_failed"
    assert projected["last_transport_before_cleanup"]["state"] == "failed"


def test_neighbor_info_diagnostic_projection_is_bounded_and_identity_free() -> None:
    """Manual-request outcomes survive projection without accepting free text."""
    source = {
        "implementation": "MeshtasticBluetoothClient",
        "neighbor_info_request_count": 4,
        "neighbor_info_response_count": 1,
        "neighbor_info_timeout_count": 1,
        "neighbor_info_rejection_count": 2,
        "neighbor_info_cancellation_count": 3,
        "neighbor_info_send_failure_count": 1,
        "neighbor_info_disconnect_count": 1,
        "last_neighbor_info_outcome": "rejected",
        "last_neighbor_info_routing_error": "BAD_REQUEST",
        "neighbor_info_routing_error_counts": {
            "BAD_REQUEST": 2,
            "NO_ROUTE": -1,
            "NO_RESPONSE": True,
            "private target name": 4,
            "another private value": 2_147_483_648,
        },
    }

    class Transport:
        @staticmethod
        def diagnostic_snapshot() -> dict[str, Any]:
            return source

    projected = MeshtasticClient._safe_bluetooth_diagnostics(Transport())

    assert projected == {
        "implementation": "MeshtasticBluetoothClient",
        "neighbor_info_request_count": 4,
        "neighbor_info_response_count": 1,
        "neighbor_info_timeout_count": 1,
        "neighbor_info_rejection_count": 2,
        "neighbor_info_cancellation_count": 3,
        "neighbor_info_send_failure_count": 1,
        "neighbor_info_disconnect_count": 1,
        "last_neighbor_info_outcome": "rejected",
        "last_neighbor_info_routing_error": "BAD_REQUEST",
        "neighbor_info_routing_error_counts": {
            "BAD_REQUEST": 2,
            "UNKNOWN": 2_147_483_647,
        },
    }
    assert "private target name" not in repr(projected)
    assert "another private value" not in repr(projected)


def test_existing_disconnected_ble_transport_is_restarted_not_reported_ready() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport()
        client, adapters = _client(hass, [transport])

        await client.async_start()
        await asyncio.sleep(0)
        assert transport.start_calls == 1

        # Model a post-active supervisor failure that leaves the transport
        # object and endpoint lease present but no longer connected.
        transport.active = False
        client.status.connected = False

        await client.async_start()

        assert transport.start_calls == 2
        assert transport.active is True
        assert client.status.connected is True
        assert adapters == ["hci0"]
        await client.async_stop()

    asyncio.run(run())


def test_ble_transport_constructor_failure_never_acquires_endpoint_lock() -> None:
    async def run() -> None:
        hass = _FakeHass()
        failed_client, failed_adapters = _client(hass, [])

        def fail_transport_construction(adapter: str) -> Any:
            failed_adapters.append(adapter)
            raise RuntimeError("async Bluetooth dependency is unavailable")

        failed_client._make_bluetooth_transport = fail_transport_construction

        with pytest.raises(
            RuntimeError,
            match="async Bluetooth dependency is unavailable",
        ):
            await failed_client.async_start()

        assert failed_adapters == ["hci0"]
        assert failed_client._ble_transport is None
        assert failed_client._native_lock is None

        # A constructor error must not leave the shared endpoint lease held.
        replacement_transport = _FakeBluetoothTransport()
        replacement_client, replacement_adapters = _client(
            hass,
            [replacement_transport],
        )
        await asyncio.wait_for(replacement_client.async_start(), timeout=0.5)
        assert replacement_adapters == ["hci0"]
        await replacement_client.async_stop()

    asyncio.run(run())


def test_missing_ble_callback_api_stops_transport_and_releases_endpoint_lock() -> None:
    async def run() -> None:
        hass = _FakeHass()
        incomplete_transport = _FakeBluetoothTransport()
        incomplete_transport.add_connection_callback = None
        failed_client, failed_adapters = _client(hass, [incomplete_transport])

        with pytest.raises(
            RuntimeError,
            match="transport has no connection callback API",
        ):
            await failed_client.async_start()

        assert failed_adapters == ["hci0"]
        assert incomplete_transport.start_calls == 0
        assert incomplete_transport.stop_calls == 1
        assert incomplete_transport.packet_unsubscribe_calls == 1
        assert incomplete_transport.packet_callbacks == []
        assert failed_client._ble_transport is None
        assert failed_client._native_lock is None

        replacement_transport = _FakeBluetoothTransport()
        replacement_client, replacement_adapters = _client(
            hass,
            [replacement_transport],
        )
        await asyncio.wait_for(replacement_client.async_start(), timeout=0.5)
        assert replacement_adapters == ["hci0"]
        await replacement_client.async_stop()

    asyncio.run(run())


def test_successive_ble_clients_serialize_start_until_confirmed_stop() -> None:
    async def run() -> None:
        hass = _FakeHass()
        old_transport = _FakeBluetoothTransport(block_stop=True)
        new_transport = _FakeBluetoothTransport()
        old_client, old_adapters = _client(hass, [old_transport])
        new_client, new_adapters = _client(hass, [new_transport])

        await old_client.async_start()
        assert old_transport.active is True

        new_waiter = asyncio.create_task(new_client.async_start())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Construction is deliberately pre-lock and Bluetooth-free; the
        # transport itself must not start until the old teardown is confirmed.
        assert new_adapters == ["hci0"]
        assert new_transport.start_calls == 0

        old_stop = asyncio.create_task(old_client.async_stop())
        await old_transport.stop_entered.wait()
        await asyncio.sleep(0)
        assert old_transport.active is True
        assert new_adapters == ["hci0"]
        assert new_transport.start_calls == 0

        old_transport.stop_gate.set()
        await old_stop
        await asyncio.wait_for(new_transport.start_entered.wait(), timeout=0.5)
        await asyncio.wait_for(new_waiter, timeout=0.5)

        assert old_adapters == ["hci0"]
        assert new_adapters == ["hci0"]
        assert old_transport.active is False
        assert new_transport.active is True
        assert old_client._native_lock is None
        await new_client.async_stop()

    asyncio.run(run())


def test_failed_ble_stop_retains_endpoint_lock_and_blocks_replacement() -> None:
    async def run() -> None:
        hass = _FakeHass()
        old_transport = _FakeBluetoothTransport(
            fail_stop=RuntimeError("GATT disconnect failed")
        )
        new_transport = _FakeBluetoothTransport()
        old_client, _old_adapters = _client(hass, [old_transport])
        new_client, new_adapters = _client(hass, [new_transport])

        await old_client.async_start()

        with pytest.raises(RuntimeError, match="GATT disconnect failed"):
            await old_client.async_stop()

        assert old_client._ble_transport is old_transport
        assert old_client._native_lock is not None
        assert old_client._native_lock.locked()

        new_waiter = asyncio.create_task(new_client.async_start())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert new_adapters == ["hci0"]
        assert new_transport.start_calls == 0
        assert not new_waiter.done()

        new_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await new_waiter
        pending_start = new_client._start_task
        assert pending_start is not None
        pending_start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_start

    asyncio.run(run())


def test_resume_after_uncertain_stop_restores_transport_callbacks() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(
            fail_stop=RuntimeError("uncertain teardown")
        )
        packets: list[Any] = []
        client, _adapters = _client(hass, [transport], packets=packets)

        await client.async_start()
        assert len(transport.packet_callbacks) == 1
        assert len(transport.connection_callbacks) == 1

        with pytest.raises(RuntimeError, match="uncertain teardown"):
            await client.async_stop()
        assert client._ble_transport is transport
        assert transport.packet_callbacks == []
        assert transport.connection_callbacks == []

        transport.fail_stop = None
        await asyncio.sleep(0)
        await client.async_start()

        assert transport.start_calls == 1
        assert len(transport.packet_callbacks) == 1
        assert len(transport.connection_callbacks) == 1
        await transport.emit_packet(
            {
                "id": 18,
                "fromId": "!12345678",
                "decoded": {
                    "portnum": "TEXT_MESSAGE_APP",
                    "text": "callback restored",
                },
            }
        )
        assert [packet.text for packet in packets] == ["callback restored"]
        await client.async_stop()

    asyncio.run(run())


def test_ble_never_uses_sync_constructor_pubsub_or_executor() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_start=True)
        client, _adapters = _client(hass, [transport])

        start_waiter = asyncio.create_task(client.async_start())
        await transport.start_entered.wait()

        diagnostics = client.diagnostic_snapshot()
        assert client._interface is None
        assert client._native_constructor_future is None
        assert diagnostics["native_constructor_state"] == "not_created"
        assert diagnostics["native_subscription_count"] == 0
        assert diagnostics["native_executor_operation_count"] == 0
        assert hass.executor_calls == 0

        transport.start_gate.set()
        await start_waiter
        assert hass.executor_calls == 0
        await client.async_stop()

    asyncio.run(run())


def test_real_ble_transport_implements_client_async_contract() -> None:
    """Prevent fake-only tests from drifting away from the real transport."""
    for method_name in (
        "async_start",
        "async_stop",
        "async_send_text",
        "async_node_snapshot",
        "diagnostic_snapshot",
        "add_packet_callback",
        "add_connection_callback",
    ):
        assert callable(getattr(MeshtasticBluetoothTransport, method_name, None))


def test_ble_uses_transport_local_callbacks_and_unsubscribes_on_stop() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport()
        statuses: list[bool] = []
        packets: list[Any] = []
        client, _adapters = _client(
            hass,
            [transport],
            statuses=statuses,
            packets=packets,
        )

        await client.async_start()
        assert len(transport.packet_callbacks) == 1
        assert len(transport.connection_callbacks) == 1
        packet_callback = transport.packet_callbacks[0]
        connection_callback = transport.connection_callbacks[0]

        await transport.emit_packet(
            {
                "id": 17,
                "fromId": "!12345678",
                "decoded": {
                    "portnum": "TEXT_MESSAGE_APP",
                    "text": "local packet",
                },
            }
        )
        await transport.emit_connection(False)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(packets) == 1
        assert packets[0].text == "local packet"
        assert packets[0].gateway_id == "ble-gateway"
        # Startup reports connected, packet receipt refreshes the same status,
        # then the transport-local link event reports the disconnect.
        assert statuses == [True, True, False]

        await client.async_stop()
        assert transport.packet_callbacks == []
        assert transport.connection_callbacks == []
        assert transport.packet_unsubscribe_calls == 1
        assert transport.connection_unsubscribe_calls == 1
        post_stop_status_count = len(statuses)

        # Even an already-queued stale callback must not mutate an unloaded
        # client after ownership has been detached.
        await packet_callback({"id": 18, "decoded": {"text": "late"}})
        await connection_callback(True)
        await asyncio.sleep(0)
        assert len(packets) == 1
        assert len(statuses) == post_stop_status_count

    asyncio.run(run())


def test_ble_send_uses_async_transport_without_executor() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport()
        statuses: list[bool] = []
        client, _adapters = _client(hass, [transport], statuses=statuses)
        await client.async_start()

        message_id = await client.async_send_message(
            target_node="!12345678",
            message="local only",
            channel="2",
            priority="normal",
            message_type="direct",
        )

        assert message_id == "305419896"
        assert transport.send_calls == [
            {
                "target_node": "!12345678",
                "message": "local only",
                "channel": "2",
                "priority": "normal",
                "message_type": "direct",
            }
        ]
        assert client.status.packets_sent == 1
        assert statuses == [True, True]
        assert hass.executor_calls == 0
        with pytest.raises(ValueError, match="validated canonical node ID"):
            await client.async_send_message(
                target_node="cached name",
                message="must not reach provider resolution",
                channel="2",
                priority="normal",
                message_type="direct",
            )
        assert len(transport.send_calls) == 1
        await client.async_stop()

    asyncio.run(run())


def test_native_send_returns_provider_on_air_packet_id() -> None:
    """Serial/TCP sends retain the packet ID needed for exact reactions."""

    async def run() -> None:
        hass = _FakeHass()

        async def noop(_value) -> None:
            return None

        class SentPacket:
            id = 0x23456789

        class Interface:
            def sendText(self, *_args, **_kwargs):
                return SentPacket()

        client = MeshtasticClient(
            hass,
            GatewayConfig(
                gateway_id="tcp-gateway",
                name="TCP Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                host="192.0.2.1",
            ),
            noop,
            noop,
            noop,
            logging.getLogger(__name__),
        )
        client._interface = Interface()

        message_id = await client.async_send_message(
            target_node="!12345678",
            message="react to this",
            channel="0",
            priority="normal",
            message_type="direct",
        )

        assert message_id == str(0x23456789)
        assert hass.executor_calls == 1

    asyncio.run(run())


def test_ble_refresh_uses_async_node_snapshot_and_normalizes_nodes() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(
            nodes={
                305419896: {
                    "num": 305419896,
                    "user": {
                        "id": "!12345678",
                        "longName": "Backyard node",
                        "shortName": "BY",
                    },
                    "deviceMetrics": {"batteryLevel": 87},
                }
            }
        )
        nodes: list[Any] = []
        client, _adapters = _client(hass, [transport], nodes=nodes)

        await client.async_start()

        assert transport.refresh_calls == 1
        assert len(nodes) == 1
        assert nodes[0].node_id == "!12345678"
        assert nodes[0].long_name == "Backyard node"
        assert nodes[0].power["battery_level"] == 87.0
        assert nodes[0].last_gateway_id == "ble-gateway"
        assert hass.executor_calls == 0
        await client.async_stop()

    asyncio.run(run())


def test_cancelled_ble_send_finishes_before_transport_stop() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_send=True)
        client, _adapters = _client(hass, [transport])
        await client.async_start()

        send_task = asyncio.create_task(
            client.async_send_message(
                target_node=None,
                message="cancel me",
                channel=None,
                priority="normal",
                message_type="broadcast",
            )
        )
        await transport.send_entered.wait()
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task

        assert transport.send_cancelled.is_set()
        assert transport.send_active is False
        await client.async_stop()

        assert transport.stop_overlapped_send is False
        assert hass.executor_calls == 0

    asyncio.run(run())


def test_ble_stop_cancels_and_awaits_active_send_before_transport_stop() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_send=True)
        client, _adapters = _client(hass, [transport])
        await client.async_start()

        send_task = asyncio.create_task(
            client.async_send_message(
                target_node=None,
                message="stop owns cancellation",
                channel=None,
                priority="normal",
                message_type="broadcast",
            )
        )
        await transport.send_entered.wait()

        await asyncio.wait_for(client.async_stop(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await send_task

        assert transport.send_cancelled.is_set()
        assert transport.stop_calls == 1
        assert transport.stop_overlapped_send is False
        assert client._native_lock is None
        assert hass.executor_calls == 0

    asyncio.run(run())


def test_ble_stop_is_bounded_when_send_resists_cancellation(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(meshtastic_client_module, "_STOP_WAIT_TIMEOUT", 0.01)
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(
            block_send=True,
            resist_send_cancel=True,
        )
        replacement = _FakeBluetoothTransport()
        client, adapters = _client(hass, [transport, replacement])
        await client.async_start()

        send_task = asyncio.create_task(
            client.async_send_message(
                target_node=None,
                message="resist cancellation",
                channel=None,
                priority="normal",
                message_type="broadcast",
            )
        )
        await transport.send_entered.wait()

        with pytest.raises(RuntimeError, match="did not stop within"):
            await asyncio.wait_for(client.async_stop(), timeout=0.5)

        assert transport.send_cancelled.is_set()
        assert transport.stop_calls == 0
        assert client._ble_transport is transport
        assert client._native_lock is not None
        assert client._native_lock.locked()
        assert "MeshNet deferred Meshtastic Bluetooth cleanup" in (
            hass.background_task_names
        )

        # A restart cannot adopt a session that an older deferred owner is
        # already committed to tearing down.
        with pytest.raises(RuntimeError, match="cleanup is still pending"):
            await client.async_start()
        assert transport.start_calls == 1
        assert replacement.start_calls == 0

        # Teardown is retried automatically only after the I/O owner yields.
        transport.send_gate.set()
        await asyncio.wait_for(send_task, timeout=0.5)
        await asyncio.wait_for(transport.stop_entered.wait(), timeout=0.5)
        for _ in range(10):
            if client._ble_transport is None:
                break
            await asyncio.sleep(0)

        assert transport.stop_calls == 1
        assert transport.stop_overlapped_send is False
        assert client._ble_transport is None
        assert client._native_lock is None

        await client.async_start()
        assert replacement.start_calls == 1
        assert client._ble_transport is replacement
        assert adapters == ["hci0", "hci0"]
        await client.async_stop()

    asyncio.run(run())


def test_cancelled_ble_refresh_finishes_before_transport_stop() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport(block_refresh=True)
        client, _adapters = _client(hass, [transport])

        # Install an already connected transport so the deliberately blocked
        # snapshot does not also block the startup path.
        client._ble_transport = transport
        client.status.connected = True
        transport.active = True

        refresh_task = asyncio.create_task(client.async_refresh())
        await transport.refresh_entered.wait()
        refresh_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh_task

        assert transport.refresh_cancelled.is_set()
        assert transport.refresh_active is False
        await client.async_stop()

        assert transport.stop_overlapped_refresh is False
        assert hass.executor_calls == 0

    asyncio.run(run())


def test_ble_stop_cancels_and_awaits_active_refresh_before_transport_stop() -> None:
    async def run() -> None:
        hass = _FakeHass()
        transport = _FakeBluetoothTransport()
        client, _adapters = _client(hass, [transport])
        await client.async_start()

        transport.refresh_entered.clear()
        transport.refresh_gate.clear()
        refresh_task = asyncio.create_task(client.async_refresh())
        await transport.refresh_entered.wait()

        await asyncio.wait_for(client.async_stop(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await refresh_task

        assert transport.refresh_cancelled.is_set()
        assert transport.stop_calls == 1
        assert transport.stop_overlapped_refresh is False
        assert client._native_lock is None
        assert hass.executor_calls == 0

    asyncio.run(run())
