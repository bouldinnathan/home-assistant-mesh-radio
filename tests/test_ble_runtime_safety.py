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

    def async_create_task(self, coroutine):
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
