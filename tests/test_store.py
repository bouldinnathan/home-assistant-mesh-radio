from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

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


def test_graph_position_history_is_hourly_bounded_persisted_and_pruned(
    tmp_path,
) -> None:
    """Rolling-day trails replace a bucket and never become permanent history."""

    async def run() -> None:
        path = tmp_path / "meshnet.sqlite3"
        now = datetime.now(UTC).replace(microsecond=0)
        bucket = now.replace(minute=0, second=0)
        store = MeshStore(path)
        await store.async_open()
        await store.async_record_graph_position(
            "meshtastic:!11111111",
            41.0,
            -87.0,
            observed_at=bucket - timedelta(hours=2) + timedelta(minutes=5),
            precision_bits=12,
        )
        await store.async_record_graph_position(
            "meshtastic:!11111111",
            41.1,
            -87.1,
            observed_at=bucket - timedelta(hours=2) + timedelta(minutes=55),
            precision_bits=14,
        )
        await store.async_record_graph_position(
            "meshtastic:!11111111",
            41.2,
            -87.2,
            observed_at=bucket - timedelta(hours=1),
        )
        await store.async_record_graph_position(
            "meshtastic:!22222222",
            42.0,
            -88.0,
            observed_at=now - timedelta(hours=25),
        )
        await store.async_close()

        reopened = MeshStore(path)
        await reopened.async_open()
        history = await reopened.async_graph_position_history(now=now)
        assert list(history) == ["meshtastic:!11111111"]
        assert len(history["meshtastic:!11111111"]) == 2
        assert history["meshtastic:!11111111"][0]["latitude"] == 41.1
        assert history["meshtastic:!11111111"][0]["precision_bits"] == 14
        assert history["meshtastic:!11111111"][1]["latitude"] == 41.2

        await reopened.async_prune(history_days=30)
        diagnostics = await reopened.async_diagnostics()
        assert diagnostics["graph_position_observation_count"] == 2
        assert diagnostics["metadata_airtime_reservation_count"] == 0
        assert "meshtastic:!11111111" not in repr(diagnostics)
        await reopened.async_close()

    asyncio.run(run())


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
    assert await store.async_pending_outbox(
        after=(pending[0].timestamp.isoformat(), pending[0].message_id)
    ) == []
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


def test_cached_reads_isolate_invalid_rows(tmp_path) -> None:
    """One malformed cache row must not prevent adjacent valid state loading."""

    async def run() -> None:
        store = MeshStore(tmp_path / "meshnet.sqlite3")
        await store.async_open()
        await store.async_upsert_node(
            NodeState(
                node_key="meshtastic:valid",
                protocol="meshtastic",
                node_id="!12345678",
            )
        )
        await store._execute(
            """
            INSERT INTO nodes(node_key, protocol, updated_at, data)
            VALUES(?, ?, ?, ?)
            """,
            ("broken-json", "meshtastic", "2026-07-29T12:00:00+00:00", "{"),
        )
        mismatched_node = NodeState(
            node_key="meshtastic:payload-key",
            protocol="meshtastic",
            node_id="!22222222",
        ).as_dict()
        await store._execute(
            """
            INSERT INTO nodes(node_key, protocol, updated_at, data)
            VALUES(?, ?, ?, ?)
            """,
            (
                "meshtastic:row-key",
                "meshtastic",
                "2026-07-29T12:01:00+00:00",
                json.dumps(mismatched_node),
            ),
        )
        invalid_scalar_node = NodeState(
            node_key="meshtastic:invalid-scalar",
            protocol="meshtastic",
            node_id="!33333333",
        ).as_dict()
        invalid_scalar_node["long_name"] = ["not", "text"]
        invalid_scalar_node["hardware_model"] = {"not": "text"}
        await store._execute(
            """
            INSERT INTO nodes(node_key, protocol, updated_at, data)
            VALUES(?, ?, ?, ?)
            """,
            (
                "meshtastic:invalid-scalar",
                "meshtastic",
                "2026-07-29T12:02:00+00:00",
                json.dumps(invalid_scalar_node),
            ),
        )

        base = datetime(2026, 7, 29, 12, tzinfo=UTC)
        for message_id, offset in (("valid-old", 0), ("valid-new", 1)):
            await store.async_add_message(
                MessageRecord(
                    message_id=message_id,
                    protocol="meshtastic",
                    gateway_id="gateway-1",
                    sender="homeassistant",
                    receiver=None,
                    channel="0",
                    text=message_id,
                    timestamp=base + timedelta(minutes=offset),
                )
            )

        await store._execute(
            """
            INSERT INTO messages(
                message_id, protocol, gateway_id, direction, timestamp,
                sender, receiver, channel, text, data
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invalid-json",
                "meshtastic",
                "gateway-1",
                "rx",
                "2026-07-29T12:04:00+00:00",
                None,
                None,
                "0",
                "not exposed",
                "{",
            ),
        )
        invalid_shape = MessageRecord(
            message_id="invalid-shape",
            protocol="meshtastic",
            gateway_id="gateway-1",
            sender=None,
            receiver=None,
            channel="0",
            text="not exposed",
            timestamp=base + timedelta(minutes=3),
        ).as_dict()
        invalid_shape["raw"] = ["not", "a", "mapping"]
        await store._execute(
            """
            INSERT INTO messages(
                message_id, protocol, gateway_id, direction, timestamp,
                sender, receiver, channel, text, data
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invalid-shape",
                "meshtastic",
                "gateway-1",
                "rx",
                "2026-07-29T12:03:00+00:00",
                None,
                None,
                "0",
                "not exposed",
                json.dumps(invalid_shape),
            ),
        )
        invalid_hops = MessageRecord(
            message_id="invalid-hops",
            protocol="meshtastic",
            gateway_id="gateway-1",
            sender=None,
            receiver=None,
            channel="0",
            text="not exposed",
            timestamp=base + timedelta(minutes=3, seconds=30),
        ).as_dict()
        invalid_hops["hops"] = float("inf")
        await store._execute(
            """
            INSERT INTO messages(
                message_id, protocol, gateway_id, direction, timestamp,
                sender, receiver, channel, text, data
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invalid-hops",
                "meshtastic",
                "gateway-1",
                "rx",
                "2026-07-29T12:03:30+00:00",
                None,
                None,
                "0",
                "not exposed",
                json.dumps(invalid_hops),
            ),
        )
        mismatched_message = MessageRecord(
            message_id="payload-message-id",
            protocol="meshtastic",
            gateway_id="gateway-1",
            sender=None,
            receiver=None,
            channel="0",
            text="not exposed",
            timestamp=base + timedelta(minutes=2),
        ).as_dict()
        await store._execute(
            """
            INSERT INTO messages(
                message_id, protocol, gateway_id, direction, timestamp,
                sender, receiver, channel, text, data
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "row-message-id",
                "meshtastic",
                "gateway-1",
                "rx",
                "2026-07-29T12:02:00+00:00",
                None,
                None,
                "0",
                "not exposed",
                json.dumps(mismatched_message),
            ),
        )

        snapshot = await store.async_load_snapshot(recent_limit=2)
        recent = await store.async_recent_messages(limit=2)

        assert set(snapshot.nodes) == {"meshtastic:valid"}
        assert [message.message_id for message in snapshot.recent_messages] == [
            "valid-old",
            "valid-new",
        ]
        assert [message.message_id for message in recent] == [
            "valid-old",
            "valid-new",
        ]
        await store.async_close()

    asyncio.run(run())


def test_pending_outbox_filters_before_limit_and_quarantines_poison(
    tmp_path,
) -> None:
    """Historical and malformed rows cannot hide a valid queued message."""

    async def run() -> None:
        store = MeshStore(tmp_path / "meshnet.sqlite3")
        await store.async_open()
        base = datetime(2026, 7, 29, 12, tzinfo=UTC)
        for index in range(100):
            await store.async_add_message(
                MessageRecord(
                    message_id=f"sent-{index:03d}",
                    protocol="meshtastic",
                    gateway_id="gateway-1",
                    sender="homeassistant",
                    receiver=None,
                    channel="0",
                    text="sent",
                    timestamp=base + timedelta(seconds=index),
                    direction="tx",
                    raw={"status": "sent"},
                )
            )
        await store._execute(
            """
            INSERT INTO messages(
                message_id, protocol, gateway_id, direction, timestamp,
                sender, receiver, channel, text, data
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "malformed-json",
                "unknown",
                "queued",
                "tx",
                "2026-07-29T12:01:40+00:00",
                "homeassistant",
                None,
                "0",
                "not exposed",
                "{",
            ),
        )
        poisoned = MessageRecord(
            message_id="poisoned-route",
            protocol="unknown",
            gateway_id="queued",
            sender="homeassistant",
            receiver=None,
            channel="0",
            text="not exposed",
            timestamp=base + timedelta(seconds=101),
            direction="tx",
            raw={"status": "queued", "gateway_id": ["not", "hashable"]},
        )
        await store.async_add_message(poisoned)
        poisoned_timestamp = MessageRecord(
            message_id="poisoned-timestamp",
            protocol="unknown",
            gateway_id="queued",
            sender="homeassistant",
            receiver=None,
            channel="0",
            text="not exposed",
            timestamp=base + timedelta(seconds=102),
            direction="tx",
            raw={"status": "queued"},
        )
        await store.async_add_message(poisoned_timestamp)
        mismatched_timestamp_data = poisoned_timestamp.as_dict()
        mismatched_timestamp_data["timestamp"] = (
            base + timedelta(seconds=999)
        ).isoformat()
        await store._execute(
            "UPDATE messages SET data = ? WHERE message_id = ?",
            (json.dumps(mismatched_timestamp_data), "poisoned-timestamp"),
        )
        await store.async_add_message(
            MessageRecord(
                message_id="deliverable",
                protocol="unknown",
                gateway_id="queued",
                sender="homeassistant",
                receiver=None,
                channel="0",
                text="deliverable",
                timestamp=base + timedelta(seconds=103),
                direction="tx",
                raw={"status": "queued"},
            )
        )

        pending = await store.async_pending_outbox(limit=1)

        assert [message.message_id for message in pending] == ["deliverable"]
        for message_id in ("poisoned-route", "poisoned-timestamp"):
            poisoned_row = await store._fetchone(
                "SELECT data FROM messages WHERE message_id = ?", (message_id,)
            )
            assert poisoned_row is not None
            quarantined = json.loads(poisoned_row["data"])
            assert quarantined["raw"]["status"] == "blocked"
            assert quarantined["raw"]["last_error_code"] == "invalid_message"
        diagnostics = await store.async_diagnostics()
        assert diagnostics["message_direction_counts"]["queued"] == 1
        await store.async_close()

    asyncio.run(run())


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
