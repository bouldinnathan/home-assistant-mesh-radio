# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
# SPDX-FileCopyrightText: 2025 Hendrik @novag
# SPDX-FileCopyrightText: 2026 MeshNet contributors
#
# SPDX-License-Identifier: MIT

"""Bounded, asyncio-native Meshtastic Bluetooth byte transport.

This is an adapted, Bluetooth-only derivative of the ``aiomeshtastic``
transport vendored by ``meshtastic/home-assistant``.  It deliberately has no
MQTT, serial, TCP, pubsub, Home Assistant, or generated-protobuf dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

from .errors import (
    MeshtasticCleanupError,
    MeshtasticConnectionError,
    MeshtasticNotConnectedError,
)

MESHTASTIC_SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"

ClientConnector = Callable[[Any, Callable[[Any], None]], Awaitable[Any]]


class BluetoothConnection:
    """Own one Meshtastic GATT connection and its single FromRadio reader."""

    def __init__(
        self,
        *,
        address: str,
        ble_device: Any,
        connector: ClientConnector | None = None,
        connect_timeout: float = 30.0,
        notify_timeout: float = 10.0,
        io_timeout: float = 15.0,
        disconnect_timeout: float = 5.0,
        idle_read_timeout: float = 300.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._address = address
        self._ble_device = ble_device
        self._connector = connector or self._default_connector
        self._connect_timeout = connect_timeout
        self._notify_timeout = notify_timeout
        self._io_timeout = io_timeout
        self._disconnect_timeout = disconnect_timeout
        self._idle_read_timeout = idle_read_timeout
        self._logger = logger or logging.getLogger(__name__)

        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._client: Any | None = None
        self._from_radio: Any | None = None
        self._to_radio: Any | None = None
        self._from_num: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._read_wakeup = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._notifications_ready = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closing = False
        self._state = "idle"
        self._last_error_type: str | None = None
        self._last_failure_phase: str | None = None
        self._connect_count = 0
        self._disconnect_count = 0
        self._notification_count = 0
        self._read_count = 0
        self._write_count = 0
        self._forced_read_count = 0
        self._notify_restart_count = 0

    @property
    def is_connected(self) -> bool:
        """Return whether the underlying GATT client still owns a live link."""
        client = self._client
        return bool(client is not None and getattr(client, "is_connected", False))

    @property
    def owns_endpoint(self) -> bool:
        """Return whether GATT ownership or its cleanup task remains live."""
        cleanup_task = self._cleanup_task
        return bool(
            self.is_connected
            or (cleanup_task is not None and not cleanup_task.done())
        )

    async def async_connect(self) -> None:
        """Connect, validate the Meshtastic profile, and arm FromNum."""
        async with self._lifecycle_lock:
            if self.is_connected and self._notifications_ready:
                return
            self._closing = False
            self._last_error_type = None
            self._last_failure_phase = None
            self._loop = asyncio.get_running_loop()
            self._read_wakeup.clear()
            self._disconnected.clear()
            self._state = "connecting"
            client: Any | None = None
            try:
                async with asyncio.timeout(self._connect_timeout):
                    client = await self._connector(self._ble_device, self._disconnected_callback)
                self._client = client
                self._state = "validating_profile"
                self._bind_characteristics(client)
                self._state = "enabling_notifications"
                async with asyncio.timeout(self._notify_timeout):
                    await client.start_notify(self._from_num, self._notification_handler)
                self._notifications_ready = True
                self._connect_count += 1
                self._state = "connected"
            except BaseException as err:
                if not isinstance(err, asyncio.CancelledError):
                    self._last_error_type = type(err).__name__
                    self._last_failure_phase = self._state
                self._state = "connect_cleanup"
                cleanup_error = await self._cleanup_client(client)
                still_connected = bool(
                    client is not None and getattr(client, "is_connected", False)
                )
                if still_connected or self.owns_endpoint:
                    # Keep the live client and characteristics reachable so a
                    # later stop can retry.  Forgetting it could let the caller
                    # open a second GATT owner for the same radio.
                    self._client = client
                    self._state = "cleanup_incomplete"
                else:
                    self._clear_runtime_references()
                    self._state = "failed"
                if isinstance(err, asyncio.CancelledError):
                    raise
                if isinstance(err, MeshtasticConnectionError):
                    raise
                detail = f"; cleanup: {type(cleanup_error).__name__}" if cleanup_error else ""
                raise MeshtasticConnectionError(f"Meshtastic Bluetooth connection failed: {type(err).__name__}{detail}") from err

    async def async_disconnect(self) -> None:
        """Stop notifications and disconnect within one total cleanup bound."""
        async with self._lifecycle_lock:
            self._closing = True
            self._state = "disconnecting"
            client = self._client
            cleanup_error = await self._cleanup_client(client)
            still_connected = bool(client is not None and getattr(client, "is_connected", False))
            self._disconnect_count += 1
            if still_connected or self.owns_endpoint:
                # Retain all ownership references.  The caller must not release
                # its endpoint lease or reconnect until a later retry confirms
                # that this client is no longer connected.
                self._state = "cleanup_incomplete"
                error = MeshtasticCleanupError(
                    "Meshtastic Bluetooth disconnect was not confirmed within the cleanup timeout"
                )
                if cleanup_error is not None:
                    error.__cause__ = cleanup_error
                raise error
            self._clear_runtime_references()
            self._state = "disconnected"

    async def async_send(self, payload: bytes, *, force_read: bool = False) -> None:
        """Write one serialized ToRadio record and wake the active reader."""
        if not payload:
            return
        async with self._write_lock:
            client = self._client
            characteristic = self._to_radio
            if not self.is_connected or self._closing or characteristic is None:
                raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not connected")
            try:
                async with asyncio.timeout(self._io_timeout):
                    try:
                        await client.write_gatt_char(characteristic, payload, response=True)
                    except TypeError:
                        # Test doubles and older backends may not expose the keyword.
                        await client.write_gatt_char(characteristic, payload)
            except BaseException as err:
                if isinstance(err, asyncio.CancelledError):
                    raise
                self._last_error_type = type(err).__name__
                self._last_failure_phase = "writing_to_radio"
                raise MeshtasticConnectionError(f"Meshtastic Bluetooth write failed: {type(err).__name__}") from err
            self._write_count += 1
            if force_read:
                # Meshtastic firmware does not reliably emit FromNum after a
                # want_config write.  Wake only after the write has completed,
                # avoiding the pre-write race in the original implementation.
                self._forced_read_count += 1
                self._read_wakeup.set()

    async def packet_stream(self) -> AsyncIterator[bytes]:
        """Yield serialized FromRadio records until disconnect or cancellation."""
        if not self.is_connected or self._closing:
            raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not connected")
        notify_timeout_count = 0
        while self.is_connected and not self._closing:
            payload = await self._async_read()
            if payload:
                notify_timeout_count = 0
                yield payload
                continue

            outcome = await self._wait_for_read_request()
            if outcome == "disconnected":
                raise MeshtasticConnectionError("Meshtastic Bluetooth disconnected")
            if outcome == "timeout":
                notify_timeout_count += 1
                if notify_timeout_count > 2:
                    notify_timeout_count = 0
                    await self._restart_notifications()
            else:
                notify_timeout_count = 0

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return cached, endpoint-free transport diagnostics."""
        return {
            "state": self._state,
            "connected": self.is_connected,
            "owns_endpoint": self.owns_endpoint,
            "notifications_ready": self._notifications_ready,
            "closing": self._closing,
            "connect_count": self._connect_count,
            "disconnect_count": self._disconnect_count,
            "notification_count": self._notification_count,
            "read_count": self._read_count,
            "write_count": self._write_count,
            "forced_read_count": self._forced_read_count,
            "notify_restart_count": self._notify_restart_count,
            "cleanup_task": self._task_state(self._cleanup_task),
            "last_error_type": self._last_error_type,
            "last_failure_phase": self._last_failure_phase,
            "timeouts": {
                "connect": self._connect_timeout,
                "notify": self._notify_timeout,
                "io": self._io_timeout,
                "disconnect": self._disconnect_timeout,
                "idle_read": self._idle_read_timeout,
            },
        }

    async def _async_read(self) -> bytes:
        client = self._client
        characteristic = self._from_radio
        if not self.is_connected or self._closing or characteristic is None:
            raise MeshtasticNotConnectedError("Meshtastic Bluetooth is not connected")
        try:
            async with asyncio.timeout(self._io_timeout):
                payload = await client.read_gatt_char(characteristic)
        except BaseException as err:
            if isinstance(err, asyncio.CancelledError):
                raise
            self._last_error_type = type(err).__name__
            self._last_failure_phase = "reading_from_radio"
            raise MeshtasticConnectionError(f"Meshtastic Bluetooth read failed: {type(err).__name__}") from err
        self._read_count += 1
        return bytes(payload)

    async def _wait_for_read_request(self) -> str:
        wake_task = asyncio.create_task(self._read_wakeup.wait(), name="meshtastic-wait-read")
        disconnect_task = asyncio.create_task(self._disconnected.wait(), name="meshtastic-wait-disconnect")
        try:
            done, pending = await asyncio.wait(
                {wake_task, disconnect_task},
                timeout=self._idle_read_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done and self._disconnected.is_set():
                return "disconnected"
            if wake_task in done:
                self._read_wakeup.clear()
                return "wakeup"
            return "timeout"
        finally:
            for task in (wake_task, disconnect_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake_task, disconnect_task, return_exceptions=True)

    async def _restart_notifications(self) -> None:
        client = self._client
        characteristic = self._from_num
        if not self.is_connected or characteristic is None:
            return
        self._state = "restarting_notifications"
        try:
            async with asyncio.timeout(self._notify_timeout):
                with suppress(Exception):
                    await client.stop_notify(characteristic)
                await client.start_notify(characteristic, self._notification_handler)
        except BaseException as err:
            if isinstance(err, asyncio.CancelledError):
                raise
            self._last_error_type = type(err).__name__
            self._last_failure_phase = "restarting_notifications"
            raise MeshtasticConnectionError(
                f"Meshtastic Bluetooth notification restart failed: {type(err).__name__}"
            ) from err
        self._notify_restart_count += 1
        self._state = "connected"

    def _bind_characteristics(self, client: Any) -> None:
        services = getattr(client, "services", None)
        service_getter = getattr(services, "get_service", None)
        characteristic_getter = getattr(services, "get_characteristic", None)
        if service_getter is not None and service_getter(MESHTASTIC_SERVICE_UUID) is None:
            raise MeshtasticConnectionError("Meshtastic Bluetooth service was not found")
        if characteristic_getter is None:
            raise MeshtasticConnectionError("Bluetooth returned no usable GATT service collection")
        self._from_radio = characteristic_getter(FROMRADIO_UUID)
        self._to_radio = characteristic_getter(TORADIO_UUID)
        self._from_num = characteristic_getter(FROMNUM_UUID)
        missing = [
            label
            for label, value in (
                ("FromRadio", self._from_radio),
                ("ToRadio", self._to_radio),
                ("FromNum", self._from_num),
            )
            if value is None
        ]
        if missing:
            raise MeshtasticConnectionError("Meshtastic GATT profile is missing " + ", ".join(missing))

    def _notification_handler(self, _sender: Any, data: bytearray) -> None:
        if self._closing:
            return
        if len(data) >= 4:
            with suppress(struct.error):
                struct.unpack("<I", bytes(data[:4]))
        self._notification_count += 1
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._read_wakeup.set)

    def _disconnected_callback(self, client: Any) -> None:
        if client is not self._client:
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._mark_disconnected, client)

    def _mark_disconnected(self, client: Any) -> None:
        if client is not self._client:
            return
        self._notifications_ready = False
        self._disconnected.set()
        self._read_wakeup.set()

    async def _cleanup_client(self, client: Any | None) -> BaseException | None:
        if client is None:
            return None

        async def cleanup() -> None:
            if self._notifications_ready and self._from_num is not None:
                with suppress(Exception):
                    await client.stop_notify(self._from_num)
            if getattr(client, "is_connected", False):
                await client.disconnect()

        cleanup_task = self._cleanup_task
        if cleanup_task is not None and cleanup_task.done():
            try:
                cleanup_task.result()
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_task = None
            cleanup_task = None
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(
                cleanup(),
                name="meshnet-aiomeshtastic-gatt-cleanup",
            )
            self._cleanup_task = cleanup_task
        try:
            done, _ = await asyncio.wait(
                {cleanup_task},
                timeout=self._disconnect_timeout,
            )
            if cleanup_task not in done:
                cleanup_task.cancel()
                # Some platform BLE calls have historically delayed or ignored
                # cancellation.  Give cancellation a small bounded turn, then
                # retain the task and client so unload cannot hang or falsely
                # report a released endpoint.
                await asyncio.wait(
                    {cleanup_task},
                    timeout=min(0.25, self._disconnect_timeout),
                )
                error = TimeoutError("Bluetooth cleanup exceeded its safety timeout")
                self._last_error_type = type(error).__name__
                return error
            try:
                cleanup_task.result()
            except asyncio.CancelledError:
                error = TimeoutError("Bluetooth cleanup was cancelled before confirmation")
                self._last_error_type = type(error).__name__
                return error
            except BaseException as err:
                self._last_error_type = type(err).__name__
                return err
            if self._cleanup_task is cleanup_task:
                self._cleanup_task = None
            return None
        except asyncio.CancelledError:
            # A second cancellation must not orphan the cleanup child.  Cancel
            # and await it, then retain the GATT references in the caller when
            # disconnection cannot be confirmed.
            if not cleanup_task.done():
                cleanup_task.cancel()
            await asyncio.wait(
                {cleanup_task},
                timeout=min(0.25, self._disconnect_timeout),
            )
            raise

    def _clear_runtime_references(self) -> None:
        self._client = None
        self._from_radio = None
        self._to_radio = None
        self._from_num = None
        self._notifications_ready = False
        self._read_wakeup.set()
        self._disconnected.set()
        self._ble_device = None

    @staticmethod
    def _task_state(task: asyncio.Task[Any] | None) -> str:
        if task is None:
            return "not_created"
        if task.cancelled():
            return "cancelled"
        if not task.done():
            return "pending"
        return "failed" if task.exception() is not None else "finished"

    async def _default_connector(self, ble_device: Any, disconnected_callback: Callable[[Any], None]) -> Any:
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection

        kwargs: dict[str, Any] = {
            "client_class": BleakClient,
            "device": ble_device,
            "name": "MeshNet Meshtastic radio",
            "max_attempts": 2,
            "disconnected_callback": disconnected_callback,
        }
        with suppress(TypeError, ValueError):
            parameters = inspect.signature(establish_connection).parameters
            if "use_services_cache" in parameters:
                kwargs["use_services_cache"] = False
        return await establish_connection(**kwargs)
