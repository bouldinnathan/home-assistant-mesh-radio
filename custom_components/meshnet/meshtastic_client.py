"""Meshtastic gateway adapter."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from weakref import WeakKeyDictionary

from .const import (
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    DEFAULT_MESHTASTIC_MQTT_TOPIC,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)
from .gateway import MeshGateway
from .models import (
    GatewayConfig,
    MeshPacket,
    NodeState,
    canonical_node_key,
    coerce_float,
    coerce_int,
    parse_timestamp,
    utcnow,
)

_LOGGER = logging.getLogger(__name__)

_STOP_WAIT_TIMEOUT = 2.0
_BLUEZ_ADAPTER_INTERFACE = "org.bluez.Adapter1"
_LOCAL_ADAPTER_RE = re.compile(r"hci[0-9]+\Z")
_BLUETOOTH_ADDRESS_RE = re.compile(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}\Z")
_NATIVE_ENDPOINT_LOCKS: WeakKeyDictionary[
    Any, dict[tuple[str, str], asyncio.Lock]
] = WeakKeyDictionary()


def _native_endpoint_lock(endpoint: tuple[str, str]) -> asyncio.Lock:
    """Return one process-wide native transport lock for this HA event loop."""
    loop = asyncio.get_running_loop()
    locks = _NATIVE_ENDPOINT_LOCKS.get(loop)
    if locks is None:
        locks = {}
        _NATIVE_ENDPOINT_LOCKS[loop] = locks
    lock = locks.get(endpoint)
    if lock is None:
        lock = asyncio.Lock()
        locks[endpoint] = lock
    return lock


async def _async_get_local_bluetooth_adapter_details() -> dict[str, Any]:
    """Return local BlueZ adapter details through the public HA dependency."""
    try:
        from bluetooth_adapters import get_bluetooth_adapter_details
    except ImportError as err:
        raise RuntimeError(
            "The local Bluetooth adapter service is unavailable"
        ) from err

    try:
        details = await get_bluetooth_adapter_details()
    except Exception as err:
        raise RuntimeError(
            "Home Assistant could not verify the local Bluetooth adapters"
        ) from err
    if not isinstance(details, dict):
        raise RuntimeError(
            "Home Assistant returned invalid local Bluetooth adapter data"
        )
    return details


async def _async_validate_ble_adapter(
    config: GatewayConfig,
) -> tuple[dict[str, int | bool], str]:
    """Resolve the currently powered controller for the verified stable MAC.

    The async Bluetooth transport selects the exact Home Assistant scanner that
    belongs to this controller, so other local adapters may remain powered.
    """
    saved_adapter = config.options.get(CONF_BLUETOOTH_ADAPTER)
    saved_adapter_address = config.options.get(CONF_BLUETOOTH_ADAPTER_ADDRESS)
    if (
        not isinstance(saved_adapter, str)
        or _LOCAL_ADAPTER_RE.fullmatch(saved_adapter) is None
        or not isinstance(saved_adapter_address, str)
        or _BLUETOOTH_ADDRESS_RE.fullmatch(saved_adapter_address.upper()) is None
        or saved_adapter_address.upper()
        in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}
    ):
        raise RuntimeError(
            "Bluetooth setup has no verified local adapter; reconfigure this gateway"
        )

    details = await _async_get_local_bluetooth_adapter_details()
    saved_adapter_address = saved_adapter_address.upper()
    powered_adapters: set[tuple[str, str]] = set()
    for adapter, interfaces in details.items():
        if (
            not isinstance(adapter, str)
            or _LOCAL_ADAPTER_RE.fullmatch(adapter) is None
            or not isinstance(interfaces, dict)
        ):
            raise RuntimeError("Bluetooth adapter data is incomplete or invalid")
        adapter_properties = interfaces.get(_BLUEZ_ADAPTER_INTERFACE)
        if not isinstance(adapter_properties, dict):
            raise RuntimeError("Bluetooth adapter data is incomplete or invalid")
        powered = adapter_properties.get("Powered")
        if not isinstance(powered, bool):
            raise RuntimeError("Bluetooth adapter data is incomplete or invalid")
        adapter_address = adapter_properties.get("Address")
        if (
            not isinstance(adapter_address, str)
            or _BLUETOOTH_ADDRESS_RE.fullmatch(adapter_address.upper()) is None
            or adapter_address.upper()
            in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}
        ):
            raise RuntimeError("Bluetooth adapter data is incomplete or invalid")
        if powered:
            powered_adapters.add((adapter, adapter_address.upper()))

    selected_adapters = [
        adapter
        for adapter, address in powered_adapters
        if address == saved_adapter_address
    ]
    saved_adapter_is_powered = len(selected_adapters) == 1
    if not saved_adapter_is_powered:
        raise RuntimeError(
            "The paired Bluetooth adapter is not available and powered"
        )
    return (
        {
            "adapter_count": len(details),
            "powered_adapter_count": len(powered_adapters),
            "saved_adapter_is_powered": saved_adapter_is_powered,
            "selected_adapter_path_count": len(selected_adapters),
        },
        selected_adapters[0],
    )


def _bluetooth_adapter_failure_category(error: Exception) -> str:
    """Classify adapter validation without exposing adapter identity."""
    message = str(error).casefold()
    if "no verified local adapter" in message:
        return "missing_verified_adapter_metadata"
    if "not available and powered" in message:
        return "powered_adapter_mismatch"
    if "unavailable" in message or "could not verify" in message:
        return "adapter_service_unavailable"
    if "incomplete or invalid" in message:
        return "invalid_adapter_data"
    return "adapter_validation_failed"


class MeshtasticClient(MeshGateway):
    """Gateway adapter for Meshtastic native and MQTT transports."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._interface: Any | None = None
        self._ble_transport: Any | None = None
        self._ble_packet_unsubscribe: Callable[[], None] | None = None
        self._ble_connection_unsubscribe: Callable[[], None] | None = None
        self._ble_callback_transport: Any | None = None
        self._ble_operation_tasks: set[asyncio.Task[Any]] = set()
        self._ble_deferred_cleanup_task: asyncio.Task[Any] | None = None
        self._unsub_mqtt: Any | None = None
        self._stopping = False
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._native_lock: asyncio.Lock | None = None
        self._native_constructor_future: asyncio.Future[Any] | None = None
        self._native_constructor_abandoned = False
        self._native_constructor_abandonment_count = 0
        self._native_constructor_cleanup_task: asyncio.Task[Any] | None = None
        self._native_executor_tasks: dict[int, set[asyncio.Future[Any]]] = {}
        self._startup_phase = "idle"
        self._startup_phase_started_monotonic: float | None = None
        self._startup_started_monotonic: float | None = None
        self._last_start_duration_seconds: float | None = None
        self._last_start_outcome = "not_started"
        self._last_start_exception_type: str | None = None
        self._last_start_failed_phase: str | None = None
        self._bluetooth_adapter_validation: dict[str, Any] = {
            "status": (
                "not_started"
                if self.config.transport == TRANSPORT_BLUETOOTH
                else "not_applicable"
            )
        }
        self._pub = None
        self._receive_handler = None
        self._connect_handler = None
        self._disconnect_handler = None

    def _owns_interface(self, interface: Any) -> bool:
        """Return whether a process-global pubsub event belongs to this client."""
        return self._interface is not None and interface is self._interface

    @property
    def start_pending(self) -> bool:
        """Return whether this client already has a transport start in flight."""
        return self._start_task is not None and not self._start_task.done()

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return cached Meshtastic lifecycle state without SDK or endpoint data."""
        snapshot = super().diagnostic_snapshot()
        constructor = self._native_constructor_future
        constructor_pending = constructor is not None and not constructor.done()
        startup_pending = self.start_pending
        now = time.monotonic()
        snapshot.update(
            {
                "interface_active": (
                    self._interface is not None or self._ble_transport is not None
                ),
                "mqtt_subscription_active": self._unsub_mqtt is not None,
                "stopping": self._stopping,
                "start_task": self._diagnostic_task_state(self._start_task),
                "stop_task": self._diagnostic_task_state(self._stop_task),
                "native_endpoint_lock_held": (
                    self._native_lock is not None and self._native_lock.locked()
                ),
                "native_constructor_state": self._diagnostic_task_state(
                    constructor
                ),
                "native_constructor_pending": constructor_pending,
                "native_constructor_abandoned": self._native_constructor_abandoned,
                "native_constructor_abandonment_count": (
                    self._native_constructor_abandonment_count
                ),
                "native_constructor_cleanup": self._diagnostic_task_state(
                    self._native_constructor_cleanup_task
                ),
                "native_executor_operation_count": (
                    int(constructor_pending)
                    + sum(
                        len(tasks)
                        for tasks in self._native_executor_tasks.values()
                    )
                ),
                "native_interface_executor_count": len(self._native_executor_tasks),
                "bluetooth_operation_count": len(self._ble_operation_tasks),
                "bluetooth_callbacks_bound": (
                    self._ble_callback_transport is self._ble_transport
                    and self._ble_packet_unsubscribe is not None
                    and self._ble_connection_unsubscribe is not None
                ),
                "bluetooth_deferred_cleanup": self._diagnostic_task_state(
                    self._ble_deferred_cleanup_task
                ),
                "native_subscription_count": sum(
                    handler is not None
                    for handler in (
                        self._receive_handler,
                        self._connect_handler,
                        self._disconnect_handler,
                    )
                ),
                "startup_phase": self._startup_phase,
                "startup_elapsed_seconds": (
                    round(now - self._startup_started_monotonic, 3)
                    if startup_pending
                    and self._startup_started_monotonic is not None
                    else None
                ),
                "startup_phase_elapsed_seconds": (
                    round(now - self._startup_phase_started_monotonic, 3)
                    if startup_pending
                    and self._startup_phase_started_monotonic is not None
                    else None
                ),
                "last_start_duration_seconds": self._last_start_duration_seconds,
                "last_start_outcome": self._last_start_outcome,
                "last_start_exception_type": self._last_start_exception_type,
                "last_start_failed_phase": self._last_start_failed_phase,
                "bluetooth_adapter_validation": dict(
                    self._bluetooth_adapter_validation
                ),
                "bluetooth_transport": (
                    self._ble_transport.diagnostic_snapshot()
                    if self._ble_transport is not None
                    else {
                        "implementation": "not_created",
                        "state": "not_applicable"
                        if self.config.transport != TRANSPORT_BLUETOOTH
                        else "not_created",
                    }
                ),
            }
        )
        return snapshot

    def _set_startup_phase(self, phase: str) -> None:
        """Set one identity-free startup phase for cached diagnostics."""
        self._startup_phase = phase
        self._startup_phase_started_monotonic = time.monotonic()

    def _schedule_abandoned_constructor_cleanup(
        self,
        constructor: asyncio.Future[Any],
    ) -> None:
        """Close a constructor result whose startup owner was cancelled."""
        if not self._native_constructor_abandoned or not constructor.done():
            return
        cleanup_task = self._native_constructor_cleanup_task
        if cleanup_task is not None and not cleanup_task.done():
            return
        try:
            interface = constructor.result()
        except asyncio.CancelledError:
            if self._native_constructor_future is constructor:
                self._native_constructor_future = None
            self._release_native_lock()
            return
        except Exception:
            if self._native_constructor_future is constructor:
                self._native_constructor_future = None
            self._release_native_lock()
            return

        async def cleanup_late_interface() -> None:
            try:
                await self._async_close_interface(
                    interface,
                    release_native_lock=True,
                )
            finally:
                if self._native_constructor_future is constructor:
                    self._native_constructor_future = None

        cleanup_task = self._async_create_background_task(
            cleanup_late_interface(),
            "MeshNet abandoned Meshtastic constructor cleanup",
        )
        self._native_constructor_cleanup_task = cleanup_task

        def cleanup_done(done_task: asyncio.Task[Any]) -> None:
            if self._native_constructor_cleanup_task is done_task:
                self._native_constructor_cleanup_task = None
            if not done_task.cancelled():
                done_task.exception()

        cleanup_task.add_done_callback(cleanup_done)

    async def async_start(self) -> None:
        """Start the Meshtastic transport."""
        stop_task = self._stop_task
        if stop_task is not None:
            await asyncio.shield(stop_task)

        deferred_cleanup = self._ble_deferred_cleanup_task
        if deferred_cleanup is not None and not deferred_cleanup.done():
            # That task is irrevocably committed to stopping and clearing the
            # current transport after an older cancellation-resistant operation
            # yields. Reporting a restart now would let it tear down the newly
            # reported session underneath the caller.
            raise RuntimeError(
                "Meshtastic Bluetooth cleanup is still pending; retry startup later"
            )

        # An explicit start after a completed stop may safely adopt a still-
        # running constructor. It must never enqueue a second constructor.
        self._stopping = False
        start_task = self._start_task
        if start_task is None:
            start_task = self._async_create_background_task(
                self._async_start_once(),
                "MeshNet Meshtastic transport startup",
            )
            self._start_task = start_task
            start_task.add_done_callback(self._start_done)

        # Cancellation of one waiter must not abandon a synchronous interface
        # constructor that is still occupying Home Assistant's executor. A
        # concurrent stop waits for the same task and disposes of a late result.
        await asyncio.shield(start_task)

    async def _async_start_once(self) -> None:
        """Start one transport instance."""
        self._startup_started_monotonic = time.monotonic()
        self._last_start_duration_seconds = None
        self._last_start_outcome = "pending"
        self._last_start_exception_type = None
        self._last_start_failed_phase = None
        self._set_startup_phase("starting")
        try:
            if self.config.transport == TRANSPORT_MQTT:
                if self._unsub_mqtt is None:
                    self._set_startup_phase("subscribing_mqtt")
                    await self._start_mqtt()
            elif self.config.transport == TRANSPORT_BLUETOOTH:
                if self._ble_transport is None:
                    await self._start_native()
                else:
                    # A persistent BLE supervisor can terminate after a session
                    # failure while the adapter object and endpoint lease remain.
                    # Rejoin/restart that exact transport instead of reporting a
                    # disconnected object as ready.
                    await self._resume_bluetooth_transport()
            elif self._interface is None:
                await self._start_native()
        except asyncio.CancelledError:
            self._last_start_outcome = "cancelled"
            self._last_start_failed_phase = self._startup_phase
            self._set_startup_phase("cancelled")
            raise
        except Exception as err:
            self._last_start_outcome = "failed"
            self._last_start_exception_type = type(err).__name__
            self._last_start_failed_phase = self._startup_phase
            self._set_startup_phase("failed")
            raise
        else:
            if self._stopping:
                self._last_start_outcome = "stopped_during_start"
                self._set_startup_phase("stopped")
            else:
                self._last_start_outcome = "succeeded"
                self._set_startup_phase("ready")
        finally:
            if self._startup_started_monotonic is not None:
                self._last_start_duration_seconds = round(
                    time.monotonic() - self._startup_started_monotonic,
                    3,
                )

    def _start_done(self, task: asyncio.Task[None]) -> None:
        """Clear the single-flight start task without disturbing a newer one."""
        if self._start_task is task:
            self._start_task = None
        if not task.cancelled():
            # Retrieve a failure even if every public waiter was cancelled. The
            # start path has already emitted the user-visible error.
            task.exception()
        if self._stopping:
            # async_stop is intentionally bounded. If a synchronous constructor
            # outlives that bound, its completion gets one final idempotent
            # cleanup pass without blocking Home Assistant unload.
            self._async_create_background_task(
                self._async_cleanup_after_late_start(),
                "MeshNet late Meshtastic transport cleanup",
            )

    def _stop_done(self, task: asyncio.Task[None]) -> None:
        """Clear the single-flight stop task without disturbing a newer one."""
        if self._stop_task is task:
            self._stop_task = None
        if not task.cancelled():
            task.exception()

    async def async_stop(self) -> None:
        """Stop the Meshtastic transport."""
        stop_task = self._stop_task
        if stop_task is None:
            # Set this before scheduling cleanup so an executor constructor that
            # finishes concurrently cannot publish its interface as connected.
            self._stopping = True
            self._set_startup_phase("stopping")
            stop_task = self.hass.async_create_task(self._async_stop_once())
            self._stop_task = stop_task
            stop_task.add_done_callback(self._stop_done)
        await asyncio.shield(stop_task)

    async def _async_stop_once(self) -> None:
        """Stop one transport instance and wait out any pending constructor."""
        start_task = self._start_task
        start_drained = True
        try:
            if start_task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(start_task),
                        timeout=_STOP_WAIT_TIMEOUT,
                    )
                except TimeoutError:
                    self._logger.debug(
                        "Meshtastic start did not finish within %.1f seconds; "
                        "continuing bounded shutdown",
                        _STOP_WAIT_TIMEOUT,
                    )
                    if self.config.transport == TRANSPORT_BLUETOOTH:
                        # Cancellation is normally prompt, but a platform BLE
                        # await may delay it. Never turn that OS behavior into an
                        # unbounded Home Assistant unload.
                        start_task.cancel()
                        done, _ = await asyncio.wait(
                            {start_task},
                            timeout=_STOP_WAIT_TIMEOUT,
                        )
                        start_drained = start_task in done
                except asyncio.CancelledError:
                    if not start_task.cancelled():
                        raise
                except Exception:
                    # Start errors are reported by the start path. Cleanup must
                    # still remove subscriptions and partial state.
                    pass
        finally:
            if start_drained:
                await self._async_cleanup_transport(emit_status=True)
            self._set_startup_phase(
                "stopping_waiting_for_start"
                if self.start_pending
                else "stopped"
            )
        if not start_drained:
            # _start_done owns the deferred cleanup once the cancellation-
            # resistant start finally yields. Keep the endpoint lease meanwhile.
            raise RuntimeError(
                "Meshtastic Bluetooth startup did not stop within the cleanup bound"
            )

    async def _async_cleanup_transport(self, *, emit_status: bool) -> None:
        """Detach transport state and close its interface idempotently."""
        deferred_cleanup = self._ble_deferred_cleanup_task
        if (
            deferred_cleanup is not None
            and deferred_cleanup is not asyncio.current_task()
            and not deferred_cleanup.done()
        ):
            done, _ = await asyncio.wait(
                {deferred_cleanup},
                timeout=_STOP_WAIT_TIMEOUT,
            )
            if deferred_cleanup not in done:
                raise RuntimeError(
                    "Meshtastic Bluetooth cleanup is waiting for an active operation"
                )
        if self._unsub_mqtt:
            unsubscribe = self._unsub_mqtt
            self._unsub_mqtt = None
            try:
                unsubscribe()
            except Exception as err:
                self._logger.debug("Failed to unsubscribe Meshtastic MQTT handler: %s", err)
        self._unsubscribe_native_events()
        if self._ble_transport is not None:
            transport = self._ble_transport
            self._unsubscribe_bluetooth_events()
            pending_operations = await self._async_cancel_bluetooth_operations()
            if pending_operations:
                self._schedule_deferred_bluetooth_cleanup(
                    transport,
                    pending_operations,
                )
                raise RuntimeError(
                    "Meshtastic Bluetooth operations did not stop within the cleanup bound"
                )
            self._interface = None
            try:
                await transport.async_stop()
            except Exception:
                # An unconfirmed GATT teardown must retain the endpoint lease.
                self._ble_transport = transport
                raise
            else:
                if self._ble_transport is transport:
                    self._ble_transport = None
                self._release_native_lock()
        elif self._interface is not None:
            interface = self._interface
            self._interface = None
            await self._async_close_interface(interface, release_native_lock=True)
        if emit_status:
            await self._set_connected(False)

    async def _async_cleanup_after_late_start(self) -> None:
        """Clean a late start only if the client has not been started again."""
        if not self._stopping:
            return
        await self._async_cleanup_transport(emit_status=False)

    async def _async_close_interface(
        self,
        interface: Any,
        *,
        release_native_lock: bool = False,
    ) -> None:
        """Close an interface without allowing a stuck close to hang unload."""
        async def close_interface() -> None:
            pending = set(self._native_executor_tasks.get(id(interface), set()))
            if pending:
                await asyncio.gather(
                    *(asyncio.shield(future) for future in pending),
                    return_exceptions=True,
                )
            await self.hass.async_add_executor_job(interface.close)

        close_job = self._async_create_background_task(
            close_interface(),
            "MeshNet Meshtastic interface close",
        )

        def close_done(task: asyncio.Future[Any]) -> None:
            if task.cancelled():
                return
            try:
                error = task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self._logger.debug("Failed to close Meshtastic interface: %s", error)
                return
            if release_native_lock:
                self._release_native_lock()

        close_job.add_done_callback(close_done)
        try:
            await asyncio.wait_for(
                asyncio.shield(close_job),
                timeout=_STOP_WAIT_TIMEOUT,
            )
        except TimeoutError:
            self._logger.debug(
                "Meshtastic interface close exceeded %.1f seconds; "
                "continuing bounded shutdown",
                _STOP_WAIT_TIMEOUT,
            )

    async def _async_run_native_executor(
        self,
        interface: Any,
        target: Callable[[], Any],
        *,
        name: str,
    ) -> Any:
        """Run and retain work that owns one native interface.

        Cancelling an asyncio waiter cannot stop a function already running in
        Home Assistant's executor. Keep the raw executor future strongly owned
        and delay cancellation completion until that owner finishes. Interface
        close also waits for the same retained future before touching the SDK.
        """

        future = asyncio.ensure_future(self.hass.async_add_executor_job(target))
        if isinstance(future, asyncio.Task):
            future.set_name(name)
        interface_key = id(interface)
        tasks = self._native_executor_tasks.setdefault(interface_key, set())
        tasks.add(future)

        def executor_done(done_future: asyncio.Future[Any]) -> None:
            current_tasks = self._native_executor_tasks.get(interface_key)
            if current_tasks is not None:
                current_tasks.discard(done_future)
                if not current_tasks:
                    self._native_executor_tasks.pop(interface_key, None)
            if not done_future.cancelled():
                done_future.exception()

        future.add_done_callback(executor_done)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            try:
                await asyncio.wait({future})
            except asyncio.CancelledError:
                # A second cancellation may end the public waiter. The raw
                # future remains retained for interface-close ordering.
                pass
            raise

    def _native_endpoint(self) -> tuple[str, str]:
        """Return a non-secret key that serializes one native endpoint."""
        if self.config.transport == TRANSPORT_BLUETOOTH:
            endpoint = (self.config.ble_address or "").upper()
        elif self.config.transport == TRANSPORT_SERIAL:
            endpoint = self.config.serial_path or ""
        else:
            host = self.config.host or self.config.api_url or ""
            endpoint = f"{host}:{self.config.port or 0}"
        return self.config.transport, endpoint

    def _release_native_lock(self) -> None:
        """Release this client's endpoint lease exactly once."""
        lock = self._native_lock
        if lock is None:
            return
        self._native_lock = None
        self._native_constructor_abandoned = False
        if lock.locked():
            lock.release()

    def _unsubscribe_native_events(self) -> None:
        """Remove all process-global pubsub handlers idempotently."""
        subscriptions = (
            (self._receive_handler, "meshtastic.receive"),
            (self._connect_handler, "meshtastic.connection.established"),
            (self._disconnect_handler, "meshtastic.connection.lost"),
        )
        if self._pub:
            for handler, topic in subscriptions:
                if handler is None:
                    continue
                try:
                    self._pub.unsubscribe(handler, topic)
                except Exception as err:
                    self._logger.debug("Failed to unsubscribe %s handler: %s", topic, err)
        self._pub = None
        self._receive_handler = None
        self._connect_handler = None
        self._disconnect_handler = None

    def _unsubscribe_bluetooth_events(self) -> None:
        """Detach transport-local callbacks without touching global pubsub."""
        callbacks = (
            self._ble_packet_unsubscribe,
            self._ble_connection_unsubscribe,
        )
        self._ble_packet_unsubscribe = None
        self._ble_connection_unsubscribe = None
        self._ble_callback_transport = None
        for unsubscribe in callbacks:
            if unsubscribe is None:
                continue
            try:
                unsubscribe()
            except Exception as err:
                self._logger.debug(
                    "Failed to unsubscribe Meshtastic Bluetooth callback: %s",
                    err,
                )

    def _subscribe_bluetooth_events(self, transport: Any) -> None:
        """Attach one exact transport's callbacks idempotently."""
        if (
            self._ble_callback_transport is transport
            and self._ble_packet_unsubscribe is not None
            and self._ble_connection_unsubscribe is not None
        ):
            return
        # A partial registration is never useful and can duplicate delivery on
        # retry. Remove it before rebuilding the pair.
        self._unsubscribe_bluetooth_events()

        async def packet_handler(packet: dict[str, Any]) -> None:
            if self._ble_transport is not transport or self._stopping:
                return
            await self._handle_native_packet(packet)

        async def connection_handler(connected: bool) -> None:
            if self._ble_transport is not transport or self._stopping:
                return
            if self.status.connected != connected:
                await self._set_connected(connected)

        try:
            add_packet_callback = getattr(transport, "add_packet_callback", None)
            if not callable(add_packet_callback):
                raise RuntimeError(
                    "Meshtastic Bluetooth transport has no packet callback API"
                )
            packet_unsubscribe = add_packet_callback(packet_handler)
            if not callable(packet_unsubscribe):
                raise RuntimeError(
                    "Meshtastic Bluetooth packet callback has no remover"
                )
            self._ble_packet_unsubscribe = packet_unsubscribe
            add_connection_callback = getattr(
                transport, "add_connection_callback", None
            )
            if not callable(add_connection_callback):
                raise RuntimeError(
                    "Meshtastic Bluetooth transport has no connection callback API"
                )
            connection_unsubscribe = add_connection_callback(connection_handler)
            if not callable(connection_unsubscribe):
                raise RuntimeError(
                    "Meshtastic Bluetooth connection callback has no remover"
                )
            self._ble_connection_unsubscribe = connection_unsubscribe
            self._ble_callback_transport = transport
        except BaseException:
            self._unsubscribe_bluetooth_events()
            raise

    async def _async_cancel_bluetooth_operations(
        self,
    ) -> set[asyncio.Task[Any]]:
        """Cancel BLE operations without allowing an OS await to hang unload."""
        current = asyncio.current_task()
        tasks = {
            task
            for task in self._ble_operation_tasks
            if task is not current and not task.done()
        }
        for task in tasks:
            task.cancel()
        if not tasks:
            return set()
        _done, pending = await asyncio.wait(
            tasks,
            timeout=_STOP_WAIT_TIMEOUT,
        )
        return set(pending)

    def _schedule_deferred_bluetooth_cleanup(
        self,
        transport: Any,
        pending_operations: set[asyncio.Task[Any]],
    ) -> None:
        """Retry teardown after cancellation-resistant BlueZ work yields."""
        existing = self._ble_deferred_cleanup_task
        if existing is not None and not existing.done():
            return

        async def cleanup_after_operations() -> None:
            await asyncio.gather(*pending_operations, return_exceptions=True)
            if self._ble_transport is not transport:
                return
            try:
                await transport.async_stop()
            except Exception as err:
                # Keep both the transport and endpoint lease. A later explicit
                # stop can retry; releasing either would permit two GATT owners.
                self._logger.debug(
                    "Deferred Meshtastic Bluetooth cleanup failed: %s",
                    err,
                )
                return
            if self._ble_transport is transport:
                self._ble_transport = None
            self._release_native_lock()
            await self._set_connected(False)

        cleanup_task = self._async_create_background_task(
            cleanup_after_operations(),
            "MeshNet deferred Meshtastic Bluetooth cleanup",
        )
        self._ble_deferred_cleanup_task = cleanup_task

        def cleanup_done(done_task: asyncio.Task[Any]) -> None:
            if self._ble_deferred_cleanup_task is done_task:
                self._ble_deferred_cleanup_task = None
            if not done_task.cancelled():
                done_task.exception()

        cleanup_task.add_done_callback(cleanup_done)

    async def _async_run_bluetooth_operation(self, operation: Any) -> Any:
        """Retain one caller-owned async BLE operation until it is finished."""
        task = asyncio.current_task()
        if task is not None:
            self._ble_operation_tasks.add(task)
        try:
            return await operation
        finally:
            if task is not None:
                self._ble_operation_tasks.discard(task)

    async def async_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
    ) -> str:
        """Send a Meshtastic text message."""
        message_id = hashlib.sha256(
            f"{self.config.gateway_id}:{target_node}:{channel}:{message}:{utcnow().timestamp()}".encode()
        ).hexdigest()[:16]
        if self.config.transport == TRANSPORT_MQTT:
            await self._mqtt_publish_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
                message_id=message_id,
            )
        elif self.config.transport == TRANSPORT_BLUETOOTH:
            transport = self._ble_transport
            if transport is None:
                raise RuntimeError("Meshtastic Bluetooth is not connected")
            await self._async_run_bluetooth_operation(
                transport.async_send_text(
                    target_node=target_node,
                    message=message,
                    channel=channel,
                    priority=priority,
                    message_type=message_type,
                )
            )
        else:
            interface = self._interface
            if interface is None:
                raise RuntimeError("Meshtastic interface is not connected")
            destination = target_node if target_node else "^all"
            kwargs: dict[str, Any] = {}
            if channel is not None:
                kwargs["channelIndex"] = coerce_int(channel) or 0
            await self._async_run_native_executor(
                interface,
                lambda: interface.sendText(
                    message,
                    destinationId=destination,
                    **kwargs,
                ),
                name=f"MeshNet Meshtastic message send {self.config.gateway_id}",
            )
        self.status.packets_sent += 1
        await self._emit_status()
        return message_id

    async def async_refresh(self) -> None:
        """Refresh node DB from the native interface."""
        if self.config.transport == TRANSPORT_BLUETOOTH:
            transport = self._ble_transport
            if transport is None:
                return
            nodes = await self._async_run_bluetooth_operation(
                transport.async_node_snapshot()
            )
            for node_id, node in nodes.items():
                normalized = meshtastic_node_to_state(
                    node,
                    gateway_id=self.config.gateway_id,
                    fallback_node_id=node_id,
                )
                await self._emit_node(normalized)
            return

        interface = self._interface
        if interface is None:
            return
        nodes = await self._async_run_native_executor(
            interface,
            lambda: dict(interface.nodes),
            name=f"MeshNet Meshtastic node refresh {self.config.gateway_id}",
        )
        for node_id, node in nodes.items():
            normalized = meshtastic_node_to_state(
                node,
                gateway_id=self.config.gateway_id,
                fallback_node_id=node_id,
            )
            await self._emit_node(normalized)

    async def _start_native(self) -> None:
        if self.config.transport == TRANSPORT_BLUETOOTH:
            self._set_startup_phase("validating_bluetooth_adapter")
            self._bluetooth_adapter_validation = {"status": "pending"}
            try:
                adapter_summary, adapter = await _async_validate_ble_adapter(
                    self.config
                )
            except Exception as err:
                self._bluetooth_adapter_validation = {
                    "status": "failed",
                    "failure_category": _bluetooth_adapter_failure_category(err),
                    "exception_type": type(err).__name__,
                }
                if not self._stopping:
                    await self._emit_error(err)
                raise
            self._bluetooth_adapter_validation = {
                "status": "passed",
                **adapter_summary,
            }
            if not self._stopping:
                await self._start_bluetooth(adapter)
            return

        await self._start_sync_native()

    async def _start_sync_native(self) -> None:
        """Start the legacy synchronous SDK for serial and TCP only."""
        try:
            from pubsub import pub
        except ImportError as err:
            await self._emit_error(
                "pypubsub is unavailable; Home Assistant must install meshtastic requirements"
            )
            raise err

        def receive_handler(packet: dict[str, Any], interface: Any = None) -> None:
            if not self._owns_interface(interface):
                return
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._handle_native_packet(packet))
            )

        def connect_handler(interface: Any, topic: Any = None) -> None:
            if not self._owns_interface(interface):
                return
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._set_connected(True))
            )

        def disconnect_handler(interface: Any, topic: Any = None) -> None:
            if not self._owns_interface(interface):
                return
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._set_connected(False))
            )

        self._set_startup_phase("waiting_for_endpoint_lock")
        native_lock = _native_endpoint_lock(self._native_endpoint())
        await native_lock.acquire()
        self._native_lock = native_lock
        if self._stopping:
            self._release_native_lock()
            return

        self._set_startup_phase("constructing_interface")
        self._native_constructor_abandoned = False
        constructor = asyncio.ensure_future(
            self.hass.async_add_executor_job(self._make_native_interface)
        )
        if isinstance(constructor, asyncio.Task):
            constructor.set_name("MeshNet Meshtastic native interface constructor")
        self._native_constructor_future = constructor

        def constructor_done(done_future: asyncio.Future[Any]) -> None:
            if not done_future.cancelled():
                done_future.exception()
            self._schedule_abandoned_constructor_cleanup(done_future)

        constructor.add_done_callback(constructor_done)
        try:
            interface = await asyncio.shield(constructor)
        except asyncio.CancelledError:
            if not self._native_constructor_abandoned:
                self._native_constructor_abandonment_count += 1
            self._native_constructor_abandoned = True
            self._schedule_abandoned_constructor_cleanup(constructor)
            raise
        except Exception as err:
            if self._native_constructor_future is constructor:
                self._native_constructor_future = None
            self._release_native_lock()
            if not self._stopping:
                await self._emit_error(err)
            raise
        if self._native_constructor_future is constructor:
            self._native_constructor_future = None

        if self._stopping:
            await self._async_close_interface(interface, release_native_lock=True)
            return

        self._interface = interface
        self._pub = pub
        self._receive_handler = receive_handler
        self._connect_handler = connect_handler
        self._disconnect_handler = disconnect_handler
        self._set_startup_phase("subscribing_native_events")
        try:
            pub.subscribe(receive_handler, "meshtastic.receive")
            pub.subscribe(connect_handler, "meshtastic.connection.established")
            pub.subscribe(disconnect_handler, "meshtastic.connection.lost")
        except Exception as err:
            self._unsubscribe_native_events()
            self._interface = None
            await self._async_close_interface(interface, release_native_lock=True)
            if not self._stopping:
                await self._emit_error(err)
            raise
        try:
            self._set_startup_phase("marking_connected")
            await self._set_connected(True)
            if self._stopping:
                return
            self._set_startup_phase("refreshing_nodes")
            await self.async_refresh()
        except BaseException:
            if self._interface is interface:
                self._unsubscribe_native_events()
                self._interface = None
                await self._async_close_interface(interface, release_native_lock=True)
            raise

    async def _start_bluetooth(self, adapter: str) -> None:
        """Start the bounded asyncio BLE transport on one verified controller."""
        # Construction imports protobuf code but does not touch Bluetooth. Do
        # it before taking the process endpoint lease so a dependency or API
        # error cannot strand a locked endpoint.
        transport = self._make_bluetooth_transport(adapter)
        self._set_startup_phase("waiting_for_endpoint_lock")
        native_lock = _native_endpoint_lock(self._native_endpoint())
        await native_lock.acquire()
        self._native_lock = native_lock
        if self._stopping:
            self._release_native_lock()
            return

        self._ble_transport = transport

        try:
            self._subscribe_bluetooth_events(transport)
            await transport.async_start()
            if self._stopping:
                await self._async_cleanup_transport(emit_status=False)
                return
            if not self.status.connected:
                await self._set_connected(True)
            self._set_startup_phase("refreshing_nodes")
            await self.async_refresh()
        except BaseException:
            self._unsubscribe_bluetooth_events()
            try:
                await transport.async_stop()
            except Exception as cleanup_error:
                self._ble_transport = transport
                self._logger.debug(
                    "Failed to stop Meshtastic Bluetooth after startup failure: %s",
                    cleanup_error,
                )
            else:
                if self._ble_transport is transport:
                    self._ble_transport = None
                self._release_native_lock()
            raise

    async def _resume_bluetooth_transport(self) -> None:
        """Rejoin or restart the existing persistent BLE transport safely."""
        deferred_cleanup = self._ble_deferred_cleanup_task
        if deferred_cleanup is not None and not deferred_cleanup.done():
            raise RuntimeError(
                "Meshtastic Bluetooth cleanup is still pending; retry startup later"
            )
        transport = self._ble_transport
        if transport is None:
            raise RuntimeError("Meshtastic Bluetooth transport is unavailable")
        if not bool(getattr(transport, "connected", False)):
            self._set_startup_phase("resuming_bluetooth")
            await transport.async_start()
        self._subscribe_bluetooth_events(transport)
        if self._stopping:
            await self._async_cleanup_transport(emit_status=False)
            return
        if not self.status.connected:
            await self._set_connected(True)
        self._set_startup_phase("refreshing_nodes")
        await self.async_refresh()

    def _make_bluetooth_transport(self, adapter: str) -> Any:
        """Construct the local-only async BLE adapter lazily."""
        from .meshtastic_ble import MeshtasticBluetoothTransport

        if not self.config.ble_address:
            raise RuntimeError("Bluetooth transport requires ble_address")
        adapter_address = self.config.options.get(CONF_BLUETOOTH_ADAPTER_ADDRESS)
        if not isinstance(adapter_address, str):
            raise RuntimeError("Bluetooth setup has no verified local adapter")
        return MeshtasticBluetoothTransport(
            self.hass,
            address=self.config.ble_address,
            adapter=adapter,
            adapter_address=adapter_address,
            logger=self._logger,
            phase_callback=self._set_startup_phase,
        )

    def _make_native_interface(self) -> Any:
        if self.config.transport == TRANSPORT_SERIAL:
            import meshtastic.serial_interface

            return meshtastic.serial_interface.SerialInterface(self.config.serial_path)
        if self.config.transport == TRANSPORT_TCP:
            import meshtastic.tcp_interface

            host = self.config.host or self.config.api_url
            if not host:
                raise RuntimeError("TCP transport requires host")
            if self.config.port:
                return meshtastic.tcp_interface.TCPInterface(host, portNumber=self.config.port)
            return meshtastic.tcp_interface.TCPInterface(host)
        if self.config.transport == TRANSPORT_BLUETOOTH:
            raise RuntimeError(
                "Bluetooth uses MeshNet's bounded asynchronous transport"
            )
        raise RuntimeError(f"Unsupported Meshtastic transport: {self.config.transport}")

    async def _start_mqtt(self) -> None:
        try:
            from homeassistant.components import mqtt
        except ImportError as err:
            await self._emit_error("Home Assistant MQTT integration is unavailable")
            raise err

        topic = self.config.mqtt_topic or DEFAULT_MESHTASTIC_MQTT_TOPIC

        async def message_received(msg: Any) -> None:
            try:
                raw_payload = msg.payload
                if isinstance(raw_payload, bytes):
                    raw_payload = raw_payload.decode(errors="replace")
                raw = json.loads(raw_payload)
            except Exception as err:
                await self._emit_error(f"invalid Meshtastic MQTT payload on {msg.topic}: {err}")
                return
            packet = meshtastic_packet_to_state_packet(
                raw,
                gateway_id=self.config.gateway_id,
                topic=msg.topic,
            )
            await self._handle_packet(packet)

        unsubscribe = await mqtt.async_subscribe(self.hass, topic, message_received, 0)
        if self._stopping:
            try:
                unsubscribe()
            except Exception as err:
                self._logger.debug("Failed to unsubscribe late Meshtastic MQTT handler: %s", err)
            return
        self._unsub_mqtt = unsubscribe
        await self._set_connected(True, mqtt_topic=topic)

    async def _mqtt_publish_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
        message_id: str,
    ) -> None:
        publish_topic = str(self.config.options.get("publish_topic") or "").strip()
        if not publish_topic:
            raise RuntimeError(
                "Meshtastic MQTT sending requires options.publish_topic "
                "(for example msh/US/2/json/mqtt/)"
            )
        if "#" in publish_topic or "+" in publish_topic:
            raise RuntimeError("Meshtastic MQTT publish_topic cannot contain wildcards")
        mqtt_node_id = _meshtastic_node_number(self.config.options.get("mqtt_node_id"))
        if mqtt_node_id is None:
            raise RuntimeError("Meshtastic MQTT sending requires options.mqtt_node_id")

        from homeassistant.components import mqtt

        payload = {
            "from": mqtt_node_id,
            "type": "sendtext",
            "payload": message,
        }
        if target_node:
            destination = _meshtastic_node_number(target_node)
            if destination is None:
                raise RuntimeError(f"Invalid Meshtastic MQTT target node: {target_node}")
            payload["to"] = destination
        if channel is not None:
            channel_index = coerce_int(channel)
            if channel_index is None or not 0 <= channel_index <= 7:
                raise RuntimeError(f"Invalid Meshtastic MQTT channel: {channel}")
            payload["channel"] = channel_index
        await mqtt.async_publish(
            self.hass,
            publish_topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )

    async def _handle_native_packet(self, raw: dict[str, Any]) -> None:
        packet = meshtastic_packet_to_state_packet(raw, gateway_id=self.config.gateway_id)
        await self._handle_packet(packet)

    async def _handle_packet(self, packet: MeshPacket) -> None:
        await self._emit_packet(packet)
        node = meshtastic_packet_to_node(packet)
        if node:
            await self._emit_node(node)


def meshtastic_packet_to_state_packet(
    raw: dict[str, Any],
    *,
    gateway_id: str,
    topic: str | None = None,
) -> MeshPacket:
    """Normalize a Meshtastic packet dict or MQTT JSON payload."""
    decoded = raw.get("decoded") if isinstance(raw.get("decoded"), dict) else {}
    data = decoded.get("data") if isinstance(decoded.get("data"), dict) else {}
    telemetry = decoded.get("telemetry") if isinstance(decoded.get("telemetry"), dict) else {}
    mqtt_payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    payload = raw.get("payload", decoded.get("payload", data.get("payload")))
    text = (
        raw.get("text")
        or decoded.get("text")
        or data.get("text")
        or telemetry.get("text")
        or mqtt_payload.get("text")
    )
    if isinstance(payload, bytes):
        payload = payload.hex()
    packet_time = parse_timestamp(raw.get("rxTime") or raw.get("timestamp") or raw.get("time")) or utcnow()
    channel_value = raw.get("channel")
    if channel_value is None:
        channel_value = decoded.get("channel")
    return MeshPacket(
        protocol=PROTOCOL_MESHTASTIC,
        gateway_id=gateway_id,
        packet_id=str(raw.get("id") or raw.get("packet_id") or "") or None,
        sender=str(raw.get("fromId") or raw.get("from") or raw.get("from_num") or "") or None,
        receiver=str(raw.get("toId") or raw.get("to") or raw.get("to_num") or "") or None,
        channel=str(channel_value) if channel_value is not None else None,
        portnum=str(
            decoded.get("portnum")
            or decoded.get("portnumName")
            or raw.get("portnum")
            or raw.get("type")
            or ""
        )
        or None,
        payload=payload,
        text=text,
        encrypted=bool(raw.get("encrypted")) if "encrypted" in raw else None,
        rssi=coerce_float(raw.get("rxRssi") or raw.get("rssi")),
        snr=coerce_float(raw.get("rxSnr") or raw.get("snr")),
        hops=coerce_int(raw.get("hopsAway") or raw.get("hops")),
        hop_limit=coerce_int(raw.get("hopLimit") or raw.get("hop_limit")),
        timestamp=packet_time,
        raw={**raw, **({"topic": topic} if topic else {})},
    )


def meshtastic_node_to_state(
    raw: dict[str, Any],
    *,
    gateway_id: str,
    fallback_node_id: str | None = None,
) -> NodeState:
    """Normalize Meshtastic node DB entries into NodeState."""
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    position = raw.get("position") if isinstance(raw.get("position"), dict) else {}
    device_metrics = raw.get("deviceMetrics") if isinstance(raw.get("deviceMetrics"), dict) else {}
    node_id = str(user.get("id") or raw.get("id") or raw.get("num") or fallback_node_id or "") or None
    mac = _normalize_meshtastic_mac(user.get("macaddr") or user.get("mac"))
    node_key = canonical_node_key(PROTOCOL_MESHTASTIC, node_id=node_id, mac=mac)
    last_heard = parse_timestamp(raw.get("lastHeard")) or parse_timestamp(raw.get("last_heard"))
    if last_heard is None and isinstance(raw.get("lastHeard"), (int, float)):
        last_heard = datetime.fromtimestamp(raw["lastHeard"], tz=UTC)
    sensors = {}
    for source_key in ("environmentMetrics", "airQualityMetrics", "powerMetrics"):
        source = raw.get(source_key)
        if isinstance(source, dict):
            sensors.update(_flatten_metrics(source))
    return NodeState(
        node_key=node_key,
        protocol=PROTOCOL_MESHTASTIC,
        node_id=node_id,
        mac=mac,
        user_name=user.get("userName") or user.get("name"),
        long_name=user.get("longName"),
        short_name=user.get("shortName"),
        hardware_model=user.get("hwModel") or raw.get("hwModel"),
        firmware_version=raw.get("firmwareVersion") or raw.get("firmware_version"),
        role=raw.get("role"),
        online=True,
        last_heard=last_heard or utcnow(),
        last_gateway_id=gateway_id,
        gateway_ids={gateway_id},
        connectivity={
            "snr": coerce_float(raw.get("snr")),
            "rssi": coerce_float(raw.get("rssi")),
            "channel_utilization": coerce_float(device_metrics.get("channelUtilization")),
            "air_utilization": coerce_float(device_metrics.get("airUtilTx")),
        },
        power={
            "battery_level": coerce_float(device_metrics.get("batteryLevel")),
            "voltage": coerce_float(device_metrics.get("voltage")),
        },
        location={
            "latitude": coerce_float(position.get("latitude")),
            "longitude": coerce_float(position.get("longitude")),
            "altitude": coerce_float(position.get("altitude")),
            "speed": coerce_float(position.get("groundSpeed")),
            "heading": coerce_float(position.get("groundTrack")),
            "precision": coerce_float(position.get("precisionBits")),
        },
        sensors=sensors,
        raw=raw,
    )


def meshtastic_packet_to_node(packet: MeshPacket) -> NodeState | None:
    """Derive a node update from a Meshtastic packet."""
    raw = packet.raw
    decoded = raw.get("decoded") if isinstance(raw.get("decoded"), dict) else {}
    telemetry = decoded.get("telemetry") if isinstance(decoded.get("telemetry"), dict) else {}
    user = decoded.get("user") if isinstance(decoded.get("user"), dict) else {}
    position = decoded.get("position") if isinstance(decoded.get("position"), dict) else {}
    mqtt_payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    if str(raw.get("type") or "").lower() == "nodeinfo" and mqtt_payload:
        user = {
            "id": mqtt_payload.get("id"),
            "userName": mqtt_payload.get("username"),
            "longName": mqtt_payload.get("longname") or mqtt_payload.get("long_name"),
            "shortName": mqtt_payload.get("shortname") or mqtt_payload.get("short_name"),
            "hwModel": mqtt_payload.get("hardware") or mqtt_payload.get("hw_model"),
        }
    if not packet.sender and not user and not position and not telemetry:
        return None
    mac = _normalize_meshtastic_mac(user.get("macaddr") or user.get("mac"))
    node_id = str(user.get("id") or packet.sender or "") or None
    node_key = canonical_node_key(PROTOCOL_MESHTASTIC, node_id=node_id, mac=mac)
    sensors: dict[str, Any] = {}
    power: dict[str, Any] = {}
    connectivity = {
        "snr": packet.snr,
        "rssi": packet.rssi,
        "hops": packet.hops,
        "hop_limit": packet.hop_limit,
    }
    for key in ("deviceMetrics", "environmentMetrics", "airQualityMetrics", "powerMetrics"):
        metrics = telemetry.get(key)
        if isinstance(metrics, dict):
            flattened = _flatten_metrics(metrics)
            sensors.update(flattened)
            if key == "deviceMetrics":
                power.update(
                    {
                        "battery_level": coerce_float(metrics.get("batteryLevel")),
                        "voltage": coerce_float(metrics.get("voltage")),
                    }
                )
                connectivity.update(
                    {
                        "channel_utilization": coerce_float(metrics.get("channelUtilization")),
                        "air_utilization": coerce_float(metrics.get("airUtilTx")),
                    }
                )
    return NodeState(
        node_key=node_key,
        protocol=PROTOCOL_MESHTASTIC,
        node_id=node_id,
        mac=mac,
        user_name=user.get("userName") or user.get("name"),
        long_name=user.get("longName"),
        short_name=user.get("shortName"),
        hardware_model=user.get("hwModel"),
        online=True,
        last_heard=packet.timestamp,
        last_gateway_id=packet.gateway_id,
        gateway_ids={packet.gateway_id},
        connectivity=connectivity,
        power=power,
        location={
            "latitude": coerce_float(position.get("latitude")),
            "longitude": coerce_float(position.get("longitude")),
            "altitude": coerce_float(position.get("altitude")),
            "speed": coerce_float(position.get("groundSpeed") or position.get("speed")),
            "heading": coerce_float(position.get("groundTrack") or position.get("heading")),
            "precision": coerce_float(position.get("precisionBits") or position.get("precision")),
        },
        sensors=sensors,
        raw=raw,
    )


def _flatten_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            flattened[_snake(key)] = value
    return flattened


def _normalize_meshtastic_mac(value: Any) -> str | None:
    """Return a stable hex MAC from SDK bytes or protobuf JSON base64.

    ``MessageToDict`` represents the protobuf ``User.macaddr`` bytes field as
    case-sensitive base64.  Passing that representation to the generic node-key
    helper would lowercase and corrupt it, so normalize valid six-byte values
    before deriving the public node key.  Existing textual identifiers that are
    not a Meshtastic MAC are left intact for backward compatibility.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(value)
        return raw_bytes.hex() if len(raw_bytes) == 6 else None
    if not isinstance(value, str):
        text = str(value).strip()
        return text or None

    text = value.strip()
    if not text:
        return None
    compact = text.replace(":", "").replace("-", "")
    if len(compact) == 12 and all(char in "0123456789abcdefABCDEF" for char in compact):
        return compact.lower()
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return text
    return decoded.hex() if len(decoded) == 6 else text


def _snake(value: str) -> str:
    out = []
    for char in value:
        if char.isupper() and out:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _meshtastic_node_number(value: Any) -> int | None:
    """Parse decimal, !hex, 0xhex, or canonical Meshtastic node IDs."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip()
        if text.lower().startswith("meshtastic:"):
            text = text.split(":", 1)[1]
        try:
            if text.startswith("!"):
                number = int(text[1:], 16)
            elif text.lower().startswith("0x"):
                number = int(text, 16)
            else:
                number = int(text, 10)
        except (TypeError, ValueError):
            return None
    return number if 0 <= number <= 0xFFFFFFFF else None
