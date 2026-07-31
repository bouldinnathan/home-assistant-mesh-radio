"""Gateway adapter base classes."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from .models import GatewayConfig, GatewayStatus, MeshPacket, NodeState, utcnow

PacketCallback = Callable[[MeshPacket], Awaitable[None]]
NodeCallback = Callable[[NodeState], Awaitable[None]]
StatusCallback = Callable[[GatewayStatus], Awaitable[None]]


def _safe_error_category(error: Exception | str) -> str:
    """Classify a failure without returning endpoint or credential text."""
    lowered = str(error).casefold()
    categories = (
        ("authentication", ("auth", "credential", "password", "pin", "token")),
        ("bluetooth", ("bluetooth", "bluez", "ble", "dbus", "gatt")),
        ("configuration", ("config", "invalid", "missing", "unsupported")),
        ("connection", ("connect", "socket", "network", "unreachable")),
        ("data", ("decode", "json", "parse", "payload", "protobuf")),
        ("permission", ("permission", "access denied", "read-only")),
        ("serial", ("serial", "tty", "baud")),
        ("timeout", ("timeout", "timed out")),
    )
    for category, markers in categories:
        if any(marker in lowered for marker in markers):
            return category
    return "other"


class GatewayError(RuntimeError):
    """Raised when a gateway operation fails."""


class MeshGateway(ABC):
    """Base class for gateway implementations."""

    def __init__(
        self,
        hass: Any,
        config: GatewayConfig,
        on_packet: PacketCallback,
        on_node: NodeCallback,
        on_status: StatusCallback,
        logger: logging.Logger,
    ) -> None:
        self.hass = hass
        self.config = config
        self._on_packet = on_packet
        self._on_node = on_node
        self._on_status = on_status
        self._logger = logger
        self.status = GatewayStatus(
            gateway_id=config.gateway_id,
            name=config.name,
            protocol=config.protocol,
            transport=config.transport,
        )

    def _async_create_background_task(
        self, target: Coroutine[Any, Any, Any], name: str
    ) -> asyncio.Task[Any]:
        """Create long-lived work without making it block HA startup."""
        creator = getattr(self.hass, "async_create_background_task", None)
        if callable(creator):
            return creator(target, name)
        # Lightweight test doubles and older embedding callers may expose only
        # the normal task API. Supported Home Assistant releases use the branch
        # above.
        return self.hass.async_create_task(target)

    @abstractmethod
    async def async_start(self) -> None:
        """Start the gateway connection."""

    @abstractmethod
    async def async_stop(self) -> None:
        """Stop the gateway connection."""

    @abstractmethod
    async def async_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
    ) -> str:
        """Send a mesh message and return a provider message id."""

    async def async_refresh(self) -> None:
        """Refresh gateway state if supported."""
        return None

    async def async_manual_traceroute(self, target_node: str) -> dict[str, Any]:
        """Run one explicitly requested traceroute when an adapter supports it."""
        del target_node
        raise GatewayError("This gateway does not support manual traceroute")

    async def async_manual_neighbor_info(
        self, target_node: str
    ) -> dict[str, Any]:
        """Run one explicitly requested NeighborInfo query when supported."""
        del target_node
        raise GatewayError("This gateway does not support manual NeighborInfo")

    async def async_get_settings_snapshot(self) -> dict[str, Any]:
        """Return a privacy-safe live settings schema for this gateway.

        Protocol adapters override this only when they can read settings from
        the physically connected radio.  Keeping the default read-only makes
        unsupported transports fail closed instead of exposing a generic raw
        command surface.
        """
        return {
            "categories": [],
            "warnings": [],
            "writable": False,
            "read_only_reason": (
                "This gateway transport does not provide a validated local "
                "settings interface."
            ),
        }

    async def async_apply_settings_plan(
        self, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply one already validated local settings plan.

        The protocol-neutral manager validates paths, types, ranges, stale
        revisions, and critical confirmation before this method can run.
        Adapters must still enforce their protocol and hardware constraints.
        """
        raise GatewayError(
            "This gateway transport does not support validated settings writes"
        )

    @staticmethod
    def _diagnostic_task_state(task: asyncio.Future[Any] | None) -> str:
        """Return a task state without inspecting or exposing its exception."""
        if task is None:
            return "not_created"
        if task.cancelled():
            return "cancelled"
        if task.done():
            return "finished"
        if isinstance(task, asyncio.Task) and task.cancelling():
            return "cancelling"
        return "pending"

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return cached client lifecycle state without endpoint identity."""
        return {
            "implementation": type(self).__name__,
        }

    async def _emit_packet(self, packet: MeshPacket) -> None:
        self.status.packets_received += 1
        self.status.last_packet = packet.timestamp
        await self._on_packet(packet)
        await self._emit_status()

    async def _emit_node(self, node: NodeState) -> None:
        await self._on_node(node)

    async def _set_connected(self, connected: bool, **detail: Any) -> None:
        self.status.connected = connected
        if connected:
            self.status.last_connected = utcnow()
        self.status.detail = {**self.status.detail, **detail}
        await self._emit_status()

    async def _emit_error(self, error: Exception | str) -> None:
        message = str(error)
        self.status.failure_count += 1
        self.status.last_failure_category = _safe_error_category(error)
        self.status.last_failure_at = utcnow()
        self.status.errors.append(message)
        self.status.errors = self.status.errors[-20:]
        self._logger.warning(
            "Mesh gateway adapter %s reported a %s failure",
            type(self).__name__,
            _safe_error_category(error),
        )
        await self._emit_status()

    async def _emit_status(self) -> None:
        await self._on_status(self.status)
