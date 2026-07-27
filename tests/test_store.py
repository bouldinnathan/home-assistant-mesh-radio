from __future__ import annotations

import asyncio

from custom_components.meshnet.models import MeshPacket, MessageRecord, NodeState
from custom_components.meshnet.store import MeshStore


def test_store_round_trip(tmp_path) -> None:
    asyncio.run(_store_round_trip(tmp_path))


async def _store_round_trip(tmp_path) -> None:
    store = MeshStore(tmp_path / "meshnet.sqlite3")
    await store.async_open()
    node = NodeState(
        node_key="meshtastic:1",
        protocol="meshtastic",
        node_id="1",
        long_name="Node 1",
    )
    await store.async_upsert_node(node)
    message = MessageRecord(
        message_id="msg1",
        protocol="meshtastic",
        gateway_id="g1",
        sender="1",
        receiver=None,
        channel="0",
        text="hello",
    )
    await store.async_add_message(message)
    assert await store.async_add_packet(
        MeshPacket(protocol="meshtastic", gateway_id="g1", packet_id="pkt1", text="hello")
    )
    assert not await store.async_add_packet(
        MeshPacket(protocol="meshtastic", gateway_id="g2", packet_id="pkt1", text="hello")
    )

    snapshot = await store.async_load_snapshot()
    assert snapshot.nodes["meshtastic:1"].long_name == "Node 1"
    assert snapshot.recent_messages[0].text == "hello"
    diagnostics = await store.async_diagnostics()
    assert diagnostics == {
        "node_count": 1,
        "message_count": 1,
        "packet_count": 1,
    }
    assert "hello" not in repr(diagnostics)
    assert str(store.path) not in repr(diagnostics)
    await store.async_close()


def test_pending_outbox(tmp_path) -> None:
    asyncio.run(_pending_outbox(tmp_path))


async def _pending_outbox(tmp_path) -> None:
    store = MeshStore(tmp_path / "meshnet.sqlite3")
    await store.async_open()
    await store.async_add_message(
        MessageRecord(
            message_id="queued",
            protocol="unknown",
            gateway_id="queued",
            sender="homeassistant",
            receiver=None,
            channel="0",
            text="queued",
            direction="tx",
            raw={"status": "queued"},
        )
    )
    pending = await store.async_pending_outbox()
    assert [message.message_id for message in pending] == ["queued"]
    await store.async_close()
