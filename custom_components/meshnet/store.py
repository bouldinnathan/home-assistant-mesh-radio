"""SQLite persistence for MeshNet."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import MeshPacket, MeshSnapshot, MessageRecord, NodeState, stable_json, timestamp_to_json, utcnow

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
        async with self._lock:
            close_task = self._close_task
            if self._conn is not None:
                conn = self._conn
                self._conn = None
                close_task = asyncio.create_task(
                    self._async_finish_close(conn, set(self._inflight))
                )
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
        node_rows = await self._fetchall("SELECT data FROM nodes")
        message_rows = await self._fetchall(
            "SELECT data FROM messages ORDER BY timestamp DESC LIMIT ?",
            (recent_limit,),
        )
        nodes: dict[str, NodeState] = {}
        for row in node_rows:
            node = NodeState.from_dict(json.loads(row["data"]))
            nodes[node.node_key] = node
        messages = [
            MessageRecord.from_dict(json.loads(row["data"]))
            for row in reversed(message_rows)
        ]
        return MeshSnapshot(nodes=nodes, recent_messages=messages)

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
        rows = await self._fetchall(
            "SELECT data FROM messages ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        return [MessageRecord.from_dict(json.loads(row["data"])) for row in reversed(rows)]

    async def async_pending_outbox(self, limit: int = 100) -> list[MessageRecord]:
        """Return queued outbound messages oldest-first."""
        rows = await self._fetchall(
            "SELECT data FROM messages WHERE direction = 'tx' ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        )
        messages = [MessageRecord.from_dict(json.loads(row["data"])) for row in rows]
        return [message for message in messages if message.raw.get("status") == "queued"]

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

    async def async_diagnostics(self) -> dict[str, int]:
        """Return aggregate database diagnostics without stored mesh content."""
        node_count = await self._count("nodes")
        message_count = await self._count("messages")
        packet_count = await self._count("packets")
        return {
            "node_count": node_count,
            "message_count": message_count,
            "packet_count": packet_count,
        }

    async def _count(self, table: str) -> int:
        row = await self._fetchone(f"SELECT COUNT(*) AS count FROM {table}")
        return int(row["count"] if row else 0)

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._lock:
            conn = self._conn
            if conn is None:
                raise RuntimeError("MeshStore is not open")
            await self._run(lambda: conn.execute(sql, params))

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        async with self._lock:
            conn = self._conn
            if conn is None:
                raise RuntimeError("MeshStore is not open")
            return await self._run(lambda: conn.execute(sql, params).fetchone())

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            conn = self._conn
            if conn is None:
                raise RuntimeError("MeshStore is not open")
            return await self._run(lambda: conn.execute(sql, params).fetchall())

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
