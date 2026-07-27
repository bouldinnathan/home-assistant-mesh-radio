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
    MessageRecord,
    MeshSnapshot,
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
