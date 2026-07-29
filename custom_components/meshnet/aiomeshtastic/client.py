# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
# SPDX-FileCopyrightText: 2025 Hendrik @novag
# SPDX-FileCopyrightText: 2026 MeshNet contributors
#
# SPDX-License-Identifier: MIT

"""Small, asyncio-native Meshtastic client for one Bluetooth radio.

This is a Bluetooth-only adaptation of ``aiomeshtastic`` from
``meshtastic/home-assistant``.  It uses the installed Meshtastic package only
for its protobuf API; it never constructs the package's threaded interfaces
and has no MQTT, serial, TCP, pubsub, or Home Assistant dependency.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import inspect
import logging
import math
import secrets
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2

from .bluetooth import BluetoothConnection
from .errors import (
    MeshtasticAsyncError,
    MeshtasticCleanupError,
    MeshtasticConfigurationError,
    MeshtasticConnectionError,
    MeshtasticNotConnectedError,
)

_BROADCAST_NUM = 0xFFFFFFFF
_BROADCAST_ID = "^all"
_NODELESS_WANT_CONFIG_ID = 69420
_STREAM_END = object()
_NODE_NAME_FIELDS = (
    "shortName",
    "longName",
    "short_name",
    "long_name",
    "shortname",
    "longname",
)

_CONNECTION_DIAGNOSTIC_FIELDS = frozenset(
    {
        "state",
        "connected",
        "owns_endpoint",
        "notifications_ready",
        "closing",
        "io_locked",
        "connect_count",
        "disconnect_count",
        "notification_count",
        "read_trigger_count",
        "idle_read_count",
        "read_count",
        "read_timeout_count",
        "empty_read_retry_count",
        "write_count",
        "forced_read_count",
        "notify_restart_count",
        "cleanup_task",
        "last_error_type",
        "last_failure_phase",
    }
)
_CONNECTION_TIMEOUT_FIELDS = frozenset(
    {"connect", "notify", "io", "read", "disconnect", "idle_read"}
)


def _diagnostic_scalar(value: Any) -> str | bool | int | float | None:
    """Return one bounded identity-free primitive or ``None`` when unsafe."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and 0 < len(value) <= 128 and all(
        character.isalnum() or character in "_.-" for character in value
    ):
        return value
    return None


def _normalized_node_name(value: Any) -> str | None:
    """Return one conservative key for exact case-insensitive name matching."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return normalized or None


type DeviceProvider = Callable[[], Any | Awaitable[Any]]
type PacketCallback = Callable[[dict[str, Any]], Any | Awaitable[Any]]
type ConnectionCallback = Callable[[bool], Any | Awaitable[Any]]
type StateCallback = Callable[[str], Any | Awaitable[Any]]
type ConnectionFactory = Callable[..., BluetoothConnection]


class MeshtasticBluetoothClient:
    """Maintain one local Meshtastic Bluetooth protocol session."""

    def __init__(
        self,
        *,
        address: str,
        device_provider: DeviceProvider,
        connection_factory: ConnectionFactory = BluetoothConnection,
        connect_timeout: float = 30.0,
        configuration_timeout: float = 60.0,
        io_timeout: float = 15.0,
        read_timeout: float | None = None,
        disconnect_timeout: float = 5.0,
        start_timeout: float | None = None,
        stop_timeout: float = 12.0,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        heartbeat_interval: float = 300.0,
        stream_queue_size: int = 128,
        state_callback: StateCallback | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not address.strip():
            raise ValueError("address cannot be empty")
        resolved_read_timeout = (
            read_timeout
            if read_timeout is not None
            else max(io_timeout, configuration_timeout + 5.0)
        )
        if min(
            connect_timeout,
            configuration_timeout,
            io_timeout,
            resolved_read_timeout,
            disconnect_timeout,
            stop_timeout,
            reconnect_initial_delay,
            reconnect_max_delay,
            heartbeat_interval,
        ) <= 0:
            raise ValueError("timeouts and reconnect delays must be positive")
        if stream_queue_size < 1:
            raise ValueError("stream_queue_size must be positive")

        self._address = address
        self._device_provider = device_provider
        self._connection_factory = connection_factory
        self._connect_timeout = connect_timeout
        self._configuration_timeout = configuration_timeout
        self._io_timeout = io_timeout
        self._read_timeout = resolved_read_timeout
        self._disconnect_timeout = disconnect_timeout
        self._start_timeout = start_timeout or (
            connect_timeout + configuration_timeout + disconnect_timeout + 5.0
        )
        self._stop_timeout = stop_timeout
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._heartbeat_interval = heartbeat_interval
        self._stream_queue_size = stream_queue_size
        self._state_callback = state_callback
        self._logger = logger or logging.getLogger(__name__)

        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._active_event = asyncio.Event()
        self._start_result_event = asyncio.Event()
        self._config_complete_event = asyncio.Event()
        self._runner_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stop_cleanup_task: asyncio.Task[None] | None = None
        self._connection: BluetoothConnection | None = None
        self._callback_tasks: set[asyncio.Future[Any]] = set()
        self._packet_callbacks: set[PacketCallback] = set()
        self._connection_callbacks: set[ConnectionCallback] = set()
        self._stream_queues: set[asyncio.Queue[Any]] = set()
        self._nodes: dict[int, dict[str, Any]] = {}

        self._state = "bluetooth_idle"
        self._connected = False
        self._stopping = False
        self._ever_active = False
        self._initial_error: BaseException | None = None
        self._last_connection_snapshot: dict[str, Any] | None = None
        self._last_connection_cleanup_outcome = "not_started"
        self._config_id: int | None = None
        self._my_node_num: int | None = None
        self._packet_sequence = secrets.randbits(10)
        self._last_error_type: str | None = None
        self._last_failure_phase: str | None = None
        self._connect_attempts = 0
        self._successful_connections = 0
        self._disconnect_count = 0
        self._reconnect_count = 0
        self._from_radio_count = 0
        self._packet_count = 0
        self._node_update_count = 0
        self._parse_error_count = 0
        self._callback_error_count = 0
        self._dropped_stream_packet_count = 0
        self._sent_text_count = 0
        self._last_connected_monotonic: float | None = None

    @property
    def connected(self) -> bool:
        """Return whether both GATT and the Meshtastic handshake are active."""
        connection = self._connection
        return bool(
            self._connected
            and connection is not None
            and connection.is_connected
            and not self._stopping
        )

    async def async_start(self) -> None:
        """Start one session and wait for the initial node/config download."""
        async with self._lifecycle_lock:
            if self.connected:
                return
            self._consume_completed_cleanup_owners()
            if self._cleanup_owners_pending():
                raise MeshtasticCleanupError(
                    "Previous Meshtastic Bluetooth owner tasks are still stopping; "
                    "cleanup must finish before restart"
                )
            if self._connection_owns_endpoint(self._connection):
                raise MeshtasticCleanupError(
                    "A previous Bluetooth client is still connected; cleanup must be retried"
                )
            runner = self._runner_task
            if runner is None or runner.done():
                self._stopping = False
                self._ever_active = False
                self._initial_error = None
                self._last_failure_phase = None
                self._stop_event.clear()
                self._active_event.clear()
                self._start_result_event.clear()
                self._set_state("bluetooth_starting")
                runner = asyncio.create_task(
                    self._run_supervisor(),
                    name="meshnet-aiomeshtastic-supervisor",
                )
                self._runner_task = runner
            wait_event = self._active_event if self._ever_active else self._start_result_event

        try:
            async with asyncio.timeout(self._start_timeout):
                await wait_event.wait()
        except TimeoutError as err:
            await self.async_stop()
            raise MeshtasticConfigurationError(
                "Meshtastic Bluetooth startup exceeded its safety timeout"
            ) from err
        except asyncio.CancelledError:
            await self.async_stop()
            raise

        if self.connected:
            return
        error = self._initial_error
        if isinstance(error, BaseException):
            raise error
        raise MeshtasticConnectionError("Meshtastic Bluetooth stopped before becoming active")

    async def async_stop(self) -> None:
        """Stop the supervisor and all owned work within a fixed bound."""
        async with self._lifecycle_lock:
            self._stopping = True
            self._set_state("bluetooth_stopping")
            self._stop_event.set()
            self._active_event.clear()
            runner = self._runner_task
            if runner is not None and not runner.done():
                runner.cancel()

        cleanup_error: BaseException | None = None
        deadline = asyncio.get_running_loop().time() + self._stop_timeout
        if runner is not None:
            done, _ = await asyncio.wait(
                {runner},
                timeout=self._remaining(deadline),
            )
            if runner in done:
                try:
                    runner.result()
                except asyncio.CancelledError:
                    pass
                except BaseException as err:
                    self._remember_error(err)
                    if isinstance(err, MeshtasticCleanupError):
                        cleanup_error = err
            else:
                cleanup_error = TimeoutError("Bluetooth supervisor did not stop")

        # Only retry GATT teardown directly after the supervisor has actually
        # yielded ownership.  Concurrent cleanup attempts can corrupt backend
        # state and obscure which task still owns the endpoint.
        runner_pending = bool(runner is not None and not runner.done())
        session_tasks = tuple(
            task
            for task in (self._reader_task, self._heartbeat_task)
            if task is not None and not task.done()
        )
        if not runner_pending and session_tasks:
            for task in session_tasks:
                task.cancel()
            await asyncio.wait(
                session_tasks,
                timeout=min(0.25, self._remaining(deadline)),
            )
            if self._reader_task is not None and self._reader_task.done():
                self._consume_task_result(self._reader_task)
                self._reader_task = None
            if self._heartbeat_task is not None and self._heartbeat_task.done():
                self._consume_task_result(self._heartbeat_task)
                self._heartbeat_task = None

        connection = self._connection
        session_owners_pending = any(
            task is not None and not task.done()
            for task in (self._reader_task, self._heartbeat_task)
        )
        if (
            not runner_pending
            and not session_owners_pending
            and self._connection_owns_endpoint(connection)
        ):
            cleanup_task = self._stop_cleanup_task
            if cleanup_task is not None and cleanup_task.done():
                self._consume_task_result(cleanup_task)
                self._stop_cleanup_task = None
                cleanup_task = None
            if cleanup_task is None:
                cleanup_task = asyncio.create_task(
                    connection.async_disconnect(),
                    name="meshnet-aiomeshtastic-stop-cleanup",
                )
                self._stop_cleanup_task = cleanup_task
            done, _ = await asyncio.wait(
                {cleanup_task},
                timeout=self._remaining(deadline),
            )
            if cleanup_task in done:
                try:
                    cleanup_task.result()
                    if not self._connection_owns_endpoint(connection):
                        cleanup_error = None
                except asyncio.CancelledError:
                    cleanup_error = TimeoutError("Bluetooth cleanup was cancelled")
                except BaseException as err:
                    cleanup_error = err
                    self._remember_error(err)
            else:
                cleanup_task.cancel()
                cleanup_error = TimeoutError("Bluetooth cleanup task did not stop")

        if not runner_pending:
            remaining_session_tasks = tuple(
                task
                for task in (self._reader_task, self._heartbeat_task)
                if task is not None and not task.done()
            )
            if remaining_session_tasks:
                await asyncio.wait(
                    remaining_session_tasks,
                    timeout=self._remaining(deadline),
                )
            if self._reader_task is not None and self._reader_task.done():
                self._consume_task_result(self._reader_task)
                self._reader_task = None
            if self._heartbeat_task is not None and self._heartbeat_task.done():
                self._consume_task_result(self._heartbeat_task)
                self._heartbeat_task = None

        await self._set_connected(False)
        self._end_packet_streams()
        self._start_result_event.set()
        endpoint_owned = self._connection_owns_endpoint(self._connection)
        owners_pending = any(
            task is not None and not task.done()
            for task in (
                self._runner_task,
                self._reader_task,
                self._heartbeat_task,
                self._stop_cleanup_task,
            )
        )
        cleanup_confirmed = not endpoint_owned and not owners_pending
        self._set_state(
            "bluetooth_stopped"
            if cleanup_confirmed
            else "bluetooth_cleanup_incomplete"
        )
        callbacks_pending = await self._cancel_callback_tasks(
            timeout=self._remaining(deadline)
        )
        self._stopping = False

        if cleanup_confirmed and not callbacks_pending:
            self._connection = None
            self._reader_task = None
            self._heartbeat_task = None
            self._stop_cleanup_task = None
            self._config_id = None
            self._config_complete_event.clear()
            self._runner_task = None
            return

        if cleanup_error is not None or endpoint_owned or owners_pending or callbacks_pending:
            if cleanup_error is not None:
                self._remember_error(cleanup_error)
            self._state = "bluetooth_cleanup_incomplete"
            raise MeshtasticCleanupError(
                "Meshtastic Bluetooth cleanup was not confirmed; endpoint ownership is retained"
            ) from cleanup_error

    async def async_send_text(
        self,
        text: str,
        *,
        destination_id: int | str | None = None,
        want_ack: bool = False,
        channel_index: int = 0,
    ) -> int:
        """Send UTF-8 text through the active radio and return its packet ID."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        payload = text.encode("utf-8")
        maximum = int(getattr(mesh_pb2.Constants, "DATA_PAYLOAD_LEN", 237))
        if not payload:
            raise ValueError("text cannot be empty")
        if len(payload) > maximum:
            raise ValueError(f"text exceeds the Meshtastic payload limit ({maximum} bytes)")
        if not isinstance(channel_index, int) or not 0 <= channel_index <= 7:
            raise ValueError("channel_index must be between 0 and 7")

        destination = self._resolve_destination(destination_id)
        async with self._send_lock:
            connection = self._connection
            if not self.connected or connection is None:
                raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not active")

            packet = mesh_pb2.MeshPacket()
            packet.to = destination
            if self._my_node_num is not None:
                setattr(packet, "from", self._my_node_num)
            packet.channel = channel_index
            packet.decoded.payload = payload
            packet.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
            packet.id = self._next_packet_id()
            packet.want_ack = want_ack
            packet.priority = (
                mesh_pb2.MeshPacket.Priority.RELIABLE
                if want_ack
                else mesh_pb2.MeshPacket.Priority.DEFAULT
            )
            to_radio = mesh_pb2.ToRadio()
            to_radio.packet.CopyFrom(packet)
            await connection.async_send(to_radio.SerializeToString())
            self._sent_text_count += 1
            return int(packet.id)

    def node_snapshot(self) -> dict[int, dict[str, Any]]:
        """Return a detached, plain-dictionary copy of the known node database."""
        return copy.deepcopy(self._nodes)

    async def async_node_snapshot(self) -> dict[int, dict[str, Any]]:
        """Async compatibility wrapper for callers with an async gateway API."""
        return self.node_snapshot()

    def add_packet_callback(self, callback: PacketCallback) -> Callable[[], None]:
        """Register a plain-dictionary packet callback and return its remover."""
        self._packet_callbacks.add(callback)

        def unsubscribe() -> None:
            self._packet_callbacks.discard(callback)

        return unsubscribe

    def add_connection_callback(self, callback: ConnectionCallback) -> Callable[[], None]:
        """Register a callback receiving each active/inactive transition."""
        self._connection_callbacks.add(callback)

        def unsubscribe() -> None:
            self._connection_callbacks.discard(callback)

        return unsubscribe

    async def packet_stream(self) -> AsyncIterator[dict[str, Any]]:
        """Yield detached packet dictionaries until the client is stopped."""
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._stream_queue_size)
        self._stream_queues.add(queue)
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    return
                yield item
        finally:
            self._stream_queues.discard(queue)

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return endpoint-free lifecycle and traffic counters from cached state."""
        now = time.monotonic()
        connection = self._connection
        return {
            "implementation": type(self).__name__,
            "upstream_commit": "3594f3525f4451880d33e988dd0e4956dab75f53",
            "state": self._state,
            "connected": self.connected,
            "stopping": self._stopping,
            "ever_active": self._ever_active,
            "runner_task": self._task_state(self._runner_task),
            "reader_task": self._task_state(self._reader_task),
            "heartbeat_task": self._task_state(self._heartbeat_task),
            "stop_cleanup_task": self._task_state(self._stop_cleanup_task),
            "callback_task_count": len(self._callback_tasks),
            "packet_callback_count": len(self._packet_callbacks),
            "connection_callback_count": len(self._connection_callbacks),
            "packet_stream_count": len(self._stream_queues),
            "node_count": len(self._nodes),
            "connect_attempts": self._connect_attempts,
            "successful_connections": self._successful_connections,
            "disconnect_count": self._disconnect_count,
            "reconnect_count": self._reconnect_count,
            "from_radio_count": self._from_radio_count,
            "packet_count": self._packet_count,
            "node_update_count": self._node_update_count,
            "parse_error_count": self._parse_error_count,
            "callback_error_count": self._callback_error_count,
            "dropped_stream_packet_count": self._dropped_stream_packet_count,
            "sent_text_count": self._sent_text_count,
            "last_error_type": self._last_error_type,
            "last_failure_phase": self._last_failure_phase,
            "last_transport_before_cleanup": copy.deepcopy(
                self._last_connection_snapshot
            ),
            "last_transport_cleanup_outcome": (
                self._last_connection_cleanup_outcome
            ),
            "connected_elapsed_seconds": (
                round(now - self._last_connected_monotonic, 3)
                if self.connected and self._last_connected_monotonic is not None
                else None
            ),
            "timeouts": {
                "connect": self._connect_timeout,
                "configuration": self._configuration_timeout,
                "io": self._io_timeout,
                "read": self._read_timeout,
                "disconnect": self._disconnect_timeout,
                "start": self._start_timeout,
                "stop": self._stop_timeout,
                "heartbeat_interval": self._heartbeat_interval,
            },
            "transport": (
                self._connection_diagnostics(connection)
                if connection is not None
                else None
            ),
        }

    async def _run_supervisor(self) -> None:
        reconnect_delay = self._reconnect_initial_delay
        try:
            while not self._stop_event.is_set():
                reader: asyncio.Task[None] | None = None
                heartbeat: asyncio.Task[None] | None = None
                connection: BluetoothConnection | None = None
                was_active = False
                try:
                    self._set_state("bluetooth_resolving_device")
                    device = await self._resolve_device()
                    if device is None:
                        raise MeshtasticConnectionError(
                            "The selected Meshtastic Bluetooth radio is not currently visible"
                        )

                    self._set_state("bluetooth_connecting")
                    self._connect_attempts += 1
                    connection = self._connection_factory(
                        address=self._address,
                        ble_device=device,
                        connect_timeout=self._connect_timeout,
                        io_timeout=self._io_timeout,
                        read_timeout=self._read_timeout,
                        disconnect_timeout=self._disconnect_timeout,
                        logger=self._logger,
                    )
                    self._connection = connection
                    self._last_connection_snapshot = None
                    self._last_connection_cleanup_outcome = "not_started"
                    await connection.async_connect()

                    self._set_state("bluetooth_requesting_configuration")
                    self._config_complete_event.clear()
                    self._config_id = self._new_config_id()
                    request = mesh_pb2.ToRadio()
                    request.want_config_id = self._config_id
                    await connection.async_send(request.SerializeToString(), force_read=True)
                    # Match Meshtastic's official BLE client: let the completed
                    # ToRadio ATT write propagate before beginning FromRadio
                    # reads. The forced-read event is already armed, but no
                    # reader exists yet, so this does not introduce a race.
                    await asyncio.sleep(0.01)

                    # Meshtastic's GATT server handles ToRadio writes and
                    # FromRadio reads on the same BLE callback task.  Its
                    # initial FromRadio read may block while waiting for the
                    # want_config request, so beginning that read first can
                    # prevent the preceding write-with-response from ever
                    # completing.  The device API requires write, then read.
                    reader = asyncio.create_task(
                        self._read_packets(connection),
                        name="meshnet-aiomeshtastic-reader",
                    )
                    self._reader_task = reader

                    self._set_state("bluetooth_synchronizing_configuration")
                    await self._wait_for_configuration(reader)
                    if self._stop_event.is_set():
                        raise asyncio.CancelledError

                    was_active = True
                    self._ever_active = True
                    reconnect_delay = self._reconnect_initial_delay
                    self._successful_connections += 1
                    self._last_connected_monotonic = time.monotonic()
                    self._set_state("bluetooth_active")
                    await self._set_connected(True)
                    self._start_result_event.set()
                    heartbeat = asyncio.create_task(
                        self._heartbeat_loop(connection),
                        name="meshnet-aiomeshtastic-heartbeat",
                    )
                    self._heartbeat_task = heartbeat
                    await self._wait_for_active_session(reader, heartbeat)
                except asyncio.CancelledError:
                    raise
                except BaseException as err:
                    self._last_failure_phase = self._state
                    self._remember_error(err)
                    if not self._ever_active:
                        self._initial_error = self._public_error(err)
                        # The outer finally publishes the result only after
                        # session teardown has completed or been reported as
                        # unconfirmed. A caller must never begin a fresh-link
                        # retry while this connection still owns GATT.
                        return
                finally:
                    if was_active or self._connected:
                        await self._set_connected(False)
                    await self._cleanup_session(reader, heartbeat, connection)

                if self._stop_event.is_set():
                    break
                self._reconnect_count += 1
                self._set_state("bluetooth_reconnect_wait")
                try:
                    async with asyncio.timeout(reconnect_delay):
                        await self._stop_event.wait()
                except TimeoutError:
                    pass
                reconnect_delay = min(reconnect_delay * 2, self._reconnect_max_delay)
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            if self._last_failure_phase is None:
                self._last_failure_phase = self._state
            self._remember_error(err)
            if not self._ever_active:
                self._initial_error = self._public_error(err)
        finally:
            await self._set_connected(False)
            self._active_event.clear()
            self._start_result_event.set()
            if not self._stopping and self._state != "bluetooth_stopped":
                self._set_state(
                    "bluetooth_failed" if self._last_error_type else "bluetooth_stopped"
                )

    async def _resolve_device(self) -> Any:
        try:
            async with asyncio.timeout(self._connect_timeout):
                result = self._device_provider()
                if inspect.isawaitable(result):
                    return await result
                return result
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            raise MeshtasticConnectionError(
                f"Meshtastic Bluetooth device resolution failed: {type(err).__name__}"
            ) from err

    async def _read_packets(self, connection: BluetoothConnection) -> None:
        async for payload in connection.packet_stream():
            self._from_radio_count += 1
            try:
                self._handle_from_radio(payload)
            except DecodeError as err:
                self._parse_error_count += 1
                self._last_error_type = type(err).__name__
                self._logger.debug("Discarding malformed Meshtastic FromRadio record")

    async def _wait_for_configuration(self, reader: asyncio.Task[None]) -> None:
        config_wait = asyncio.create_task(
            self._config_complete_event.wait(),
            name="meshnet-aiomeshtastic-wait-config",
        )
        stop_wait = asyncio.create_task(
            self._stop_event.wait(),
            name="meshnet-aiomeshtastic-wait-stop",
        )
        waiters = (config_wait, stop_wait)
        try:
            done, _ = await asyncio.wait(
                {config_wait, stop_wait, reader},
                timeout=self._configuration_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if config_wait in done and self._config_complete_event.is_set():
                return
            if stop_wait in done and self._stop_event.is_set():
                raise asyncio.CancelledError
            if reader in done:
                reader.result()
                raise MeshtasticConnectionError(
                    "Meshtastic Bluetooth reader stopped during configuration"
                )
            raise MeshtasticConfigurationError(
                "Meshtastic radio did not complete configuration in time"
            )
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _heartbeat_loop(self, connection: BluetoothConnection) -> None:
        """Send the same persistent-session heartbeat used by official clients."""
        while not self._stop_event.is_set():
            heartbeat = mesh_pb2.ToRadio()
            heartbeat.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
            await connection.async_send(heartbeat.SerializeToString())
            try:
                async with asyncio.timeout(self._heartbeat_interval):
                    await self._stop_event.wait()
            except TimeoutError:
                continue

    async def _wait_for_active_session(
        self,
        reader: asyncio.Task[None],
        heartbeat: asyncio.Task[None],
    ) -> None:
        """Wait until either persistent session owner stops or fails."""
        done, _ = await asyncio.wait(
            {reader, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
        if not self._stop_event.is_set():
            raise MeshtasticConnectionError(
                "A Meshtastic Bluetooth persistent-session task stopped"
            )

    def _handle_from_radio(self, payload: bytes) -> None:
        from_radio = mesh_pb2.FromRadio()
        from_radio.ParseFromString(payload)

        if from_radio.HasField("my_info"):
            self._my_node_num = int(from_radio.my_info.my_node_num)
        if from_radio.HasField("node_info"):
            node = MessageToDict(from_radio.node_info)
            self._merge_node(int(from_radio.node_info.num), node)
        if from_radio.HasField("packet"):
            packet = self._packet_to_dict(from_radio.packet)
            self._update_node_from_packet(from_radio.packet, packet)
            self._publish_packet(packet)
        if self._config_id is not None and from_radio.config_complete_id == self._config_id:
            self._config_complete_event.set()

    def _packet_to_dict(self, packet: Any) -> dict[str, Any]:
        result = MessageToDict(packet)
        source = int(getattr(packet, "from"))
        destination = int(packet.to)
        hop_start = int(packet.hop_start)
        hop_limit = int(packet.hop_limit)
        result.update(
            {
                "from": source,
                "to": destination,
                "fromId": self._node_id_for_num(source, destination=False),
                "toId": self._node_id_for_num(destination, destination=True),
                "id": int(packet.id),
                "channel": int(packet.channel),
                "rxTime": int(packet.rx_time),
                "rxSnr": float(packet.rx_snr),
                "rxRssi": int(packet.rx_rssi),
                "hopStart": hop_start,
                "hopLimit": hop_limit,
            }
        )
        # hop_start is a non-optional protobuf scalar, so zero cannot be
        # distinguished from an older sender that omitted it. Only a positive
        # start value is sufficient evidence; an equal remaining limit still
        # correctly records a direct (zero-hop) reception.
        if hop_start > 0 and 0 <= hop_limit <= hop_start:
            result["hopsAway"] = hop_start - hop_limit
        if packet.HasField("decoded"):
            decoded = MessageToDict(packet.decoded)
            decoded["payload"] = bytes(packet.decoded.payload)
            try:
                decoded["portnum"] = portnums_pb2.PortNum.Name(packet.decoded.portnum)
            except ValueError:
                # Preserve packets from newer firmware whose port enum is not
                # yet known to the installed Python package.
                decoded["portnum"] = f"UNKNOWN_APP_{int(packet.decoded.portnum)}"
            self._decode_application_payload(packet.decoded, decoded)
            result["decoded"] = decoded
        self._packet_count += 1
        return result

    def _decode_application_payload(self, data: Any, decoded: dict[str, Any]) -> None:
        payload = bytes(data.payload)
        try:
            if data.portnum == portnums_pb2.TEXT_MESSAGE_APP:
                decoded["text"] = payload.decode("utf-8", errors="replace")
            elif data.portnum == portnums_pb2.POSITION_APP:
                position = mesh_pb2.Position()
                position.ParseFromString(payload)
                decoded["position"] = self._position_dict(position)
            elif data.portnum == portnums_pb2.NODEINFO_APP:
                user = mesh_pb2.User()
                user.ParseFromString(payload)
                decoded["user"] = MessageToDict(user)
            elif data.portnum == portnums_pb2.TELEMETRY_APP:
                telemetry = telemetry_pb2.Telemetry()
                telemetry.ParseFromString(payload)
                decoded["telemetry"] = MessageToDict(telemetry)
        except DecodeError:
            self._parse_error_count += 1

    def _update_node_from_packet(self, packet: Any, packet_dict: Mapping[str, Any]) -> None:
        source = int(getattr(packet, "from"))
        if source in (0, _BROADCAST_NUM):
            return
        update: dict[str, Any] = {
            "num": source,
            "snr": float(packet.rx_snr),
            "lastHeard": int(packet.rx_time),
        }
        if "hopsAway" in packet_dict:
            update["hopsAway"] = packet_dict["hopsAway"]
        decoded = packet_dict.get("decoded")
        if isinstance(decoded, Mapping):
            for key in ("position", "user"):
                value = decoded.get(key)
                if isinstance(value, Mapping):
                    update[key] = dict(value)
            telemetry = decoded.get("telemetry")
            if isinstance(telemetry, Mapping):
                for key in (
                    "deviceMetrics",
                    "environmentMetrics",
                    "airQualityMetrics",
                    "powerMetrics",
                    "localStats",
                ):
                    value = telemetry.get(key)
                    if isinstance(value, Mapping):
                        update[key] = dict(value)
        self._merge_node(source, update)

    def _merge_node(self, node_num: int, update: Mapping[str, Any]) -> None:
        if node_num in (0, _BROADCAST_NUM):
            return
        merged = copy.deepcopy(self._nodes.get(node_num, {"num": node_num}))
        self._deep_merge(merged, update)
        position = merged.get("position")
        if isinstance(position, dict):
            self._fix_position(position)
        self._nodes[node_num] = merged
        self._node_update_count += 1

    def _publish_packet(self, packet: dict[str, Any]) -> None:
        for queue in tuple(self._stream_queues):
            item = copy.deepcopy(packet)
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                self._dropped_stream_packet_count += 1
            with suppress(asyncio.QueueFull):
                queue.put_nowait(item)
        for callback in tuple(self._packet_callbacks):
            self._invoke_callback(callback, copy.deepcopy(packet))

    async def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        if connected:
            self._active_event.set()
        else:
            self._active_event.clear()
            self._disconnect_count += 1
        for callback in tuple(self._connection_callbacks):
            self._invoke_callback(callback, connected)

    def _set_state(self, state: str) -> None:
        """Update the privacy-safe lifecycle phase and notify its observer."""
        if self._state == state:
            return
        self._state = state
        if self._state_callback is not None:
            self._invoke_callback(self._state_callback, state)

    def _invoke_callback(self, callback: Callable[[Any], Any], value: Any) -> None:
        try:
            result = callback(value)
        except Exception:
            self._callback_error_count += 1
            self._logger.exception("Meshtastic callback failed")
            return
        if not inspect.isawaitable(result):
            return
        task = asyncio.ensure_future(result)
        if isinstance(task, asyncio.Task):
            task.set_name("meshnet-aiomeshtastic-callback")
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_done)

    def _callback_done(self, task: asyncio.Future[Any]) -> None:
        self._callback_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            self._callback_error_count += 1
            self._logger.exception("Meshtastic async callback failed")

    async def _cancel_callback_tasks(self, *, timeout: float) -> bool:
        tasks = tuple(self._callback_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self._callback_tasks = {task for task in self._callback_tasks if not task.done()}
        return bool(self._callback_tasks)

    async def _cleanup_session(
        self,
        reader: asyncio.Task[None] | None,
        heartbeat: asyncio.Task[None] | None,
        connection: BluetoothConnection | None,
    ) -> None:
        if connection is not None:
            self._last_connection_snapshot = self._connection_diagnostics(
                connection
            )
            self._last_connection_cleanup_outcome = "pending"
        session_tasks = tuple(
            task for task in (reader, heartbeat) if task is not None
        )
        for task in session_tasks:
            if not task.done():
                task.cancel()
        pending: set[asyncio.Task[None]] = set()
        if session_tasks:
            _, pending = await asyncio.wait(
                session_tasks,
                timeout=min(self._stop_timeout / 2, self._disconnect_timeout),
            )
        if self._reader_task is reader and reader not in pending:
            if reader is not None:
                self._consume_task_result(reader)
            self._reader_task = None
        if self._heartbeat_task is heartbeat and heartbeat not in pending:
            if heartbeat is not None:
                self._consume_task_result(heartbeat)
            self._heartbeat_task = None
        if pending:
            # A task that is still inside read_gatt_char/write_gatt_char remains
            # a live GATT owner.  Do not race that operation with either the
            # protocol disconnect write or physical notify/link teardown.  Keep
            # every reference reachable so a later stop can retry after the
            # cancellation-resistant platform call finally returns.
            self._last_connection_cleanup_outcome = "session_tasks_pending"
            raise MeshtasticCleanupError(
                "Meshtastic session tasks did not stop within their cleanup bound"
            )
        if connection is not None:
            # Match the official client lifecycle: ask firmware to close the
            # protocol session before releasing GATT.  This is best-effort and
            # never prevents the mandatory physical teardown attempt.
            if connection.is_connected:
                disconnect = mesh_pb2.ToRadio()
                disconnect.disconnect = True
                try:
                    async with asyncio.timeout(min(self._io_timeout, 2.0)):
                        await connection.async_send(disconnect.SerializeToString())
                except (Exception, TimeoutError):
                    pass
            try:
                await connection.async_disconnect()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._remember_error(err)
                if self._connection_owns_endpoint(connection):
                    # Propagate so the supervisor terminates instead of opening
                    # another link while this GATT owner remains live.
                    self._last_connection_cleanup_outcome = "unconfirmed"
                    raise MeshtasticCleanupError(
                        "Meshtastic GATT teardown was not confirmed"
                    ) from err
                self._last_connection_cleanup_outcome = "error_without_owner"
            else:
                self._last_connection_cleanup_outcome = "confirmed"
        if self._connection is connection and not self._connection_owns_endpoint(connection):
            self._connection = None
        self._config_complete_event.clear()
        self._config_id = None

    @staticmethod
    def _connection_diagnostics(
        connection: BluetoothConnection,
    ) -> dict[str, Any] | None:
        """Capture identity-free connection state without risking cleanup."""
        snapshot = getattr(connection, "diagnostic_snapshot", None)
        if not callable(snapshot):
            return None
        try:
            value = snapshot()
        except Exception as err:
            return {
                "state": "snapshot_failed",
                "snapshot_error_type": type(err).__name__,
            }
        if not isinstance(value, dict):
            return None
        projected: dict[str, Any] = {}
        for key in _CONNECTION_DIAGNOSTIC_FIELDS:
            if key not in value:
                continue
            scalar = _diagnostic_scalar(value[key])
            if scalar is not None or value[key] is None:
                projected[key] = scalar
        raw_timeouts = value.get("timeouts")
        if isinstance(raw_timeouts, dict):
            timeouts: dict[str, int | float] = {}
            for key in _CONNECTION_TIMEOUT_FIELDS:
                raw_value = raw_timeouts.get(key)
                scalar = _diagnostic_scalar(raw_value)
                if isinstance(scalar, (int, float)) and not isinstance(
                    scalar, bool
                ):
                    timeouts[key] = scalar
            if timeouts:
                projected["timeouts"] = timeouts
        return projected

    def _resolve_destination(self, value: int | str | None) -> int:
        if value is None:
            return _BROADCAST_NUM
        if isinstance(value, bool):
            raise ValueError("destination_id must be a node number or node ID")
        if isinstance(value, int):
            destination = value
        elif isinstance(value, str):
            text = value.strip()
            if text.casefold().startswith("meshtastic:"):
                text = text.split(":", 1)[1].strip()
                if not text:
                    raise ValueError("destination_id has an empty Meshtastic node key")
            if text.casefold().startswith("mac:"):
                aliases = self._mac_aliases(text.split(":", 1)[1])
                matches = [
                    node_num
                    for node_num, node in self._nodes.items()
                    if isinstance(node.get("user"), Mapping)
                    and any(
                        aliases & self._mac_aliases(node["user"].get(key))
                        for key in ("macaddr", "mac")
                    )
                ]
                if len(matches) != 1:
                    raise ValueError(
                        "destination_id MAC key is unknown or ambiguous"
                    )
                return matches[0]
            if text.casefold() in {"", _BROADCAST_ID, "all", "broadcast"}:
                return _BROADCAST_NUM
            if text.startswith("!"):
                try:
                    destination = int(text[1:], 16)
                except ValueError as err:
                    raise ValueError("destination_id has an invalid Meshtastic node ID") from err
            elif text.casefold().startswith("0x"):
                try:
                    destination = int(text, 16)
                except ValueError as err:
                    raise ValueError("destination_id has an invalid hexadecimal node ID") from err
            else:
                match = next(
                    (
                        num
                        for num, node in self._nodes.items()
                        if isinstance(node.get("user"), Mapping)
                        and node["user"].get("id") == text
                    ),
                    None,
                )
                if match is not None:
                    return match
                normalized_name = _normalized_node_name(text)
                name_matches = {
                    num
                    for num, node in self._nodes.items()
                    if isinstance(node.get("user"), Mapping)
                    and any(
                        _normalized_node_name(node["user"].get(field))
                        == normalized_name
                        for field in _NODE_NAME_FIELDS
                    )
                }
                if len(name_matches) > 1:
                    raise ValueError(
                        "destination_id Meshtastic node name is ambiguous"
                    )
                if name_matches:
                    return next(iter(name_matches))
                try:
                    destination = int(text, 10)
                except ValueError as err:
                    raise ValueError("destination_id is not a known Meshtastic node") from err
        else:
            raise TypeError("destination_id must be an integer, string, or None")
        if not 0 <= destination <= _BROADCAST_NUM:
            raise ValueError("destination_id must fit in an unsigned 32-bit node number")
        return destination

    def _node_id_for_num(self, node_num: int, *, destination: bool) -> str:
        if node_num == _BROADCAST_NUM and destination:
            return _BROADCAST_ID
        node = self._nodes.get(node_num)
        user = node.get("user") if isinstance(node, Mapping) else None
        if isinstance(user, Mapping) and isinstance(user.get("id"), str):
            return user["id"]
        return f"!{node_num:08x}"

    def _next_packet_id(self) -> int:
        self._packet_sequence = (self._packet_sequence + 1) & 0x3FF
        packet_id = (secrets.randbits(22) << 10) | self._packet_sequence
        return packet_id or 1

    @staticmethod
    def _mac_aliases(value: Any) -> set[str]:
        if isinstance(value, bytes):
            return {value.hex()}
        if not isinstance(value, str):
            return set()
        raw = value.strip()
        if not raw:
            return set()
        text = raw.casefold()
        compact = text.replace(":", "").replace("-", "")
        aliases = {compact}
        try:
            aliases.add(base64.b64decode(raw, validate=True).hex())
        except (binascii.Error, ValueError):
            pass
        return aliases

    @staticmethod
    def _new_config_id() -> int:
        config_id = secrets.randbits(32)
        while config_id in (0, _NODELESS_WANT_CONFIG_ID):
            config_id = secrets.randbits(32)
        return config_id

    @staticmethod
    def _deep_merge(target: dict[str, Any], update: Mapping[str, Any]) -> None:
        for key, value in update.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                MeshtasticBluetoothClient._deep_merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    @staticmethod
    def _position_dict(position: Any) -> dict[str, Any]:
        result = MessageToDict(position)
        MeshtasticBluetoothClient._fix_position(result)
        return result

    @staticmethod
    def _fix_position(position: dict[str, Any]) -> None:
        latitude = position.get("latitudeI")
        longitude = position.get("longitudeI")
        if isinstance(latitude, (int, float)):
            position["latitude"] = float(latitude) * 1e-7
        if isinstance(longitude, (int, float)):
            position["longitude"] = float(longitude) * 1e-7

    @staticmethod
    def _public_error(error: BaseException) -> BaseException:
        if isinstance(error, MeshtasticAsyncError):
            return error
        return MeshtasticConnectionError(
            f"Meshtastic Bluetooth session failed: {type(error).__name__}"
        )

    def _remember_error(self, error: BaseException) -> None:
        if not isinstance(error, asyncio.CancelledError):
            self._last_error_type = type(error).__name__

    @staticmethod
    def _connection_owns_endpoint(connection: BluetoothConnection | None) -> bool:
        if connection is None:
            return False
        return bool(getattr(connection, "owns_endpoint", connection.is_connected))

    def _consume_completed_cleanup_owners(self) -> None:
        """Clear finished owner references before considering a fresh session."""
        for attribute in (
            "_runner_task",
            "_reader_task",
            "_heartbeat_task",
            "_stop_cleanup_task",
        ):
            task = getattr(self, attribute)
            if task is None or not task.done():
                continue
            self._consume_task_result(task)
            setattr(self, attribute, None)

    def _cleanup_owners_pending(self) -> bool:
        """Return whether an older lifecycle can still touch shared state/GATT."""
        runner = self._runner_task
        runner_live = runner is not None and not runner.done()
        stale_lifecycle = self._stop_event.is_set() or (
            self._state == "bluetooth_cleanup_incomplete"
        )
        if self._stop_cleanup_task is not None and not self._stop_cleanup_task.done():
            return True
        session_owner_pending = any(
            task is not None and not task.done()
            for task in (self._reader_task, self._heartbeat_task)
        )
        if session_owner_pending and (stale_lifecycle or not runner_live):
            return True
        if runner_live and stale_lifecycle:
            return True
        callback_pending = any(not task.done() for task in self._callback_tasks)
        return callback_pending and (stale_lifecycle or not runner_live)

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    def _end_packet_streams(self) -> None:
        for queue in tuple(self._stream_queues):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(_STREAM_END)
        self._stream_queues.clear()

    def _consume_task_result(self, task: asyncio.Future[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except BaseException as err:
            self._remember_error(err)

    @staticmethod
    def _task_state(task: asyncio.Future[Any] | None) -> str:
        if task is None:
            return "not_created"
        if task.cancelled():
            return "cancelled"
        if not task.done():
            return "pending"
        return "failed" if task.exception() is not None else "finished"
