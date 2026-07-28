from __future__ import annotations

import asyncio
import logging
import sys
import types

import pytest

from custom_components.meshnet import meshtastic_client as meshtastic_client_module
from custom_components.meshnet.const import (
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
)
from custom_components.meshnet.meshtastic_client import MeshtasticClient
from custom_components.meshnet.models import GatewayConfig

ADAPTER_ADDRESS = "00:11:22:33:44:55"


class _ControlledExecutorHass:
    """Home Assistant shim that can hold the first executor job in flight."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.constructor_started = asyncio.Event()
        self.release_constructor = asyncio.Event()
        self.executor_calls = 0
        self.background_task_names: list[str] = []

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    def async_create_background_task(self, coroutine, name):
        self.background_task_names.append(name)
        return asyncio.create_task(coroutine)

    async def async_add_executor_job(self, target, *args):
        self.executor_calls += 1
        if self.executor_calls == 1:
            self.constructor_started.set()
            await self.release_constructor.wait()
        return target(*args)


class _FakeInterface:
    def __init__(self) -> None:
        self.nodes = {}
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _CancellationResistantExecutorHass:
    """Model a worker that continues after its asyncio waiter is cancelled."""

    def __init__(self) -> None:
        self.operation_started = asyncio.Event()
        self.release_operation = asyncio.Event()
        self.background_task_names: list[str] = []
        self.interface = None

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    def async_create_background_task(self, coroutine, name):
        self.background_task_names.append(name)
        return asyncio.create_task(coroutine)

    async def async_add_executor_job(self, target, *args):
        if getattr(target, "__self__", None) is self.interface:
            return target(*args)
        self.operation_started.set()
        try:
            await self.release_operation.wait()
        except asyncio.CancelledError:
            # A running executor function cannot actually be cancelled. Model
            # that ownership even if a wrapper task is cancelled by shutdown.
            await self.release_operation.wait()
        return target(*args)


class _BlockingNativeInterface:
    """Expose independently blocking send and refresh executor operations."""

    def __init__(self) -> None:
        self.send_started = False
        self.send_finished = False
        self.refresh_started = False
        self.refresh_finished = False
        self.close_started = asyncio.Event()
        self.close_calls = 0
        self.close_overlapped_send = False
        self.close_overlapped_refresh = False

    def sendText(self, *_args, **_kwargs) -> None:
        self.send_finished = True

    @property
    def nodes(self) -> dict:
        self.refresh_finished = True
        return {}

    def close(self) -> None:
        self.close_overlapped_send = self.send_started and not self.send_finished
        self.close_overlapped_refresh = (
            self.refresh_started and not self.refresh_finished
        )
        self.close_calls += 1
        self.close_started.set()


class _FakePub:
    def __init__(self) -> None:
        self.subscriptions = []

    def subscribe(self, handler, topic) -> None:
        self.subscriptions.append((handler, topic))

    def unsubscribe(self, handler, topic) -> None:
        self.subscriptions.remove((handler, topic))


def _install_fake_pubsub(monkeypatch) -> _FakePub:
    pubsub = types.ModuleType("pubsub")
    pub = _FakePub()
    pubsub.pub = pub
    monkeypatch.setitem(sys.modules, "pubsub", pubsub)
    return pub


@pytest.fixture(autouse=True)
def _one_powered_local_adapter(monkeypatch) -> None:
    """Model the only layout the adapter-less Meshtastic SDK can use safely."""

    async def adapter_details():
        return {
            "hci0": {
                "org.bluez.Adapter1": {
                    "Address": "00:11:22:33:44:55",
                    "Powered": True,
                }
            },
            "hci1": {
                "org.bluez.Adapter1": {
                    "Address": "00:11:22:33:44:66",
                    "Powered": False,
                }
            },
        }

    monkeypatch.setattr(
        meshtastic_client_module,
        "_async_get_local_bluetooth_adapter_details",
        adapter_details,
    )


def _client(hass, interface, statuses) -> MeshtasticClient:
    async def async_noop(_value) -> None:
        return None

    async def on_status(status) -> None:
        statuses.append(status.connected)

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
        async_noop,
        async_noop,
        on_status,
        logging.getLogger(__name__),
    )
    client._make_native_interface = lambda: interface
    return client


def test_meshtastic_ble_start_is_single_flight(monkeypatch) -> None:
    async def run() -> None:
        _install_fake_pubsub(monkeypatch)
        hass = _ControlledExecutorHass()
        interface = _FakeInterface()
        statuses = []
        client = _client(hass, interface, statuses)

        first_waiter = asyncio.create_task(client.async_start())
        await hass.constructor_started.wait()
        second_waiter = asyncio.create_task(client.async_start())
        await asyncio.sleep(0)

        assert client.start_pending is True
        assert hass.executor_calls == 1

        hass.release_constructor.set()
        await asyncio.gather(first_waiter, second_waiter)

        assert client._interface is interface
        assert statuses == [True]
        await client.async_stop()
        assert interface.close_calls == 1

    asyncio.run(run())


def test_meshtastic_ble_diagnostics_track_pending_constructor(monkeypatch) -> None:
    """A blocked SDK constructor must be visible without exposing its endpoint."""

    async def run() -> None:
        _install_fake_pubsub(monkeypatch)
        hass = _ControlledExecutorHass()
        interface = _FakeInterface()
        client = _client(hass, interface, [])

        start_waiter = asyncio.create_task(client.async_start())
        await hass.constructor_started.wait()

        diagnostics = client.diagnostic_snapshot()

        assert diagnostics["startup_phase"] == "constructing_interface"
        assert diagnostics["startup_elapsed_seconds"] is not None
        assert diagnostics["startup_phase_elapsed_seconds"] is not None
        assert diagnostics["native_constructor_state"] == "pending"
        assert diagnostics["native_constructor_pending"] is True
        assert diagnostics["native_executor_operation_count"] == 1
        assert diagnostics["last_start_outcome"] == "pending"
        assert diagnostics["bluetooth_adapter_validation"] == {
            "status": "passed",
            "adapter_count": 2,
            "powered_adapter_count": 1,
            "saved_adapter_is_powered": True,
            "only_powered_adapter_matches": True,
        }
        serialized = repr(diagnostics)
        assert "AA:BB:CC:DD:EE:FF" not in serialized
        assert ADAPTER_ADDRESS not in serialized

        hass.release_constructor.set()
        await start_waiter
        await asyncio.sleep(0)

        completed = client.diagnostic_snapshot()
        assert completed["startup_phase"] == "ready"
        assert completed["native_constructor_pending"] is False
        assert completed["native_executor_operation_count"] == 0
        assert completed["last_start_outcome"] == "succeeded"
        assert completed["last_start_duration_seconds"] is not None

        await client.async_stop()

    asyncio.run(run())


def test_cancelled_internal_start_closes_late_constructor_before_replacement(
    monkeypatch,
) -> None:
    """HA owner cancellation cannot orphan an interface or overlap replacement."""

    async def run() -> None:
        _install_fake_pubsub(monkeypatch)
        hass = _ControlledExecutorHass()
        old_interface = _FakeInterface()
        new_interface = _FakeInterface()
        old_client = _client(hass, old_interface, [])
        new_client = _client(hass, new_interface, [])

        old_waiter = asyncio.create_task(old_client.async_start())
        await hass.constructor_started.wait()
        old_owner = old_client._start_task
        assert old_owner is not None

        old_owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old_waiter
        await asyncio.sleep(0)

        abandoned = old_client.diagnostic_snapshot()
        assert abandoned["native_constructor_abandoned"] is True
        assert abandoned["native_constructor_abandonment_count"] == 1
        assert abandoned["native_constructor_pending"] is True
        assert abandoned["native_endpoint_lock_held"] is True

        replacement_waiter = asyncio.create_task(new_client.async_start())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert hass.executor_calls == 1
        assert new_client._native_lock is None

        hass.release_constructor.set()
        await asyncio.wait_for(replacement_waiter, timeout=0.2)
        await asyncio.sleep(0)

        assert old_interface.close_calls == 1
        assert old_client._native_constructor_future is None
        assert old_client._native_constructor_cleanup_task is None
        assert old_client._native_lock is None
        assert old_client._native_constructor_abandoned is False
        assert new_client._interface is new_interface
        # Old constructor + old close + replacement constructor + replacement
        # node refresh. The replacement cannot reach either of its calls until
        # the old close releases the endpoint lock.
        assert hass.executor_calls == 4

        await new_client.async_stop()
        assert new_interface.close_calls == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("saved_adapter", "details"),
    [
        (
            "hci0",
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
                "hci1": {
                    "org.bluez.Adapter1": {
                        "Address": "00:11:22:33:44:66",
                        "Powered": True,
                    }
                },
            },
        ),
        (
            "hci1",
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": "00:11:22:33:44:66",
                        "Powered": True,
                    }
                }
            },
        ),
        (
            "hci0",
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
            "hci0",
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
                "hci1": {"org.bluez.Adapter1": {}},
            },
        ),
        (
            "hci0",
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
                "hci1": {
                    "org.bluez.Adapter1": {
                        "Address": "00:11:22:33:44:66",
                        "Powered": "unknown",
                    }
                },
            },
        ),
        (
            "hci0",
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
                "hci1": {},
            },
        ),
        (
            "hci0",
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                },
                "not-an-adapter": {
                    "org.bluez.Adapter1": {
                        "Address": "00:11:22:33:44:66",
                        "Powered": False,
                    }
                },
            },
        ),
        (
            None,
            {
                "hci0": {
                    "org.bluez.Adapter1": {
                        "Address": ADAPTER_ADDRESS,
                        "Powered": True,
                    }
                }
            },
        ),
    ],
)
def test_ble_start_fails_closed_before_sdk_constructor(
    monkeypatch, saved_adapter, details
) -> None:
    async def run() -> None:
        _install_fake_pubsub(monkeypatch)

        async def adapter_details():
            return details

        monkeypatch.setattr(
            meshtastic_client_module,
            "_async_get_local_bluetooth_adapter_details",
            adapter_details,
        )
        hass = _ControlledExecutorHass()
        hass.release_constructor.set()
        interface = _FakeInterface()
        client = _client(hass, interface, [])
        if saved_adapter is None:
            client.config.options.clear()
        else:
            client.config.options[CONF_BLUETOOTH_ADAPTER] = saved_adapter
        constructor_calls = 0

        def make_interface():
            nonlocal constructor_calls
            constructor_calls += 1
            return interface

        client._make_native_interface = make_interface

        with pytest.raises(RuntimeError, match="Bluetooth"):
            await client.async_start()
        assert constructor_calls == 0
        assert hass.executor_calls == 0
        assert client._interface is None

    asyncio.run(run())


def test_ble_runtime_follows_stable_adapter_identity_after_hci_renumber(
    monkeypatch,
) -> None:
    async def run() -> None:
        _install_fake_pubsub(monkeypatch)

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
        hass = _ControlledExecutorHass()
        hass.release_constructor.set()
        interface = _FakeInterface()
        client = _client(hass, interface, [])

        await client.async_start()

        assert client._interface is interface
        await client.async_stop()

    asyncio.run(run())


def test_meshtastic_ble_stop_disposes_late_constructor_result(monkeypatch) -> None:
    async def run() -> None:
        _install_fake_pubsub(monkeypatch)
        monkeypatch.setattr(meshtastic_client_module, "_STOP_WAIT_TIMEOUT", 0.01)
        hass = _ControlledExecutorHass()
        interface = _FakeInterface()
        statuses = []
        client = _client(hass, interface, statuses)

        start_waiter = asyncio.create_task(client.async_start())
        await hass.constructor_started.wait()
        stop_waiter = asyncio.create_task(client.async_stop())
        await asyncio.wait_for(stop_waiter, timeout=0.2)

        assert stop_waiter.done() is True
        assert hass.executor_calls == 1
        assert client._interface is None
        assert client.status.connected is False

        hass.release_constructor.set()
        await start_waiter
        await asyncio.sleep(0)

        assert interface.close_calls == 1
        assert client._interface is None
        assert client.status.connected is False
        assert True not in statuses

        # A completed unload must not spawn another constructor on its own.
        await asyncio.sleep(0)
        assert hass.executor_calls == 2

    asyncio.run(run())


def test_failed_start_does_not_duplicate_pubsub_subscriptions(monkeypatch) -> None:
    async def run() -> None:
        pub = _install_fake_pubsub(monkeypatch)
        hass = _ControlledExecutorHass()
        hass.release_constructor.set()
        interface = _FakeInterface()
        statuses = []
        client = _client(hass, interface, statuses)
        constructor_calls = 0

        def make_interface():
            nonlocal constructor_calls
            constructor_calls += 1
            if constructor_calls == 1:
                raise RuntimeError("cannot connect")
            return interface

        client._make_native_interface = make_interface

        with pytest.raises(RuntimeError, match="cannot connect"):
            await client.async_start()
        await asyncio.sleep(0)

        assert pub.subscriptions == []
        await client.async_start()
        assert len(pub.subscriptions) == 3
        assert len(set(pub.subscriptions)) == 3

        await client.async_stop()
        assert pub.subscriptions == []

    asyncio.run(run())


def test_cancelled_start_waiter_does_not_duplicate_ble_constructor(monkeypatch) -> None:
    async def run() -> None:
        _install_fake_pubsub(monkeypatch)
        hass = _ControlledExecutorHass()
        interface = _FakeInterface()
        statuses = []
        client = _client(hass, interface, statuses)

        cancelled_waiter = asyncio.create_task(client.async_start())
        await hass.constructor_started.wait()
        cancelled_waiter.cancel()
        try:
            await cancelled_waiter
        except asyncio.CancelledError:
            pass

        surviving_waiter = asyncio.create_task(client.async_start())
        await asyncio.sleep(0)
        assert hass.executor_calls == 1

        hass.release_constructor.set()
        await surviving_waiter
        assert client._interface is interface
        await client.async_stop()

    asyncio.run(run())


def test_successive_ble_clients_serialize_constructor_and_close_late_result(
    monkeypatch,
) -> None:
    """A replacement client must wait for the old late result to be closed."""

    class EndpointExecutorHass(_ControlledExecutorHass):
        def __init__(self) -> None:
            super().__init__()
            self.old_constructor_started = asyncio.Event()
            self.release_old_constructor = asyncio.Event()
            self.old_close_started = asyncio.Event()
            self.release_old_close = asyncio.Event()
            self.new_constructor_started = asyncio.Event()
            self.constructor_submissions = 0
            self.active_constructors = 0
            self.max_active_constructors = 0
            self.old_interface = None

        async def async_add_executor_job(self, target, *args):
            if target.__name__.startswith("make_"):
                self.constructor_submissions += 1
                self.active_constructors += 1
                self.max_active_constructors = max(
                    self.max_active_constructors,
                    self.active_constructors,
                )
                try:
                    if target.__name__ == "make_old_interface":
                        self.old_constructor_started.set()
                        await self.release_old_constructor.wait()
                    else:
                        self.new_constructor_started.set()
                    return target(*args)
                finally:
                    self.active_constructors -= 1
            if getattr(target, "__self__", None) is self.old_interface:
                self.old_close_started.set()
                await self.release_old_close.wait()
            self.executor_calls += 1
            return target(*args)

    async def run() -> None:
        _install_fake_pubsub(monkeypatch)
        monkeypatch.setattr(meshtastic_client_module, "_STOP_WAIT_TIMEOUT", 0.01)
        hass = EndpointExecutorHass()
        old_interface = _FakeInterface()
        new_interface = _FakeInterface()
        old_statuses = []
        new_statuses = []
        hass.old_interface = old_interface
        old_client = _client(hass, old_interface, old_statuses)
        new_client = _client(hass, new_interface, new_statuses)

        def make_old_interface():
            return old_interface

        def make_new_interface():
            return new_interface

        old_client._make_native_interface = make_old_interface
        new_client._make_native_interface = make_new_interface

        old_waiter = asyncio.create_task(old_client.async_start())
        await hass.old_constructor_started.wait()
        await asyncio.wait_for(old_client.async_stop(), timeout=0.2)
        old_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old_waiter

        new_waiter = asyncio.create_task(new_client.async_start())
        await asyncio.sleep(0)
        assert hass.constructor_submissions == 1
        assert not hass.new_constructor_started.is_set()

        hass.release_old_constructor.set()
        await asyncio.wait_for(hass.old_close_started.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not hass.new_constructor_started.is_set()
        assert old_interface.close_calls == 0

        hass.release_old_close.set()
        await asyncio.wait_for(hass.new_constructor_started.wait(), timeout=0.2)
        await asyncio.wait_for(new_waiter, timeout=0.2)

        assert hass.constructor_submissions == 2
        assert hass.max_active_constructors == 1
        assert old_interface.close_calls == 1
        assert old_client._interface is None
        assert True not in old_statuses
        assert new_client._interface is new_interface
        assert new_statuses == [True]

        await new_client.async_stop()
        assert new_interface.close_calls == 1
        assert any("transport startup" in name for name in hass.background_task_names)
        assert any("interface close" in name for name in hass.background_task_names)

    asyncio.run(run())


def test_failed_close_keeps_replacement_constructor_blocked(monkeypatch) -> None:
    """An unconfirmed native close must keep the endpoint lease held."""

    class TrackingExecutorHass(_ControlledExecutorHass):
        def __init__(self) -> None:
            super().__init__()
            self.constructor_submissions = 0

        async def async_add_executor_job(self, target, *args):
            if getattr(target, "__name__", "").startswith("make_"):
                self.constructor_submissions += 1
            return await super().async_add_executor_job(target, *args)

    class FailingCloseInterface(_FakeInterface):
        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("native close failed")

    async def run() -> None:
        _install_fake_pubsub(monkeypatch)
        hass = TrackingExecutorHass()
        hass.release_constructor.set()
        old_interface = FailingCloseInterface()
        new_interface = _FakeInterface()
        old_client = _client(hass, old_interface, [])
        new_client = _client(hass, new_interface, [])

        def make_old_interface():
            return old_interface

        def make_new_interface():
            return new_interface

        old_client._make_native_interface = make_old_interface
        new_client._make_native_interface = make_new_interface

        await old_client.async_start()
        assert hass.constructor_submissions == 1

        with pytest.raises(RuntimeError, match="native close failed"):
            await old_client.async_stop()

        assert old_interface.close_calls == 1
        assert old_client._native_lock is not None
        assert old_client._native_lock.locked()

        new_waiter = asyncio.create_task(new_client.async_start())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The failed close means it is unsafe to submit a second native
        # constructor for the same Bluetooth address.
        assert hass.constructor_submissions == 1
        assert new_client._native_lock is None
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


def test_cancelled_native_send_finishes_before_interface_close(monkeypatch) -> None:
    """A canceled service waiter cannot hide a running SDK executor thread."""

    async def run() -> None:
        monkeypatch.setattr(meshtastic_client_module, "_STOP_WAIT_TIMEOUT", 0.01)
        hass = _CancellationResistantExecutorHass()
        interface = _BlockingNativeInterface()
        hass.interface = interface
        client = _client(hass, interface, [])
        client._interface = interface
        client.status.connected = True

        send_task = asyncio.create_task(
            client.async_send_message(
                target_node=None,
                message="thread-owned send",
                channel=None,
                priority="normal",
                message_type="broadcast",
            )
        )
        try:
            await asyncio.wait_for(hass.operation_started.wait(), timeout=1)
            interface.send_started = True
            send_task.cancel()
            await asyncio.sleep(0)

            # Cancellation is retained until the executor owner drains, so the
            # coordinator cannot mistake the SDK operation for completed work.
            assert send_task.done() is False

            await asyncio.wait_for(client.async_stop(), timeout=0.2)

            # Public stop is bounded, but close remains retained behind the
            # exact interface operation rather than racing its worker thread.
            assert interface.close_started.is_set() is False
            assert client._native_executor_tasks
        finally:
            hass.release_operation.set()

        with pytest.raises(asyncio.CancelledError):
            await send_task
        await asyncio.wait_for(interface.close_started.wait(), timeout=1)
        await asyncio.sleep(0)

        assert interface.close_calls == 1
        assert interface.close_overlapped_send is False
        assert client._native_executor_tasks == {}

    asyncio.run(run())


def test_cancelled_native_refresh_finishes_before_interface_close(
    monkeypatch,
) -> None:
    """A canceled refresh cannot let close race an SDK node snapshot thread."""

    async def run() -> None:
        monkeypatch.setattr(meshtastic_client_module, "_STOP_WAIT_TIMEOUT", 0.01)
        hass = _CancellationResistantExecutorHass()
        interface = _BlockingNativeInterface()
        hass.interface = interface
        client = _client(hass, interface, [])
        client._interface = interface
        client.status.connected = True

        refresh_task = asyncio.create_task(client.async_refresh())
        try:
            await asyncio.wait_for(hass.operation_started.wait(), timeout=1)
            interface.refresh_started = True
            refresh_task.cancel()
            await asyncio.sleep(0)

            assert refresh_task.done() is False

            await asyncio.wait_for(client.async_stop(), timeout=0.2)

            assert interface.close_started.is_set() is False
            assert client._native_executor_tasks
        finally:
            hass.release_operation.set()

        with pytest.raises(asyncio.CancelledError):
            await refresh_task
        await asyncio.wait_for(interface.close_started.wait(), timeout=1)
        await asyncio.sleep(0)

        assert interface.close_calls == 1
        assert interface.close_overlapped_refresh is False
        assert client._native_executor_tasks == {}

    asyncio.run(run())
