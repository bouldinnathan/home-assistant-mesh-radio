from __future__ import annotations

import asyncio

import pytest

from custom_components.meshnet import store as store_module
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
    assert diagnostics["available"] is True
    assert diagnostics["node_count"] == 1
    assert diagnostics["message_count"] == 1
    assert diagnostics["packet_count"] == 1
    assert diagnostics["route_count"] == 0
    assert diagnostics["message_direction_counts"] == {
        "received": 1,
        "sent": 0,
        "queued": 0,
    }
    assert diagnostics["node_protocol_counts"] == {"meshtastic": 1}
    assert diagnostics["message_protocol_counts"] == {"meshtastic": 1}
    assert diagnostics["packet_protocol_counts"] == {"meshtastic": 1}
    assert diagnostics["journal_mode"] == "wal"
    assert diagnostics["file_sizes"]["database_bytes"] > 0
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
    diagnostics = await store.async_diagnostics()
    assert diagnostics["message_direction_counts"] == {
        "received": 0,
        "sent": 1,
        "queued": 1,
    }
    await store.async_close()
    closed_diagnostics = await store.async_diagnostics()
    assert closed_diagnostics["available"] is False
    assert "node_count" not in closed_diagnostics


def test_close_waits_for_active_database_operation(tmp_path) -> None:
    """Storage close must serialize behind an in-flight operation."""

    async def run() -> None:
        store = MeshStore(tmp_path / "meshnet.sqlite3")
        await store.async_open()
        operation_started = asyncio.Event()
        release_operation = asyncio.Event()

        async def controlled_executor(target):
            operation_started.set()
            await release_operation.wait()
            return target()

        store._executor = controlled_executor
        write_task = asyncio.create_task(
            store.async_upsert_node(
                NodeState(
                    node_key="meshtastic:1",
                    protocol="meshtastic",
                    node_id="1",
                )
            )
        )
        await operation_started.wait()
        close_task = asyncio.create_task(store.async_close())
        await asyncio.sleep(0)

        assert close_task.done() is False
        assert store._conn is not None

        release_operation.set()
        await write_task
        await close_task
        assert store._conn is None

    asyncio.run(run())


def test_cancelled_database_operation_drains_executor_before_close(tmp_path) -> None:
    """Cancellation cannot release SQLite ownership while its thread is active."""

    async def run() -> None:
        store = MeshStore(tmp_path / "meshnet.sqlite3")
        await store.async_open()
        operation_started = asyncio.Event()
        release_operation = asyncio.Event()
        close_executor_started = asyncio.Event()
        executor_calls = 0

        async def controlled_executor(target):
            nonlocal executor_calls
            executor_calls += 1
            if executor_calls == 1:
                operation_started.set()
                await release_operation.wait()
                return target()
            close_executor_started.set()
            return target()

        store._executor = controlled_executor
        write_task = asyncio.create_task(
            store.async_upsert_node(
                NodeState(
                    node_key="meshtastic:1",
                    protocol="meshtastic",
                    node_id="1",
                )
            )
        )
        await operation_started.wait()
        write_task.cancel()
        close_task = asyncio.create_task(store.async_close())
        try:
            await asyncio.sleep(0.01)
            assert close_task.done() is False
            assert close_executor_started.is_set() is False
            # The close owner is queued behind the exact executor lease and
            # cannot detach or close the connection underneath it.
            assert store._conn is not None
        finally:
            release_operation.set()

        with pytest.raises(asyncio.CancelledError):
            await write_task
        await close_task

        assert close_executor_started.is_set()
        assert store._conn is None

    asyncio.run(run())


def test_cancelled_diagnostics_keep_sqlite_serialized_until_executor_drains(
    tmp_path,
) -> None:
    """A timed-out download cannot overlap its query with a later store write."""

    async def run() -> None:
        store = MeshStore(tmp_path / "meshnet.sqlite3")
        await store.async_open()
        first_executor_started = asyncio.Event()
        release_first_executor = asyncio.Event()
        second_executor_started = asyncio.Event()
        executor_calls = 0

        async def controlled_executor(target):
            nonlocal executor_calls
            executor_calls += 1
            if executor_calls == 1:
                first_executor_started.set()
                await release_first_executor.wait()
            else:
                second_executor_started.set()
            return target()

        store._executor = controlled_executor
        diagnostics_task = asyncio.create_task(store.async_diagnostics())
        await first_executor_started.wait()

        diagnostics_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await diagnostics_task

        write_task = asyncio.create_task(
            store.async_upsert_node(
                NodeState(
                    node_key="meshtastic:1",
                    protocol="meshtastic",
                    node_id="1",
                )
            )
        )
        await asyncio.sleep(0.01)

        assert second_executor_started.is_set() is False
        assert store._conn is not None

        release_first_executor.set()
        await asyncio.wait_for(second_executor_started.wait(), timeout=0.2)
        await write_task
        await store.async_close()

    asyncio.run(run())


def test_cancelled_initial_connect_is_closed_after_bounded_rollback(
    monkeypatch, tmp_path
) -> None:
    """A late SQLite connect result must retain an exact close owner."""

    async def run() -> None:
        monkeypatch.setattr(store_module, "_STORE_CLOSE_WAIT_TIMEOUT", 0.01)
        connect_started = asyncio.Event()
        release_connect = asyncio.Event()
        connection_closed = asyncio.Event()
        executor_calls = 0

        class Connection:
            closed = False

            def close(self) -> None:
                self.closed = True
                connection_closed.set()

        connection = Connection()

        async def controlled_executor(target):
            nonlocal executor_calls
            executor_calls += 1
            if executor_calls == 1:
                connect_started.set()
                await release_connect.wait()
            return target()

        monkeypatch.setattr(store_module.sqlite3, "connect", lambda *_args, **_kwargs: connection)
        store = MeshStore(
            tmp_path / "meshnet.sqlite3",
            executor=controlled_executor,
        )
        open_task = asyncio.create_task(store.async_open())
        await connect_started.wait()

        open_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await open_task

        assert store._conn is None
        assert store._close_task is not None

        # Rollback remains bounded even though the executor still owns connect.
        await asyncio.wait_for(store.async_close(), timeout=0.2)
        assert connection.closed is False
        close_owner = store._close_task
        assert close_owner is not None

        release_connect.set()
        await asyncio.wait_for(connection_closed.wait(), timeout=0.2)
        await asyncio.wait_for(close_owner, timeout=0.2)
        await asyncio.sleep(0)

        assert connection.closed is True
        assert store._conn is None
        assert store._close_task is None
        assert store._inflight == set()

    asyncio.run(run())
