"""Backend contracts for manual RF work and Home Assistant automations.

These tests intentionally exercise public/coordinator seams instead of the panel.
The browser is never trusted to enforce airtime, identity, or privacy rules.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import sys
import types
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.meshnet.const import (
    MESSAGE_TYPE_DIRECT,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_TCP,
)
from custom_components.meshnet.gateway import MeshGateway
from custom_components.meshnet.meshcore_client import (
    meshcore_payload_to_node,
    meshcore_payload_to_packet,
)
from custom_components.meshnet.meshtastic_client import (
    meshtastic_packet_to_node,
    meshtastic_packet_to_state_packet,
)
from custom_components.meshnet.models import (
    GatewayConfig,
    GatewayStatus,
    MeshPacket,
    MeshSnapshot,
    MessageRecord,
    NodeState,
)
from custom_components.meshnet.store import MeshStore


def _load_coordinator_without_home_assistant(monkeypatch: pytest.MonkeyPatch):
    """Load the coordinator in both lightweight and HA-backed test jobs."""
    try:
        return importlib.import_module(
            "custom_components.meshnet.coordinator"
        ).MeshNetCoordinator
    except ModuleNotFoundError as err:
        if err.name != "homeassistant":
            raise

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")
    exceptions = types.ModuleType("homeassistant.exceptions")
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    class HomeAssistantError(Exception):
        pass

    config_entries.ConfigEntry = object
    core.HomeAssistant = object
    exceptions.HomeAssistantError = HomeAssistantError
    issue_registry.IssueSeverity = SimpleNamespace(ERROR="error", WARNING="warning")
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = RuntimeError
    helpers.issue_registry = issue_registry
    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.issue_registry": issue_registry,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop("custom_components.meshnet.coordinator", None)
    return importlib.import_module(
        "custom_components.meshnet.coordinator"
    ).MeshNetCoordinator


def _reservation_won(result: object) -> bool:
    """Read the semantic reservation result without fixing its DTO class."""
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        if "reserved" in result:
            return result["reserved"] is True
        return result.get("status") == "reserved"
    if hasattr(result, "reserved"):
        return result.reserved is True
    return getattr(result, "status", None) == "reserved"


def test_traceroute_cooldown_is_atomic_persisted_and_exact_at_boundary(
    tmp_path,
) -> None:
    """One SQLite reservation owns airtime for 3,600 seconds across reopen."""

    async def run() -> None:
        path = tmp_path / "meshnet.sqlite3"
        start = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        store = MeshStore(path)
        await store.async_open()

        racers = await asyncio.gather(
            *(
                store.async_reserve_traceroute(
                    "ble-gateway",
                    "meshtastic:!1234abcd",
                    cooldown_seconds=3600,
                    now=start,
                )
                for _ in range(12)
            )
        )
        assert sum(_reservation_won(result) for result in racers) == 1
        assert not _reservation_won(
            await store.async_reserve_traceroute(
                "ble-gateway",
                "meshtastic:!1234abcd",
                cooldown_seconds=3600,
                now=start + timedelta(seconds=3599, microseconds=999999),
            )
        )
        assert not _reservation_won(
            await store.async_reserve_traceroute(
                "second-gateway",
                "meshtastic:!99999999",
                cooldown_seconds=3600,
                now=start + timedelta(seconds=3599),
            )
        )
        await store.async_close()

        reopened = MeshStore(path)
        await reopened.async_open()
        status = await reopened.async_get_traceroute_status(
            "ble-gateway", "meshtastic:!1234abcd", now=start + timedelta(seconds=1)
        )
        assert status is not None
        assert not _reservation_won(
            await reopened.async_reserve_traceroute(
                "ble-gateway",
                "meshtastic:!1234abcd",
                cooldown_seconds=3600,
                now=start + timedelta(seconds=3599),
            )
        )
        assert _reservation_won(
            await reopened.async_reserve_traceroute(
                "second-gateway",
                "meshtastic:!99999999",
                cooldown_seconds=3600,
                now=start + timedelta(seconds=3600),
            )
        )
        assert not _reservation_won(
            await reopened.async_reserve_traceroute(
                "ble-gateway",
                "meshtastic:!1234abcd",
                cooldown_seconds=3600,
                now=start + timedelta(seconds=3600),
            )
        )
        await reopened.async_close()

    asyncio.run(run())


def test_traceroute_status_is_integration_wide_and_result_is_allowlisted(
    tmp_path,
) -> None:
    """Any selected target sees one global cooldown and only safe route evidence."""

    async def run() -> None:
        start = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        store = MeshStore(tmp_path / "meshnet.sqlite3")
        await store.async_open()

        first = await store.async_reserve_traceroute(
            "ble-gateway",
            "meshtastic:!1234abcd",
            cooldown_seconds=1,
            now=start,
        )
        assert _reservation_won(first)
        assert first["next_allowed_at"] == "2026-07-30T13:00:00+00:00"

        await store.async_store_traceroute_result(
            "ble-gateway",
            "meshtastic:!1234abcd",
            {
                "schema_version": 1,
                "gateway_id": "ble-gateway",
                "completed_at": "2026-07-30T12:00:05+00:00",
                "source": "meshtastic:!aaaaaaaa",
                "destination": "meshtastic:!1234abcd",
                "channel": 0,
                "forward_route": [
                    "meshtastic:!aaaaaaaa",
                    "meshtastic:!1234abcd",
                ],
                "reverse_route": ["meshtastic:!00000000"] * 65,
                "snr_towards": [-128, 0.25, 128],
                "snr_back": [math.nan],
                "correlation_id": "provider-correlation-must-not-persist",
                "status": "complete",
                "error_code": "private-provider-detail",
                "private_key": "private-key-must-not-persist",
                "session_passkey": "passkey-must-not-persist",
            },
            now=start + timedelta(seconds=5),
        )

        stored = await store._fetchone(  # noqa: SLF001 - verify durable data
            """
            SELECT result_data
            FROM traceroutes
            WHERE gateway_id = ? AND target_node = ?
            """,
            ("ble-gateway", "meshtastic:!1234abcd"),
        )
        assert stored is not None
        assert json.loads(stored["result_data"]) == {
            "schema_version": 1,
            "gateway_id": "ble-gateway",
            "completed_at": "2026-07-30T12:00:05+00:00",
            "source": "meshtastic:!aaaaaaaa",
            "destination": "meshtastic:!1234abcd",
            "channel": 0,
            "forward_route": [
                "meshtastic:!aaaaaaaa",
                "meshtastic:!1234abcd",
            ],
            "snr_towards": [-128.0, 0.25, 128.0],
        }

        status = await store.async_get_traceroute_status(
            "different-gateway",
            "meshtastic:!99999999",
            now=start + timedelta(seconds=1),
        )
        assert status == {
            "schema_version": 1,
            "scope": "integration",
            "reserved": True,
            "status": "cooldown",
            "gateway_id": "ble-gateway",
            "target_node": "meshtastic:!1234abcd",
            "reserved_at": "2026-07-30T12:00:00+00:00",
            "next_allowed_at": "2026-07-30T13:00:00+00:00",
            "remaining_seconds": 3599,
            "result_updated_at": "2026-07-30T12:00:05+00:00",
            "result": {
                "schema_version": 1,
                "gateway_id": "ble-gateway",
                "completed_at": "2026-07-30T12:00:05+00:00",
                "source": "meshtastic:!aaaaaaaa",
                "destination": "meshtastic:!1234abcd",
                "channel": 0,
                "forward_route": [
                    "meshtastic:!aaaaaaaa",
                    "meshtastic:!1234abcd",
                ],
                "snr_towards": [-128.0, 0.25, 128.0],
            },
        }
        blocked = await store.async_reserve_traceroute(
            "different-gateway",
            "meshtastic:!99999999",
            cooldown_seconds=1,
            now=start + timedelta(seconds=3599),
        )
        assert not _reservation_won(blocked)
        assert blocked["gateway_id"] == "ble-gateway"
        assert blocked["target_node"] == "meshtastic:!1234abcd"
        await store.async_close()

    asyncio.run(run())


def test_traceroute_prune_removes_only_expired_old_records(tmp_path) -> None:
    """History pruning retains an active integration-wide RF cooldown."""

    async def run() -> None:
        current = datetime.now(UTC)
        store = MeshStore(tmp_path / "meshnet.sqlite3")
        await store.async_open()
        await store.async_reserve_traceroute(
            "expired-gateway",
            "meshtastic:!11111111",
            now=current - timedelta(days=10),
        )
        active = await store.async_reserve_traceroute(
            "active-gateway",
            "meshtastic:!22222222",
            cooldown_seconds=10 * 24 * 60 * 60,
            now=current - timedelta(days=2),
        )
        assert _reservation_won(active)

        await store.async_prune(history_days=1)

        diagnostics = await store.async_diagnostics()
        assert diagnostics["traceroute_count"] == 1
        status = await store.async_get_global_traceroute_status(now=current)
        assert status is not None
        assert status["reserved"] is True
        assert status["gateway_id"] == "active-gateway"
        assert status["target_node"] == "meshtastic:!22222222"
        await store.async_close()

    asyncio.run(run())


def _traceroute_coordinator(
    coordinator_class,
    *,
    send_error: Exception | None = None,
    transport: str = TRANSPORT_BLUETOOTH,
):
    order: list[str] = []
    node = NodeState(
        node_key="meshtastic:!1234abcd",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!1234abcd",
        public_key="AQIDBA==",
    )
    config = GatewayConfig(
        gateway_id="ble-gateway",
        name="BLE gateway",
        protocol=PROTOCOL_MESHTASTIC,
        transport=transport,
    )

    class Store:
        reservations = 0
        reservation_args: list[tuple[str, str]] = []

        async def async_reserve_traceroute(self, gateway_id, target_node, **_kwargs):
            self.reservation_args.append((gateway_id, target_node))
            self.reservations += 1
            order.append("reserve")
            return {"reserved": self.reservations == 1}

        async def async_store_traceroute_result(self, *_args, **_kwargs):
            order.append("store-result")

    class Gateway:
        def __init__(self) -> None:
            self.config = config
            self.status = GatewayStatus(
                gateway_id=config.gateway_id,
                name=config.name,
                protocol=config.protocol,
                transport=config.transport,
                connected=True,
            )
            self.calls: list[str] = []
            self.local_node_id = "!aaaaaaaa"

        async def async_manual_traceroute(self, target_node: str):
            self.calls.append(target_node)
            order.append("send")
            if send_error is not None:
                raise send_error
            return {
                "correlation_id": "trace-correlation",
                "source": "!aaaaaaaa",
                "destination": target_node,
                "channel": 0,
                "forward_route": ["!aaaaaaaa", target_node],
            }

    gateway = Gateway()
    coordinator = object.__new__(coordinator_class)
    coordinator._gateway_generation = 1
    coordinator._shutting_down = False
    coordinator._reconnect_suspended = False
    coordinator.snapshot = MeshSnapshot(nodes={node.node_key: node})
    coordinator._node_alias_redirects = {node.node_key: node.node_key}
    coordinator.gateways = {config.gateway_id: gateway}
    coordinator._gateway_configs = [config]
    coordinator.store = Store()
    return coordinator, gateway, order


def test_manual_traceroute_reserves_before_one_exact_unicast_and_correlates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator reserves first and sends once to the canonical node ID."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator, gateway, order = _traceroute_coordinator(coordinator_class)

        result = await coordinator.async_manual_traceroute(
            gateway_id="ble-gateway",
            target_node="meshtastic:!1234abcd",
        )

        assert order[:2] == ["reserve", "send"]
        assert coordinator.store.reservation_args == [
            ("ble-gateway", "meshtastic:!1234abcd")
        ]
        assert gateway.calls == ["!1234abcd"]
        assert result["schema_version"] == 1
        assert result["correlation_id"] == "trace-correlation"
        assert result["destination"] == "meshtastic:!1234abcd"

    asyncio.run(run())


@pytest.mark.parametrize(
    "target",
    [None, "^all", "Field Node", "meshtastic:!aaaaaaaa", "meshtastic:!99999999"],
)
def test_manual_traceroute_rejects_non_unicast_targets_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
) -> None:
    """Broadcast, names, self, and unknown identities consume no cooldown."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        error_type = coordinator_class.async_manual_traceroute.__globals__[
            "HomeAssistantError"
        ]
        coordinator, gateway, _order = _traceroute_coordinator(coordinator_class)
        coordinator.snapshot.nodes["meshtastic:!1234abcd"].long_name = "Field Node"
        with pytest.raises(error_type):
            await coordinator.async_manual_traceroute(
                gateway_id="ble-gateway",
                target_node=target,
            )
        assert coordinator.store.reservations == 0
        assert gateway.calls == []

    asyncio.run(run())


def test_manual_traceroute_rejects_wrong_transport_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial RF implementation is Meshtastic Bluetooth only."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        error_type = coordinator_class.async_manual_traceroute.__globals__[
            "HomeAssistantError"
        ]
        coordinator, gateway, _order = _traceroute_coordinator(
            coordinator_class, transport=TRANSPORT_TCP
        )
        with pytest.raises(error_type):
            await coordinator.async_manual_traceroute(
                gateway_id="ble-gateway",
                target_node="meshtastic:!1234abcd",
            )
        assert coordinator.store.reservations == 0
        assert gateway.calls == []

    asyncio.run(run())


def test_manual_traceroute_timeout_consumes_reservation_and_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown RF outcome permits neither an automatic nor immediate retry."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        error_type = coordinator_class.async_manual_traceroute.__globals__[
            "HomeAssistantError"
        ]
        coordinator, gateway, order = _traceroute_coordinator(
            coordinator_class, send_error=TimeoutError("private endpoint timed out")
        )
        with pytest.raises(error_type):
            await coordinator.async_manual_traceroute(
                gateway_id="ble-gateway",
                target_node="meshtastic:!1234abcd",
            )
        assert order == ["reserve", "send"]
        assert gateway.calls == ["!1234abcd"]

        with pytest.raises(error_type):
            await coordinator.async_manual_traceroute(
                gateway_id="ble-gateway",
                target_node="meshtastic:!1234abcd",
            )
        assert gateway.calls == ["!1234abcd"]

    asyncio.run(run())


def test_radio_quiesce_cancels_traceroute_and_fences_new_rf_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unload/reload fencing must own traceroute tasks as well as admin writes."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator, gateway, _order = _traceroute_coordinator(coordinator_class)
        coordinator._traceroute_tasks = set()
        coordinator._radio_operations_accepting = True
        provider_started = asyncio.Event()

        async def blocked_traceroute(target_node: str):
            gateway.calls.append(target_node)
            provider_started.set()
            await asyncio.Future()

        gateway.async_manual_traceroute = blocked_traceroute
        coordinator.remote_admin = SimpleNamespace(
            async_quiesce=AsyncMock(return_value=True),
            resume=Mock(return_value=True),
        )

        trace_task = asyncio.create_task(
            coordinator.async_manual_traceroute(
                gateway_id="ble-gateway",
                target_node="meshtastic:!1234abcd",
            )
        )
        await provider_started.wait()

        assert await coordinator.async_quiesce_radio_operations() is True
        with pytest.raises(asyncio.CancelledError):
            await trace_task
        assert coordinator._traceroute_tasks == set()

        error_type = coordinator_class.async_manual_traceroute.__globals__[
            "HomeAssistantError"
        ]
        with pytest.raises(error_type, match="temporarily unavailable"):
            await coordinator.async_manual_traceroute(
                gateway_id="ble-gateway",
                target_node="meshtastic:!1234abcd",
            )
        assert coordinator.store.reservations == 1
        assert gateway.calls == ["!1234abcd"]

        assert coordinator.resume_radio_operations() is True
        coordinator.remote_admin.resume.assert_called_once_with()

    asyncio.run(run())


def test_cancellation_suppressing_traceroute_cannot_write_after_lifecycle_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late provider result cannot touch storage after bounded teardown wins."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_traceroute_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator, gateway, _order = _traceroute_coordinator(coordinator_class)
        coordinator._traceroute_tasks = set()
        coordinator._radio_operations_accepting = True
        coordinator.remote_admin = SimpleNamespace(
            async_quiesce=AsyncMock(return_value=True),
            resume=Mock(return_value=True),
        )
        coordinator.store.async_store_traceroute_result = AsyncMock()
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()

        async def stubborn_traceroute(target_node: str):
            provider_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_provider.wait()
            return {
                "correlation_id": "late-result",
                "source": "!aaaaaaaa",
                "destination": target_node,
                "channel": 0,
                "forward_route": ["!aaaaaaaa", target_node],
            }

        gateway.async_manual_traceroute = stubborn_traceroute
        trace_task = asyncio.create_task(
            coordinator.async_manual_traceroute(
                gateway_id="ble-gateway",
                target_node="meshtastic:!1234abcd",
            )
        )
        await provider_started.wait()

        assert await coordinator.async_quiesce_radio_operations() is False
        release_provider.set()
        error_type = coordinator_class.async_manual_traceroute.__globals__[
            "HomeAssistantError"
        ]
        with pytest.raises(error_type, match="lifecycle changed"):
            await trace_task

        coordinator.store.async_store_traceroute_result.assert_not_awaited()
        assert coordinator._traceroute_tasks == set()

    asyncio.run(run())


class _EventBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def _message_coordinator(coordinator_class, *, connected: bool, send_error=None):
    config = GatewayConfig(
        gateway_id="gateway-one",
        name="Gateway",
        protocol=PROTOCOL_MESHTASTIC,
        transport=TRANSPORT_TCP,
    )
    gateway = SimpleNamespace(
        config=config,
        status=GatewayStatus(
            gateway_id=config.gateway_id,
            name=config.name,
            protocol=config.protocol,
            transport=config.transport,
            connected=connected,
        ),
        async_send_message=AsyncMock(
            side_effect=send_error,
            return_value="provider-id",
        ),
    )
    bus = _EventBus()
    coordinator = object.__new__(coordinator_class)
    coordinator._gateway_generation = 1
    coordinator._shutting_down = False
    coordinator._reconnect_suspended = False
    coordinator._active_send_message_ids = set()
    coordinator._send_tasks = set()
    coordinator._gateway_configs = [config]
    coordinator._node_alias_redirects = {}
    coordinator.snapshot = MeshSnapshot()
    coordinator.gateways = {config.gateway_id: gateway}
    coordinator.store = SimpleNamespace(
        async_add_message=AsyncMock(),
        async_recent_messages=AsyncMock(return_value=[]),
    )
    coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
    coordinator.hass = SimpleNamespace(bus=bus)
    coordinator.async_set_updated_data = Mock()
    coordinator._create_issue = Mock()
    return coordinator, gateway, bus


def _only_status_event(bus: _EventBus) -> dict:
    events = [data for event, data in bus.events if event == "meshnet_message_status"]
    assert len(events) == 1
    data = events[0]
    assert data["schema_version"] == 1
    assert isinstance(data["message_id"], str) and data["message_id"]
    assert not ({"text", "message", "raw", "payload", "exception"} & data.keys())
    return data


@pytest.mark.parametrize(
    ("connected", "send_error", "expected_status", "retryable"),
    [
        (True, None, "sent", False),
        (False, None, "queued", True),
        (True, RuntimeError("private /dev/tty error"), "failed", True),
    ],
)
def test_send_attempt_emits_one_safe_correlated_status(
    monkeypatch: pytest.MonkeyPatch,
    connected: bool,
    send_error: Exception | None,
    expected_status: str,
    retryable: bool,
) -> None:
    """Immediate, offline, and failed submissions have one safe outcome event."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator, _gateway, bus = _message_coordinator(
            coordinator_class,
            connected=connected,
            send_error=send_error,
        )
        message_id = await coordinator.async_send_message(
            target_node=None,
            message="private message text",
            channel="0",
            priority="normal",
            message_type="broadcast",
            gateway_id="gateway-one",
        )
        event = _only_status_event(bus)
        correlation = (
            message_id.get("message_id")
            if isinstance(message_id, Mapping)
            else message_id
        )
        assert event["message_id"] == correlation
        assert event["status"] == expected_status
        assert event["retryable"] is retryable
        serialized = json.dumps(event)
        assert "private message text" not in serialized
        assert "/dev/tty" not in serialized
        if send_error is not None:
            assert event["error_code"] == "send_failed"

    asyncio.run(run())


def test_send_action_returns_versioned_correlation_and_durable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA action responses expose acceptance, not logs or provider details."""

    class FakeSchema:
        def __init__(self, value):
            self.value = value

    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Required = lambda key, **_kwargs: key
    voluptuous.Optional = lambda key, **_kwargs: key
    voluptuous.In = lambda values: tuple(values)
    voluptuous.Schema = FakeSchema
    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = str
    monkeypatch.setitem(sys.modules, "voluptuous", voluptuous)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.config_validation", cv)

    registrations: dict[str, tuple[object, dict]] = {}

    class Services:
        @staticmethod
        def has_service(_domain: str, _service: str) -> bool:
            return False

        @staticmethod
        def async_register(domain, service, handler, **kwargs) -> None:
            assert domain == "meshnet"
            registrations[service] = (handler, kwargs)

    response = {
        "schema_version": 1,
        "message_id": "correlation-one",
        "status": "queued",
    }
    coordinator = SimpleNamespace(async_send_message=AsyncMock(return_value=response))
    hass = SimpleNamespace(services=Services(), data={"meshnet": {"entry": coordinator}})
    integration = importlib.import_module("custom_components.meshnet")
    integration._async_register_services(hass)

    handler, registration = registrations["send_message"]
    supports_response = registration.get("supports_response")
    assert supports_response is not None
    result = asyncio.run(
        handler(
            SimpleNamespace(
                data={
                    "target_node": "meshtastic:!1234abcd",
                    "message": "private message",
                    "message_type": "direct",
                }
            )
        )
    )
    assert result == response
    assert set(result) == {"schema_version", "message_id", "status"}
    assert "private message" not in json.dumps(result)


def test_blocked_outbox_and_later_replay_each_emit_one_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent rejection and a separate replay attempt are both observable."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        blocked = MessageRecord(
            message_id="blocked-correlation",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-one",
            sender="homeassistant",
            receiver=None,
            channel="0",
            text="private blocked text",
            message_type="direct",
            direction="tx",
            raw={"status": "queued", "gateway_id": "gateway-one"},
        )
        replay = MessageRecord(
            message_id="replay-correlation",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-one",
            sender="homeassistant",
            receiver=None,
            channel="0",
            text="private replay text",
            direction="tx",
            raw={"status": "queued", "gateway_id": "gateway-one"},
        )
        coordinator, gateway, bus = _message_coordinator(
            coordinator_class, connected=True
        )
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator.store.async_pending_outbox = AsyncMock(
            side_effect=[[blocked, replay], []]
        )
        coordinator.store.async_recent_messages = AsyncMock(
            return_value=[blocked, replay]
        )

        await coordinator._flush_outbox(gateway_generation=1)

        events = [data for name, data in bus.events if name == "meshnet_message_status"]
        assert [(event["message_id"], event["status"]) for event in events] == [
            ("blocked-correlation", "blocked"),
            ("replay-correlation", "sent"),
        ]
        assert all("text" not in event and "raw" not in event for event in events)
        gateway.async_send_message.assert_awaited_once()

    asyncio.run(run())


def test_gateway_status_event_fires_once_per_real_transition_and_ignores_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connectivity automations receive bounded transitions, never poll noise."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        config = GatewayConfig(
            gateway_id="gateway-one",
            name="Private Gateway Name",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_BLUETOOTH,
        )
        initial = GatewayStatus(
            gateway_id=config.gateway_id,
            name=config.name,
            protocol=config.protocol,
            transport=config.transport,
            connected=False,
        )
        gateway = SimpleNamespace(config=config, status=initial)
        bus = _EventBus()
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 4
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        coordinator.gateways = {config.gateway_id: gateway}
        coordinator.snapshot = MeshSnapshot(gateways={config.gateway_id: initial})
        coordinator.hass = SimpleNamespace(bus=bus)
        coordinator._flush_outbox = AsyncMock()
        coordinator._schedule_reconnect = Mock()
        coordinator._delete_resolved_issue = Mock()
        coordinator.async_set_updated_data = Mock()

        await coordinator._handle_gateway_status(initial, gateway_generation=4)
        assert [name for name, _data in bus.events if name == "meshnet_gateway_status"] == []

        connected = GatewayStatus(
            gateway_id=config.gateway_id,
            name=config.name,
            protocol=config.protocol,
            transport=config.transport,
            connected=True,
        )
        gateway.status = connected
        await coordinator._handle_gateway_status(connected, gateway_generation=4)
        await coordinator._handle_gateway_status(connected, gateway_generation=4)

        stale = GatewayStatus(
            gateway_id=config.gateway_id,
            name=config.name,
            protocol=config.protocol,
            transport=config.transport,
            connected=False,
        )
        gateway.status = stale
        await coordinator._handle_gateway_status(stale, gateway_generation=3)

        events = [data for name, data in bus.events if name == "meshnet_gateway_status"]
        assert len(events) == 1
        event = events[0]
        assert event["schema_version"] == 1
        assert event["gateway_id"] == "gateway-one"
        assert event["previous_connected"] is False
        assert event["connected"] is True
        assert event["transition"] == "connected"
        assert isinstance(event["failure_count"], int)
        assert not ({"raw", "detail", "errors", "exception", "name"} & event.keys())
        assert "Private Gateway Name" not in json.dumps(event)

    asyncio.run(run())


def test_gateway_failure_count_is_monotonic_beyond_bounded_error_history() -> None:
    """The automation counter must continue after private error history is capped."""

    class Gateway(MeshGateway):
        async def async_start(self) -> None:
            return None

        async def async_stop(self) -> None:
            return None

        async def async_send_message(self, **_kwargs) -> str:
            return "unused"

    async def run() -> None:
        gateway = Gateway(
            SimpleNamespace(),
            GatewayConfig(
                gateway_id="gateway-one",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_BLUETOOTH,
            ),
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            Mock(),
        )
        for index in range(25):
            await gateway._emit_error(f"private endpoint failure {index}")
        assert gateway.status.failure_count == 25
        assert len(gateway.status.errors) <= 20

    asyncio.run(run())


def test_received_and_compatibility_packet_events_are_bounded_and_raw_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automation events expose decoded text/metadata, never provider objects."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator, gateway, bus = _message_coordinator(
            coordinator_class, connected=True
        )
        coordinator.deduplicator = SimpleNamespace(is_duplicate=lambda _packet: False)
        coordinator.store.async_add_packet = AsyncMock()
        packet = MeshPacket(
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id=gateway.config.gateway_id,
            packet_id="packet-one",
            sender="!1234abcd",
            receiver="!ffffffff",
            channel="0",
            portnum="TEXT_MESSAGE_APP",
            payload={"private": "payload-sentinel"},
            text="bounded received text",
            rssi=0,
            snr=0,
            raw={"private_key": "private-sentinel", "nested": {"token": "secret"}},
        )

        await coordinator._handle_packet(packet, gateway_generation=1)

        packet_event = next(data for name, data in bus.events if name == "meshnet_packet")
        received = next(
            data for name, data in bus.events if name == "meshnet_message_received"
        )
        assert packet_event["schema_version"] == 1
        assert not ({"raw", "payload"} & packet_event.keys())
        assert received["schema_version"] == 1
        assert received["text"] == "bounded received text"
        assert received["delivery"] in {"direct", "channel", "broadcast", "unknown"}
        assert not ({"raw", "payload", "exception"} & received.keys())
        serialized = json.dumps(bus.events)
        assert "private-sentinel" not in serialized
        assert "payload-sentinel" not in serialized

    asyncio.run(run())


def test_message_projection_keeps_primary_and_other_channel_threads_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real broadcast destinations must not hide channel 1-7 conversations."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    project = coordinator_class._handle_packet.__globals__["message_api_dict"]

    def record(*, receiver: str | None, channel: str | None, direction: str = "rx"):
        return MessageRecord(
            message_id=f"{direction}-{receiver}-{channel}",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="ble-gateway",
            sender="!01020304" if direction == "rx" else "homeassistant",
            receiver=receiver,
            channel=channel,
            text="thread test",
            direction=direction,
        )

    incoming_channel = project(record(receiver="^all", channel="3"))
    outgoing_channel = project(
        record(receiver=None, channel="3", direction="tx")
    )
    primary = project(record(receiver=None, channel=None, direction="tx"))
    direct = project(record(receiver="!1234abcd", channel="3", direction="tx"))

    assert (incoming_channel["delivery"], incoming_channel["channel"]) == (
        "channel",
        "3",
    )
    assert (outgoing_channel["delivery"], outgoing_channel["channel"]) == (
        "channel",
        "3",
    )
    assert (primary["delivery"], primary["channel"]) == ("broadcast", "0")
    assert direct["delivery"] == "direct"
    assert direct["peer_node_key"] == "meshtastic:!1234abcd"


def test_message_projection_keeps_meshcore_direct_and_channel_threads_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MeshCore contact events use exact public-key peers, never channel guesses."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    project = coordinator_class._handle_packet.__globals__["message_api_dict"]

    incoming_direct = project(
        MessageRecord(
            message_id="meshcore-rx-direct",
            protocol=PROTOCOL_MESHCORE,
            gateway_id="meshcore-gateway",
            sender="abcdef012345",
            receiver=None,
            channel=None,
            text="private reply",
            message_type=MESSAGE_TYPE_DIRECT,
            direction="rx",
        )
    )
    outgoing_direct = project(
        MessageRecord(
            message_id="meshcore-tx-direct",
            protocol=PROTOCOL_MESHCORE,
            gateway_id="meshcore-gateway",
            sender="homeassistant",
            receiver="pub:abcdef012345",
            channel=None,
            text="private request",
            message_type=MESSAGE_TYPE_DIRECT,
            direction="tx",
        )
    )
    channel_message = project(
        MessageRecord(
            message_id="meshcore-channel",
            protocol=PROTOCOL_MESHCORE,
            gateway_id="meshcore-gateway",
            sender="abcdef012345",
            receiver=None,
            channel="2",
            text="channel text",
            direction="rx",
        )
    )

    assert (incoming_direct["delivery"], incoming_direct["peer_node_key"]) == (
        "direct",
        "pub:abcdef012345",
    )
    assert (outgoing_direct["delivery"], outgoing_direct["peer_node_key"]) == (
        "direct",
        "pub:abcdef012345",
    )
    assert (channel_message["delivery"], channel_message["channel"]) == (
        "channel",
        "2",
    )
    assert channel_message["peer_node_key"] is None


def test_meshcore_contact_event_is_persisted_as_direct_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normalized SDK event type must survive the common message boundary."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator, gateway, _bus = _message_coordinator(
            coordinator_class, connected=True
        )
        coordinator.deduplicator = SimpleNamespace(
            is_duplicate=lambda _packet: False
        )
        coordinator.store.async_add_packet = AsyncMock()
        coordinator.store.async_add_message = AsyncMock()

        await coordinator._handle_packet(
            MeshPacket(
                protocol=PROTOCOL_MESHCORE,
                gateway_id=gateway.config.gateway_id,
                packet_id="meshcore-contact",
                sender="abcdef012345",
                portnum="contact_message",
                text="private reply",
            ),
            gateway_generation=1,
        )

        record = coordinator.store.async_add_message.await_args.args[0]
        assert record.message_type == MESSAGE_TYPE_DIRECT
        assert record.direction == "rx"
        assert record.sender == "abcdef012345"

    asyncio.run(run())


@pytest.mark.parametrize("portnum", ["ADMIN_APP", "CONFIG_APP", "UNKNOWN_APP"])
def test_admin_config_and_unknown_ports_never_reach_events_or_storage(
    monkeypatch: pytest.MonkeyPatch,
    portnum: str,
) -> None:
    """The common boundary fails closed even if a provider callback bypasses SDK filtering."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator, gateway, bus = _message_coordinator(
            coordinator_class, connected=True
        )
        coordinator.deduplicator = SimpleNamespace(is_duplicate=lambda _packet: False)
        coordinator.store.async_add_packet = AsyncMock()
        coordinator.store.async_add_message = AsyncMock()

        await coordinator._handle_packet(
            MeshPacket(
                protocol=PROTOCOL_MESHTASTIC,
                gateway_id=gateway.config.gateway_id,
                sender="!1234abcd",
                portnum=portnum,
                payload="private-session-passkey",
                text="private-channel-psk",
                raw={"private_key": "private-key-sentinel"},
            ),
            gateway_generation=1,
        )

        assert bus.events == []
        coordinator.store.async_add_packet.assert_not_awaited()
        coordinator.store.async_add_message.assert_not_awaited()

    asyncio.run(run())


def test_passive_telemetry_is_finite_bounded_allowlisted_and_zero_safe() -> None:
    """Both protocols retain valid zeroes while discarding hostile metrics."""
    hostile = {f"unknown_metric_{index}": index for index in range(500)}
    hostile.update(
        {
            "temperature": 0,
            "humidity": 0.0,
            "pressure": float("nan"),
            "co2": float("inf"),
            "private_key": "private-sentinel",
            "x" * 1000: "oversized",
        }
    )
    meshtastic = meshtastic_packet_to_state_packet(
        {
            "id": 0,
            "fromId": "!1234abcd",
            "rxRssi": 0,
            "rxSnr": 0.0,
            "hopStart": 1,
            "hopLimit": 1,
            "decoded": {
                "portnum": "TELEMETRY_APP",
                "telemetry": {"environmentMetrics": hostile},
            },
        },
        gateway_id="ble-gateway",
    )
    meshtastic_node = meshtastic_packet_to_node(meshtastic)
    assert meshtastic.packet_id == "0"
    assert meshtastic.rssi == 0
    assert meshtastic.snr == 0
    assert meshtastic.hops == 0
    assert meshtastic_node is not None
    assert meshtastic_node.sensors["temperature"] == 0
    assert meshtastic_node.sensors["humidity"] == 0
    assert "private_key" not in meshtastic_node.sensors
    assert len(meshtastic_node.sensors) <= 64

    meshcore_packet = meshcore_payload_to_packet(
        {
            "type": "telemetry",
            "payload": {
                "id": 0,
                "sender": "node-one",
                "rssi": 0,
                "snr": 0.0,
                "hops": 0,
                "data": 0,
            },
        },
        gateway_id="meshcore-gateway",
    )
    meshcore_node = meshcore_payload_to_node(
        {
            "id": "node-one",
            "battery": 0,
            "voltage": 0.0,
            "latitude": 0,
            "longitude": 12.5,
            "sensors": hostile,
        },
        "meshcore-gateway",
        packet=meshcore_packet,
    )
    assert meshcore_packet.packet_id == "0"
    assert meshcore_packet.payload == 0
    assert meshcore_packet.rssi == 0
    assert meshcore_packet.snr == 0
    assert meshcore_packet.hops == 0
    assert meshcore_node is not None
    assert meshcore_node.power["battery_level"] == 0
    assert meshcore_node.power["voltage"] == 0
    assert meshcore_node.location["latitude"] == 0
    assert meshcore_node.sensors["temperature"] == 0
    assert "private_key" not in meshcore_node.sensors
    assert len(meshcore_node.sensors) <= 64
    for node in (meshtastic_node, meshcore_node):
        assert all(
            not isinstance(value, float) or math.isfinite(value)
            for value in node.sensors.values()
        )


def test_malformed_or_oversized_meshtastic_text_is_not_projected() -> None:
    """An untrusted bridge cannot place arbitrary objects or huge text in HA."""
    for text in ({"private": "object"}, "x" * 238):
        packet = meshtastic_packet_to_state_packet(
            {
                "fromId": "!1234abcd",
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
            },
            gateway_id="gateway-one",
        )
        assert packet.text is None
