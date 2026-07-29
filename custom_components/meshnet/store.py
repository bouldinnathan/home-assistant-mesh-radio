"""SQLite persistence for MeshNet."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .const import STORAGE_SCHEMA_VERSION
from .models import (
    MeshPacket,
    MeshSnapshot,
    MessageRecord,
    NodeState,
    parse_timestamp,
    stable_json,
    timestamp_to_json,
    utcnow,
)

_STORE_CLOSE_WAIT_TIMEOUT = 2.0


class MeshStore:
    """Durable SQLite cache for nodes, messages, packets, and routes."""

    def __init__(
        self,
        path: Path,
        executor: Callable[[Callable[..., Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self.path = path
        self._executor = executor
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._inflight: set[asyncio.Future[Any]] = set()
        self._operation_tasks: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None

    async def async_open(self) -> None:
        """Open and initialize the database."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def connect() -> sqlite3.Connection:
            return sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,
            )

        if self._executor is None:
            conn = connect()
        else:
            connect_future = self._start_executor(connect)
            try:
                conn = await asyncio.shield(connect_future)
            except asyncio.CancelledError:
                # async_setup_entry rollback can call async_close before the
                # executor returns, while _conn is still unset. Retain an exact
                # owner that closes the late connection instead of losing it.
                close_task = asyncio.create_task(
                    self._async_close_cancelled_open(connect_future)
                )
                self._retain_close_task(close_task)
                raise
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        await self._execute("PRAGMA journal_mode=WAL")
        await self._execute("PRAGMA foreign_keys=ON")
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                node_key TEXT PRIMARY KEY,
                protocol TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                protocol TEXT NOT NULL,
                gateway_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                sender TEXT,
                receiver TEXT,
                channel TEXT,
                text TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS packets (
                fingerprint TEXT PRIMARY KEY,
                protocol TEXT NOT NULL,
                gateway_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                packet_id TEXT,
                sender TEXT,
                receiver TEXT,
                channel TEXT,
                data TEXT NOT NULL
            )
            """
        )
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS routes (
                route_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        await self._execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
            ON messages(timestamp)
            """
        )
        await self._execute(
            """
            CREATE INDEX IF NOT EXISTS idx_packets_timestamp
            ON packets(timestamp)
            """
        )

    async def async_close(self) -> None:
        """Close the database connection."""
        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(self._async_close_serialized())
            self._retain_close_task(close_task)
        if close_task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(close_task), timeout=_STORE_CLOSE_WAIT_TIMEOUT
            )
        except TimeoutError:
            # The close task retains the connection and waits for any executor
            # operation that outlived cancellation. Never close SQLite beneath
            # a running thread, and never hold Home Assistant unload forever.
            return

    async def _async_close_serialized(self) -> None:
        """Detach and close the connection behind all serialized operations."""
        async with self._lock:
            conn = self._conn
            self._conn = None
            pending = set(self._inflight)
        if conn is not None:
            await self._async_finish_close(conn, pending)

    def _retain_close_task(self, close_task: asyncio.Task[None]) -> None:
        """Retain one close owner and consume any late failure."""
        self._close_task = close_task

        def close_done(task: asyncio.Task[None]) -> None:
            if self._close_task is task:
                self._close_task = None
            if not task.cancelled():
                task.exception()

        close_task.add_done_callback(close_done)

    async def _async_close_cancelled_open(
        self, connect_future: asyncio.Future[Any]
    ) -> None:
        """Close a connection whose setup waiter was cancelled before publish."""
        await asyncio.wait({connect_future})
        if connect_future.cancelled():
            return
        try:
            conn = connect_future.result()
        except BaseException:
            return
        await self._run(conn.close)

    async def _async_finish_close(
        self,
        conn: sqlite3.Connection,
        pending: set[asyncio.Future[Any]],
    ) -> None:
        """Close only after executor work using this connection has finished."""
        if pending:
            remaining = {future for future in pending if not future.done()}
            if remaining:
                finished = asyncio.Event()

                def executor_finished(future: asyncio.Future[Any]) -> None:
                    remaining.discard(future)
                    if not remaining:
                        finished.set()

                for future in remaining.copy():
                    future.add_done_callback(executor_finished)
                await finished.wait()
        await self._run(conn.close)

    async def async_load_snapshot(self, *, recent_limit: int = 100) -> MeshSnapshot:
        """Load cached nodes and recent messages."""
        node_rows = await self._fetchall(
            "SELECT node_key, data FROM nodes ORDER BY node_key ASC"
        )
        nodes: dict[str, NodeState] = {}
        for row in node_rows:
            node = self._node_from_row(row)
            if node is None:
                continue
            nodes[node.node_key] = node
        messages = await self._async_recent_message_records(recent_limit)
        return MeshSnapshot(nodes=nodes, recent_messages=messages)

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> NodeState | None:
        """Decode one cached node without letting corrupt data poison setup."""
        try:
            payload = json.loads(row["data"])
            if not isinstance(payload, dict):
                return None
            if payload.get("node_key") != row["node_key"]:
                return None
            if not isinstance(payload.get("node_key"), str) or not isinstance(
                payload.get("protocol"), str
            ):
                return None
            if any(
                field in payload
                and payload[field] is not None
                and not isinstance(payload[field], str)
                for field in (
                    "node_id",
                    "mac",
                    "public_key",
                    "user_name",
                    "long_name",
                    "short_name",
                    "hardware_model",
                    "firmware_version",
                    "radio_type",
                    "role",
                    "last_gateway_id",
                )
            ):
                return None
            if "online" in payload and not isinstance(payload["online"], bool):
                return None
            last_heard = payload.get("last_heard")
            if last_heard is not None:
                if not isinstance(last_heard, str):
                    return None
                parsed_last_heard = parse_timestamp(last_heard)
                if (
                    parsed_last_heard is None
                    or timestamp_to_json(parsed_last_heard) != last_heard
                ):
                    return None
            for field in (
                "connectivity",
                "power",
                "radio",
                "location",
                "routing",
                "sensors",
                "raw",
            ):
                value = payload.get(field)
                if value is not None and not isinstance(value, dict):
                    return None
            gateway_ids = payload.get("gateway_ids")
            if gateway_ids is not None and (
                not isinstance(gateway_ids, list)
                or any(not isinstance(value, str) for value in gateway_ids)
            ):
                return None
            return NodeState.from_dict(payload)
        except (
            json.JSONDecodeError,
            KeyError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> MessageRecord | None:
        """Decode one cached message and verify its stable database identity."""
        try:
            payload = json.loads(row["data"])
            if not isinstance(payload, dict):
                return None
            if payload.get("message_id") != row["message_id"]:
                return None
            if payload.get("timestamp") != row["timestamp"]:
                return None
            if any(
                not isinstance(payload.get(field), str)
                for field in (
                    "message_id",
                    "protocol",
                    "gateway_id",
                    "text",
                    "direction",
                )
            ):
                return None
            if any(
                field in payload
                and payload[field] is not None
                and not isinstance(payload[field], str)
                for field in (
                    "sender",
                    "receiver",
                    "channel",
                    "message_type",
                    "priority",
                )
            ):
                return None
            raw = payload.get("raw")
            if raw is not None and not isinstance(raw, dict):
                return None
            encrypted = payload.get("encrypted")
            if encrypted is not None and not isinstance(encrypted, bool):
                return None
            hops = payload.get("hops")
            if hops is not None and (
                isinstance(hops, bool) or not isinstance(hops, int)
            ):
                return None
            message = MessageRecord.from_dict(payload)
            if timestamp_to_json(message.timestamp) != row["timestamp"]:
                return None
            return message
        except (
            json.JSONDecodeError,
            KeyError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None

    async def _async_recent_message_records(
        self, limit: int
    ) -> list[MessageRecord]:
        """Return up to ``limit`` valid messages despite isolated bad rows."""
        if limit <= 0:
            return []
        messages_descending: list[MessageRecord] = []
        cursor: tuple[str, str] | None = None
        while len(messages_descending) < limit:
            batch_limit = max(100, min(500, limit - len(messages_descending)))
            if cursor is None:
                rows = await self._fetchall(
                    """
                    SELECT message_id, timestamp, data
                    FROM messages
                    ORDER BY timestamp DESC, message_id DESC
                    LIMIT ?
                    """,
                    (batch_limit,),
                )
            else:
                timestamp, message_id = cursor
                rows = await self._fetchall(
                    """
                    SELECT message_id, timestamp, data
                    FROM messages
                    WHERE timestamp < ?
                       OR (timestamp = ? AND message_id < ?)
                    ORDER BY timestamp DESC, message_id DESC
                    LIMIT ?
                    """,
                    (timestamp, timestamp, message_id, batch_limit),
                )
            if not rows:
                break
            for row in rows:
                message = self._message_from_row(row)
                if message is not None:
                    messages_descending.append(message)
                    if len(messages_descending) == limit:
                        break
            last_row = rows[-1]
            cursor = (last_row["timestamp"], last_row["message_id"])
            if len(rows) < batch_limit:
                break
        return list(reversed(messages_descending))

    async def async_upsert_node(self, node: NodeState) -> None:
        """Persist a node."""
        data = node.as_dict()
        await self._execute(
            """
            INSERT INTO nodes(node_key, protocol, updated_at, data)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(node_key) DO UPDATE SET
                protocol=excluded.protocol,
                updated_at=excluded.updated_at,
                data=excluded.data
            """,
            (node.node_key, node.protocol, timestamp_to_json(utcnow()), stable_json(data)),
        )

    async def async_upsert_nodes(self, nodes: Iterable[NodeState]) -> None:
        """Persist multiple nodes."""
        for node in nodes:
            await self.async_upsert_node(node)

    async def async_add_message(self, message: MessageRecord) -> None:
        """Persist a message."""
        await self._execute(
            """
            INSERT OR REPLACE INTO messages(
                message_id, protocol, gateway_id, direction, timestamp,
                sender, receiver, channel, text, data
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.protocol,
                message.gateway_id,
                message.direction,
                timestamp_to_json(message.timestamp),
                message.sender,
                message.receiver,
                message.channel,
                message.text,
                stable_json(message.as_dict()),
            ),
        )

    async def async_add_packet(self, packet: MeshPacket) -> bool:
        """Persist a packet, returning True if it was new."""
        try:
            await self._execute(
                """
                INSERT INTO packets(
                    fingerprint, protocol, gateway_id, timestamp, packet_id,
                    sender, receiver, channel, data
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet.fingerprint(),
                    packet.protocol,
                    packet.gateway_id,
                    timestamp_to_json(packet.timestamp),
                    packet.packet_id,
                    packet.sender,
                    packet.receiver,
                    packet.channel,
                    stable_json(packet.as_dict()),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    async def async_recent_messages(self, limit: int = 100) -> list[MessageRecord]:
        """Return recent messages oldest-first."""
        return await self._async_recent_message_records(limit)

    async def async_pending_outbox(
        self,
        limit: int = 100,
        *,
        after: tuple[str, str] | None = None,
    ) -> list[MessageRecord]:
        """Return queued outbound messages oldest-first."""
        if limit <= 0:
            return []
        messages: list[MessageRecord] = []
        cursor = after
        while len(messages) < limit:
            batch_limit = max(100, min(500, limit - len(messages)))
            if cursor is None:
                rows = await self._fetchall(
                    """
                    SELECT message_id, timestamp, data
                    FROM messages
                    WHERE direction = 'tx'
                      AND CASE
                            WHEN json_valid(data)
                            THEN json_extract(data, '$.raw.status')
                            ELSE NULL
                          END = 'queued'
                    ORDER BY timestamp ASC, message_id ASC
                    LIMIT ?
                    """,
                    (batch_limit,),
                )
            else:
                timestamp, message_id = cursor
                rows = await self._fetchall(
                    """
                    SELECT message_id, timestamp, data
                    FROM messages
                    WHERE direction = 'tx'
                      AND CASE
                            WHEN json_valid(data)
                            THEN json_extract(data, '$.raw.status')
                            ELSE NULL
                          END = 'queued'
                      AND (timestamp > ? OR
                           (timestamp = ? AND message_id > ?))
                    ORDER BY timestamp ASC, message_id ASC
                    LIMIT ?
                    """,
                    (timestamp, timestamp, message_id, batch_limit),
                )
            if not rows:
                break
            for row in rows:
                message = self._message_from_row(row)
                route_gateway = (
                    message.raw.get("gateway_id")
                    if message is not None
                    else None
                )
                if (
                    message is None
                    or message.direction != "tx"
                    or message.raw.get("status") != "queued"
                    or (
                        route_gateway is not None
                        and not isinstance(route_gateway, str)
                    )
                ):
                    # Quarantine one parseable poison record without preventing
                    # later valid queued messages from being delivered. Database
                    # failures still propagate from _execute.
                    await self._execute(
                        """
                        UPDATE messages
                        SET data = json_set(
                            data,
                            '$.raw.status', 'blocked',
                            '$.raw.last_error_code', 'invalid_message'
                        )
                        WHERE message_id = ? AND json_valid(data)
                        """,
                        (row["message_id"],),
                    )
                    continue
                messages.append(message)
                if len(messages) == limit:
                    break
            last_row = rows[-1]
            cursor = (last_row["timestamp"], last_row["message_id"])
            if len(messages) == limit or len(rows) < batch_limit:
                break
        return messages

    async def async_messages_since(self, when: datetime) -> int:
        """Return message count since a timestamp."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS count FROM messages WHERE timestamp >= ?",
            (timestamp_to_json(when.astimezone(UTC)),),
        )
        return int(row["count"] if row else 0)

    async def async_prune(self, history_days: int) -> None:
        """Prune old packet and message history."""
        cutoff = utcnow() - timedelta(days=history_days)
        cutoff_text = timestamp_to_json(cutoff)
        await self._execute("DELETE FROM messages WHERE timestamp < ?", (cutoff_text,))
        await self._execute("DELETE FROM packets WHERE timestamp < ?", (cutoff_text,))

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return store health and aggregate metadata without stored content."""
        diagnostics: dict[str, Any] = {
            "available": self._conn is not None,
            "schema_version": STORAGE_SCHEMA_VERSION,
            "sqlite_version": sqlite3.sqlite_version,
            "close_pending": self._close_task is not None,
            "inflight_operation_count": len(self._inflight),
        }
        if self._conn is None:
            return diagnostics

        table_rows = await self._fetchall(
            """
            SELECT 'nodes' AS table_name, COUNT(*) AS count FROM nodes
            UNION ALL
            SELECT 'messages', COUNT(*) FROM messages
            UNION ALL
            SELECT 'packets', COUNT(*) FROM packets
            UNION ALL
            SELECT 'routes', COUNT(*) FROM routes
            """
        )
        table_counts = {
            str(row["table_name"]): int(row["count"]) for row in table_rows
        }
        message_summary = await self._fetchone(
            """
            SELECT
                MIN(timestamp) AS oldest_timestamp,
                MAX(timestamp) AS newest_timestamp,
                SUM(CASE WHEN direction = 'rx' THEN 1 ELSE 0 END) AS received_count,
                SUM(CASE WHEN direction = 'tx' THEN 1 ELSE 0 END) AS sent_count,
                SUM(
                    CASE
                        WHEN direction = 'tx'
                        AND CASE
                              WHEN json_valid(data)
                              THEN json_extract(data, '$.raw.status')
                              ELSE NULL
                            END = 'queued'
                        THEN 1 ELSE 0
                    END
                ) AS queued_count
            FROM messages
            """
        )
        packet_summary = await self._fetchone(
            """
            SELECT
                MIN(timestamp) AS oldest_timestamp,
                MAX(timestamp) AS newest_timestamp
            FROM packets
            """
        )
        node_protocol_rows = await self._fetchall(
            "SELECT protocol, COUNT(*) AS count FROM nodes GROUP BY protocol"
        )
        message_protocol_rows = await self._fetchall(
            "SELECT protocol, COUNT(*) AS count FROM messages GROUP BY protocol"
        )
        packet_protocol_rows = await self._fetchall(
            "SELECT protocol, COUNT(*) AS count FROM packets GROUP BY protocol"
        )
        journal_row = await self._fetchone("PRAGMA journal_mode")

        def file_sizes() -> dict[str, int]:
            def size(path: Path) -> int:
                try:
                    return path.stat().st_size
                except OSError:
                    return 0

            return {
                "database_bytes": size(self.path),
                "wal_bytes": size(self.path.with_name(f"{self.path.name}-wal")),
                "shared_memory_bytes": size(
                    self.path.with_name(f"{self.path.name}-shm")
                ),
            }

        sizes = await self._run(file_sizes)
        diagnostics.update(
            {
                "node_count": table_counts.get("nodes", 0),
                "message_count": table_counts.get("messages", 0),
                "packet_count": table_counts.get("packets", 0),
                "route_count": table_counts.get("routes", 0),
                "table_counts": table_counts,
                "message_direction_counts": {
                    "received": int(message_summary["received_count"] or 0)
                    if message_summary
                    else 0,
                    "sent": int(message_summary["sent_count"] or 0)
                    if message_summary
                    else 0,
                    "queued": int(message_summary["queued_count"] or 0)
                    if message_summary
                    else 0,
                },
                "node_protocol_counts": self._diagnostic_group_counts(
                    node_protocol_rows
                ),
                "message_protocol_counts": self._diagnostic_group_counts(
                    message_protocol_rows
                ),
                "packet_protocol_counts": self._diagnostic_group_counts(
                    packet_protocol_rows
                ),
                "message_age_seconds": self._diagnostic_age_range(message_summary),
                "packet_age_seconds": self._diagnostic_age_range(packet_summary),
                "journal_mode": (
                    str(journal_row["journal_mode"]) if journal_row else "unknown"
                ),
                "file_sizes": sizes,
            }
        )
        return diagnostics

    @staticmethod
    def _diagnostic_group_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
        """Return counts grouped by a non-identifying protocol field."""
        return {
            str(row["protocol"]): int(row["count"])
            for row in rows
            if row["protocol"]
        }

    @staticmethod
    def _diagnostic_age_range(row: sqlite3.Row | None) -> dict[str, int | None]:
        """Return record age bounds instead of activity timestamps."""
        if row is None:
            return {"oldest": None, "newest": None}

        def age(value: Any) -> int | None:
            parsed = parse_timestamp(value)
            if parsed is None:
                return None
            return max(0, int((utcnow() - parsed).total_seconds()))

        return {
            "oldest": age(row["oldest_timestamp"]),
            "newest": age(row["newest_timestamp"]),
        }

    async def _count(self, table: str) -> int:
        row = await self._fetchone(f"SELECT COUNT(*) AS count FROM {table}")
        return int(row["count"] if row else 0)

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self._run_serialized(lambda conn: conn.execute(sql, params))

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return await self._run_serialized(
            lambda conn: conn.execute(sql, params).fetchone()
        )

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return await self._run_serialized(
            lambda conn: conn.execute(sql, params).fetchall()
        )

    async def _run_serialized(
        self,
        target: Callable[[sqlite3.Connection], Any],
    ) -> Any:
        """Run one connection operation whose owner survives caller cancellation."""
        owner = asyncio.create_task(self._async_run_serialized(target))
        self._operation_tasks.add(owner)

        def operation_done(task: asyncio.Task[Any]) -> None:
            self._operation_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        owner.add_done_callback(operation_done)
        return await asyncio.shield(owner)

    async def _async_run_serialized(
        self,
        target: Callable[[sqlite3.Connection], Any],
    ) -> Any:
        """Hold the SQLite lease until the exact executor future has drained."""
        await self._lock.acquire()
        try:
            conn = self._conn
            if conn is None:
                raise RuntimeError("MeshStore is not open")
            if self._executor is None:
                try:
                    return target(conn)
                finally:
                    self._lock.release()
            future = self._start_executor(lambda: target(conn))
        except BaseException:
            if self._lock.locked():
                self._lock.release()
            raise

        def release_connection_lease(_future: asyncio.Future[Any]) -> None:
            if self._lock.locked():
                self._lock.release()

        future.add_done_callback(release_connection_lease)
        return await asyncio.shield(future)

    async def _run(self, func: Callable[[], Any]) -> Any:
        if self._executor is not None:
            future = self._start_executor(func)
            return await asyncio.shield(future)
        return func()

    def _start_executor(self, func: Callable[[], Any]) -> asyncio.Future[Any]:
        """Start, retain, and observe one executor operation."""
        if self._executor is None:
            raise RuntimeError("MeshStore executor is unavailable")
        future = asyncio.ensure_future(self._executor(func))
        self._inflight.add(future)

        def executor_done(done_future: asyncio.Future[Any]) -> None:
            self._inflight.discard(done_future)
            if not done_future.cancelled():
                done_future.exception()

        future.add_done_callback(executor_done)
        return future
