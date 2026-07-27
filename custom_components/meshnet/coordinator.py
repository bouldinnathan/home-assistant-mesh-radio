"""Coordinator for the MeshNet integration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR_CHANNEL,
    ATTR_GATEWAY_ID,
    ATTR_MESSAGE,
    ATTR_MESSAGE_TYPE,
    ATTR_PRIORITY,
    ATTR_TARGET_NODE,
    CONF_GATEWAYS,
    CONF_HISTORY_DAYS,
    CONF_NODE_TIMEOUT,
    CONF_SCAN_INTERVAL,
    DEFAULT_DATABASE_NAME,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_NODE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_MESSAGE_RECEIVED,
    EVENT_MESSAGE_SENT,
    EVENT_PACKET,
    MESSAGE_TYPE_BROADCAST,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_REST,
)
from .dedupe import PacketDeduplicator
from .gateway import MeshGateway
from .meshcore_client import MeshCoreClient
from .meshtastic_client import MeshtasticClient
from .models import (
    GatewayConfig,
    GatewayStatus,
    MessageRecord,
    MeshPacket,
    MeshSnapshot,
    NodeState,
    stable_json,
    timestamp_to_json,
    utcnow,
)
from .rate_limiter import TokenBucket
from .store import MeshStore


_LOGGER = logging.getLogger(__name__)


class MeshNetCoordinator(DataUpdateCoordinator[MeshSnapshot]):
    """Single merged coordinator for a MeshNet config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.node_timeout = int(entry.options.get(CONF_NODE_TIMEOUT, entry.data.get(CONF_NODE_TIMEOUT, DEFAULT_NODE_TIMEOUT)))
        self.history_days = int(entry.options.get(CONF_HISTORY_DAYS, entry.data.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)))
        self.store = MeshStore(Path(hass.config.path(DEFAULT_DATABASE_NAME)), executor=hass.async_add_executor_job)
        self.deduplicator = PacketDeduplicator()
        self.tx_limiter = TokenBucket(rate=0.5, capacity=5)
        self.snapshot = MeshSnapshot()
        self.gateways: dict[str, MeshGateway] = {}
        self._gateway_configs = self._load_gateway_configs(entry)
        self._outbox_lock = asyncio.Lock()
        self._outbox_flush_owner: asyncio.Task[Any] | None = None
        self._reconnect_tasks: dict[str, asyncio.Task[Any]] = {}
        self._shutting_down = False
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=30),
            always_update=True,
        )

    async def _async_setup(self) -> None:
        await self.store.async_open()
        cached = await self.store.async_load_snapshot(recent_limit=100)
        self.snapshot.nodes.update(cached.nodes)
        self.snapshot.recent_messages = cached.recent_messages
        await self._rebuild_gateways()
        await self._start_gateways()
        await self._flush_outbox()

    async def _async_update_data(self) -> MeshSnapshot:
        try:
            await self.store.async_prune(self.history_days)
            self._mark_stale_nodes()
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            self.snapshot.messages_today = await self.store.async_messages_since(midnight)
            self.snapshot.mesh_health_score = self._mesh_health_score()
            return self.snapshot
        except Exception as err:
            raise UpdateFailed(str(err)) from err

    async def async_shutdown(self) -> None:
        """Stop gateways and close durable storage."""
        self._shutting_down = True
        for task in list(self._reconnect_tasks.values()):
            task.cancel()
        if self._reconnect_tasks:
            await asyncio.gather(*self._reconnect_tasks.values(), return_exceptions=True)
        self._reconnect_tasks.clear()
        for gateway in list(self.gateways.values()):
            try:
                await gateway.async_stop()
            except Exception as err:
                _LOGGER.debug("Error stopping gateway %s: %s", gateway.config.gateway_id, err)
        await self.store.async_close()

    async def async_reload_gateways(self) -> None:
        """Reload gateway configuration from the current config entry."""
        for gateway in list(self.gateways.values()):
            await gateway.async_stop()
        self._gateway_configs = self._load_gateway_configs(self.entry)
        await self._rebuild_gateways()
        await self._start_gateways()

    async def async_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None = None,
        priority: str = "normal",
        message_type: str = MESSAGE_TYPE_BROADCAST,
        gateway_id: str | None = None,
    ) -> str:
        """Send or queue a mesh message."""
        gateway = self._select_gateway(gateway_id=gateway_id, target_node=target_node)
        message_id = self._message_id(
            target_node=target_node,
            message=message,
            channel=channel,
            gateway_id=gateway.config.gateway_id if gateway else gateway_id,
        )
        record = MessageRecord(
            message_id=message_id,
            protocol=gateway.config.protocol if gateway else "unknown",
            gateway_id=gateway.config.gateway_id if gateway else gateway_id or "queued",
            sender="homeassistant",
            receiver=target_node,
            channel=channel,
            text=message,
            message_type=message_type,
            priority=priority,
            direction="tx",
            raw={
                "status": "queued" if gateway is None else "sending",
                "target_node": target_node,
                "gateway_id": gateway_id,
            },
        )
        await self.store.async_add_message(record)
        if gateway is None:
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            self.async_set_updated_data(self.snapshot)
            return message_id

        await self.tx_limiter.acquire()
        try:
            provider_id = await gateway.async_send_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
            )
        except Exception as err:
            record.raw["status"] = "queued"
            record.raw["last_error"] = str(err)
            await self.store.async_add_message(record)
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            self.async_set_updated_data(self.snapshot)
            self._create_issue(
                issue_id=f"send_failed_{gateway.config.gateway_id}",
                message=f"Message queued after send failure on {gateway.config.name}: {err}",
            )
            return message_id
        record.raw["status"] = "sent"
        record.raw["provider_id"] = provider_id
        await self.store.async_add_message(record)
        self.hass.bus.async_fire(EVENT_MESSAGE_SENT, record.as_dict())
        self.snapshot.recent_messages = await self.store.async_recent_messages(100)
        self.async_set_updated_data(self.snapshot)
        return message_id

    async def async_gateway_refresh(self, gateway_id: str | None = None) -> None:
        """Refresh one or all gateways."""
        gateways: Iterable[MeshGateway]
        if gateway_id:
            gateway = self.gateways.get(gateway_id)
            if gateway is None:
                raise HomeAssistantError(f"Unknown gateway: {gateway_id}")
            gateways = [gateway]
        else:
            gateways = self.gateways.values()
        for gateway in gateways:
            await gateway.async_refresh()

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return aggregate diagnostics without identifiers or mesh content."""
        return {
            "configuration": {
                "node_timeout": self.node_timeout,
                "history_days": self.history_days,
                "gateway_count": len(self.gateways),
            },
            "gateways": [
                {
                    "protocol": gateway.status.protocol,
                    "transport": gateway.status.transport,
                    "connected": gateway.status.connected,
                    "last_connected": timestamp_to_json(gateway.status.last_connected),
                    "last_packet": timestamp_to_json(gateway.status.last_packet),
                    "packets_received": gateway.status.packets_received,
                    "packets_sent": gateway.status.packets_sent,
                    "duplicate_packets": gateway.status.duplicate_packets,
                    "error_count": len(gateway.status.errors),
                }
                for gateway in self.gateways.values()
            ],
            "dedupe": self.deduplicator.stats(),
            "rate_limit": self.tx_limiter.snapshot(),
            "snapshot": {
                "node_count": len(self.snapshot.nodes),
                "message_count": len(self.snapshot.recent_messages),
                "mesh_health_score": self.snapshot.mesh_health_score,
            },
            "store": await self.store.async_diagnostics(),
        }

    async def _handle_packet(self, packet: MeshPacket) -> None:
        gateway = self.gateways.get(packet.gateway_id)
        if self.deduplicator.is_duplicate(packet):
            if gateway:
                gateway.status.duplicate_packets += 1
            return
        await self.store.async_add_packet(packet)
        self.hass.bus.async_fire(EVENT_PACKET, packet.as_dict())
        if packet.text:
            record = MessageRecord(
                message_id=packet.fingerprint(),
                protocol=packet.protocol,
                gateway_id=packet.gateway_id,
                sender=packet.sender,
                receiver=packet.receiver,
                channel=packet.channel,
                text=packet.text,
                encrypted=packet.encrypted,
                hops=packet.hops,
                timestamp=packet.timestamp,
                raw=packet.raw,
            )
            await self.store.async_add_message(record)
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            self.hass.bus.async_fire(EVENT_MESSAGE_RECEIVED, record.as_dict())
        self.async_set_updated_data(self.snapshot)

    async def _handle_node(self, node: NodeState) -> None:
        existing = self.snapshot.nodes.get(node.node_key)
        if existing:
            existing.merge(node)
            node = existing
        self.snapshot.nodes[node.node_key] = node
        await self.store.async_upsert_node(node)
        self.async_set_updated_data(self.snapshot)

    async def _handle_gateway_status(self, status: GatewayStatus) -> None:
        self.snapshot.gateways[status.gateway_id] = status
        if status.connected:
            reconnect_task = self._reconnect_tasks.pop(status.gateway_id, None)
            if reconnect_task:
                reconnect_task.cancel()
            await self._flush_outbox(gateway_id=status.gateway_id)
        elif not self._shutting_down:
            self._schedule_reconnect(status.gateway_id)
        self.async_set_updated_data(self.snapshot)

    async def _rebuild_gateways(self) -> None:
        self.gateways = {}
        for config in self._gateway_configs:
            if config.protocol == PROTOCOL_MESHTASTIC:
                gateway = MeshtasticClient(
                    self.hass,
                    config,
                    self._handle_packet,
                    self._handle_node,
                    self._handle_gateway_status,
                    _LOGGER,
                )
            elif config.protocol == PROTOCOL_MESHCORE:
                gateway = MeshCoreClient(
                    self.hass,
                    config,
                    self._handle_packet,
                    self._handle_node,
                    self._handle_gateway_status,
                    _LOGGER,
                )
            else:
                self._create_issue(
                    issue_id=f"unsupported_protocol_{config.gateway_id}",
                    message=f"Unsupported protocol {config.protocol}",
                )
                continue
            self.gateways[config.gateway_id] = gateway
            self.snapshot.gateways[config.gateway_id] = gateway.status

    async def _start_gateways(self) -> None:
        if not self.gateways:
            self._create_issue(
                issue_id="no_gateways",
                message="MeshNet has no configured gateways.",
                severity=ir.IssueSeverity.ERROR,
            )
            return
        results = await asyncio.gather(
            *(gateway.async_start() for gateway in self.gateways.values()),
            return_exceptions=True,
        )
        for gateway, result in zip(self.gateways.values(), results, strict=False):
            if isinstance(result, Exception):
                self._create_issue(
                    issue_id=f"gateway_start_{gateway.config.gateway_id}",
                    message=f"{gateway.config.name} failed to start: {result}",
                    severity=ir.IssueSeverity.WARNING,
                )

    async def _flush_outbox(self, gateway_id: str | None = None) -> None:
        current_task = asyncio.current_task()
        if current_task is not None and current_task is self._outbox_flush_owner:
            return
        async with self._outbox_lock:
            self._outbox_flush_owner = current_task
            try:
                pending = await self.store.async_pending_outbox(limit=100)
                sent_any = False
                for record in pending:
                    desired_gateway = record.raw.get("gateway_id") or gateway_id
                    gateway = self._select_gateway(
                        gateway_id=desired_gateway,
                        target_node=record.receiver,
                    )
                    if gateway is None:
                        continue
                    await self.tx_limiter.acquire()
                    try:
                        provider_id = await gateway.async_send_message(
                            target_node=record.receiver,
                            message=record.text,
                            channel=record.channel,
                            priority=record.priority,
                            message_type=record.message_type,
                        )
                    except Exception as err:
                        record.raw["status"] = "queued"
                        record.raw["last_error"] = str(err)
                        await self.store.async_add_message(record)
                        continue
                    record.gateway_id = gateway.config.gateway_id
                    record.protocol = gateway.config.protocol
                    record.raw["status"] = "sent"
                    record.raw["provider_id"] = provider_id
                    await self.store.async_add_message(record)
                    self.hass.bus.async_fire(EVENT_MESSAGE_SENT, record.as_dict())
                    sent_any = True
                if sent_any:
                    self.snapshot.recent_messages = await self.store.async_recent_messages(100)
                    self.async_set_updated_data(self.snapshot)
            finally:
                self._outbox_flush_owner = None

    def _schedule_reconnect(self, gateway_id: str) -> None:
        if gateway_id in self._reconnect_tasks:
            return
        task = self.hass.async_create_task(self._delayed_reconnect(gateway_id))
        self._reconnect_tasks[gateway_id] = task
        task.add_done_callback(lambda _: self._reconnect_tasks.pop(gateway_id, None))

    async def _delayed_reconnect(self, gateway_id: str) -> None:
        await asyncio.sleep(30)
        if self._shutting_down:
            return
        gateway = self.gateways.get(gateway_id)
        if gateway is None or gateway.status.connected:
            return
        try:
            await gateway.async_stop()
            await gateway.async_start()
        except Exception as err:
            await gateway._emit_error(f"Reconnect failed: {err}")

    def _select_gateway(self, *, gateway_id: str | None, target_node: str | None) -> MeshGateway | None:
        if gateway_id:
            gateway = self.gateways.get(gateway_id)
            if gateway and gateway.status.connected:
                return gateway
            return None
        for node in self.snapshot.nodes.values():
            if target_node and target_node in {node.node_key, node.node_id, node.public_key, node.mac}:
                if node.last_gateway_id:
                    gateway = self.gateways.get(node.last_gateway_id)
                    if gateway and gateway.status.connected:
                        return gateway
        for gateway in self.gateways.values():
            if gateway.status.connected:
                return gateway
        return None

    def _mark_stale_nodes(self) -> None:
        now = utcnow()
        for node in self.snapshot.nodes.values():
            if node.last_heard and (now - node.last_heard).total_seconds() > self.node_timeout:
                node.online = False

    def _mesh_health_score(self) -> float:
        nodes = list(self.snapshot.nodes.values())
        if not nodes:
            return 0.0
        online_count = sum(1 for node in nodes if node.online)
        online_score = online_count / len(nodes)
        battery_values = [
            float(node.power["battery_level"])
            for node in nodes
            if isinstance(node.power.get("battery_level"), (int, float))
        ]
        battery_score = (sum(battery_values) / len(battery_values) / 100) if battery_values else 1.0
        duplicate_penalty = min(self.deduplicator.duplicate_ratio, 0.5)
        connected_gateways = sum(1 for gateway in self.snapshot.gateways.values() if gateway.connected)
        gateway_score = 1.0 if connected_gateways else 0.0
        score = (online_score * 0.45) + (battery_score * 0.2) + (gateway_score * 0.25) + ((1 - duplicate_penalty) * 0.1)
        return round(max(0.0, min(score * 100, 100.0)), 1)

    def _create_issue(
        self,
        *,
        issue_id: str,
        message: str,
        severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    ) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=severity,
            translation_key="gateway_issue",
            translation_placeholders={"message": message},
        )

    @staticmethod
    def _message_id(
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        gateway_id: str | None,
    ) -> str:
        import hashlib

        return hashlib.sha256(
            stable_json(
                {
                    "target_node": target_node,
                    "message": message,
                    "channel": channel,
                    "gateway_id": gateway_id,
                    "timestamp": utcnow().timestamp(),
                }
            ).encode()
        ).hexdigest()[:20]

    @staticmethod
    def _load_gateway_configs(entry: ConfigEntry) -> list[GatewayConfig]:
        if CONF_GATEWAYS in entry.options:
            gateways = entry.options[CONF_GATEWAYS]
        else:
            gateways = entry.data.get(CONF_GATEWAYS) or []
        scan_interval = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )
        configs: list[GatewayConfig] = []
        for gateway_data in gateways:
            gateway = dict(gateway_data)
            if gateway.get("transport") == TRANSPORT_REST:
                gateway_options = dict(gateway.get("options") or {})
                gateway_options.setdefault(CONF_SCAN_INTERVAL, scan_interval)
                gateway["options"] = gateway_options
            configs.append(GatewayConfig.from_dict(gateway))
        return configs


def service_fields(call_data: dict[str, Any]) -> dict[str, Any]:
    """Return normalized service call fields."""
    return {
        "target_node": call_data.get(ATTR_TARGET_NODE),
        "message": call_data[ATTR_MESSAGE],
        "channel": call_data.get(ATTR_CHANNEL),
        "priority": call_data.get(ATTR_PRIORITY, "normal"),
        "message_type": call_data.get(ATTR_MESSAGE_TYPE, MESSAGE_TYPE_BROADCAST),
        "gateway_id": call_data.get(ATTR_GATEWAY_ID),
    }
