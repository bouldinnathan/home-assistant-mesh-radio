"""Tests for MeshNet integration setup helpers."""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.meshnet import (
    _async_register_panel,
    async_migrate_entry,
    async_remove_entry,
)
from custom_components.meshnet.const import (
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    CONF_BLUETOOTH_BOND_MANAGED,
    CONF_GATEWAYS,
    DATA_BLUETOOTH_PAIRING,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
)

ADAPTER_ADDRESS = "00:11:22:33:44:55"


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
