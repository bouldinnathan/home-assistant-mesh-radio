"""Gateway adapter base classes."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from .models import GatewayConfig, GatewayStatus, MeshPacket, NodeState, utcnow

PacketCallback = Callable[[MeshPacket], Awaitable[None]]
NodeCallback = Callable[[NodeState], Awaitable[None]]
StatusCallback = Callable[[GatewayStatus], Awaitable[None]]


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
        self.status.errors.append(message)
        self.status.errors = self.status.errors[-20:]
        self._logger.warning("Mesh gateway %s error: %s", self.config.gateway_id, message)
        await self._emit_status()

    async def _emit_status(self) -> None:
        await self._on_status(self.status)
