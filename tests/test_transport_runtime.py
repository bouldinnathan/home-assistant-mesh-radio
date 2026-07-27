from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from types import SimpleNamespace

import pytest

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

    def async_create_task(self, coroutine):
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
        await client._start_rest()

        assert fetches == 1
        assert client.status.connected is True

        async def fetch_failed() -> None:
            raise RuntimeError("endpoint unavailable")

        client._rest_fetch = fetch_failed
        await client._rest_poll_once()

        assert client.status.connected is False
        assert "REST poll failed: endpoint unavailable" in client.status.errors[-1]
        assert statuses[-1] is False
        await client.async_stop()

    asyncio.run(run())


def test_meshcore_rest_initial_failure_never_marks_connected() -> None:
    async def run() -> None:
        client, _packets, _nodes, statuses = _meshcore_client(transport=TRANSPORT_REST)

        async def fetch_failed() -> None:
            raise RuntimeError("cannot connect")

        client._rest_fetch = fetch_failed
        with pytest.raises(RuntimeError, match="cannot connect"):
            await client._start_rest()

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
