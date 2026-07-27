"""Tests for MeshNet integration setup helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.meshnet import _async_register_panel


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
