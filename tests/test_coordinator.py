from __future__ import annotations

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace

from custom_components.meshnet.const import PROTOCOL_MESHTASTIC, TRANSPORT_TCP
from custom_components.meshnet.models import (
    GatewayConfig,
    GatewayStatus,
    MeshSnapshot,
    MessageRecord,
)


def _load_coordinator_without_home_assistant(monkeypatch):
    """Load the coordinator with minimal HA shims in the lightweight test env."""
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
    update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )

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

    module = importlib.import_module("custom_components.meshnet.coordinator")
    sys.modules.pop(module.__name__, None)
    return module.MeshNetCoordinator


def test_flush_outbox_ignores_reentrant_gateway_status_flush(monkeypatch) -> None:
    """A status emitted while sending must not reacquire the outbox lock."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        record = MessageRecord(
            message_id="queued-message",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-1",
            sender="homeassistant",
            receiver=None,
            channel=None,
            text="hello",
            direction="tx",
            raw={"status": "queued", "gateway_id": "gateway-1"},
        )

        class Store:
            async def async_pending_outbox(self, *, limit: int) -> list[MessageRecord]:
                assert limit == 100
                return [record] if record.raw["status"] == "queued" else []

            async def async_add_message(self, message: MessageRecord) -> None:
                assert message is record

            async def async_recent_messages(self, limit: int) -> list[MessageRecord]:
                assert limit == 100
                return [record]

        class Limiter:
            async def acquire(self) -> None:
                return None

        class Bus:
            def __init__(self) -> None:
                self.events: list[tuple[str, dict]] = []

            def async_fire(self, event: str, data: dict) -> None:
                self.events.append((event, data))

        coordinator = object.__new__(coordinator_class)
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._reconnect_tasks = {}
        coordinator._shutting_down = False
        coordinator.store = Store()
        coordinator.tx_limiter = Limiter()
        coordinator.snapshot = MeshSnapshot()
        coordinator.hass = SimpleNamespace(bus=Bus())
        coordinator.async_set_updated_data = lambda _snapshot: None

        class Gateway:
            def __init__(self) -> None:
                self.config = GatewayConfig(
                    gateway_id="gateway-1",
                    name="Gateway",
                    protocol=PROTOCOL_MESHTASTIC,
                    transport=TRANSPORT_TCP,
                )
                self.status = GatewayStatus(
                    gateway_id="gateway-1",
                    name="Gateway",
                    protocol=PROTOCOL_MESHTASTIC,
                    transport=TRANSPORT_TCP,
                    connected=True,
                )
                self.send_count = 0

            async def async_send_message(self, **_kwargs) -> str:
                self.send_count += 1
                await coordinator._handle_gateway_status(self.status)
                return "provider-message"

        gateway = Gateway()
        coordinator.gateways = {gateway.config.gateway_id: gateway}

        await asyncio.wait_for(coordinator._flush_outbox(), timeout=0.25)

        assert gateway.send_count == 1
        assert record.raw == {
            "status": "sent",
            "gateway_id": "gateway-1",
            "provider_id": "provider-message",
        }

    asyncio.run(run())


def test_diagnostics_exclude_gateway_identity_errors_and_mesh_content(monkeypatch) -> None:
    """Downloaded diagnostics must be useful without exposing private mesh data."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

        class Store:
            async def async_diagnostics(self) -> dict[str, int]:
                return {"node_count": 1, "message_count": 1, "packet_count": 1}

        coordinator = object.__new__(coordinator_class)
        coordinator.node_timeout = 900
        coordinator.history_days = 30
        coordinator.store = Store()
        coordinator.deduplicator = SimpleNamespace(
            stats=lambda: {"entries": 1, "total_packets": 1}
        )
        coordinator.tx_limiter = SimpleNamespace(
            snapshot=lambda: {"rate": 0.5, "capacity": 5.0, "tokens": 4.0}
        )
        coordinator.snapshot = MeshSnapshot(
            nodes={"private-node-id": object()},
            recent_messages=[
                MessageRecord(
                    message_id="private-message-id",
                    protocol=PROTOCOL_MESHTASTIC,
                    gateway_id="private-gateway-id",
                    sender="private-sender",
                    receiver=None,
                    channel="private-channel",
                    text="private message text",
                )
            ],
        )
        coordinator.gateways = {
            "private-gateway-id": SimpleNamespace(
                status=GatewayStatus(
                    gateway_id="private-gateway-id",
                    name="Private Home Gateway",
                    protocol=PROTOCOL_MESHTASTIC,
                    transport=TRANSPORT_TCP,
                    connected=False,
                    errors=["connection failed at 192.0.2.99 with token=private"],
                    detail={"host": "192.0.2.99"},
                )
            )
        }

        diagnostics = await coordinator.async_diagnostics()
        serialized = repr(diagnostics)

        assert diagnostics["configuration"]["gateway_count"] == 1
        assert diagnostics["gateways"][0]["error_count"] == 1
        for private_value in (
            "private-node-id",
            "private-message-id",
            "private-gateway-id",
            "Private Home Gateway",
            "private message text",
            "private-sender",
            "private-channel",
            "192.0.2.99",
            "token=private",
        ):
            assert private_value not in serialized

    asyncio.run(run())


def test_reconnect_loop_retries_with_increasing_backoff(monkeypatch) -> None:
    """A failed reconnect remains single-flight and retries until connected."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False

        delays = []
        original_sleep = asyncio.sleep

        async def fast_sleep(delay: float) -> None:
            delays.append(delay)
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        coordinator._reconnect_delay = lambda attempt: float(30 * (2**attempt))

        class Gateway:
            def __init__(self) -> None:
                self.status = SimpleNamespace(connected=False)
                self.start_pending = False
                self.start_calls = 0
                self.stop_calls = 0
                self.errors = []

            async def async_stop(self) -> None:
                self.stop_calls += 1

            async def async_start(self) -> None:
                self.start_calls += 1
                if self.start_calls < 3:
                    raise RuntimeError(f"failure {self.start_calls}")
                self.status.connected = True

            async def _emit_error(self, error: str) -> None:
                self.errors.append(error)

        gateway = Gateway()
        coordinator.gateways = {"gateway-1": gateway}

        await coordinator._delayed_reconnect("gateway-1")

        assert delays == [30.0, 60.0, 120.0]
        assert gateway.stop_calls == 3
        assert gateway.start_calls == 3
        assert gateway.errors == [
            "Reconnect failed: failure 1",
            "Reconnect failed: failure 2",
        ]

    asyncio.run(run())


def test_reconnect_joins_pending_start_before_retry(monkeypatch) -> None:
    """Reconnect must not stop or duplicate an in-flight BLE constructor."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_delay = lambda _attempt: 0.0

        order = []
        pending_joined = asyncio.Event()
        release_pending = asyncio.Event()
        original_sleep = asyncio.sleep

        async def fast_sleep(_delay: float) -> None:
            order.append("sleep")
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        class Gateway:
            def __init__(self) -> None:
                self.status = SimpleNamespace(connected=False)
                self.start_pending = True

            async def async_start(self) -> None:
                if self.start_pending:
                    order.append("join")
                    pending_joined.set()
                    await release_pending.wait()
                    self.start_pending = False
                    raise RuntimeError("original start failed")
                order.append("retry")
                self.status.connected = True

            async def async_stop(self) -> None:
                order.append("stop")

            async def _emit_error(self, _error: str) -> None:
                return None

        coordinator.gateways = {"gateway-1": Gateway()}
        reconnect_task = asyncio.create_task(coordinator._delayed_reconnect("gateway-1"))

        await pending_joined.wait()
        assert order == ["join"]
        release_pending.set()
        await reconnect_task

        assert order == ["join", "sleep", "stop", "retry"]

    asyncio.run(run())


def test_reconnect_backoff_is_exponential_jittered_and_capped(monkeypatch) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

    first = coordinator_class._reconnect_delay(0)
    second = coordinator_class._reconnect_delay(1)
    capped = coordinator_class._reconnect_delay(100)

    assert 24.0 <= first <= 36.0
    assert 48.0 <= second <= 72.0
    assert second > first
    assert 240.0 <= capped <= 300.0


def test_shutdown_cancels_reconnect_before_transport_restart(monkeypatch) -> None:
    """A reconnect sleeping during unload cannot restart its gateway."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_delay = lambda _attempt: 30.0

        sleep_started = asyncio.Event()

        async def blocking_sleep(_delay: float) -> None:
            sleep_started.set()
            await asyncio.Future()

        monkeypatch.setattr(asyncio, "sleep", blocking_sleep)

        class Gateway:
            def __init__(self) -> None:
                self.status = SimpleNamespace(connected=False)
                self.start_pending = False
                self.calls = []

            async def async_stop(self) -> None:
                self.calls.append("stop")

            async def async_start(self) -> None:
                self.calls.append("start")

            async def _emit_error(self, _error: str) -> None:
                return None

        gateway = Gateway()
        coordinator.gateways = {"gateway-1": gateway}
        reconnect_task = asyncio.create_task(coordinator._delayed_reconnect("gateway-1"))
        coordinator._reconnect_tasks = {"gateway-1": reconnect_task}

        await sleep_started.wait()
        coordinator._shutting_down = True
        coordinator._reconnect_suspended = True
        await coordinator._cancel_reconnect_tasks()

        assert reconnect_task.cancelled()
        assert coordinator._reconnect_tasks == {}
        assert gateway.calls == []

    asyncio.run(run())
