"""Coordinator for the MeshNet integration."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import Counter
from collections.abc import Coroutine, Iterable
from datetime import UTC, datetime, timedelta
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
from .diagnostic_safety import safe_node_metadata
from .gateway import MeshGateway
from .meshcore_client import MeshCoreClient
from .meshtastic_client import MeshtasticClient
from .models import (
    GatewayConfig,
    GatewayStatus,
    MeshPacket,
    MeshSnapshot,
    MessageRecord,
    NodeState,
    stable_json,
    timestamp_to_json,
    utcnow,
)
from .rate_limiter import TokenBucket
from .store import MeshStore

_LOGGER = logging.getLogger(__name__)

_RECONNECT_INITIAL_DELAY = 30.0
_RECONNECT_MAX_DELAY = 300.0
_RECONNECT_JITTER_RATIO = 0.2
_GATEWAY_TASK_CANCEL_TIMEOUT = 2.0
_DIAGNOSTIC_STORE_TIMEOUT = 2.0
_GATEWAY_ISSUE_CATEGORIES = (
    "gateway_start",
    "send_failed",
    "unsupported_protocol",
)
_SAFE_GATEWAY_ISSUE_RE = re.compile(
    r"^(?:gateway_start|send_failed|unsupported_protocol)_gateway_"
    r"(?:[0-9]{3}|unknown)$"
)


def _diagnostic_task_state(task: asyncio.Future[Any] | None) -> str:
    """Return task state without evaluating or exposing an exception."""
    if task is None:
        return "not_created"
    if task.cancelled():
        return "cancelled"
    if task.done():
        return "finished"
    if isinstance(task, asyncio.Task) and task.cancelling():
        return "cancelling"
    return "pending"


def _diagnostic_error_category(error: str) -> str:
    """Classify an error without returning endpoint or credential-bearing text."""
    lowered = error.casefold()
    categories = (
        ("authentication", ("auth", "credential", "password", "pin", "token")),
        ("bluetooth", ("bluetooth", "bluez", "ble", "dbus")),
        ("configuration", ("config", "invalid", "missing", "required", "unsupported")),
        ("connection", ("connect", "disconnect", "socket", "network", "unreachable")),
        ("data", ("decode", "json", "parse", "payload", "protobuf")),
        ("permission", ("permission", "access denied", "read-only")),
        ("serial", ("serial", "tty", "baud")),
        ("timeout", ("timeout", "timed out")),
    )
    for category, markers in categories:
        if any(marker in lowered for marker in markers):
            return category
    return "other"


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
        self._gateway_startup_task: asyncio.Task[Any] | None = None
        self._reconnect_tasks: dict[str, asyncio.Task[Any]] = {}
        self._send_tasks: set[asyncio.Task[Any]] = set()
        self._active_send_message_ids: set[str] = set()
        self._gateway_generation = 0
        self._shutting_down = False
        self._reconnect_suspended = False
        self._last_update_attempt_at: datetime | None = None
        self._last_update_success_at: datetime | None = None
        self._last_update_duration_seconds: float | None = None
        self._last_update_error_category: str | None = None
        self._legacy_issue_cleanup_count = 0
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=30),
            always_update=True,
        )

    async def _async_setup(self) -> None:
        self._legacy_issue_cleanup_count = self._delete_legacy_gateway_issues()
        await self.store.async_open()
        cached = await self.store.async_load_snapshot(recent_limit=100)
        self.snapshot.nodes.update(cached.nodes)
        self.snapshot.recent_messages = cached.recent_messages
        await self._rebuild_gateways()

    async def async_start_gateways(self) -> None:
        """Start radio transports after config-entry setup has completed.

        Meshtastic's synchronous BLE constructor can spend several minutes in
        discovery and configuration waits.  Home Assistant awaits config-entry
        setup before completing a config flow, so radio I/O must run in the
        entry-owned background task created by ``async_setup_entry``.
        """
        if self._shutting_down:
            return
        _LOGGER.debug(
            "Starting %d MeshNet gateway transport(s) in the background",
            len(self.gateways),
        )
        await self._start_gateways()
        if not self._shutting_down:
            await self._flush_outbox()
            _LOGGER.debug("MeshNet background gateway startup pass completed")

    def async_start_gateways_background(self) -> None:
        """Start and retain the entry-owned transport startup waiter."""
        if self._shutting_down:
            return
        task = self._gateway_startup_task
        if task is not None and not task.done():
            return
        task = self._async_create_background_task(
            self.async_start_gateways(), "MeshNet gateway startup"
        )
        self._gateway_startup_task = task

        def clear_startup(done_task: asyncio.Task[Any]) -> None:
            if self._gateway_startup_task is done_task:
                self._gateway_startup_task = None

        task.add_done_callback(clear_startup)

    def _async_create_background_task(
        self, target: Coroutine[Any, Any, Any], name: str
    ) -> asyncio.Task[Any]:
        """Create long-lived work tied to this config entry when possible."""
        creator = getattr(self.entry, "async_create_background_task", None)
        if callable(creator):
            return creator(self.hass, target, name)
        creator = getattr(self.hass, "async_create_background_task", None)
        if callable(creator):
            return creator(target, name)
        return self.hass.async_create_task(target)

    async def _async_update_data(self) -> MeshSnapshot:
        started = time.monotonic()
        self._last_update_attempt_at = utcnow()
        try:
            await self.store.async_prune(self.history_days)
            self._mark_stale_nodes()
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            self.snapshot.messages_today = await self.store.async_messages_since(midnight)
            self.snapshot.mesh_health_score = self._mesh_health_score()
        except Exception as err:
            self._last_update_duration_seconds = round(
                time.monotonic() - started, 3
            )
            self._last_update_error_category = _diagnostic_error_category(
                str(err)
            )
            raise UpdateFailed(str(err)) from err
        self._last_update_success_at = utcnow()
        self._last_update_duration_seconds = round(
            time.monotonic() - started, 3
        )
        self._last_update_error_category = None
        return self.snapshot

    async def async_shutdown(self) -> None:
        """Stop gateways and close durable storage."""
        self._shutting_down = True
        self._reconnect_suspended = True
        await super().async_shutdown()
        startup_drained = await self._cancel_gateway_startup_task()
        reconnects_drained = await self._cancel_reconnect_tasks()
        outbox_drained = await self._cancel_outbox_flush_owner()
        sends_drained = await self._cancel_send_tasks()
        if not startup_drained:
            _LOGGER.warning(
                "Gateway startup did not stop promptly; continuing bounded "
                "shutdown with transport stop fences"
            )
        if not reconnects_drained:
            _LOGGER.warning(
                "Gateway reconnect did not stop promptly; continuing bounded "
                "shutdown with transport stop fences"
            )
        if not outbox_drained:
            _LOGGER.warning(
                "Outbox delivery did not stop promptly; continuing bounded "
                "shutdown with lifecycle fences"
            )
        if not sends_drained:
            _LOGGER.warning(
                "Message delivery did not stop promptly; continuing bounded "
                "shutdown with lifecycle fences"
            )
        for gateway in list(self.gateways.values()):
            try:
                await gateway.async_stop()
            except Exception as err:
                _LOGGER.debug("Error stopping gateway %s: %s", gateway.config.gateway_id, err)
        await self.store.async_close()

    async def async_reload_gateways(self) -> None:
        """Reload gateway configuration from the current config entry."""
        self._reconnect_suspended = True
        try:
            startup_drained = await self._cancel_gateway_startup_task()
            reconnects_drained = await self._cancel_reconnect_tasks()
            outbox_drained = await self._cancel_outbox_flush_owner()
            sends_drained = await self._cancel_send_tasks()
            if (
                not startup_drained
                or not reconnects_drained
                or not outbox_drained
                or not sends_drained
            ):
                _LOGGER.warning(
                    "Gateway reload deferred because previous transport work "
                    "did not stop promptly"
                )
                return
            for gateway in list(self.gateways.values()):
                await gateway.async_stop()
            self._gateway_configs = self._load_gateway_configs(self.entry)
            await self._rebuild_gateways()
        finally:
            self._reconnect_suspended = False
        self.async_start_gateways_background()

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
        task = asyncio.current_task()
        if task is not None:
            self._send_tasks.add(task)
        try:
            return await self._async_send_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
                gateway_id=gateway_id,
            )
        finally:
            if task is not None:
                self._send_tasks.discard(task)

    async def _async_send_message(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
        gateway_id: str | None,
    ) -> str:
        """Send once while fencing storage and events from lifecycle changes."""
        gateway_generation = self._gateway_generation
        if not self._gateway_callback_is_current(gateway_generation):
            raise HomeAssistantError("MeshNet is stopping or reloading")
        gateway = self._select_gateway(gateway_id=gateway_id, target_node=target_node)
        message_id = self._message_id(
            target_node=target_node,
            message=message,
            channel=channel,
            gateway_id=gateway.config.gateway_id if gateway else gateway_id,
        )
        active_message_ids = getattr(self, "_active_send_message_ids", None)
        if active_message_ids is None:
            active_message_ids = set()
            self._active_send_message_ids = active_message_ids
        active_message_ids.add(message_id)
        try:
            return await self._async_send_message_record(
                message_id=message_id,
                gateway=gateway,
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
                gateway_id=gateway_id,
                gateway_generation=gateway_generation,
            )
        finally:
            active_message_ids.discard(message_id)

    async def _async_send_message_record(
        self,
        *,
        message_id: str,
        gateway: MeshGateway | None,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
        gateway_id: str | None,
        gateway_generation: int,
    ) -> str:
        """Persist and deliver one direct-send record."""
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
                "status": "queued",
                "target_node": target_node,
                "gateway_id": gateway_id,
            },
        )
        await self.store.async_add_message(record)
        if not self._gateway_callback_is_current(gateway_generation):
            return message_id
        if gateway is None:
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            if not self._gateway_callback_is_current(gateway_generation):
                return message_id
            self.async_set_updated_data(self.snapshot)
            return message_id

        await self.tx_limiter.acquire()
        if not self._gateway_callback_is_current(gateway_generation):
            return message_id
        try:
            provider_id = await gateway.async_send_message(
                target_node=target_node,
                message=message,
                channel=channel,
                priority=priority,
                message_type=message_type,
            )
        except Exception as err:
            if not self._gateway_callback_is_current(gateway_generation):
                return message_id
            record.raw["status"] = "queued"
            record.raw["last_error"] = str(err)
            await self.store.async_add_message(record)
            if not self._gateway_callback_is_current(gateway_generation):
                return message_id
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            if not self._gateway_callback_is_current(gateway_generation):
                return message_id
            self.async_set_updated_data(self.snapshot)
            self._create_issue(
                issue_id=self._gateway_issue_id(
                    "send_failed", gateway.config.gateway_id
                ),
                message=f"Message queued after send failure on {gateway.config.name}: {err}",
            )
            return message_id
        if not self._gateway_callback_is_current(gateway_generation):
            return message_id
        record.raw["status"] = "sent"
        record.raw["provider_id"] = provider_id
        await self.store.async_add_message(record)
        if not self._gateway_callback_is_current(gateway_generation):
            return message_id
        self.hass.bus.async_fire(EVENT_MESSAGE_SENT, record.as_dict())
        self.snapshot.recent_messages = await self.store.async_recent_messages(100)
        if not self._gateway_callback_is_current(gateway_generation):
            return message_id
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
        """Return thorough cached diagnostics without identity or mesh content."""
        gateway_items = sorted(self.gateways.items(), key=lambda item: item[0])
        nodes = list(self.snapshot.nodes.values())
        gateway_diagnostics: list[dict[str, Any]] = []
        for index, (_gateway_id, gateway) in enumerate(gateway_items, start=1):
            status = gateway.status
            error_categories = Counter(
                _diagnostic_error_category(error) for error in status.errors
            )
            client_snapshot = getattr(gateway, "diagnostic_snapshot", None)
            try:
                client = client_snapshot() if callable(client_snapshot) else {}
            except Exception as err:
                client = {
                    "available": False,
                    "collection_error": type(err).__name__,
                }
            config = getattr(gateway, "config", None)
            options = getattr(config, "options", {}) or {}
            numeric_options = {
                key: options[key]
                for key in (
                    "baudrate",
                    "message_poll_interval",
                    "scan_interval",
                )
                if isinstance(options.get(key), (int, float))
            }
            gateway_diagnostics.append(
                {
                    "diagnostic_id": f"gateway_{index:03d}",
                    "protocol": status.protocol,
                    "transport": status.transport,
                    "connected": status.connected,
                    "last_connected": timestamp_to_json(status.last_connected),
                    "last_packet": timestamp_to_json(status.last_packet),
                    "packets_received": status.packets_received,
                    "packets_sent": status.packets_sent,
                    "duplicate_packets": status.duplicate_packets,
                    "error_count": len(status.errors),
                    "error_categories": dict(sorted(error_categories.items())),
                    "status_detail_field_count": len(status.detail),
                    "configured": {
                        "host_configured": bool(getattr(config, "host", None)),
                        "port": getattr(config, "port", None),
                        "serial_endpoint_configured": bool(
                            getattr(config, "serial_path", None)
                        ),
                        "bluetooth_endpoint_configured": bool(
                            getattr(config, "ble_address", None)
                        ),
                        "mqtt_subscription_configured": bool(
                            getattr(config, "mqtt_topic", None)
                        ),
                        "rest_endpoint_configured": bool(
                            getattr(config, "api_url", None)
                        ),
                        "api_key_configured": bool(
                            getattr(config, "api_key", None)
                        ),
                        "publish_endpoint_configured": bool(
                            options.get("publish_topic")
                        ),
                        "custom_send_endpoint_configured": bool(
                            options.get("send_url")
                        ),
                        "bluetooth_adapter_metadata_configured": bool(
                            options.get("bluetooth_adapter")
                            and options.get("bluetooth_adapter_address")
                        ),
                        "bluetooth_bond_managed": bool(
                            options.get("bluetooth_bond_managed")
                        ),
                        "debug": bool(options.get("debug")),
                        **numeric_options,
                    },
                    "client": client,
                }
            )

        reconnect_tasks = getattr(self, "_reconnect_tasks", {})
        send_tasks = getattr(self, "_send_tasks", set())
        reconnect_states = Counter(
            _diagnostic_task_state(task) for task in reconnect_tasks.values()
        )
        send_states = Counter(
            _diagnostic_task_state(task) for task in send_tasks
        )
        update_interval = getattr(self, "update_interval", None)
        last_update_attempt_time = getattr(self, "_last_update_attempt_at", None)
        last_update_success_time = getattr(self, "_last_update_success_at", None)

        try:
            async with asyncio.timeout(_DIAGNOSTIC_STORE_TIMEOUT):
                store_diagnostics = await self.store.async_diagnostics()
        except TimeoutError:
            store_diagnostics = {
                "available": False,
                "collection_error": "timeout",
            }
        except asyncio.CancelledError:
            raise
        except Exception as err:
            store_diagnostics = {
                "available": False,
                "collection_error": type(err).__name__,
            }

        gateway_configs = getattr(self, "_gateway_configs", [])
        protocol_counts = Counter(node.protocol for node in nodes)
        role_counts = Counter(
            safe_node_metadata(node.role, "role") or "redacted_or_unknown"
            for node in nodes
        )
        hardware_counts = Counter(
            safe_node_metadata(node.hardware_model, "hardware_model")
            or "redacted_or_unknown"
            for node in nodes
        )
        firmware_counts = Counter(
            safe_node_metadata(node.firmware_version, "firmware_version")
            or "redacted_or_unknown"
            for node in nodes
        )
        radio_type_counts = Counter(
            safe_node_metadata(node.radio_type, "radio_type")
            or "redacted_or_unknown"
            for node in nodes
        )
        gateway_reachability = Counter(str(len(node.gateway_ids)) for node in nodes)
        last_heard_values = [node.last_heard for node in nodes if node.last_heard]

        return {
            "configuration": {
                "node_timeout": self.node_timeout,
                "history_days": self.history_days,
                "gateway_count": len(self.gateways),
                "protocol_counts": dict(
                    sorted(
                        Counter(config.protocol for config in gateway_configs).items()
                    )
                ),
                "transport_counts": dict(
                    sorted(
                        Counter(config.transport for config in gateway_configs).items()
                    )
                ),
            },
            "lifecycle": {
                "shutting_down": getattr(self, "_shutting_down", False),
                "reconnect_suspended": getattr(
                    self, "_reconnect_suspended", False
                ),
                "gateway_generation": getattr(self, "_gateway_generation", 0),
                "last_update_success": getattr(self, "last_update_success", None),
                "last_update_attempt_time": timestamp_to_json(
                    last_update_attempt_time
                ),
                "last_update_success_time": timestamp_to_json(last_update_success_time),
                "last_update_duration_seconds": getattr(
                    self, "_last_update_duration_seconds", None
                ),
                "last_update_error_category": getattr(
                    self, "_last_update_error_category", None
                ),
                "update_interval_seconds": (
                    update_interval.total_seconds() if update_interval else None
                ),
                "coordinator_data_available": getattr(self, "data", None) is not None,
            },
            "tasks": {
                "gateway_startup": _diagnostic_task_state(
                    getattr(self, "_gateway_startup_task", None)
                ),
                "reconnect_count": len(reconnect_tasks),
                "reconnect_states": dict(sorted(reconnect_states.items())),
                "outbox_flush": _diagnostic_task_state(
                    getattr(self, "_outbox_flush_owner", None)
                ),
                "outbox_lock_held": bool(
                    getattr(self, "_outbox_lock", None)
                    and self._outbox_lock.locked()
                ),
                "send_count": len(send_tasks),
                "send_states": dict(sorted(send_states.items())),
                "active_send_count": len(
                    getattr(self, "_active_send_message_ids", set())
                ),
            },
            "gateways": gateway_diagnostics,
            "dedupe": self.deduplicator.stats(),
            "rate_limit": self.tx_limiter.snapshot(),
            "snapshot": {
                "node_count": len(nodes),
                "online_node_count": sum(node.online for node in nodes),
                "offline_node_count": sum(not node.online for node in nodes),
                "message_count": len(self.snapshot.recent_messages),
                "messages_today": self.snapshot.messages_today,
                "mesh_health_score": self.snapshot.mesh_health_score,
                "nodes_with_location": sum(bool(node.location) for node in nodes),
                "nodes_with_power": sum(bool(node.power) for node in nodes),
                "nodes_with_radio": sum(bool(node.radio) for node in nodes),
                "nodes_with_routing": sum(bool(node.routing) for node in nodes),
                "nodes_with_sensors": sum(bool(node.sensors) for node in nodes),
                "protocol_counts": dict(sorted(protocol_counts.items())),
                "role_counts": dict(sorted(role_counts.items())),
                "hardware_model_counts": dict(sorted(hardware_counts.items())),
                "firmware_version_counts": dict(sorted(firmware_counts.items())),
                "radio_type_counts": dict(sorted(radio_type_counts.items())),
                "gateway_reachability_counts": dict(
                    sorted(gateway_reachability.items())
                ),
                "oldest_last_heard": timestamp_to_json(
                    min(last_heard_values) if last_heard_values else None
                ),
                "newest_last_heard": timestamp_to_json(
                    max(last_heard_values) if last_heard_values else None
                ),
            },
            "store": store_diagnostics,
            "repairs": self._repair_diagnostics(),
        }

    async def _handle_packet(
        self, packet: MeshPacket, *, gateway_generation: int | None = None
    ) -> None:
        if not self._gateway_callback_is_current(gateway_generation):
            return
        gateway = self.gateways.get(packet.gateway_id)
        if self.deduplicator.is_duplicate(packet):
            if gateway:
                gateway.status.duplicate_packets += 1
            return
        await self.store.async_add_packet(packet)
        if not self._gateway_callback_is_current(gateway_generation):
            return
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
            if not self._gateway_callback_is_current(gateway_generation):
                return
            self.snapshot.recent_messages = await self.store.async_recent_messages(100)
            if not self._gateway_callback_is_current(gateway_generation):
                return
            self.hass.bus.async_fire(EVENT_MESSAGE_RECEIVED, record.as_dict())
        self.async_set_updated_data(self.snapshot)

    async def _handle_node(
        self, node: NodeState, *, gateway_generation: int | None = None
    ) -> None:
        if not self._gateway_callback_is_current(gateway_generation):
            return
        existing = self.snapshot.nodes.get(node.node_key)
        if existing:
            existing.merge(node)
            node = existing
        self.snapshot.nodes[node.node_key] = node
        await self.store.async_upsert_node(node)
        if not self._gateway_callback_is_current(gateway_generation):
            return
        self.async_set_updated_data(self.snapshot)

    async def _handle_gateway_status(
        self, status: GatewayStatus, *, gateway_generation: int | None = None
    ) -> None:
        if not self._gateway_callback_is_current(gateway_generation):
            return
        gateway = self.gateways.get(status.gateway_id)
        if gateway is None or gateway.status is not status:
            return
        self.snapshot.gateways[status.gateway_id] = status
        if status.connected:
            reconnect_task = self._reconnect_tasks.get(status.gateway_id)
            if reconnect_task and reconnect_task is not asyncio.current_task():
                reconnect_task.cancel()
            await self._flush_outbox(
                gateway_id=status.gateway_id,
                gateway_generation=gateway_generation,
            )
            if not self._gateway_callback_is_current(gateway_generation):
                return
        else:
            self._schedule_reconnect(status.gateway_id)
        self.async_set_updated_data(self.snapshot)

    async def _rebuild_gateways(self) -> None:
        self._gateway_generation = getattr(self, "_gateway_generation", 0) + 1
        gateway_generation = self._gateway_generation
        self.gateways = {}

        async def handle_packet(packet: MeshPacket) -> None:
            await self._handle_packet(
                packet, gateway_generation=gateway_generation
            )

        async def handle_node(node: NodeState) -> None:
            await self._handle_node(node, gateway_generation=gateway_generation)

        async def handle_status(status: GatewayStatus) -> None:
            await self._handle_gateway_status(
                status, gateway_generation=gateway_generation
            )

        for config in self._gateway_configs:
            if config.protocol == PROTOCOL_MESHTASTIC:
                gateway = MeshtasticClient(
                    self.hass,
                    config,
                    handle_packet,
                    handle_node,
                    handle_status,
                    _LOGGER,
                )
            elif config.protocol == PROTOCOL_MESHCORE:
                gateway = MeshCoreClient(
                    self.hass,
                    config,
                    handle_packet,
                    handle_node,
                    handle_status,
                    _LOGGER,
                )
            else:
                self._create_issue(
                    issue_id=self._gateway_issue_id(
                        "unsupported_protocol", config.gateway_id
                    ),
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
                    issue_id=self._gateway_issue_id(
                        "gateway_start", gateway.config.gateway_id
                    ),
                    message=f"{gateway.config.name} failed to start: {result}",
                    severity=ir.IssueSeverity.WARNING,
                )

    async def _flush_outbox(
        self,
        gateway_id: str | None = None,
        *,
        gateway_generation: int | None = None,
    ) -> None:
        if gateway_generation is None:
            gateway_generation = getattr(self, "_gateway_generation", 0)
        if not self._gateway_callback_is_current(gateway_generation):
            return
        current_task = asyncio.current_task()
        if current_task is not None and current_task is self._outbox_flush_owner:
            return
        async with self._outbox_lock:
            if not self._gateway_callback_is_current(gateway_generation):
                return
            self._outbox_flush_owner = current_task
            try:
                pending = await self.store.async_pending_outbox(limit=100)
                if not self._gateway_callback_is_current(gateway_generation):
                    return
                sent_any = False
                for record in pending:
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    if record.message_id in getattr(
                        self, "_active_send_message_ids", set()
                    ):
                        continue
                    desired_gateway = record.raw.get("gateway_id") or gateway_id
                    gateway = self._select_gateway(
                        gateway_id=desired_gateway,
                        target_node=record.receiver,
                    )
                    if gateway is None:
                        continue
                    await self.tx_limiter.acquire()
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    try:
                        provider_id = await gateway.async_send_message(
                            target_node=record.receiver,
                            message=record.text,
                            channel=record.channel,
                            priority=record.priority,
                            message_type=record.message_type,
                        )
                    except Exception as err:
                        if not self._gateway_callback_is_current(gateway_generation):
                            return
                        record.raw["status"] = "queued"
                        record.raw["last_error"] = str(err)
                        await self.store.async_add_message(record)
                        continue
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    record.gateway_id = gateway.config.gateway_id
                    record.protocol = gateway.config.protocol
                    record.raw["status"] = "sent"
                    record.raw["provider_id"] = provider_id
                    await self.store.async_add_message(record)
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    self.hass.bus.async_fire(EVENT_MESSAGE_SENT, record.as_dict())
                    sent_any = True
                if sent_any:
                    self.snapshot.recent_messages = await self.store.async_recent_messages(100)
                    if not self._gateway_callback_is_current(gateway_generation):
                        return
                    self.async_set_updated_data(self.snapshot)
            finally:
                self._outbox_flush_owner = None

    def _gateway_callback_is_current(
        self, gateway_generation: int | None
    ) -> bool:
        """Return whether provider work still belongs to the active gateways."""
        if getattr(self, "_shutting_down", False) or getattr(
            self, "_reconnect_suspended", False
        ):
            return False
        return gateway_generation is None or gateway_generation == getattr(
            self, "_gateway_generation", gateway_generation
        )

    def _schedule_reconnect(self, gateway_id: str) -> None:
        if self._shutting_down or self._reconnect_suspended or gateway_id in self._reconnect_tasks:
            return
        task = self._async_create_background_task(
            self._delayed_reconnect(gateway_id), "MeshNet gateway reconnect"
        )
        self._reconnect_tasks[gateway_id] = task

        def clear_reconnect(done_task: asyncio.Task[Any]) -> None:
            if self._reconnect_tasks.get(gateway_id) is done_task:
                self._reconnect_tasks.pop(gateway_id, None)

        task.add_done_callback(clear_reconnect)

    async def _delayed_reconnect(self, gateway_id: str) -> None:
        attempt = 0
        while not self._shutting_down and not self._reconnect_suspended:
            gateway = self.gateways.get(gateway_id)
            if gateway is None or gateway.status.connected:
                return

            # A Meshtastic BLE constructor is synchronous underneath its async
            # executor wrapper. Join that single-flight start instead of stopping
            # it or queueing another constructor in Home Assistant's executor.
            if getattr(gateway, "start_pending", False):
                try:
                    await gateway.async_start()
                except Exception:
                    pass
                continue

            await asyncio.sleep(self._reconnect_delay(attempt))
            if self._shutting_down or self._reconnect_suspended:
                return

            gateway = self.gateways.get(gateway_id)
            if gateway is None or gateway.status.connected:
                return
            if getattr(gateway, "start_pending", False):
                continue

            try:
                await gateway.async_stop()
                if self._shutting_down or self._reconnect_suspended:
                    return
                await gateway.async_start()
            except Exception as err:
                if self._shutting_down or self._reconnect_suspended:
                    return
                await gateway._emit_error(f"Reconnect failed: {err}")

            if gateway.status.connected:
                return
            attempt += 1

    @staticmethod
    def _reconnect_delay(attempt: int) -> float:
        """Return capped exponential reconnect delay with bounded jitter."""
        exponent = min(max(attempt, 0), 16)
        base_delay = min(_RECONNECT_INITIAL_DELAY * (2**exponent), _RECONNECT_MAX_DELAY)
        jitter = base_delay * _RECONNECT_JITTER_RATIO
        return min(
            _RECONNECT_MAX_DELAY,
            max(0.0, random.uniform(base_delay - jitter, base_delay + jitter)),
        )

    async def _cancel_reconnect_tasks(self) -> bool:
        """Cancel reconnect loops within a bound and retain pending owners."""
        reconnect_tasks = set(self._reconnect_tasks.values())
        if not reconnect_tasks:
            return True
        current_task = asyncio.current_task()
        waitable_tasks = reconnect_tasks - {current_task}
        for task in waitable_tasks:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set()
        if waitable_tasks:
            done, pending = await asyncio.wait(
                waitable_tasks, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT
            )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for gateway_id, task in list(self._reconnect_tasks.items()):
            if task in done:
                self._reconnect_tasks.pop(gateway_id, None)
        return not pending and current_task not in reconnect_tasks

    async def _cancel_gateway_startup_task(self) -> bool:
        """Cancel the startup waiter within a bound and report if it drained."""
        task = self._gateway_startup_task
        if task is None:
            return True
        if task is asyncio.current_task():
            return False
        if not task.done() and task.cancelling() == 0:
            task.cancel()
        done, _pending = await asyncio.wait(
            {task}, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT
        )
        if task not in done:
            # Keep the entry-owned task retained. Its original done callback
            # clears this reference if the provider eventually returns.
            return False
        await asyncio.gather(task, return_exceptions=True)
        if self._gateway_startup_task is task:
            self._gateway_startup_task = None
        return True

    async def _cancel_outbox_flush_owner(self) -> bool:
        """Cancel active outbox delivery within the gateway task bound."""
        task = getattr(self, "_outbox_flush_owner", None)
        if task is None:
            return True
        if task is asyncio.current_task():
            return False
        if not task.done() and task.cancelling() == 0:
            task.cancel()
        done, _pending = await asyncio.wait(
            {task}, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT
        )
        if task not in done:
            return False
        await asyncio.gather(task, return_exceptions=True)
        if self._outbox_flush_owner is task:
            self._outbox_flush_owner = None
        return True

    async def _cancel_send_tasks(self) -> bool:
        """Cancel active direct sends within the gateway task bound."""
        send_tasks = set(getattr(self, "_send_tasks", set()))
        if not send_tasks:
            return True
        current_task = asyncio.current_task()
        waitable_tasks = send_tasks - {current_task}
        for task in waitable_tasks:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set()
        if waitable_tasks:
            done, pending = await asyncio.wait(
                waitable_tasks, timeout=_GATEWAY_TASK_CANCEL_TIMEOUT
            )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
            self._send_tasks.difference_update(done)
        return not pending and current_task not in send_tasks

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

    @staticmethod
    def _issue_category(issue_id: str) -> str:
        """Return a fixed repair category without returning its identifier."""
        for category in _GATEWAY_ISSUE_CATEGORIES:
            if issue_id.startswith(f"{category}_"):
                return category
        if issue_id == "no_gateways":
            return "no_gateways"
        return "other"

    def _delete_legacy_gateway_issues(self) -> int:
        """Delete pre-v0.4.2 issue keys that embedded configured identities."""
        registry_getter = getattr(ir, "async_get", None)
        issue_deleter = getattr(ir, "async_delete_issue", None)
        hass = getattr(self, "hass", None)
        if not callable(registry_getter) or not callable(issue_deleter) or hass is None:
            return 0
        try:
            registry = registry_getter(hass)
            issues = getattr(registry, "issues", {})
            issue_keys = list(issues)
        except Exception:
            return 0

        deleted = 0
        for issue_key in issue_keys:
            if not isinstance(issue_key, tuple) or len(issue_key) != 2:
                continue
            domain, issue_id = issue_key
            if domain != DOMAIN or not isinstance(issue_id, str):
                continue
            category = self._issue_category(issue_id)
            if (
                category not in _GATEWAY_ISSUE_CATEGORIES
                or _SAFE_GATEWAY_ISSUE_RE.fullmatch(issue_id)
            ):
                continue
            try:
                issue_deleter(hass, DOMAIN, issue_id)
            except Exception as err:
                _LOGGER.debug(
                    "Could not remove one legacy MeshNet repair key: %s",
                    type(err).__name__,
                )
            else:
                deleted += 1
        return deleted

    def _repair_diagnostics(self) -> dict[str, Any]:
        """Return cached repair aggregates without issue identifiers or text."""
        registry_getter = getattr(ir, "async_get", None)
        hass = getattr(self, "hass", None)
        if not callable(registry_getter) or hass is None:
            return {"available": False}
        try:
            issues = getattr(registry_getter(hass), "issues", {})
            entries = [
                (issue_id, issue)
                for (domain, issue_id), issue in issues.items()
                if domain == DOMAIN and isinstance(issue_id, str)
            ]
        except Exception as err:
            return {
                "available": False,
                "collection_error": type(err).__name__,
            }

        categories = Counter(
            self._issue_category(issue_id) for issue_id, _issue in entries
        )
        return {
            "available": True,
            "issue_count": len(entries),
            "active_issue_count": sum(
                bool(getattr(issue, "active", False))
                for _issue_id, issue in entries
            ),
            "persistent_issue_count": sum(
                bool(getattr(issue, "is_persistent", False))
                for _issue_id, issue in entries
            ),
            "legacy_identity_issue_count": sum(
                self._issue_category(issue_id) in _GATEWAY_ISSUE_CATEGORIES
                and _SAFE_GATEWAY_ISSUE_RE.fullmatch(issue_id) is None
                for issue_id, _issue in entries
            ),
            "legacy_issues_removed_during_setup": getattr(
                self, "_legacy_issue_cleanup_count", 0
            ),
            "category_counts": dict(sorted(categories.items())),
        }

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

    def _gateway_issue_id(self, category: str, gateway_id: str) -> str:
        """Return a stable issue key that does not expose a gateway identifier."""
        gateway_configs = list(getattr(self, "_gateway_configs", []))
        for index, config in enumerate(gateway_configs, start=1):
            if config.gateway_id == gateway_id:
                return f"{category}_gateway_{index:03d}"
        gateway_ids = sorted(getattr(self, "gateways", {}))
        try:
            index = gateway_ids.index(gateway_id) + 1
        except ValueError:
            return f"{category}_gateway_unknown"
        return f"{category}_gateway_{index:03d}"

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
