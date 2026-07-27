from __future__ import annotations

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.meshnet.const import PROTOCOL_MESHTASTIC, TRANSPORT_TCP
from custom_components.meshnet.models import (
    GatewayConfig,
    GatewayStatus,
    MeshPacket,
    MeshSnapshot,
    MessageRecord,
    NodeState,
)


def _load_coordinator_without_home_assistant(monkeypatch):
    """Load the coordinator with minimal HA shims in the lightweight test env."""
    try:
        coordinator_class = importlib.import_module(
            "custom_components.meshnet.coordinator"
        ).MeshNetCoordinator

        async def async_shutdown(self) -> None:
            self._super_shutdown_called = True

        monkeypatch.setattr(
            coordinator_class.__mro__[1], "async_shutdown", async_shutdown
        )
        return coordinator_class
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

        async def async_shutdown(self) -> None:
            self._super_shutdown_called = True

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


def test_coordinator_first_refresh_does_not_start_radio_transports(
    monkeypatch,
) -> None:
    """Keep blocking radio SDK constructors out of config-entry setup."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

        class Store:
            async def async_open(self) -> None:
                return None

            async def async_load_snapshot(self, *, recent_limit: int):
                assert recent_limit == 100
                return MeshSnapshot()

        coordinator = object.__new__(coordinator_class)
        coordinator.store = Store()
        coordinator.snapshot = MeshSnapshot()
        coordinator._rebuild_gateways = AsyncMock()
        coordinator._start_gateways = AsyncMock(
            side_effect=AssertionError("radio SDK started during first refresh")
        )

        await asyncio.wait_for(coordinator._async_setup(), timeout=0.1)

        coordinator._rebuild_gateways.assert_awaited_once_with()
        coordinator._start_gateways.assert_not_awaited()

    asyncio.run(run())


def test_start_gateways_is_noop_after_shutdown_begins(monkeypatch) -> None:
    """Late background callbacks must not start transports during unload."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = True
        coordinator.gateways = {"gateway-1": object()}
        coordinator._start_gateways = AsyncMock()
        coordinator._flush_outbox = AsyncMock()

        await coordinator.async_start_gateways()

        coordinator._start_gateways.assert_not_awaited()
        coordinator._flush_outbox.assert_not_awaited()

    asyncio.run(run())


def test_shutdown_cancels_queued_startup_before_stopping_gateway(
    monkeypatch,
) -> None:
    """A retained startup that has not run is canceled and drained first."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        order: list[str] = []

        async def queued_startup() -> None:
            order.append("startup-ran")

        startup_task = asyncio.create_task(queued_startup())
        coordinator._gateway_startup_task = startup_task

        class Gateway:
            async def async_stop(self) -> None:
                assert startup_task.done()
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()

        # Do not yield between task creation and shutdown: this represents an
        # entry-owned startup queued by setup while unload begins immediately.
        await coordinator.async_shutdown()

        assert startup_task.cancelled()
        assert coordinator._gateway_startup_task is None
        assert coordinator._super_shutdown_called is True
        assert order == ["stop", "close"]

    asyncio.run(run())


def test_shutdown_drains_active_startup_before_gateway_stop(monkeypatch) -> None:
    """Startup cancellation must finish before transport stop can run."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        order: list[str] = []
        startup_active = asyncio.Event()
        gateway_stopped = False

        async def active_startup() -> None:
            order.append("startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                order.append("startup-cancelled")
                # Cancellation cleanup is allowed to yield. Shutdown must
                # still drain it before it stops any gateway.
                await asyncio.sleep(0)
                assert gateway_stopped is False
                order.append("startup-drained")
                raise

        startup_task = asyncio.create_task(active_startup())
        coordinator._gateway_startup_task = startup_task

        class Gateway:
            async def async_stop(self) -> None:
                nonlocal gateway_stopped
                gateway_stopped = True
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()

        await startup_active.wait()
        await coordinator.async_shutdown()

        assert startup_task.cancelled()
        assert order == [
            "startup-active",
            "startup-cancelled",
            "startup-drained",
            "stop",
            "close",
        ]

    asyncio.run(run())


def test_stubborn_startup_cancel_is_bounded_and_retained(monkeypatch) -> None:
    """A provider that suppresses cancellation cannot hang the coordinator."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_gateway_startup_task.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._gateway_startup_task = None
        startup_active = asyncio.Event()
        cancellation_ignored = asyncio.Event()
        release_startup = asyncio.Event()

        async def stubborn_startup() -> None:
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_ignored.set()
                await release_startup.wait()

        coordinator.async_start_gateways = stubborn_startup
        coordinator._async_create_background_task = (
            lambda target, _name: asyncio.create_task(target)
        )
        coordinator.async_start_gateways_background()
        startup_task = coordinator._gateway_startup_task
        assert startup_task is not None

        await startup_active.wait()
        assert await coordinator._cancel_gateway_startup_task() is False

        assert cancellation_ignored.is_set()
        assert coordinator._gateway_startup_task is startup_task
        assert not startup_task.done()

        release_startup.set()
        await startup_task
        await asyncio.sleep(0)

        assert coordinator._gateway_startup_task is None

    asyncio.run(run())


def test_stubborn_reconnect_cancel_is_bounded_retained_and_not_repeated(
    monkeypatch,
) -> None:
    """A cancellation-suppressing reconnect stays owned without cancel spam."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_reconnect_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._reconnect_tasks = {}
        reconnect_active = asyncio.Event()
        cancellation_ignored = asyncio.Event()
        release_reconnect = asyncio.Event()

        async def stubborn_reconnect() -> None:
            reconnect_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_ignored.set()
                await release_reconnect.wait()

        reconnect_task = asyncio.create_task(stubborn_reconnect())
        coordinator._reconnect_tasks["gateway-1"] = reconnect_task

        def clear_reconnect(done_task: asyncio.Task[None]) -> None:
            if coordinator._reconnect_tasks.get("gateway-1") is done_task:
                coordinator._reconnect_tasks.pop("gateway-1", None)

        reconnect_task.add_done_callback(clear_reconnect)

        await reconnect_active.wait()
        assert await coordinator._cancel_reconnect_tasks() is False
        first_cancel_count = reconnect_task.cancelling()

        assert cancellation_ignored.is_set()
        assert first_cancel_count == 1
        assert coordinator._reconnect_tasks == {"gateway-1": reconnect_task}
        assert await coordinator._cancel_reconnect_tasks() is False
        assert reconnect_task.cancelling() == first_cancel_count

        release_reconnect.set()
        await reconnect_task
        await asyncio.sleep(0)

        assert coordinator._reconnect_tasks == {}

    asyncio.run(run())


def test_shutdown_continues_when_startup_ignores_cancellation(monkeypatch) -> None:
    """A stuck startup cannot hold unload open or flush after transport stop."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_gateway_startup_task.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        coordinator._gateway_startup_task = None
        order: list[str] = []
        startup_active = asyncio.Event()
        release_startup = asyncio.Event()

        async def stubborn_gateway_start() -> None:
            order.append("startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_startup.wait()
            order.append("late-start-finished")

        coordinator._start_gateways = stubborn_gateway_start
        coordinator._flush_outbox = AsyncMock()
        coordinator._async_create_background_task = (
            lambda target, _name: asyncio.create_task(target)
        )

        class Gateway:
            config = SimpleNamespace(gateway_id="gateway-1")

            async def async_stop(self) -> None:
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()
        coordinator.async_start_gateways_background()
        startup_task = coordinator._gateway_startup_task
        assert startup_task is not None

        await startup_active.wait()
        await asyncio.wait_for(coordinator.async_shutdown(), timeout=0.1)

        assert coordinator._gateway_startup_task is startup_task
        assert order == ["startup-active", "stop", "close"]
        coordinator._flush_outbox.assert_not_awaited()

        release_startup.set()
        await startup_task
        await asyncio.sleep(0)

        assert order == [
            "startup-active",
            "stop",
            "close",
            "late-start-finished",
        ]
        assert coordinator._gateway_startup_task is None
        coordinator._flush_outbox.assert_not_awaited()

    asyncio.run(run())


def test_shutdown_continues_when_reconnect_ignores_cancellation(
    monkeypatch,
) -> None:
    """A stuck reconnect cannot hold gateway stop or store close open."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_reconnect_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_startup_task = None
        coordinator._reconnect_tasks = {}
        order: list[str] = []
        reconnect_active = asyncio.Event()
        release_reconnect = asyncio.Event()

        async def stubborn_reconnect() -> None:
            order.append("reconnect-active")
            reconnect_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_reconnect.wait()
            order.append("late-reconnect-finished")

        reconnect_task = asyncio.create_task(stubborn_reconnect())
        coordinator._reconnect_tasks["gateway-1"] = reconnect_task

        def clear_reconnect(done_task: asyncio.Task[None]) -> None:
            if coordinator._reconnect_tasks.get("gateway-1") is done_task:
                coordinator._reconnect_tasks.pop("gateway-1", None)

        reconnect_task.add_done_callback(clear_reconnect)

        class Gateway:
            config = SimpleNamespace(gateway_id="gateway-1")

            async def async_stop(self) -> None:
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()

        await reconnect_active.wait()
        await asyncio.wait_for(coordinator.async_shutdown(), timeout=0.1)

        assert coordinator._reconnect_tasks == {"gateway-1": reconnect_task}
        assert order == ["reconnect-active", "stop", "close"]

        release_reconnect.set()
        await reconnect_task
        await asyncio.sleep(0)

        assert order == [
            "reconnect-active",
            "stop",
            "close",
            "late-reconnect-finished",
        ]
        assert coordinator._reconnect_tasks == {}

    asyncio.run(run())


def test_reload_cancels_old_startup_before_stop_and_rebuild(monkeypatch) -> None:
    """Reload cannot let an old startup race the replacement gateways."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        coordinator.entry = object()
        order: list[str] = []
        startup_active = asyncio.Event()

        async def old_startup() -> None:
            order.append("old-startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                order.append("old-startup-drained")
                raise

        startup_task = asyncio.create_task(old_startup())
        coordinator._gateway_startup_task = startup_task

        class Gateway:
            async def async_stop(self) -> None:
                assert startup_task.done()
                order.append("old-stop")

        coordinator.gateways = {"old-gateway": Gateway()}

        def load_gateway_configs(entry):
            assert entry is coordinator.entry
            order.append("load")
            return ["replacement-config"]

        async def rebuild_gateways() -> None:
            assert coordinator._gateway_configs == ["replacement-config"]
            order.append("rebuild")

        def start_gateways_background() -> None:
            assert coordinator._reconnect_suspended is False
            order.append("new-startup")

        coordinator._load_gateway_configs = load_gateway_configs
        coordinator._rebuild_gateways = rebuild_gateways
        coordinator.async_start_gateways_background = start_gateways_background

        await startup_active.wait()
        await coordinator.async_reload_gateways()

        assert startup_task.cancelled()
        assert coordinator._gateway_startup_task is None
        assert order == [
            "old-startup-active",
            "old-startup-drained",
            "old-stop",
            "load",
            "rebuild",
            "new-startup",
        ]

    asyncio.run(run())


def test_reload_defers_until_stubborn_startup_finishes(monkeypatch) -> None:
    """A timed-out old startup cannot overlap replacement gateway objects."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_gateway_startup_task.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        coordinator.entry = object()
        coordinator._gateway_startup_task = None
        order: list[str] = []
        startup_active = asyncio.Event()
        release_startup = asyncio.Event()

        async def stubborn_startup() -> None:
            order.append("old-startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_startup.wait()
            order.append("old-startup-finished")

        coordinator.async_start_gateways = stubborn_startup
        coordinator._async_create_background_task = (
            lambda target, _name: asyncio.create_task(target)
        )

        class Gateway:
            async def async_stop(self) -> None:
                order.append("old-stop")

        coordinator.gateways = {"old-gateway": Gateway()}

        def load_gateway_configs(_entry):
            order.append("load")
            return ["replacement-config"]

        async def rebuild_gateways() -> None:
            order.append("rebuild")

        replacement_starts = 0

        def start_replacement() -> None:
            nonlocal replacement_starts
            replacement_starts += 1
            order.append("new-startup")

        coordinator._load_gateway_configs = load_gateway_configs
        coordinator._rebuild_gateways = rebuild_gateways
        coordinator.async_start_gateways_background()
        startup_task = coordinator._gateway_startup_task
        assert startup_task is not None
        coordinator.async_start_gateways_background = start_replacement

        await startup_active.wait()
        await asyncio.wait_for(coordinator.async_reload_gateways(), timeout=0.1)

        assert coordinator._reconnect_suspended is False
        assert coordinator._gateway_startup_task is startup_task
        assert replacement_starts == 0
        assert order == ["old-startup-active"]

        release_startup.set()
        await startup_task
        await asyncio.sleep(0)
        assert coordinator._gateway_startup_task is None

        await coordinator.async_reload_gateways()

        assert coordinator._reconnect_suspended is False
        assert replacement_starts == 1
        assert order == [
            "old-startup-active",
            "old-startup-finished",
            "old-stop",
            "load",
            "rebuild",
            "new-startup",
        ]

    asyncio.run(run())


def test_reload_defers_until_stubborn_reconnect_finishes(monkeypatch) -> None:
    """A timed-out reconnect cannot race stopped or replacement gateways."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_reconnect_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_startup_task = None
        coordinator._reconnect_tasks = {}
        coordinator.entry = object()
        order: list[str] = []
        reconnect_active = asyncio.Event()
        release_reconnect = asyncio.Event()

        async def stubborn_reconnect() -> None:
            order.append("old-reconnect-active")
            reconnect_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_reconnect.wait()
            order.append("old-reconnect-finished")

        reconnect_task = asyncio.create_task(stubborn_reconnect())
        coordinator._reconnect_tasks["old-gateway"] = reconnect_task

        def clear_reconnect(done_task: asyncio.Task[None]) -> None:
            if coordinator._reconnect_tasks.get("old-gateway") is done_task:
                coordinator._reconnect_tasks.pop("old-gateway", None)

        reconnect_task.add_done_callback(clear_reconnect)

        class Gateway:
            async def async_stop(self) -> None:
                order.append("old-stop")

        coordinator.gateways = {"old-gateway": Gateway()}

        def load_gateway_configs(_entry):
            order.append("load")
            return ["replacement-config"]

        async def rebuild_gateways() -> None:
            order.append("rebuild")

        replacement_starts = 0

        def start_replacement() -> None:
            nonlocal replacement_starts
            replacement_starts += 1
            order.append("new-startup")

        coordinator._load_gateway_configs = load_gateway_configs
        coordinator._rebuild_gateways = rebuild_gateways
        coordinator.async_start_gateways_background = start_replacement

        await reconnect_active.wait()
        await asyncio.wait_for(coordinator.async_reload_gateways(), timeout=0.1)

        assert coordinator._reconnect_suspended is False
        assert coordinator._reconnect_tasks == {
            "old-gateway": reconnect_task
        }
        assert replacement_starts == 0
        assert order == ["old-reconnect-active"]

        release_reconnect.set()
        await reconnect_task
        await asyncio.sleep(0)
        assert coordinator._reconnect_tasks == {}

        await coordinator.async_reload_gateways()

        assert coordinator._reconnect_suspended is False
        assert replacement_starts == 1
        assert order == [
            "old-reconnect-active",
            "old-reconnect-finished",
            "old-stop",
            "load",
            "rebuild",
            "new-startup",
        ]

    asyncio.run(run())


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


def test_late_provider_callbacks_are_ignored_during_shutdown(monkeypatch) -> None:
    """No late radio callback may touch state or storage after unload begins."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = True
        coordinator._reconnect_suspended = True
        coordinator._gateway_generation = 4
        coordinator.snapshot = MeshSnapshot()
        coordinator.store = SimpleNamespace(
            async_add_packet=AsyncMock(),
            async_add_message=AsyncMock(),
            async_recent_messages=AsyncMock(),
            async_upsert_node=AsyncMock(),
        )
        coordinator._flush_outbox = AsyncMock()
        coordinator._schedule_reconnect = AsyncMock()
        status = GatewayStatus(
            gateway_id="gateway-1",
            name="Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            connected=True,
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(status=status)
        }

        await coordinator._handle_packet(
            MeshPacket(protocol=PROTOCOL_MESHTASTIC, gateway_id="gateway-1"),
            gateway_generation=4,
        )
        await coordinator._handle_node(
            NodeState(
                node_key="meshtastic:1",
                protocol=PROTOCOL_MESHTASTIC,
                last_gateway_id="gateway-1",
            ),
            gateway_generation=4,
        )
        await coordinator._handle_gateway_status(
            status, gateway_generation=4
        )

        coordinator.store.async_add_packet.assert_not_awaited()
        coordinator.store.async_upsert_node.assert_not_awaited()
        coordinator._flush_outbox.assert_not_awaited()
        coordinator._schedule_reconnect.assert_not_awaited()
        assert coordinator.snapshot == MeshSnapshot()

    asyncio.run(run())


def test_stale_same_id_gateway_status_cannot_mutate_replacement(
    monkeypatch,
) -> None:
    """A callback from an old gateway object cannot target its replacement."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 2
        coordinator._reconnect_tasks = {}
        coordinator.snapshot = MeshSnapshot()
        coordinator._flush_outbox = AsyncMock()
        old_status = GatewayStatus(
            gateway_id="gateway-1",
            name="Old",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            connected=True,
        )
        replacement_status = GatewayStatus(
            gateway_id="gateway-1",
            name="Replacement",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(status=replacement_status)
        }

        await coordinator._handle_gateway_status(
            old_status, gateway_generation=2
        )

        coordinator._flush_outbox.assert_not_awaited()
        assert coordinator.snapshot.gateways == {}

    asyncio.run(run())


def test_outbox_does_not_write_after_shutdown_begins_mid_send(
    monkeypatch,
) -> None:
    """A radio send completing late cannot write into a closing store."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator.snapshot = MeshSnapshot()
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
        coordinator.store = SimpleNamespace(
            async_pending_outbox=AsyncMock(return_value=[record]),
            async_add_message=AsyncMock(),
            async_recent_messages=AsyncMock(),
        )
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )

        class Gateway:
            config = GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            )
            status = GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            )

            async def async_send_message(self, **_kwargs) -> str:
                coordinator._shutting_down = True
                return "provider-message"

        coordinator.gateways = {"gateway-1": Gateway()}

        await coordinator._flush_outbox(gateway_generation=1)

        coordinator.store.async_add_message.assert_not_awaited()
        coordinator.store.async_recent_messages.assert_not_awaited()
        assert record.raw == {"status": "queued", "gateway_id": "gateway-1"}
        assert coordinator._outbox_flush_owner is None

    asyncio.run(run())


def test_outbox_owner_cancellation_is_bounded_and_retained(monkeypatch) -> None:
    """A send that suppresses cancellation cannot hold unload open."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_outbox_flush_owner.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        release = asyncio.Event()
        active = asyncio.Event()

        async def stubborn_flush() -> None:
            active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(stubborn_flush())
        coordinator._outbox_flush_owner = task
        await active.wait()

        assert await coordinator._cancel_outbox_flush_owner() is False
        assert coordinator._outbox_flush_owner is task

        release.set()
        await task
        assert await coordinator._cancel_outbox_flush_owner() is True
        assert coordinator._outbox_flush_owner is None

    asyncio.run(run())


def test_status_does_not_publish_after_lifecycle_changes_during_flush(
    monkeypatch,
) -> None:
    """A status callback must recheck shutdown after awaiting the outbox."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator._reconnect_tasks = {}
        coordinator.snapshot = MeshSnapshot()
        updates: list[MeshSnapshot] = []
        coordinator.async_set_updated_data = updates.append
        status = GatewayStatus(
            gateway_id="gateway-1",
            name="Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            connected=True,
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(status=status)
        }

        async def begin_shutdown(**_kwargs) -> None:
            coordinator._shutting_down = True

        coordinator._flush_outbox = begin_shutdown

        await coordinator._handle_gateway_status(
            status, gateway_generation=1
        )

        assert updates == []

    asyncio.run(run())


def test_direct_send_cannot_write_after_bounded_shutdown(monkeypatch) -> None:
    """A cancellation-suppressing provider send cannot outlive storage safely."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_send_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator._gateway_startup_task = None
        coordinator._reconnect_tasks = {}
        coordinator._outbox_flush_owner = None
        coordinator._send_tasks = set()
        coordinator.snapshot = MeshSnapshot()
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        sent_events: list[tuple[str, dict]] = []
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(
                async_fire=lambda event, data: sent_events.append((event, data))
            )
        )
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()

        class Store:
            def __init__(self) -> None:
                self.closed = False
                self.added: list[MessageRecord] = []
                self.recent_calls = 0

            async def async_add_message(self, record: MessageRecord) -> None:
                assert self.closed is False
                self.added.append(record)

            async def async_recent_messages(self, _limit: int):
                assert self.closed is False
                self.recent_calls += 1
                return []

            async def async_close(self) -> None:
                self.closed = True

        store = Store()
        coordinator.store = store

        class Gateway:
            config = GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            )
            status = GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            )

            async def async_send_message(self, **_kwargs) -> str:
                provider_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    await release_provider.wait()
                return "provider-message"

            async def async_stop(self) -> None:
                return None

        coordinator.gateways = {"gateway-1": Gateway()}
        send_task = asyncio.create_task(
            coordinator.async_send_message(
                target_node=None,
                message="hello",
                gateway_id="gateway-1",
            )
        )
        await provider_started.wait()

        await asyncio.wait_for(coordinator.async_shutdown(), timeout=0.2)

        assert store.closed is True
        assert len(store.added) == 1
        assert store.added[0].raw["status"] == "queued"
        assert coordinator._send_tasks == {send_task}

        release_provider.set()
        await send_task

        assert coordinator._send_tasks == set()
        assert len(store.added) == 1
        assert store.recent_calls == 0
        assert sent_events == []

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
