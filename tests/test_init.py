"""Tests for MeshNet integration setup helpers."""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.meshnet import (
    _async_register_panel,
    async_migrate_entry,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.meshnet.const import (
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    CONF_BLUETOOTH_BOND_MANAGED,
    CONF_GATEWAYS,
    DATA_BLUETOOTH_PAIRING,
    DOMAIN,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
)

ADAPTER_ADDRESS = "00:11:22:33:44:55"


def test_setup_entry_does_not_wait_for_radio_sdk_startup(monkeypatch) -> None:
    """A stuck BLE constructor must not hold the config-flow response open."""

    async def run() -> None:
        startup_began = asyncio.Event()
        release_startup = asyncio.Event()

        class FakeCoordinator:
            def __init__(self, hass, entry) -> None:
                self.hass = hass
                self.entry = entry
                self._startup_task = None

            async def async_config_entry_first_refresh(self) -> None:
                return None

            async def async_start_gateways(self) -> None:
                startup_began.set()
                await release_startup.wait()

            def async_start_gateways_background(self) -> None:
                self._startup_task = self.entry.async_create_background_task(
                    self.hass,
                    self.async_start_gateways(),
                    "MeshNet gateway startup",
                )

        coordinator_module = types.ModuleType(
            "custom_components.meshnet.coordinator"
        )
        coordinator_module.MeshNetCoordinator = FakeCoordinator
        monkeypatch.setitem(
            sys.modules,
            "custom_components.meshnet.coordinator",
            coordinator_module,
        )

        background_tasks: list[asyncio.Task[None]] = []
        task_names: list[str] = []

        class Entry:
            entry_id = "entry-id"

            def async_create_background_task(self, hass, target, name):
                del hass
                task_names.append(name)
                task = asyncio.create_task(target)
                background_tasks.append(task)
                return task

            def add_update_listener(self, listener):
                del listener
                return lambda: None

            def async_on_unload(self, callback) -> None:
                del callback

        hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(return_value=None)
            ),
        )
        entry = Entry()

        assert await asyncio.wait_for(async_setup_entry(hass, entry), 0.1) is True
        await startup_began.wait()

        assert task_names == ["MeshNet gateway startup"]
        assert len(background_tasks) == 1
        assert not background_tasks[0].done()

        release_startup.set()
        await background_tasks[0]

    asyncio.run(run())


def test_setup_entry_closes_coordinator_when_first_refresh_fails(
    monkeypatch,
) -> None:
    """A never-loaded entry must not orphan an opened store or coordinator."""

    async def run() -> None:
        coordinator_instances = []

        class FakeCoordinator:
            def __init__(self, hass, entry) -> None:
                del hass, entry
                self.shutdown_calls = 0
                coordinator_instances.append(self)

            async def async_config_entry_first_refresh(self) -> None:
                raise RuntimeError("snapshot failed")

            async def async_shutdown(self) -> None:
                self.shutdown_calls += 1

        coordinator_module = types.ModuleType(
            "custom_components.meshnet.coordinator"
        )
        coordinator_module.MeshNetCoordinator = FakeCoordinator
        monkeypatch.setitem(
            sys.modules,
            "custom_components.meshnet.coordinator",
            coordinator_module,
        )
        config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )
        existing = object()
        hass = SimpleNamespace(
            data={DOMAIN: {"existing": existing}},
            config_entries=config_entries,
        )
        entry = SimpleNamespace(entry_id="failed-entry")

        with pytest.raises(RuntimeError, match="snapshot failed"):
            await async_setup_entry(hass, entry)

        assert coordinator_instances[0].shutdown_calls == 1
        assert hass.data[DOMAIN] == {"existing": existing}
        config_entries.async_forward_entry_setups.assert_not_awaited()
        config_entries.async_unload_platforms.assert_not_awaited()

    asyncio.run(run())


def test_setup_entry_rolls_back_forwarded_platforms_on_failure(
    monkeypatch,
) -> None:
    """Partial platform setup is unloaded before coordinator resources close."""

    async def run() -> None:
        events: list[str] = []

        class FakeCoordinator:
            def __init__(self, hass, entry) -> None:
                self.hass = hass
                self.entry = entry

            async def async_config_entry_first_refresh(self) -> None:
                events.append("refresh")

            async def async_shutdown(self) -> None:
                assert self.hass.data[DOMAIN][self.entry.entry_id] is self
                events.append("shutdown")

        coordinator_module = types.ModuleType(
            "custom_components.meshnet.coordinator"
        )
        coordinator_module.MeshNetCoordinator = FakeCoordinator
        monkeypatch.setitem(
            sys.modules,
            "custom_components.meshnet.coordinator",
            coordinator_module,
        )

        async def fail_forward(_entry, _platforms) -> None:
            events.append("forward")
            raise RuntimeError("platform failed")

        async def unload_platforms(_entry, _platforms) -> bool:
            events.append("unload_platforms")
            return True

        hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_forward_entry_setups=fail_forward,
                async_unload_platforms=unload_platforms,
            ),
        )
        entry = SimpleNamespace(entry_id="failed-entry")

        with pytest.raises(RuntimeError, match="platform failed"):
            await async_setup_entry(hass, entry)

        assert events == [
            "refresh",
            "forward",
            "unload_platforms",
            "shutdown",
        ]
        assert "failed-entry" not in hass.data[DOMAIN]

    asyncio.run(run())


def test_unload_failure_preserves_live_coordinator() -> None:
    """Failed platform unload must not close state still used by entities."""

    async def run() -> None:
        coordinator = SimpleNamespace(async_shutdown=AsyncMock())
        entry = SimpleNamespace(entry_id="entry-id")
        hass = SimpleNamespace(
            data={DOMAIN: {entry.entry_id: coordinator}},
            config_entries=SimpleNamespace(
                async_unload_platforms=AsyncMock(return_value=False)
            ),
        )

        assert await async_unload_entry(hass, entry) is False
        assert hass.data[DOMAIN][entry.entry_id] is coordinator
        coordinator.async_shutdown.assert_not_awaited()

    asyncio.run(run())


def test_successful_unload_closes_before_removing_coordinator() -> None:
    """Keep the coordinator reachable until its bounded shutdown succeeds."""

    async def run() -> None:
        entry = SimpleNamespace(entry_id="entry-id")

        class Coordinator:
            async def async_shutdown(self) -> None:
                assert hass.data[DOMAIN][entry.entry_id] is self

        coordinator = Coordinator()
        hass = SimpleNamespace(
            data={DOMAIN: {entry.entry_id: coordinator}},
            config_entries=SimpleNamespace(
                async_unload_platforms=AsyncMock(return_value=True)
            ),
        )

        assert await async_unload_entry(hass, entry) is True
        assert entry.entry_id not in hass.data[DOMAIN]

    asyncio.run(run())


def test_unload_shutdown_error_keeps_coordinator_for_safe_retry() -> None:
    """Do not discard ownership when transport cleanup itself raises."""

    async def run() -> None:
        entry = SimpleNamespace(entry_id="entry-id")

        class Coordinator:
            async def async_shutdown(self) -> None:
                raise RuntimeError("cleanup failed")

        coordinator = Coordinator()
        hass = SimpleNamespace(
            data={DOMAIN: {entry.entry_id: coordinator}},
            config_entries=SimpleNamespace(
                async_unload_platforms=AsyncMock(return_value=True)
            ),
        )

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await async_unload_entry(hass, entry)
        assert hass.data[DOMAIN][entry.entry_id] is coordinator

    asyncio.run(run())


def test_panel_registration_is_awaited(monkeypatch) -> None:
    """Register the static files and await Home Assistant's panel API."""

    @dataclass(frozen=True)
    class FakeStaticPathConfig:
        url_path: str
        path: str
        cache_headers: bool

    register_panel = AsyncMock()
    panel_custom = types.ModuleType("homeassistant.components.panel_custom")
    panel_custom.async_register_panel = register_panel

    http_module = types.ModuleType("homeassistant.components.http")
    http_module.StaticPathConfig = FakeStaticPathConfig

    components = types.ModuleType("homeassistant.components")
    components.panel_custom = panel_custom
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.components = components

    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.panel_custom", panel_custom
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.http", http_module)

    hass = SimpleNamespace(
        http=SimpleNamespace(async_register_static_paths=AsyncMock())
    )

    asyncio.run(_async_register_panel(hass))

    hass.http.async_register_static_paths.assert_awaited_once()
    register_panel.assert_awaited_once_with(
        hass,
        webcomponent_name="meshnet-panel",
        frontend_url_path="meshnet",
        module_url="/meshnet_static/meshnet-panel.js",
        sidebar_title="MeshNet",
        sidebar_icon="mdi:radio-tower",
        require_admin=True,
        config={},
    )


def test_entry_removal_never_changes_external_bluetooth_bonds() -> None:
    async def run() -> None:
        manager = SimpleNamespace(async_forget_current_bond=AsyncMock())
        owned = {
            "gateway_id": "owned",
            "name": "Owned",
            "protocol": PROTOCOL_MESHTASTIC,
            "transport": TRANSPORT_BLUETOOTH,
            "ble_address": "AA:BB:CC:DD:EE:01",
            "options": {
                CONF_BLUETOOTH_ADAPTER: "hci0",
                CONF_BLUETOOTH_ADAPTER_ADDRESS: ADAPTER_ADDRESS,
                CONF_BLUETOOTH_BOND_MANAGED: True,
            },
        }
        preexisting = {
            **owned,
            "gateway_id": "preexisting",
            "ble_address": "AA:BB:CC:DD:EE:02",
            "options": {},
        }
        entry = SimpleNamespace(
            data={CONF_GATEWAYS: [owned, preexisting]}, options={}
        )
        hass = SimpleNamespace(data={DATA_BLUETOOTH_PAIRING: manager})

        await async_remove_entry(hass, entry)

        manager.async_forget_current_bond.assert_not_awaited()

    asyncio.run(run())


def test_pre_v04_entry_migration_strips_untrusted_bond_authority() -> None:
    gateway = {
        "gateway_id": "legacy",
        "protocol": PROTOCOL_MESHTASTIC,
        "transport": TRANSPORT_BLUETOOTH,
        "ble_address": "AA:BB:CC:DD:EE:01",
        "options": {
            CONF_BLUETOOTH_ADAPTER: "hci9",
            CONF_BLUETOOTH_ADAPTER_ADDRESS: "00:00:00:00:00:01",
            CONF_BLUETOOTH_BOND_MANAGED: True,
            "debug": True,
        },
    }
    entry = SimpleNamespace(
        version=1,
        minor_version=1,
        data={CONF_GATEWAYS: [gateway]},
        options={CONF_GATEWAYS: [gateway]},
    )
    updates: dict = {}
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda _entry, **values: updates.update(values)
        )
    )

    assert asyncio.run(async_migrate_entry(hass, entry)) is True

    assert updates["minor_version"] == 2
    for container in (updates["data"], updates["options"]):
        migrated_options = container[CONF_GATEWAYS][0]["options"]
        assert migrated_options == {"debug": True}


def test_current_entry_migration_is_idempotent() -> None:
    entry = SimpleNamespace(version=1, minor_version=2, data={}, options={})
    update = AsyncMock()
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update)
    )

    assert asyncio.run(async_migrate_entry(hass, entry)) is True
    update.assert_not_called()
