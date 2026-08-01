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
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

from ..const import REMOTE_ADMIN_WRITABLE_PATHS
from ..meshtastic_settings import (
    MeshtasticSettingsState,
    MeshtasticSettingsValidationError,
)
from ..node_identity import canonical_meshtastic_node_id
from ..sensitive_logging import suppress_sensitive_library_logs
from .bluetooth import BluetoothConnection
from .errors import (
    MeshtasticAsyncError,
    MeshtasticCleanupError,
    MeshtasticConfigurationError,
    MeshtasticConnectionError,
    MeshtasticNeighborInfoError,
    MeshtasticNotConnectedError,
    MeshtasticRemoteAdminError,
)

_BROADCAST_NUM = 0xFFFFFFFF
_BROADCAST_ID = "^all"
_NODELESS_WANT_CONFIG_ID = 69420
_STREAM_END = object()
_SETTINGS_APPLY_TIMEOUT = 105.0
_SETTINGS_READBACK_TIMEOUT = 70.0
# Firmware sessions live for 300 seconds. Refresh at half-life so transport
# latency can never make MeshNet use a passkey beyond the firmware lifetime.
_REMOTE_ADMIN_SESSION_SECONDS = 150.0
_REMOTE_ADMIN_TIMEOUT_SECONDS = 105.0
_REMOTE_CONFIG_TYPES = (
    "SESSIONKEY_CONFIG",
    "DISPLAY_CONFIG",
)
_REMOTE_ROUTING_ERRORS = {
    "PKI_SEND_FAIL_PUBLIC_KEY": (
        "remote_admin_target_public_key_unavailable",
        "The target public key is unavailable on the controller radio",
    ),
    "PKI_UNKNOWN_PUBKEY": (
        "remote_admin_target_public_key_unavailable",
        "The target public key is unavailable on the controller radio",
    ),
    "ADMIN_PUBLIC_KEY_UNAUTHORIZED": (
        "remote_admin_controller_unauthorized",
        "The target does not authorize this controller radio",
    ),
    "NOT_AUTHORIZED": (
        "remote_admin_controller_unauthorized",
        "The target does not authorize this controller radio",
    ),
    "ADMIN_BAD_SESSION_KEY": (
        "remote_admin_session_rejected",
        "The remote-admin session was rejected; load settings again",
    ),
    "NO_ROUTE": (
        "remote_admin_no_route",
        "No mesh route to the selected target is available",
    ),
    "NO_RESPONSE": (
        "remote_admin_no_response",
        "The selected target did not respond",
    ),
    "TIMEOUT": (
        "remote_admin_no_response",
        "The selected target did not respond",
    ),
    "DUTY_CYCLE_LIMIT": (
        "remote_admin_duty_cycle_limited",
        "The radio refused the request because of its duty-cycle limit",
    ),
    "RATE_LIMIT_EXCEEDED": (
        "remote_admin_rate_limited",
        "The radio refused the request because of its rate limit",
    ),
}
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
_CONNECTION_TIMEOUT_FIELDS = frozenset({"connect", "notify", "io", "read", "disconnect", "idle_read"})


def _diagnostic_scalar(value: Any) -> str | bool | int | float | None:
    """Return one bounded identity-free primitive or ``None`` when unsafe."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and all(character.isalnum() or character in "_.-" for character in value)
    ):
        return value
    return None


def _normalized_node_name(value: Any) -> str | None:
    """Return one conservative key for exact case-insensitive name matching."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return normalized or None


def _selected_admin_field(message: Any) -> str | None:
    """Return a protobuf oneof selection without assuming its group name."""
    descriptor = getattr(message, "DESCRIPTOR", None)
    for oneof in getattr(descriptor, "oneofs", ()):
        selected = message.WhichOneof(oneof.name)
        if selected is not None:
            return selected
    return None


type DeviceProvider = Callable[[], Any | Awaitable[Any]]
type PacketCallback = Callable[[dict[str, Any]], Any | Awaitable[Any]]
type ConnectionCallback = Callable[[bool], Any | Awaitable[Any]]
type StateCallback = Callable[[str], Any | Awaitable[Any]]
type ConnectionFactory = Callable[..., BluetoothConnection]


@dataclass(slots=True)
class _AdminResponse:
    """Sanitized metadata for one exactly correlated routing response."""

    kind: str
    error_reason: str | None = None
    admin_message: Any | None = field(default=None, repr=False)


@dataclass(slots=True)
class _PendingAdminResponse:
    """A single registered ADMIN_APP request."""

    future: asyncio.Future[_AdminResponse] = field(repr=False)
    source: int = 0
    channel: int = 0
    expected_admin_response: str | None = None


@dataclass(slots=True)
class _PendingTracerouteResponse:
    """One exact RouteDiscovery request awaiting a correlated response."""

    future: asyncio.Future[Any] = field(repr=False)
    source: int = 0
    channel: int = 0


@dataclass(slots=True)
class _RemoteAdminSession:
    """One target's short-lived passkey, retained in process memory only."""

    passkey: bytes = field(repr=False)
    expires_monotonic: float = field(repr=False)


class _AdminResponseTimeout(MeshtasticConfigurationError):
    """An ADMIN_APP packet was sent once but its final state is unknown."""


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
        admin_response_timeout: float = 15.0,
        stream_queue_size: int = 128,
        state_callback: StateCallback | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not address.strip():
            raise ValueError("address cannot be empty")
        resolved_read_timeout = (
            read_timeout if read_timeout is not None else max(io_timeout, configuration_timeout + 5.0)
        )
        if (
            min(
                connect_timeout,
                configuration_timeout,
                io_timeout,
                resolved_read_timeout,
                disconnect_timeout,
                stop_timeout,
                reconnect_initial_delay,
                reconnect_max_delay,
                heartbeat_interval,
                admin_response_timeout,
            )
            <= 0
        ):
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
        self._start_timeout = start_timeout or (connect_timeout + configuration_timeout + disconnect_timeout + 5.0)
        self._stop_timeout = stop_timeout
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._heartbeat_interval = heartbeat_interval
        self._admin_response_timeout = admin_response_timeout
        self._stream_queue_size = stream_queue_size
        self._state_callback = state_callback
        self._logger = logger or logging.getLogger(__name__)

        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
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
        self._settings = MeshtasticSettingsState()
        self._pending_admin_responses: dict[int, _PendingAdminResponse] = {}
        self._pending_traceroute_responses: dict[int, _PendingTracerouteResponse] = {}
        self._pending_neighbor_info_responses: dict[
            int, _PendingTracerouteResponse
        ] = {}
        self._manual_neighbor_info_response_packets: set[tuple[int, int]] = set()
        self._internal_admin_request_ids: deque[int] = deque(maxlen=128)
        self._remote_admin_sessions: dict[int, _RemoteAdminSession] = {}
        self._remote_settings: dict[int, MeshtasticSettingsState] = {}
        self._remote_admin_locks: dict[int, asyncio.Lock] = {}
        self._connection_generation = 0
        self._settings_complete_sequence = 0
        self._settings_complete_generation = 0
        self._settings_complete_signal = asyncio.Event()

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
        self._admin_request_count = 0
        self._admin_response_count = 0
        self._admin_timeout_count = 0
        self._admin_nak_count = 0
        self._traceroute_request_count = 0
        self._traceroute_response_count = 0
        self._traceroute_timeout_count = 0
        self._neighbor_info_request_count = 0
        self._neighbor_info_response_count = 0
        self._neighbor_info_timeout_count = 0
        self._neighbor_info_rejection_count = 0
        self._neighbor_info_cancellation_count = 0
        self._neighbor_info_send_failure_count = 0
        self._neighbor_info_disconnect_count = 0
        self._neighbor_info_routing_error_counts: dict[str, int] = {}
        self._last_neighbor_info_outcome = "not_requested"
        self._last_neighbor_info_routing_error: str | None = None
        self._last_connected_monotonic: float | None = None

    @property
    def connected(self) -> bool:
        """Return whether both GATT and the Meshtastic handshake are active."""
        connection = self._connection
        return bool(self._connected and connection is not None and connection.is_connected and not self._stopping)

    @property
    def local_node_id(self) -> str | None:
        """Return the controller radio's canonical node ID, when known."""
        return f"!{self._my_node_num:08x}" if self._my_node_num is not None else None

    async def async_start(self) -> None:
        """Start one session and wait for the initial node/config download."""
        async with self._lifecycle_lock:
            if self.connected:
                return
            self._consume_completed_cleanup_owners()
            if self._cleanup_owners_pending():
                raise MeshtasticCleanupError(
                    "Previous Meshtastic Bluetooth owner tasks are still stopping; cleanup must finish before restart"
                )
            if self._connection_owns_endpoint(self._connection):
                raise MeshtasticCleanupError("A previous Bluetooth client is still connected; cleanup must be retried")
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
            raise MeshtasticConfigurationError("Meshtastic Bluetooth startup exceeded its safety timeout") from err
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
            self._fail_pending_admin_responses()
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
            task for task in (self._reader_task, self._heartbeat_task) if task is not None and not task.done()
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
            task is not None and not task.done() for task in (self._reader_task, self._heartbeat_task)
        )
        if not runner_pending and not session_owners_pending and self._connection_owns_endpoint(connection):
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
                task for task in (self._reader_task, self._heartbeat_task) if task is not None and not task.done()
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
        self._set_state("bluetooth_stopped" if cleanup_confirmed else "bluetooth_cleanup_incomplete")
        callbacks_pending = await self._cancel_callback_tasks(timeout=self._remaining(deadline))
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
                mesh_pb2.MeshPacket.Priority.RELIABLE if want_ack else mesh_pb2.MeshPacket.Priority.DEFAULT
            )
            to_radio = mesh_pb2.ToRadio()
            to_radio.packet.CopyFrom(packet)
            await connection.async_send(to_radio.SerializeToString())
            self._sent_text_count += 1
            return int(packet.id)

    async def async_manual_traceroute(self, target_node: str) -> dict[str, Any]:
        """Send one exact RouteDiscovery request and never retry it."""
        if not isinstance(target_node, str):
            raise MeshtasticConfigurationError("Traceroute requires one exact Meshtastic node ID")
        canonical = canonical_meshtastic_node_id(target_node)
        if canonical is None or target_node != canonical or canonical in {"!00000000", "!ffffffff"}:
            raise MeshtasticConfigurationError("Traceroute requires one exact Meshtastic node ID")
        destination = int(canonical[1:], 16)
        if destination == self._my_node_num or destination not in self._nodes:
            raise MeshtasticConfigurationError("Traceroute requires one known remote Meshtastic node")
        target = self._nodes[destination]
        user = target.get("user") if isinstance(target, Mapping) else None
        claimed_id = user.get("id") if isinstance(user, Mapping) else None
        if claimed_id not in (None, "") and (canonical_meshtastic_node_id(claimed_id) != canonical):
            raise MeshtasticConfigurationError("Traceroute target identity is inconsistent")
        if self._my_node_num is None:
            raise MeshtasticNotConnectedError("Meshtastic Bluetooth local node identity is unavailable")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        request_id: int | None = None
        pending: _PendingTracerouteResponse | None = None
        async with self._send_lock:
            connection = self._connection
            if not self.connected or connection is None:
                raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not active")
            packet = mesh_pb2.MeshPacket()
            packet.to = destination
            setattr(packet, "from", self._my_node_num)
            packet.channel = 0
            packet.id = self._next_packet_id()
            packet.want_ack = True
            packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
            # Match the official client's _sendPacket behavior. Leaving this
            # at protobuf's zero default makes the request direct-only even
            # when the radio is configured for multi-hop routing.
            packet.hop_limit = self._settings.hop_limit()
            packet.decoded.portnum = portnums_pb2.TRACEROUTE_APP
            packet.decoded.want_response = True
            packet.decoded.payload = mesh_pb2.RouteDiscovery().SerializeToString()
            request_id = int(packet.id)
            pending = _PendingTracerouteResponse(
                future=future,
                source=destination,
                channel=0,
            )
            self._pending_traceroute_responses[request_id] = pending
            to_radio = mesh_pb2.ToRadio()
            to_radio.packet.CopyFrom(packet)
            try:
                await connection.async_send(to_radio.SerializeToString())
            except BaseException:
                if self._pending_traceroute_responses.get(request_id) is pending:
                    self._pending_traceroute_responses.pop(request_id, None)
                raise
            self._traceroute_request_count += 1

        try:
            async with asyncio.timeout(self._admin_response_timeout):
                route = await future
        except TimeoutError as err:
            self._traceroute_timeout_count += 1
            raise MeshtasticConfigurationError(
                "Meshtastic traceroute response timed out; the request was not retried"
            ) from err
        finally:
            if (
                request_id is not None
                and pending is not None
                and self._pending_traceroute_responses.get(request_id) is pending
            ):
                self._pending_traceroute_responses.pop(request_id, None)

        self._traceroute_response_count += 1
        local_id = f"!{self._my_node_num:08x}"
        route_nodes = self._normalized_route_nodes(
            route.route,
            excluded={self._my_node_num, destination},
        )
        reverse_nodes = self._normalized_route_nodes(
            route.route_back,
            excluded={self._my_node_num, destination},
        )
        return {
            "correlation_id": str(request_id),
            "source": local_id,
            "destination": canonical,
            "channel": 0,
            "forward_route": [local_id, *route_nodes, canonical],
            "reverse_route": [canonical, *reverse_nodes, local_id],
            "snr_towards": [float(value) / 4.0 for value in list(route.snr_towards)[:64]],
            "snr_back": [float(value) / 4.0 for value in list(route.snr_back)[:64]],
        }

    async def async_manual_neighbor_info(self, target_node: str) -> dict[str, Any]:
        """Request one exact node's NeighborInfo once, without retrying."""
        if not isinstance(target_node, str):
            raise MeshtasticConfigurationError(
                "NeighborInfo requires one exact Meshtastic node ID"
            )
        canonical = canonical_meshtastic_node_id(target_node)
        if (
            canonical is None
            or target_node != canonical
            or canonical in {"!00000000", "!ffffffff"}
        ):
            raise MeshtasticConfigurationError(
                "NeighborInfo requires one exact Meshtastic node ID"
            )
        destination = int(canonical[1:], 16)
        if destination == self._my_node_num or destination not in self._nodes:
            raise MeshtasticConfigurationError(
                "NeighborInfo requires one known remote Meshtastic node"
            )
        target = self._nodes[destination]
        user = target.get("user") if isinstance(target, Mapping) else None
        claimed_id = user.get("id") if isinstance(user, Mapping) else None
        if (
            not isinstance(claimed_id, str)
            or claimed_id != canonical
            or canonical_meshtastic_node_id(claimed_id) != canonical
        ):
            raise MeshtasticConfigurationError(
                "NeighborInfo target identity is inconsistent"
            )
        if self._my_node_num is None:
            raise MeshtasticNotConnectedError(
                "Meshtastic Bluetooth local node identity is unavailable"
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        request_id: int | None = None
        pending: _PendingTracerouteResponse | None = None
        async with self._send_lock:
            connection = self._connection
            if not self.connected or connection is None:
                raise MeshtasticNotConnectedError(
                    "Meshtastic Bluetooth is not active"
                )
            request = mesh_pb2.NeighborInfo()
            # Current firmware recognizes this exact dummy as a request and
            # deliberately avoids ingesting it into the neighbor database.
            request.neighbors.add(node_id=0, snr=0.0)
            packet = mesh_pb2.MeshPacket()
            packet.to = destination
            setattr(packet, "from", self._my_node_num)
            packet.channel = 0
            packet.id = self._next_packet_id()
            packet.want_ack = True
            packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
            # Match the official client's _sendPacket behavior. A zero value
            # makes this a direct-only RF request and can make a valid
            # multi-hop target return a routing NAK.
            packet.hop_limit = self._settings.hop_limit()
            packet.decoded.portnum = portnums_pb2.NEIGHBORINFO_APP
            packet.decoded.want_response = True
            packet.decoded.payload = request.SerializeToString()
            request_id = int(packet.id)
            pending = _PendingTracerouteResponse(
                future=future,
                source=destination,
                channel=0,
            )
            self._pending_neighbor_info_responses[request_id] = pending
            to_radio = mesh_pb2.ToRadio()
            to_radio.packet.CopyFrom(packet)
            self._last_neighbor_info_outcome = "sending"
            self._last_neighbor_info_routing_error = None
            try:
                await connection.async_send(to_radio.SerializeToString())
            except asyncio.CancelledError:
                self._neighbor_info_cancellation_count += 1
                self._last_neighbor_info_outcome = "cancelled"
                if self._pending_neighbor_info_responses.get(request_id) is pending:
                    self._pending_neighbor_info_responses.pop(request_id, None)
                raise
            except Exception as err:
                self._neighbor_info_send_failure_count += 1
                self._last_neighbor_info_outcome = "send_failed"
                if self._pending_neighbor_info_responses.get(request_id) is pending:
                    self._pending_neighbor_info_responses.pop(request_id, None)
                raise MeshtasticNeighborInfoError(
                    "neighbor_info_send_failed"
                ) from err
            self._neighbor_info_request_count += 1
            if not future.done():
                self._last_neighbor_info_outcome = "awaiting_response"

        try:
            async with asyncio.timeout(self._admin_response_timeout):
                report = await future
        except asyncio.CancelledError:
            self._neighbor_info_cancellation_count += 1
            self._last_neighbor_info_outcome = "cancelled"
            raise
        except TimeoutError as err:
            self._neighbor_info_timeout_count += 1
            self._last_neighbor_info_outcome = "timed_out"
            raise MeshtasticNeighborInfoError("neighbor_info_timeout") from err
        finally:
            if (
                request_id is not None
                and pending is not None
                and self._pending_neighbor_info_responses.get(request_id) is pending
            ):
                self._pending_neighbor_info_responses.pop(request_id, None)

        self._neighbor_info_response_count += 1
        self._last_neighbor_info_outcome = "responded"
        neighbors: list[dict[str, Any]] = []
        seen: set[int] = set()
        for neighbor in list(report.neighbors)[:10]:
            node_id = int(neighbor.node_id)
            snr = float(neighbor.snr)
            if (
                node_id in {0, _BROADCAST_NUM, destination, self._my_node_num}
                or node_id in seen
                or not math.isfinite(snr)
                or not -128 <= snr <= 128
            ):
                continue
            seen.add(node_id)
            neighbors.append({"node_id": f"!{node_id:08x}", "snr": snr})
        interval = int(report.node_broadcast_interval_secs)
        if not 0 <= interval <= 31_536_000:
            interval = 0
        return {
            "correlation_id": str(request_id),
            "source": canonical,
            "destination": f"!{self._my_node_num:08x}",
            "channel": 0,
            "node_broadcast_interval_secs": interval,
            "neighbors": neighbors,
        }

    def node_snapshot(self) -> dict[int, dict[str, Any]]:
        """Return a detached, plain-dictionary copy of the known node database."""
        return copy.deepcopy(self._nodes)

    async def async_node_snapshot(self) -> dict[int, dict[str, Any]]:
        """Async compatibility wrapper for callers with an async gateway API."""
        return self.node_snapshot()

    async def async_get_settings_snapshot(self) -> dict[str, Any]:
        """Return the captured local-radio settings without credential values."""
        writable = bool(self.connected and self._settings.complete and not self._settings.managed)
        if self._settings.managed:
            reason = "managed_mode_rejects_local_admin_changes"
        elif not self.connected:
            reason = "local_radio_is_not_connected"
        elif not self._settings.complete:
            reason = "local_radio_settings_download_is_incomplete"
        else:
            reason = "confirmed_admin_write_and_verification_not_available"
        return self._settings.public_snapshot(
            transport="bluetooth",
            write_supported=writable,
            apply_reason=reason,
        )

    async def async_get_remote_settings_snapshot(self, target_node: str) -> dict[str, Any]:
        """Explicitly load one exact node's reviewed settings over the mesh."""
        target_num, target, target_key = self._remote_admin_target(target_node)
        lock = self._remote_admin_locks.setdefault(target_num, asyncio.Lock())
        try:
            async with lock:
                async with asyncio.timeout(_REMOTE_ADMIN_TIMEOUT_SECONDS):
                    state = await self._async_load_remote_settings_locked(target_num, target_key)
        except TimeoutError:
            raise self._remote_error("remote_admin_no_response") from None
        return self._remote_public_snapshot(target_num, target, state)

    async def async_apply_remote_settings_plan(
        self,
        target_node: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Send one reviewed remote settings transaction and read it back."""
        self._validate_remote_changes(changes)
        target_num, target, target_key = self._remote_admin_target(target_node)
        lock = self._remote_admin_locks.setdefault(target_num, asyncio.Lock())
        async with lock:
            try:
                async with asyncio.timeout(_REMOTE_ADMIN_TIMEOUT_SECONDS):
                    state = self._remote_settings.get(target_num)
                    if state is None or not state.complete:
                        state = await self._async_load_remote_settings_locked(target_num, target_key)
                    plan = state.build_plan(
                        changes,
                        transport="bluetooth",
                        admin_message_factory=admin_pb2.AdminMessage,
                    )
                    if plan.blocked_paths or not plan.operations:
                        raise self._remote_error("remote_admin_command_forbidden")
                    session = self._remote_admin_sessions.get(target_num)
                    if session is None or session.expires_monotonic <= time.monotonic():
                        await self._async_request_remote_session_locked(target_num, target_key)
                        session = self._remote_admin_sessions.get(target_num)
                    if session is None:
                        raise self._remote_error("remote_admin_session_rejected")

                    connection = self._connection
                    if connection is None:
                        raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not active")
                    async with suppress_sensitive_library_logs("meshtastic"):
                        async with self._send_lock:
                            self._require_transaction_connection(connection)
                            for operation in plan.operations:
                                await self._async_send_admin_locked(
                                    operation.message,
                                    connection=connection,
                                    destination=target_num,
                                    public_key=target_key,
                                    session_passkey=session.passkey,
                                    remote_operation="write",
                                )

                    try:
                        refreshed = await self._async_load_remote_settings_locked(target_num, target_key)
                    except (
                        MeshtasticRemoteAdminError,
                        MeshtasticConnectionError,
                        MeshtasticCleanupError,
                    ):
                        raise self._remote_error("remote_admin_unknown_outcome") from None
                    verified, unverified = refreshed.verify_plan(plan)
            except MeshtasticSettingsValidationError:
                raise self._remote_error("remote_admin_command_forbidden") from None
            except TimeoutError:
                raise self._remote_error("remote_admin_unknown_outcome") from None

        return {
            "status": "verified" if not unverified else "readback_mismatch",
            "verified": verified,
            "unverified": unverified,
            "target": {
                "node_id": f"!{target_num:08x}",
                "short_name": self._safe_node_label(target, short=True),
            },
        }

    async def _async_load_remote_settings_locked(self, target_num: int, target_key: bytes) -> MeshtasticSettingsState:
        """Fetch only sections whose contents have a reviewed projection."""
        await self._async_request_remote_session_locked(target_num, target_key)
        state = MeshtasticSettingsState()
        state.begin_refresh()

        owner_request = admin_pb2.AdminMessage()
        owner_request.get_owner_request = True
        owner_response = await self._async_remote_admin_request_locked(
            target_num,
            owner_request,
            target_key=target_key,
            expected_response="get_owner_response",
        )
        self._capture_remote_response(state, target_num, owner_response)

        for config_name in _REMOTE_CONFIG_TYPES[1:]:
            request = admin_pb2.AdminMessage()
            request.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(config_name)
            response = await self._async_remote_admin_request_locked(
                target_num,
                request,
                target_key=target_key,
                expected_response="get_config_response",
            )
            self._capture_remote_response(state, target_num, response)
        state.mark_complete()
        self._remote_settings[target_num] = state
        return state

    async def _async_request_remote_session_locked(self, target_num: int, target_key: bytes) -> None:
        request = admin_pb2.AdminMessage()
        request.get_config_request = admin_pb2.AdminMessage.ConfigType.Value("SESSIONKEY_CONFIG")
        await self._async_remote_admin_request_locked(
            target_num,
            request,
            target_key=target_key,
            expected_response="get_config_response",
        )

    async def _async_remote_admin_request_locked(
        self,
        target_num: int,
        request: Any,
        *,
        target_key: bytes,
        expected_response: str,
    ) -> Any:
        connection = self._connection
        if connection is None:
            raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not active")
        async with suppress_sensitive_library_logs("meshtastic"):
            async with self._send_lock:
                self._require_transaction_connection(connection)
                response = await self._async_send_admin_locked(
                    request,
                    connection=connection,
                    destination=target_num,
                    public_key=target_key,
                    expected_admin_response=expected_response,
                    remote_operation="read",
                )
        message = response.admin_message
        if message is None:
            raise self._remote_error("remote_admin_no_response")
        passkey = bytes(message.session_passkey)
        if len(passkey) != 8:
            raise self._remote_error("remote_admin_session_rejected")
        self._remote_admin_sessions[target_num] = _RemoteAdminSession(
            passkey=passkey,
            expires_monotonic=time.monotonic() + _REMOTE_ADMIN_SESSION_SECONDS,
        )
        return message

    @staticmethod
    def _capture_remote_response(
        state: MeshtasticSettingsState,
        target_num: int,
        response: Any,
    ) -> None:
        """Copy a safe Admin response into an isolated settings state."""
        selected = _selected_admin_field(response)
        record = mesh_pb2.FromRadio()
        if selected == "get_owner_response":
            record.node_info.num = target_num
            record.node_info.user.CopyFrom(response.get_owner_response)
        elif selected == "get_config_response":
            section = _selected_admin_field(response.get_config_response)
            if section in {"security", "sessionkey"}:
                return
            record.config.CopyFrom(response.get_config_response)
        else:
            return
        state.capture_from_radio(record, my_node_num=target_num)

    def _remote_public_snapshot(
        self,
        target_num: int,
        target: Mapping[str, Any],
        state: MeshtasticSettingsState,
    ) -> dict[str, Any]:
        controller = self._controller_identity()
        snapshot = state.public_snapshot(
            transport="bluetooth",
            write_supported=self.connected,
            apply_reason="remote_admin_is_not_available",
        )
        # Unlike the local settings manager, this result crosses the adapter
        # boundary directly. Never return even the repr-redacted internal
        # credential revision material to a future coordinator or WebSocket.
        snapshot.pop("_secret_revision_material", None)
        snapshot["source"] = "remote_radio"
        snapshot["controller"] = controller
        snapshot["target"] = {
            "node_id": f"!{target_num:08x}",
            "long_name": self._safe_node_label(target, short=False),
            "short_name": self._safe_node_label(target, short=True),
            "public_key_available": True,
            "remote_admin_eligible": True,
        }
        return snapshot

    def _remote_admin_target(self, target_node: str) -> tuple[int, Mapping[str, Any], bytes]:
        """Resolve only an exact canonical node ID with exact key evidence."""
        self._prune_remote_admin_sessions()
        if not isinstance(target_node, str):
            raise self._remote_error("remote_admin_target_invalid")
        text = target_node.strip()
        canonical = canonical_meshtastic_node_id(text)
        if canonical is None or not text.startswith("!") or len(text) != 9:
            raise self._remote_error("remote_admin_target_invalid")
        target_num = int(canonical[1:], 16)
        if self._my_node_num is None or target_num == self._my_node_num:
            raise self._remote_error("remote_admin_target_invalid")
        target = self._nodes.get(target_num)
        if not isinstance(target, Mapping):
            raise self._remote_error("remote_admin_target_unknown")
        user = target.get("user")
        if not isinstance(user, Mapping):
            raise self._remote_error("remote_admin_target_public_key_unavailable")
        claimed_id = user.get("id")
        if claimed_id not in (None, "") and (canonical_meshtastic_node_id(claimed_id) != canonical):
            raise self._remote_error("remote_admin_target_invalid")
        public_key = self._decode_public_key(user.get("publicKey") or user.get("public_key"))
        if public_key is None:
            raise self._remote_error("remote_admin_target_public_key_unavailable")
        self._controller_identity()
        if not self.connected:
            raise self._remote_error("remote_admin_unavailable")
        return target_num, target, public_key

    def _prune_remote_admin_sessions(self) -> None:
        """Drop expired passkeys before any further remote-admin operation."""
        now = time.monotonic()
        for target_num, session in tuple(self._remote_admin_sessions.items()):
            if session.expires_monotonic <= now:
                self._remote_admin_sessions.pop(target_num, None)

    def _controller_identity(self) -> dict[str, str | None]:
        if self._my_node_num is None:
            raise self._remote_error("remote_admin_unavailable")
        node = self._nodes.get(self._my_node_num)
        user = node.get("user") if isinstance(node, Mapping) else None
        if not isinstance(user, Mapping):
            raise self._remote_error("remote_admin_controller_public_key_unavailable")
        public_key = self._decode_public_key(user.get("publicKey") or user.get("public_key"))
        if public_key is None:
            raise self._remote_error("remote_admin_controller_public_key_unavailable")
        return {
            "node_id": f"!{self._my_node_num:08x}",
            "short_name": self._safe_node_label(node, short=True),
            "public_key": f"base64:{base64.b64encode(public_key).decode()}",
        }

    @staticmethod
    def _safe_node_label(node: Mapping[str, Any], *, short: bool) -> str | None:
        user = node.get("user")
        if not isinstance(user, Mapping):
            return None
        names = ("shortName", "short_name", "shortname") if short else ("longName", "long_name", "longname")
        for name in names:
            value = user.get(name)
            if isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 64:
                return value
        return None

    @staticmethod
    def _decode_public_key(value: Any) -> bytes | None:
        if isinstance(value, (bytes, bytearray, memoryview)):
            decoded = bytes(value)
        elif isinstance(value, str):
            text = value.strip()
            if text.casefold().startswith("base64:"):
                text = text.split(":", 1)[1]
            try:
                decoded = bytes.fromhex(text) if len(text) == 64 else base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError):
                return None
        else:
            return None
        return decoded if len(decoded) == 32 else None

    @staticmethod
    def _validate_remote_changes(changes: Any) -> None:
        if not isinstance(changes, Mapping) or not 1 <= len(changes) <= 64:
            raise MeshtasticBluetoothClient._remote_error("remote_admin_command_forbidden")
        for path in changes:
            if not isinstance(path, str) or path not in REMOTE_ADMIN_WRITABLE_PATHS:
                raise MeshtasticBluetoothClient._remote_error("remote_admin_command_forbidden")

    @staticmethod
    def _remote_error(code: str) -> MeshtasticRemoteAdminError:
        messages = {
            "remote_admin_target_invalid": "Select one exact Meshtastic node ID",
            "remote_admin_target_unknown": "The selected Meshtastic node is unknown",
            "remote_admin_target_public_key_unavailable": (
                "The target public key is unavailable on the controller radio"
            ),
            "remote_admin_controller_public_key_unavailable": ("The controller radio public key is unavailable"),
            "remote_admin_controller_unauthorized": ("The target does not authorize this controller radio"),
            "remote_admin_session_rejected": ("The remote-admin session was rejected; load settings again"),
            "remote_admin_no_route": "No mesh route to the selected target is available",
            "remote_admin_no_response": "The selected target did not respond",
            "remote_admin_duty_cycle_limited": ("The radio refused the request because of its duty-cycle limit"),
            "remote_admin_rate_limited": ("The radio refused the request because of its rate limit"),
            "remote_admin_command_forbidden": ("The requested remote-admin operation is not supported"),
            "remote_admin_unknown_outcome": ("The remote write could not be verified; do not repeat it blindly"),
            "remote_admin_unavailable": "Remote administration is unavailable",
        }
        return MeshtasticRemoteAdminError(
            code,
            messages.get(code, "The remote-admin operation failed"),
        )

    async def async_apply_settings_plan(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply one validated transaction once and verify a fresh reread."""
        commit_attempted = False
        async with self._settings_lock:
            try:
                async with asyncio.timeout(_SETTINGS_APPLY_TIMEOUT):
                    if not self.connected or self._connection is None:
                        raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not active")
                    plan = self._settings.build_plan(
                        changes,
                        transport="bluetooth",
                        admin_message_factory=admin_pb2.AdminMessage,
                    )
                    if plan.blocked_paths or not plan.operations:
                        blocked_paths = ", ".join(sorted(plan.blocked_paths))
                        raise MeshtasticConfigurationError(
                            "Meshtastic settings contain unsupported paths"
                            + (f": {blocked_paths}" if blocked_paths else "")
                        )

                    connection = self._connection
                    baseline_sequence = self._settings_complete_sequence
                    baseline_generation = self._connection_generation
                    async with suppress_sensitive_library_logs("meshtastic"):
                        async with self._send_lock:
                            self._require_transaction_connection(connection)
                            if self._settings.revision != plan.revision:
                                raise MeshtasticConfigurationError(
                                    "Meshtastic settings changed before the transaction began"
                                )
                            for operation in plan.operations:
                                self._require_transaction_connection(connection)
                                is_commit = operation.operation == "commit_edit_settings"
                                if is_commit:
                                    commit_attempted = True
                                try:
                                    await self._async_send_admin_locked(
                                        operation.message,
                                        connection=connection,
                                    )
                                except (
                                    _AdminResponseTimeout,
                                    MeshtasticConnectionError,
                                    MeshtasticCleanupError,
                                ):
                                    # Commit intentionally reboots and disables
                                    # BLE, so its ACK can be lost. It is never
                                    # sent again; only a reconnect and exact raw
                                    # readback can establish success.
                                    if not is_commit:
                                        raise
                                    break
                        refreshed = await self._async_wait_for_settings_refresh(
                            baseline_sequence,
                            baseline_generation,
                        )
            except TimeoutError as err:
                if commit_attempted:
                    return {
                        "verified": [],
                        "reconnect_required": True,
                        "warning_codes": ["post_commit_readback_unavailable"],
                    }
                raise MeshtasticConfigurationError("Meshtastic settings operation exceeded its safety timeout") from err

            if not refreshed:
                return {
                    "verified": [],
                    "reconnect_required": True,
                    "warning_codes": ["post_commit_readback_unavailable"],
                }
            verified, unverified = self._settings.verify_plan(plan)
            return {
                "verified": verified,
                "reconnect_required": True,
                "warning_codes": (["post_commit_readback_mismatch"] if unverified else []),
            }

    def _require_transaction_connection(self, connection: BluetoothConnection) -> None:
        """Fail if the settings transaction lost its exact GATT owner."""
        if self._connection is not connection or not self.connected or not connection.is_connected:
            raise MeshtasticConnectionError("Meshtastic Bluetooth changed during the settings transaction")

    async def _async_send_admin_locked(
        self,
        admin_message: Any,
        *,
        connection: BluetoothConnection,
        destination: int | None = None,
        public_key: bytes | None = None,
        session_passkey: bytes | None = None,
        expected_admin_response: str | None = None,
        remote_operation: str | None = None,
    ) -> _AdminResponse:
        """Send one ADMIN_APP packet once and await its correlated response."""
        self._require_transaction_connection(connection)
        if self._my_node_num is None:
            raise MeshtasticConfigurationError("Meshtastic local node identity is unavailable")
        outgoing = admin_pb2.AdminMessage()
        outgoing.CopyFrom(admin_message)
        if session_passkey is not None:
            if len(session_passkey) != 8:
                raise self._remote_error("remote_admin_session_rejected")
            outgoing.session_passkey = session_passkey
        payload = outgoing.SerializeToString()
        maximum = int(getattr(mesh_pb2.Constants, "DATA_PAYLOAD_LEN", 237))
        if len(payload) > maximum:
            raise MeshtasticConfigurationError("Meshtastic admin request exceeds the radio payload limit")

        packet = mesh_pb2.MeshPacket()
        packet.to = self._my_node_num if destination is None else destination
        if public_key is not None:
            if len(public_key) != 32:
                raise self._remote_error("remote_admin_target_public_key_unavailable")
            packet.public_key = public_key
        packet.channel = self._settings.admin_channel_index()
        packet.decoded.payload = payload
        packet.decoded.portnum = portnums_pb2.ADMIN_APP
        packet.decoded.want_response = True
        packet.id = self._next_packet_id()
        packet.want_ack = True
        packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
        packet.pki_encrypted = True
        packet.hop_limit = self._settings.hop_limit()

        to_radio = mesh_pb2.ToRadio()
        to_radio.packet.CopyFrom(packet)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_AdminResponse] = loop.create_future()
        request_id = int(packet.id)
        self._pending_admin_responses[request_id] = _PendingAdminResponse(
            future=future,
            source=int(packet.to),
            channel=int(packet.channel),
            expected_admin_response=expected_admin_response,
        )
        self._internal_admin_request_ids.append(request_id)
        self._admin_request_count += 1
        try:
            # The future is registered before this sole write. A timeout is an
            # unknown radio state and is deliberately never retried.
            await connection.async_send(to_radio.SerializeToString())
            async with asyncio.timeout(self._admin_response_timeout):
                response = await future
        except TimeoutError as err:
            self._admin_timeout_count += 1
            if remote_operation == "write":
                raise self._remote_error("remote_admin_unknown_outcome") from err
            if remote_operation == "read":
                raise self._remote_error("remote_admin_no_response") from err
            raise _AdminResponseTimeout("Meshtastic admin response timed out; radio state is unknown") from err
        except (MeshtasticConnectionError, MeshtasticCleanupError):
            if remote_operation == "write":
                raise self._remote_error("remote_admin_unknown_outcome") from None
            if remote_operation == "read":
                raise self._remote_error("remote_admin_no_response") from None
            raise
        finally:
            pending = self._pending_admin_responses.get(request_id)
            if pending is not None and pending.future is future:
                self._pending_admin_responses.pop(request_id, None)
            if not future.done():
                future.cancel()
        if response.error_reason is not None:
            self._admin_nak_count += 1
            if remote_operation is not None:
                code, message = _REMOTE_ROUTING_ERRORS.get(
                    response.error_reason,
                    (
                        "remote_admin_unknown_outcome",
                        "The remote-admin request could not be verified",
                    ),
                )
                raise MeshtasticRemoteAdminError(code, message)
            raise MeshtasticConfigurationError(f"Meshtastic admin request was rejected ({response.error_reason})")
        return response

    async def _async_wait_for_settings_refresh(
        self,
        baseline_sequence: int,
        baseline_generation: int,
    ) -> bool:
        """Wait for a complete config from a later physical BLE connection."""
        try:
            async with asyncio.timeout(_SETTINGS_READBACK_TIMEOUT):
                while True:
                    if (
                        self._settings_complete_sequence > baseline_sequence
                        and self._settings_complete_generation > baseline_generation
                        and self._settings.complete
                    ):
                        return True
                    self._settings_complete_signal.clear()
                    if (
                        self._settings_complete_sequence > baseline_sequence
                        and self._settings_complete_generation > baseline_generation
                        and self._settings.complete
                    ):
                        return True
                    await self._settings_complete_signal.wait()
        except TimeoutError:
            return False

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
            "admin_request_count": self._admin_request_count,
            "admin_response_count": self._admin_response_count,
            "admin_timeout_count": self._admin_timeout_count,
            "admin_nak_count": self._admin_nak_count,
            "admin_response_waiter_count": len(self._pending_admin_responses),
            "traceroute_request_count": self._traceroute_request_count,
            "traceroute_response_count": self._traceroute_response_count,
            "traceroute_timeout_count": self._traceroute_timeout_count,
            "traceroute_waiter_count": len(self._pending_traceroute_responses),
            "neighbor_info_request_count": self._neighbor_info_request_count,
            "neighbor_info_response_count": self._neighbor_info_response_count,
            "neighbor_info_timeout_count": self._neighbor_info_timeout_count,
            "neighbor_info_rejection_count": self._neighbor_info_rejection_count,
            "neighbor_info_cancellation_count": (
                self._neighbor_info_cancellation_count
            ),
            "neighbor_info_send_failure_count": (
                self._neighbor_info_send_failure_count
            ),
            "neighbor_info_disconnect_count": (
                self._neighbor_info_disconnect_count
            ),
            "neighbor_info_routing_error_counts": dict(
                self._neighbor_info_routing_error_counts
            ),
            "last_neighbor_info_outcome": self._last_neighbor_info_outcome,
            "last_neighbor_info_routing_error": (
                self._last_neighbor_info_routing_error
            ),
            "neighbor_info_waiter_count": len(
                self._pending_neighbor_info_responses
            ),
            "connection_generation": self._connection_generation,
            "settings_complete_sequence": self._settings_complete_sequence,
            "settings_complete_generation": self._settings_complete_generation,
            "last_error_type": self._last_error_type,
            "last_failure_phase": self._last_failure_phase,
            "last_transport_before_cleanup": copy.deepcopy(self._last_connection_snapshot),
            "last_transport_cleanup_outcome": (self._last_connection_cleanup_outcome),
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
                "admin_response": self._admin_response_timeout,
                "settings_apply": _SETTINGS_APPLY_TIMEOUT,
                "settings_readback": _SETTINGS_READBACK_TIMEOUT,
            },
            "transport": (self._connection_diagnostics(connection) if connection is not None else None),
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
                    self._connection_generation += 1

                    self._set_state("bluetooth_requesting_configuration")
                    self._config_complete_event.clear()
                    self._settings.begin_refresh()
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
                self._set_state("bluetooth_failed" if self._last_error_type else "bluetooth_stopped")

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
                raise MeshtasticConnectionError("Meshtastic Bluetooth reader stopped during configuration")
            raise MeshtasticConfigurationError("Meshtastic radio did not complete configuration in time")
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
            raise MeshtasticConnectionError("A Meshtastic Bluetooth persistent-session task stopped")

    def _handle_from_radio(self, payload: bytes) -> None:
        from_radio = mesh_pb2.FromRadio()
        from_radio.ParseFromString(payload)

        if from_radio.HasField("my_info"):
            self._my_node_num = int(from_radio.my_info.my_node_num)
        self._settings.capture_from_radio(
            from_radio,
            my_node_num=self._my_node_num,
        )
        if from_radio.HasField("node_info"):
            node = MessageToDict(from_radio.node_info)
            self._merge_node(int(from_radio.node_info.num), node)
        if from_radio.HasField("packet"):
            if not self._handle_internal_admin_packet(from_radio.packet):
                packet = self._packet_to_dict(from_radio.packet)
                self._update_node_from_packet(from_radio.packet, packet)
                self._publish_packet(packet)
        if self._config_id is not None and from_radio.config_complete_id == self._config_id:
            self._settings.mark_complete()
            self._settings_complete_sequence += 1
            self._settings_complete_generation = self._connection_generation
            self._settings_complete_signal.set()
            self._config_complete_event.set()

    def _handle_internal_admin_packet(self, packet: Any) -> bool:
        """Consume correlated admin traffic before any public packet projection."""
        if not packet.HasField("decoded"):
            return False
        decoded = packet.decoded
        portnum = int(decoded.portnum)
        request_id = int(decoded.request_id)
        if portnum == int(portnums_pb2.NEIGHBORINFO_APP):
            pending_neighbor = self._pending_neighbor_info_responses.get(
                request_id
            )
            if pending_neighbor is None:
                return False
            if (
                pending_neighbor.future.done()
                or int(getattr(packet, "from")) != pending_neighbor.source
                or self._my_node_num is None
                or int(packet.to) != self._my_node_num
                or int(packet.channel) != pending_neighbor.channel
                or bool(packet.via_mqtt)
            ):
                return True
            try:
                neighbor_info = mesh_pb2.NeighborInfo()
                neighbor_info.ParseFromString(bytes(decoded.payload))
            except DecodeError:
                return True
            if int(neighbor_info.node_id) != pending_neighbor.source:
                return True
            self._manual_neighbor_info_response_packets.add(
                (int(getattr(packet, "from")), int(packet.id))
            )
            pending_neighbor.future.set_result(neighbor_info)
            # NeighborInfo contains no credentials. Continue through the normal
            # decoder while retaining explicit manual-request provenance.
            return False
        if portnum == int(portnums_pb2.TRACEROUTE_APP):
            pending_trace = self._pending_traceroute_responses.get(request_id)
            if pending_trace is None:
                return False
            if (
                pending_trace.future.done()
                or int(getattr(packet, "from")) != pending_trace.source
                or self._my_node_num is None
                or int(packet.to) != self._my_node_num
                or int(packet.channel) != pending_trace.channel
            ):
                return True
            try:
                route = mesh_pb2.RouteDiscovery()
                route.ParseFromString(bytes(decoded.payload))
            except DecodeError:
                return True
            pending_trace.future.set_result(route)
            return True
        if portnum == int(portnums_pb2.ADMIN_APP):
            # ADMIN_APP can contain credentials/session material. It is always
            # consumed internally and parsed only for an exact registered
            # remote read; no payload is projected or logged.
            pending = self._pending_admin_responses.get(request_id)
            if (
                pending is None
                or pending.future.done()
                or pending.expected_admin_response is None
                or int(getattr(packet, "from")) != pending.source
                or self._my_node_num is None
                or int(packet.to) != self._my_node_num
                or int(packet.channel) != pending.channel
                or not bool(packet.pki_encrypted)
            ):
                return True
            try:
                admin = admin_pb2.AdminMessage()
                admin.ParseFromString(bytes(decoded.payload))
            except DecodeError:
                return True
            if (
                _selected_admin_field(admin) != pending.expected_admin_response
                or len(bytes(admin.session_passkey)) != 8
            ):
                return True
            self._admin_response_count += 1
            pending.future.set_result(_AdminResponse(kind="admin", admin_message=admin))
            return True
        if portnum != int(portnums_pb2.ROUTING_APP):
            return False
        pending_trace = self._pending_traceroute_responses.get(request_id)
        if pending_trace is not None:
            if (
                pending_trace.future.done()
                or int(getattr(packet, "from")) != pending_trace.source
                or self._my_node_num is None
                or int(packet.to) != self._my_node_num
                or int(packet.channel) != pending_trace.channel
                or bool(packet.via_mqtt)
            ):
                return True
            try:
                routing = mesh_pb2.Routing()
                routing.ParseFromString(bytes(decoded.payload))
            except DecodeError:
                return True
            selected = _selected_admin_field(routing)
            if selected == "error_reason" and int(routing.error_reason):
                pending_trace.future.set_exception(
                    MeshtasticConfigurationError("Meshtastic traceroute was rejected by the mesh")
                )
            # A successful routing ACK is not the RouteDiscovery response.
            return True
        pending_neighbor = self._pending_neighbor_info_responses.get(request_id)
        if pending_neighbor is not None:
            if (
                pending_neighbor.future.done()
                or int(getattr(packet, "from")) != pending_neighbor.source
                or self._my_node_num is None
                or int(packet.to) != self._my_node_num
                or int(packet.channel) != pending_neighbor.channel
                or bool(packet.via_mqtt)
            ):
                return True
            try:
                routing = mesh_pb2.Routing()
                routing.ParseFromString(bytes(decoded.payload))
            except DecodeError:
                return True
            selected = _selected_admin_field(routing)
            if selected == "error_reason" and int(routing.error_reason):
                try:
                    reason = mesh_pb2.Routing.Error.Name(
                        int(routing.error_reason)
                    )
                except ValueError:
                    reason = "UNKNOWN"
                self._neighbor_info_rejection_count += 1
                self._neighbor_info_routing_error_counts[reason] = (
                    self._neighbor_info_routing_error_counts.get(reason, 0) + 1
                )
                self._last_neighbor_info_outcome = "rejected"
                self._last_neighbor_info_routing_error = reason
                pending_neighbor.future.set_exception(
                    MeshtasticNeighborInfoError(
                        "neighbor_info_unsupported"
                        if reason == "BAD_REQUEST"
                        else "neighbor_info_rejected"
                    )
                )
            # A successful routing ACK is not the NeighborInfo response.
            return True
        if request_id not in self._internal_admin_request_ids:
            return False
        try:
            routing = mesh_pb2.Routing()
            routing.ParseFromString(bytes(decoded.payload))
        except DecodeError:
            return True
        pending = self._pending_admin_responses.get(request_id)
        if pending is None or pending.future.done():
            return True
        if int(getattr(packet, "from")) != pending.source or int(packet.channel) != pending.channel:
            return True
        selected: str | None = None
        for oneof in getattr(routing.DESCRIPTOR, "oneofs", ()):
            selected = routing.WhichOneof(oneof.name)
            if selected is not None:
                break
        if selected != "error_reason":
            # Empty Routing payloads and route records are not setter ACKs.
            return True
        error_value = int(routing.error_reason)
        if not error_value and pending.expected_admin_response is not None:
            # A successful routing ACK does not replace the requested Admin
            # payload. Keep waiting for that exactly correlated response.
            return True
        if error_value:
            try:
                error_reason = mesh_pb2.Routing.Error.Name(error_value)
            except ValueError:
                error_reason = f"UNKNOWN_{error_value}"
            self._admin_response_count += 1
            pending.future.set_result(_AdminResponse(kind="routing", error_reason=error_reason))
        else:
            self._admin_response_count += 1
            pending.future.set_result(_AdminResponse(kind="routing"))
        return True

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
            packet_key = (source, int(packet.id))
            if (
                packet.decoded.portnum == portnums_pb2.NEIGHBORINFO_APP
                and packet_key in self._manual_neighbor_info_response_packets
            ):
                self._manual_neighbor_info_response_packets.discard(packet_key)
                decoded["neighborInfoProvenance"] = "manual_request"
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
            elif data.portnum == portnums_pb2.NEIGHBORINFO_APP:
                neighbor_info = mesh_pb2.NeighborInfo()
                neighbor_info.ParseFromString(payload)
                decoded["neighborInfo"] = MessageToDict(neighbor_info)
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
            neighbor_info = decoded.get("neighborInfo")
            if isinstance(neighbor_info, Mapping):
                update["neighborInfo"] = copy.deepcopy(dict(neighbor_info))
                update["neighborInfoUpdatedAt"] = int(packet.rx_time)
                provenance = decoded.get("neighborInfoProvenance")
                update["neighborInfoProvenance"] = (
                    "manual_request"
                    if provenance == "manual_request"
                    else "passive"
                )
        self._merge_node(source, update)

    def _merge_node(self, node_num: int, update: Mapping[str, Any]) -> None:
        if node_num in (0, _BROADCAST_NUM):
            return
        safe_update: Mapping[str, Any] = update
        user = update.get("user")
        if isinstance(user, Mapping):
            claimed_id = user.get("id")
            if claimed_id not in (None, "") and (
                canonical_meshtastic_node_id(claimed_id) != canonical_meshtastic_node_id(node_num)
            ):
                # The packet/config envelope owns routing identity. Never let
                # a contradictory NodeInfo claim seed cached names or a MAC
                # alias that could later target a different real node.
                sanitized = dict(update)
                sanitized.pop("user", None)
                safe_update = sanitized
        merged = copy.deepcopy(self._nodes.get(node_num, {"num": node_num}))
        self._deep_merge(merged, safe_update)
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
        except Exception as err:
            self._callback_error_count += 1
            self._logger.warning("Meshtastic callback failed (%s)", type(err).__name__)
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
        except Exception as err:
            self._callback_error_count += 1
            self._logger.warning("Meshtastic async callback failed (%s)", type(err).__name__)

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
        self._fail_pending_admin_responses()
        if connection is not None:
            self._last_connection_snapshot = self._connection_diagnostics(connection)
            self._last_connection_cleanup_outcome = "pending"
        session_tasks = tuple(task for task in (reader, heartbeat) if task is not None)
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
            raise MeshtasticCleanupError("Meshtastic session tasks did not stop within their cleanup bound")
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
                    raise MeshtasticCleanupError("Meshtastic GATT teardown was not confirmed") from err
                self._last_connection_cleanup_outcome = "error_without_owner"
            else:
                self._last_connection_cleanup_outcome = "confirmed"
        if self._connection is connection and not self._connection_owns_endpoint(connection):
            self._connection = None
        self._config_complete_event.clear()
        self._config_id = None

    def _fail_pending_admin_responses(self) -> None:
        """Fail response waiters when their exact GATT owner is gone."""
        for pending in tuple(self._pending_admin_responses.values()):
            if not pending.future.done():
                pending.future.set_exception(
                    MeshtasticConnectionError("Meshtastic Bluetooth disconnected during admin request")
                )
        self._pending_admin_responses.clear()
        for pending in tuple(self._pending_traceroute_responses.values()):
            if not pending.future.done():
                pending.future.set_exception(
                    MeshtasticConnectionError("Meshtastic Bluetooth disconnected during traceroute")
                )
        self._pending_traceroute_responses.clear()
        pending_neighbor_disconnect = False
        for pending in tuple(self._pending_neighbor_info_responses.values()):
            if not pending.future.done():
                pending_neighbor_disconnect = True
                pending.future.set_exception(
                    MeshtasticNeighborInfoError("neighbor_info_disconnected")
                )
        if pending_neighbor_disconnect:
            self._neighbor_info_disconnect_count += 1
            self._last_neighbor_info_outcome = "disconnected"
        self._pending_neighbor_info_responses.clear()
        self._manual_neighbor_info_response_packets.clear()
        self._internal_admin_request_ids.clear()
        self._remote_admin_sessions.clear()
        self._remote_settings.clear()

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
                if isinstance(scalar, (int, float)) and not isinstance(scalar, bool):
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
                    and any(aliases & self._mac_aliases(node["user"].get(key)) for key in ("macaddr", "mac"))
                ]
                if len(matches) != 1:
                    raise ValueError("destination_id MAC key is unknown or ambiguous")
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
                        if isinstance(node.get("user"), Mapping) and node["user"].get("id") == text
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
                        _normalized_node_name(node["user"].get(field)) == normalized_name for field in _NODE_NAME_FIELDS
                    )
                }
                if len(name_matches) > 1:
                    raise ValueError("destination_id Meshtastic node name is ambiguous")
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

    @staticmethod
    def _normalized_route_nodes(values: Any, *, excluded: set[int]) -> list[str]:
        """Project bounded intermediate node numbers onto canonical IDs."""
        result: list[str] = []
        previous: int | None = None
        for value in list(values)[:64]:
            node_num = int(value)
            if node_num in excluded or node_num in {0, _BROADCAST_NUM} or node_num == previous:
                continue
            result.append(f"!{node_num:08x}")
            previous = node_num
        return result

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
        return MeshtasticConnectionError(f"Meshtastic Bluetooth session failed: {type(error).__name__}")

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
        stale_lifecycle = self._stop_event.is_set() or (self._state == "bluetooth_cleanup_incomplete")
        if self._stop_cleanup_task is not None and not self._stop_cleanup_task.done():
            return True
        session_owner_pending = any(
            task is not None and not task.done() for task in (self._reader_task, self._heartbeat_task)
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
