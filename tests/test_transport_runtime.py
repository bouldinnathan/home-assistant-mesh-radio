from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from types import SimpleNamespace

import pytest

from custom_components.meshnet import meshcore_client as meshcore_client_module
from custom_components.meshnet.const import (
    MESSAGE_TYPE_BROADCAST,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_MQTT,
    TRANSPORT_REST,
    TRANSPORT_SERIAL,
)
from custom_components.meshnet.meshcore_client import MeshCoreClient
from custom_components.meshnet.meshtastic_client import MeshtasticClient
from custom_components.meshnet.models import GatewayConfig


class FakeHass:
    """Small Home Assistant task shim for transport unit tests."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.background_task_names: list[str] = []

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    def async_create_background_task(self, coroutine, name):
        self.background_task_names.append(name)
        return asyncio.create_task(coroutine)


def _meshcore_client(*, transport: str = TRANSPORT_SERIAL, options=None):
    packets = []
    nodes = []
    statuses = []

    async def on_packet(packet) -> None:
        packets.append(packet)

    async def on_node(node) -> None:
        nodes.append(node)

    async def on_status(status) -> None:
        statuses.append(status.connected)

    config = GatewayConfig(
        gateway_id="g1",
        name="Gateway",
        protocol=PROTOCOL_MESHCORE,
        transport=transport,
        serial_path="/dev/ttyUSB0",
        api_url="http://meshcore.invalid/state" if transport == TRANSPORT_REST else None,
        options=dict(options or {}),
    )
    return (
        MeshCoreClient(
            FakeHass(),
            config,
            on_packet,
            on_node,
            on_status,
            logging.getLogger(__name__),
        ),
        packets,
        nodes,
        statuses,
    )


def test_meshcore_contacts_are_emitted_as_individual_nodes() -> None:
    async def run() -> None:
        client, packets, nodes, _statuses = _meshcore_client()
        event = SimpleNamespace(
            type=SimpleNamespace(value="contacts"),
            payload={
                bytes.fromhex("aabbccdd"): {
                    "adv_name": "Field Node",
                    "adv_lat": 1.25,
                    "adv_lon": 2.5,
                }
            },
        )

        await client._handle_native_event(event)

        assert packets == []
        assert len(nodes) == 1
        assert nodes[0].public_key == "aabbccdd"
        assert nodes[0].long_name == "Field Node"
        assert nodes[0].location["latitude"] == 1.25
        assert client._contacts["pub:aabbccdd"]["adv_name"] == "Field Node"

    asyncio.run(run())


def test_meshcore_no_more_messages_yields_without_emitting_packet(monkeypatch) -> None:
    async def run() -> None:
        client, packets, _nodes, _statuses = _meshcore_client(
            options={"message_poll_interval": 0}
        )

        class Commands:
            calls = 0

            async def get_msg(self, timeout):
                self.calls += 1
                return SimpleNamespace(
                    type=SimpleNamespace(value="no_more_messages"),
                    payload={},
                )

        commands = Commands()
        client._meshcore = SimpleNamespace(commands=commands)
        sleep_delays = []

        async def stop_on_sleep(delay):
            sleep_delays.append(delay)
            raise asyncio.CancelledError

        monkeypatch.setattr("custom_components.meshnet.meshcore_client.asyncio.sleep", stop_on_sleep)
        with pytest.raises(asyncio.CancelledError):
            await client._native_message_poll_loop()

        assert commands.calls == 1
        assert sleep_delays == [0.1]
        assert packets == []

    asyncio.run(run())


def test_meshcore_polled_subscribed_message_is_not_handled_twice(monkeypatch) -> None:
    async def run() -> None:
        client, packets, _nodes, _statuses = _meshcore_client()
        client._native_subscribed_events.add("contact_message")

        events = iter(
            [
                SimpleNamespace(
                    type=SimpleNamespace(value="contact_message"),
                    payload={"pubkey_prefix": "aabb", "text": "hello"},
                ),
                SimpleNamespace(
                    type=SimpleNamespace(value="no_more_messages"),
                    payload={},
                ),
            ]
        )

        class Commands:
            async def get_msg(self, timeout):
                return next(events)

        client._meshcore = SimpleNamespace(commands=Commands())

        async def stop_on_sleep(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr("custom_components.meshnet.meshcore_client.asyncio.sleep", stop_on_sleep)
        with pytest.raises(asyncio.CancelledError):
            await client._native_message_poll_loop()

        # The SDK subscription owns the contact_message event. The polling
        # command must not emit the same event a second time.
        assert packets == []

    asyncio.run(run())


def test_meshcore_connection_events_update_status() -> None:
    async def run() -> None:
        client, packets, _nodes, statuses = _meshcore_client()

        await client._handle_native_event(
            SimpleNamespace(type=SimpleNamespace(value="connected"), payload={"reconnected": True})
        )
        await client._handle_native_event(
            SimpleNamespace(type=SimpleNamespace(value="disconnected"), payload={"reason": "lost"})
        )

        assert statuses == [True, False]
        assert client.status.connected is False
        assert packets == []

    asyncio.run(run())


def test_meshcore_subscribes_to_connection_events_when_available() -> None:
    async def run() -> None:
        client, _packets, _nodes, _statuses = _meshcore_client()
        subscribed = []

        class EventTypes:
            CONNECTED = SimpleNamespace(value="connected")
            DISCONNECTED = SimpleNamespace(value="disconnected")

        client._meshcore = SimpleNamespace(
            subscribe=lambda event_type, _handler: subscribed.append(event_type.value)
        )
        client._subscribe_native_events(EventTypes)

        assert subscribed == ["connected", "disconnected"]
        assert client._native_subscribed_events == {"connected", "disconnected"}

    asyncio.run(run())


def test_meshcore_rest_health_requires_success_and_tracks_failure() -> None:
    async def run() -> None:
        client, _packets, _nodes, statuses = _meshcore_client(transport=TRANSPORT_REST)
        fetches = 0

        async def fetch_ok() -> None:
            nonlocal fetches
            fetches += 1

        client._rest_fetch = fetch_ok
        await client.async_start()

        assert fetches == 1
        assert client.status.connected is True
        assert client.hass.background_task_names == [
            "MeshNet MeshCore REST poll g1"
        ]

        async def fetch_failed() -> None:
            raise RuntimeError("endpoint unavailable")

        client._rest_fetch = fetch_failed
        await client._rest_poll_once()

        assert client.status.connected is False
        assert "REST poll failed: endpoint unavailable" in client.status.errors[-1]
        assert statuses[-1] is False
        await client.async_stop()

    asyncio.run(run())


def test_meshcore_rest_start_cannot_resurrect_after_stop() -> None:
    async def run() -> None:
        client, _packets, _nodes, statuses = _meshcore_client(
            transport=TRANSPORT_REST
        )
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def delayed_fetch():
            fetch_started.set()
            await release_fetch.wait()
            return {"nodes": []}

        client._rest_fetch = delayed_fetch
        start_task = asyncio.create_task(client.async_start())
        await fetch_started.wait()

        await client.async_stop()
        release_fetch.set()
        await start_task

        assert client.status.connected is False
        assert True not in statuses
        assert client.hass.background_task_names == []

    asyncio.run(run())


def test_meshcore_mqtt_late_subscription_is_released_after_stop(
    monkeypatch,
) -> None:
    async def run() -> None:
        subscribe_started = asyncio.Event()
        release_subscribe = asyncio.Event()
        unsubscribe_calls = 0

        def unsubscribe() -> None:
            nonlocal unsubscribe_calls
            unsubscribe_calls += 1

        async def async_subscribe(_hass, _topic, _callback, _qos):
            subscribe_started.set()
            await release_subscribe.wait()
            return unsubscribe

        homeassistant = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        mqtt = types.ModuleType("homeassistant.components.mqtt")
        mqtt.async_subscribe = async_subscribe
        components.mqtt = mqtt
        homeassistant.components = components
        monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
        monkeypatch.setitem(sys.modules, "homeassistant.components", components)
        monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", mqtt)

        client, _packets, _nodes, statuses = _meshcore_client(
            transport=TRANSPORT_MQTT
        )
        start_task = asyncio.create_task(client.async_start())
        await subscribe_started.wait()

        await client.async_stop()
        release_subscribe.set()
        await start_task

        assert unsubscribe_calls == 1
        assert client._unsub_mqtt is None
        assert client.status.connected is False
        assert True not in statuses

    asyncio.run(run())


def test_meshcore_stale_mqtt_start_releases_its_overwritten_subscription(
    monkeypatch,
) -> None:
    async def run() -> None:
        unsubscribe_calls = [0, 0]
        subscribe_count = 0

        def make_unsubscribe(index: int):
            def unsubscribe() -> None:
                unsubscribe_calls[index] += 1

            return unsubscribe

        unsubscribes = [make_unsubscribe(0), make_unsubscribe(1)]

        async def async_subscribe(_hass, _topic, _callback, _qos):
            nonlocal subscribe_count
            unsubscribe = unsubscribes[subscribe_count]
            subscribe_count += 1
            return unsubscribe

        homeassistant = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        mqtt = types.ModuleType("homeassistant.components.mqtt")
        mqtt.async_subscribe = async_subscribe
        components.mqtt = mqtt
        homeassistant.components = components
        monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
        monkeypatch.setitem(sys.modules, "homeassistant.components", components)
        monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", mqtt)

        client, _packets, _nodes, _statuses = _meshcore_client(
            transport=TRANSPORT_MQTT
        )
        first_status_started = asyncio.Event()
        release_first_status = asyncio.Event()
        connected_calls = 0
        original_set_connected = client._set_connected

        async def controlled_set_connected(connected: bool, **detail) -> None:
            nonlocal connected_calls
            await original_set_connected(connected, **detail)
            if not connected:
                return
            connected_calls += 1
            if connected_calls == 1:
                first_status_started.set()
                await release_first_status.wait()

        client._set_connected = controlled_set_connected
        stale_start = asyncio.create_task(client.async_start())
        await first_status_started.wait()

        await client.async_start()
        assert client._unsub_mqtt is unsubscribes[1]

        release_first_status.set()
        await stale_start

        assert unsubscribe_calls == [1, 0]
        assert client._unsub_mqtt is unsubscribes[1]
        assert client.status.connected is True

        await client.async_stop()
        assert unsubscribe_calls == [1, 1]

    asyncio.run(run())


def test_meshcore_native_late_interface_is_disconnected_after_stop(
    monkeypatch,
) -> None:
    async def run() -> None:
        create_started = asyncio.Event()
        release_create = asyncio.Event()

        class Interface:
            def __init__(self) -> None:
                self.disconnect_calls = 0

            async def disconnect(self) -> None:
                self.disconnect_calls += 1

        interface = Interface()

        class MeshCore:
            @staticmethod
            async def create_serial(*_args, **_kwargs):
                create_started.set()
                await release_create.wait()
                return interface

        meshcore_module = types.ModuleType("meshcore")
        meshcore_module.EventType = SimpleNamespace()
        meshcore_module.MeshCore = MeshCore
        monkeypatch.setitem(sys.modules, "meshcore", meshcore_module)

        client, _packets, _nodes, statuses = _meshcore_client()
        start_task = asyncio.create_task(client.async_start())
        await create_started.wait()

        await client.async_stop()
        release_create.set()
        await start_task

        assert interface.disconnect_calls == 1
        assert client._meshcore is None
        assert client.status.connected is False
        assert True not in statuses
        assert client._tasks == set()
        assert not any(
            "message poll" in name for name in client.hass.background_task_names
        )

    asyncio.run(run())


def test_meshcore_native_message_poll_uses_background_task(monkeypatch) -> None:
    async def run() -> None:
        poll_started = asyncio.Event()
        release_poll = asyncio.Event()

        class Commands:
            async def get_msg(self, *, timeout):
                assert timeout == 5
                poll_started.set()
                await release_poll.wait()

        class Interface:
            def __init__(self) -> None:
                self.commands = Commands()
                self.disconnect_calls = 0

            async def disconnect(self) -> None:
                self.disconnect_calls += 1

        interface = Interface()

        class MeshCore:
            @staticmethod
            async def create_serial(*_args, **_kwargs):
                return interface

        meshcore_module = types.ModuleType("meshcore")
        meshcore_module.EventType = SimpleNamespace()
        meshcore_module.MeshCore = MeshCore
        monkeypatch.setitem(sys.modules, "meshcore", meshcore_module)

        client, _packets, _nodes, _statuses = _meshcore_client()
        await client.async_start()
        await poll_started.wait()

        assert client.hass.background_task_names == [
            "MeshNet MeshCore message poll g1"
        ]
        assert len(client._tasks) == 1

        await client.async_stop()

        assert interface.disconnect_calls == 1
        assert client._tasks == set()
        assert client.status.connected is False

    asyncio.run(run())


def test_meshcore_stop_during_initial_refresh_remains_terminal(
    monkeypatch,
) -> None:
    async def run() -> None:
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        class Commands:
            async def send_device_query(self):
                refresh_started.set()
                await release_refresh.wait()
                return SimpleNamespace(type="device_info", payload={})

        class Interface:
            def __init__(self) -> None:
                self.commands = Commands()
                self.disconnect_calls = 0

            async def disconnect(self) -> None:
                self.disconnect_calls += 1

        interface = Interface()

        class MeshCore:
            @staticmethod
            async def create_serial(*_args, **_kwargs):
                return interface

        meshcore_module = types.ModuleType("meshcore")
        meshcore_module.EventType = SimpleNamespace()
        meshcore_module.MeshCore = MeshCore
        monkeypatch.setitem(sys.modules, "meshcore", meshcore_module)

        client, _packets, _nodes, statuses = _meshcore_client()
        start_task = asyncio.create_task(client.async_start())
        await refresh_started.wait()

        await client.async_stop()
        release_refresh.set()
        await start_task

        assert interface.disconnect_calls == 1
        assert client._meshcore is None
        assert client.status.connected is False
        assert statuses[-1] is False
        assert not any(
            "message poll" in name for name in client.hass.background_task_names
        )
        assert client._tasks == set()

    asyncio.run(run())


def test_meshcore_hung_disconnect_does_not_block_stop_and_finishes_late(
    monkeypatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(
            meshcore_client_module,
            "_DISCONNECT_WAIT_TIMEOUT",
            0.01,
        )
        disconnect_started = asyncio.Event()
        release_disconnect = asyncio.Event()
        disconnect_finished = asyncio.Event()

        class Interface:
            async def disconnect(self) -> None:
                disconnect_started.set()
                await release_disconnect.wait()
                disconnect_finished.set()

        client, _packets, _nodes, statuses = _meshcore_client()
        client._meshcore = Interface()
        client.status.connected = True

        await asyncio.wait_for(client.async_stop(), timeout=0.2)

        assert disconnect_started.is_set()
        assert not disconnect_finished.is_set()
        assert client._meshcore is None
        assert client.status.connected is False
        assert statuses == [False]
        assert client.hass.background_task_names == [
            "MeshNet MeshCore interface disconnect g1"
        ]

        release_disconnect.set()
        await asyncio.wait_for(disconnect_finished.wait(), timeout=0.2)

    asyncio.run(run())


def test_meshcore_stubborn_poll_task_cannot_block_stop(monkeypatch) -> None:
    """A provider command suppressing cancellation must not hang unload."""

    async def run() -> None:
        monkeypatch.setattr(
            meshcore_client_module,
            "_POLL_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        client, _packets, _nodes, statuses = _meshcore_client()
        poll_started = asyncio.Event()
        release_poll = asyncio.Event()

        async def stubborn_poll() -> None:
            poll_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_poll.wait()

        poll_task = asyncio.create_task(stubborn_poll())
        client._tasks.add(poll_task)
        poll_task.add_done_callback(client._tasks.discard)
        await poll_started.wait()

        await asyncio.wait_for(client.async_stop(), timeout=0.2)

        assert client._stopping is True
        assert client.status.connected is False
        assert statuses == [False]
        assert client._tasks == {poll_task}

        release_poll.set()
        await poll_task
        await asyncio.sleep(0)
        assert client._tasks == set()

    asyncio.run(run())


def test_meshcore_stale_rest_poll_cannot_resurrect_after_restart(
    monkeypatch,
) -> None:
    """A cancellation-suppressing old poll cannot join a new lifecycle."""

    async def run() -> None:
        monkeypatch.setattr(
            meshcore_client_module,
            "_POLL_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        client, packets, _nodes, statuses = _meshcore_client(
            transport=TRANSPORT_REST,
            options={"scan_interval": 0},
        )
        stale_poll_started = asyncio.Event()
        release_stale_poll = asyncio.Event()
        fetch_count = 0

        async def lifecycle_fetch():
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 2:
                stale_poll_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    await release_stale_poll.wait()
                return {
                    "packets": [
                        {
                            "id": "stale-packet",
                            "message": "must not escape old lifecycle",
                        }
                    ]
                }
            return {"nodes": []}

        client._rest_fetch = lifecycle_fetch
        await client.async_start()
        await stale_poll_started.wait()
        stale_poll_task = next(iter(client._tasks))

        await asyncio.wait_for(client.async_stop(), timeout=0.2)
        assert client._tasks == {stale_poll_task}
        assert client.status.connected is False

        # Keep the replacement poll asleep so task ownership is deterministic.
        client.config.options["scan_interval"] = 3600
        await client.async_start()
        replacement_poll_task = next(
            task for task in client._tasks if task is not stale_poll_task
        )
        assert client.status.connected is True

        release_stale_poll.set()
        await asyncio.wait_for(stale_poll_task, timeout=0.2)
        await asyncio.sleep(0)

        assert packets == []
        assert client.status.connected is True
        assert statuses == [True, False, True]
        assert client._tasks == {replacement_poll_task}

        await client.async_stop()

    asyncio.run(run())


def test_meshcore_rest_initial_failure_never_marks_connected() -> None:
    async def run() -> None:
        client, _packets, _nodes, statuses = _meshcore_client(transport=TRANSPORT_REST)

        async def fetch_failed() -> None:
            raise RuntimeError("cannot connect")

        client._rest_fetch = fetch_failed
        with pytest.raises(RuntimeError, match="cannot connect"):
            await client.async_start()

        assert client.status.connected is False
        assert True not in statuses
        assert client._tasks == set()

    asyncio.run(run())


def test_meshtastic_mqtt_uses_official_downlink_envelope(monkeypatch) -> None:
    async def run() -> None:
        published = {}

        async def async_publish(hass, topic, payload, qos, retain) -> None:
            published.update(
                hass=hass,
                topic=topic,
                payload=json.loads(payload),
                qos=qos,
                retain=retain,
            )

        homeassistant = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        mqtt = types.ModuleType("homeassistant.components.mqtt")
        mqtt.async_publish = async_publish
        components.mqtt = mqtt
        homeassistant.components = components
        monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
        monkeypatch.setitem(sys.modules, "homeassistant.components", components)
        monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", mqtt)

        hass = object()
        client = MeshtasticClient(
            hass,
            GatewayConfig(
                gateway_id="g1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_MQTT,
                mqtt_topic="msh/US/2/json/#",
                options={
                    "publish_topic": "msh/US/2/json/mqtt/",
                    "mqtt_node_id": "!12345678",
                },
            ),
            _async_noop,
            _async_noop,
            _async_noop,
            logging.getLogger(__name__),
        )

        await client._mqtt_publish_message(
            target_node="!87654321",
            message="hello mesh",
            channel="0",
            priority="normal",
            message_type=MESSAGE_TYPE_BROADCAST,
            message_id="local-id",
        )

        assert published == {
            "hass": hass,
            "topic": "msh/US/2/json/mqtt/",
            "payload": {
                "from": 305419896,
                "to": 2271560481,
                "channel": 0,
                "type": "sendtext",
                "payload": "hello mesh",
            },
            "qos": 1,
            "retain": False,
        }

    asyncio.run(run())


def test_meshtastic_mqtt_send_requires_explicit_node_id() -> None:
    async def run() -> None:
        client = MeshtasticClient(
            object(),
            GatewayConfig(
                gateway_id="g1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_MQTT,
                options={"publish_topic": "msh/US/2/json/mqtt/"},
            ),
            _async_noop,
            _async_noop,
            _async_noop,
            logging.getLogger(__name__),
        )

        with pytest.raises(RuntimeError, match="mqtt_node_id"):
            await client._mqtt_publish_message(
                target_node=None,
                message="hello",
                channel=None,
                priority="normal",
                message_type=MESSAGE_TYPE_BROADCAST,
                message_id="local-id",
            )

    asyncio.run(run())


def test_meshcore_mqtt_send_requires_explicit_publish_topic() -> None:
    async def run() -> None:
        client = MeshCoreClient(
            object(),
            GatewayConfig(
                gateway_id="g1",
                name="Gateway",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_MQTT,
                mqtt_topic="meshcore/+/+/packets",
            ),
            _async_noop,
            _async_noop,
            _async_noop,
            logging.getLogger(__name__),
        )

        with pytest.raises(RuntimeError, match="publish_topic"):
            await client._mqtt_publish_message(
                target_node=None,
                message="hello",
                channel=None,
                priority="normal",
                message_type=MESSAGE_TYPE_BROADCAST,
                message_id="local-id",
            )

    asyncio.run(run())


async def _async_noop(*_args) -> None:
    return None
