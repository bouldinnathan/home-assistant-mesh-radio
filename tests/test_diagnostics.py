"""Diagnostics platform discovery, completeness, and privacy tests."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import sys
import types
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.meshnet.const import DOMAIN
from custom_components.meshnet.models import (
    GatewayConfig,
    GatewayStatus,
    MeshSnapshot,
    NodeState,
)

_PRIVATE_VALUES = (
    "private-entry-id",
    "Private Home Mesh",
    "private-gateway-id",
    "Private Owner's Radio",
    "192.0.2.44",
    "/dev/serial/by-id/private-radio",
    "AA:BB:CC:DD:EE:FF",
    "mesh/private/topic",
    "private-api-key",
    "private-pin",
    "private-node-key",
    "private-node-id",
    "private-public-key",
    "Private Owner's Node",
    "PrivateNode",
    "Private Home WiFi",
    "private-next-hop",
    "private message payload",
    "private-unrecognized-data",
    "private-unrecognized-option",
    "Private Owner's Bedroom Radio",
    "Private Owner Firmware",
    "Private Owner Radio Type",
    "Private Owner Bedroom Role",
    "Private_Owner_is_home",
    "41.1234",
    "-87.5678",
    "connection failed at 192.0.2.44 with token=private-api-key",
)


def _redact_data(data: Any, keys: list[str]) -> Any:
    """Small stand-in for HA's recursive diagnostics redactor."""
    if isinstance(data, list):
        return [_redact_data(value, keys) for value in data]
    if not isinstance(data, dict):
        return data
    return {
        key: "**REDACTED**" if key in keys else _redact_data(value, keys)
        for key, value in data.items()
    }


def _home_assistant_is_installed() -> bool:
    """Tolerate temporary module shims used by the lightweight test suite."""
    try:
        return importlib.util.find_spec("homeassistant") is not None
    except ValueError:
        return False


def _load_diagnostics(monkeypatch: pytest.MonkeyPatch):
    """Import the platform with minimal HA shims in the lightweight suite."""
    if not _home_assistant_is_installed():
        homeassistant = types.ModuleType("homeassistant")
        homeassistant.__path__ = []
        components = types.ModuleType("homeassistant.components")
        components.__path__ = []
        ha_diagnostics = types.ModuleType("homeassistant.components.diagnostics")
        config_entries = types.ModuleType("homeassistant.config_entries")
        core = types.ModuleType("homeassistant.core")
        helpers = types.ModuleType("homeassistant.helpers")
        helpers.__path__ = []
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        coordinator = types.ModuleType("custom_components.meshnet.coordinator")

        ha_diagnostics.async_redact_data = _redact_data
        config_entries.ConfigEntry = object
        core.HomeAssistant = object
        device_registry.DeviceEntry = object
        coordinator.MeshNetCoordinator = object

        homeassistant.components = components
        homeassistant.helpers = helpers
        components.diagnostics = ha_diagnostics
        helpers.device_registry = device_registry

        for name, module in {
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.diagnostics": ha_diagnostics,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.device_registry": device_registry,
            "custom_components.meshnet.coordinator": coordinator,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.delitem(
        sys.modules, "custom_components.meshnet.diagnostics", raising=False
    )
    return importlib.import_module("custom_components.meshnet.diagnostics")


class _FakeEntry:
    """Config-entry shape used by both supported HA release families."""

    entry_id = "private-entry-id"
    title = "Private Home Mesh"
    unique_id = "AA:BB:CC:DD:EE:FF"
    version = 1
    minor_version = 2

    data = {
        "gateways": [
            {
                "gateway_id": "private-gateway-id",
                "name": "Private Owner's Radio",
                "protocol": "meshtastic",
                "transport": "tcp",
                "host": "192.0.2.44",
                "port": 4403,
                "serial_path": "/dev/serial/by-id/private-radio",
                "ble_address": "AA:BB:CC:DD:EE:FF",
                "mqtt_topic": "mesh/private/topic",
                "api_key": "private-api-key",
            }
        ],
        "history_days": 30,
        "custom_label": "private-unrecognized-data",
    }
    options = {
        "node_timeout": 900,
        "pin": "private-pin",
        "custom_option": "private-unrecognized-option",
    }

    def as_dict(self) -> dict[str, Any]:
        """Return the standard ConfigEntry diagnostics representation."""
        return {
            "entry_id": self.entry_id,
            "version": self.version,
            "minor_version": self.minor_version,
            "domain": DOMAIN,
            "title": self.title,
            "data": self.data,
            "options": self.options,
            "unique_id": self.unique_id,
            "source": "user",
        }


def _private_node() -> NodeState:
    return NodeState(
        node_key="private-node-key",
        protocol="meshtastic",
        node_id="private-node-id",
        mac="AA:BB:CC:DD:EE:FF",
        public_key="private-public-key",
        user_name="Private Owner's Node",
        long_name="Private Owner's Node",
        short_name="PrivateNode",
        hardware_model="RAK4631",
        firmware_version="2.7.11",
        radio_type="sx1262",
        role="ROUTER_CLIENT",
        online=True,
        last_heard=datetime(2026, 7, 27, 5, 30, tzinfo=UTC),
        last_gateway_id="private-gateway-id",
        gateway_ids={"private-gateway-id"},
        connectivity={
            "rssi": -87,
            "snr": 6.25,
            "ip_address": "192.0.2.44",
            "ssid": "Private Home WiFi",
        },
        power={"battery_level": 87, "voltage": 4.12},
        radio={"channel_utilization": 12.5, "air_util_tx": 1.5},
        location={"latitude": 41.1234, "longitude": -87.5678},
        routing={"hops_away": 2, "next_hop": "private-next-hop"},
        sensors={
            "temperature": 23.5,
            "humidity": 48.0,
            "payload": "private message payload",
        },
        raw={"text": "private message payload", "token": "private-api-key"},
    )


class _FakeCoordinator:
    def __init__(self) -> None:
        status = GatewayStatus(
            gateway_id="private-gateway-id",
            name="Private Owner's Radio",
            protocol="meshtastic",
            transport="tcp",
            connected=True,
            last_connected=datetime(2026, 7, 27, 5, 20, tzinfo=UTC),
            last_packet=datetime(2026, 7, 27, 5, 29, tzinfo=UTC),
            packets_received=42,
            packets_sent=4,
            duplicate_packets=2,
            errors=[
                "connection failed at 192.0.2.44 with token=private-api-key"
            ],
            detail={"host": "192.0.2.44", "token": "private-api-key"},
        )
        self.entry = _FakeEntry()
        self.snapshot = MeshSnapshot(
            nodes={"private-node-key": _private_node()},
            gateways={"private-gateway-id": status},
            mesh_health_score=0.95,
            messages_today=2,
        )
        self.gateways = {
            "private-gateway-id": SimpleNamespace(
                config=GatewayConfig.from_dict(_FakeEntry.data["gateways"][0]),
                status=status,
                start_pending=False,
                _interface=object(),
                _native_executor_tasks={},
            )
        }
        # Downloading cached diagnostics must never touch a radio or transport.
        self.async_gateway_refresh = AsyncMock(
            side_effect=AssertionError("diagnostics attempted a radio refresh")
        )
        self.async_start_gateways = AsyncMock(
            side_effect=AssertionError("diagnostics attempted to start a radio")
        )
        self.async_reload_gateways = AsyncMock(
            side_effect=AssertionError("diagnostics attempted to reload a radio")
        )
        self.async_send_message = AsyncMock(
            side_effect=AssertionError("diagnostics attempted to transmit")
        )

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return a deliberately rich runtime payload, including redaction traps."""
        return {
            "configuration": {
                "node_timeout": 900,
                "history_days": 30,
                "gateway_count": 1,
            },
            "lifecycle": {
                "shutting_down": False,
                "reconnect_suspended": False,
                "gateway_generation": 3,
            },
            "tasks": {
                "gateway_startup_pending": False,
                "reconnect_task_count": 1,
                "send_task_count": 0,
                "outbox_flush_pending": False,
            },
            "gateways": [
                {
                    "gateway_id": "private-gateway-id",
                    "name": "Private Owner's Radio",
                    "protocol": "meshtastic",
                    "transport": "tcp",
                    "connected": True,
                    "packets_received": 42,
                    "packets_sent": 4,
                    "duplicate_packets": 2,
                    "errors": [
                        "connection failed at 192.0.2.44 with token=private-api-key"
                    ],
                    "client": {
                        "interface_active": True,
                        "start_pending": False,
                    },
                }
            ],
            "dedupe": {
                "entries": 40,
                "total_packets": 42,
                "duplicate_packets": 2,
                "duplicate_ratio": 2 / 42,
            },
            "rate_limit": {"rate": 0.5, "capacity": 5.0, "tokens": 4.0},
            "snapshot": {
                "node_count": 1,
                "message_count": 2,
                "messages_today": 2,
                "mesh_health_score": 0.95,
            },
            "store": {"node_count": 1, "message_count": 2, "packet_count": 42},
        }


def _assert_no_private_values(data: Any) -> None:
    serialized = json.dumps(data, sort_keys=True)
    for private_value in _PRIVATE_VALUES:
        assert private_value not in serialized


def _assert_thorough_safe_node(node: dict[str, Any]) -> None:
    """Require useful radio health while excluding identity and message content."""
    assert node["protocol"] == "meshtastic"
    assert node["hardware_model"] == "RAK4631"
    assert node["firmware_version"] == "2.7.11"
    assert node["radio_type"] == "SX1262"
    assert node["role"] == "ROUTER_CLIENT"
    assert node["online"] is True
    assert node["last_heard"] == "2026-07-27T05:30:00+00:00"
    assert node["gateway_count"] == 1
    assert node["has_location"] is True
    assert node["connectivity"]["rssi"] == -87
    assert node["connectivity"]["snr"] == 6.25
    assert node["power"] == {"battery_level": 87, "voltage": 4.12}
    assert node["radio"] == {"channel_utilization": 12.5, "air_util_tx": 1.5}
    assert node["routing"]["hops_away"] == 2
    assert node["sensors"] == {"temperature": 23.5, "humidity": 48.0}
    assert set(node).isdisjoint(
        {
            "node_key",
            "node_id",
            "mac",
            "public_key",
            "user_name",
            "long_name",
            "short_name",
            "last_gateway_id",
            "gateway_ids",
            "location",
            "raw",
        }
    )


def _assert_device_classification(
    result: dict[str, Any], expected_type: str
) -> dict[str, Any]:
    """Require one unambiguous known-device payload."""
    assert isinstance(result["schema_version"], int)
    assert result["schema_version"] >= 1
    assert result["device_type"] == expected_type
    detail_keys = {"hub", "gateway", "node"} & result.keys()
    assert detail_keys == {expected_type}
    return result[expected_type]


def _assert_no_radio_io(coordinator: _FakeCoordinator) -> None:
    coordinator.async_gateway_refresh.assert_not_awaited()
    coordinator.async_start_gateways.assert_not_awaited()
    coordinator.async_reload_gateways.assert_not_awaited()
    coordinator.async_send_message.assert_not_awaited()


def test_diagnostics_platform_exports_native_home_assistant_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA discovers these handlers and exposes Download diagnostics in the menu."""
    diagnostics = _load_diagnostics(monkeypatch)

    assert inspect.iscoroutinefunction(
        diagnostics.async_get_config_entry_diagnostics
    )
    assert list(
        inspect.signature(
            diagnostics.async_get_config_entry_diagnostics
        ).parameters
    ) == ["hass", "entry"]
    assert inspect.iscoroutinefunction(diagnostics.async_get_device_diagnostics)
    assert list(
        inspect.signature(diagnostics.async_get_device_diagnostics).parameters
    ) == ["hass", "entry", "device"]


def test_diagnostics_module_imports_against_home_assistant_current() -> None:
    """Guard the HA diagnostics import path used for native platform discovery."""
    if not _home_assistant_is_installed():
        pytest.skip("Home Assistant is not installed in the lightweight suite")

    diagnostics = importlib.import_module("custom_components.meshnet.diagnostics")
    from homeassistant.components.diagnostics import async_redact_data

    assert diagnostics.async_redact_data is async_redact_data


def test_config_entry_diagnostics_are_thorough_serializable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collection uses cached state only; the fake exposes no radio I/O methods."""
    diagnostics = _load_diagnostics(monkeypatch)
    coordinator = _FakeCoordinator()
    hass = SimpleNamespace(
        data={DOMAIN: {_FakeEntry.entry_id: coordinator}},
        config=SimpleNamespace(version="2026.7.4"),
    )

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, _FakeEntry())
    )

    assert set(result) >= {
        "generated_at",
        "config_entry",
        "versions",
        "runtime",
        "nodes",
    }
    datetime.fromisoformat(result["generated_at"])
    assert result["config_entry"]["version"] == 1
    assert result["config_entry"]["minor_version"] == 2
    assert result["config_entry"]["data"]["history_days"] == 30
    assert result["config_entry"]["options"]["node_timeout"] == 900
    assert result["config_entry"]["data"]["gateways"][0]["protocol"] == "meshtastic"
    assert result["config_entry"]["data"]["gateways"][0]["transport"] == "tcp"
    assert result["config_entry"]["data"]["gateways"][0]["port"] == 4403
    safe_gateway = result["config_entry"]["data"]["gateways"][0]
    assert safe_gateway["omitted_identity_field_count"] == 2
    assert safe_gateway["omitted_unknown_field_count"] == 0
    assert safe_gateway["omitted_unknown_option_count"] == 0
    assert result["config_entry"]["data"][
        "omitted_top_level_value_count"
    ] == 1
    assert result["config_entry"]["options"][
        "omitted_top_level_value_count"
    ] == 2

    privacy = result["privacy"]
    assert privacy["policy_version"] == 3
    assert privacy["radio_operations_performed"] is False
    assert privacy["native_home_assistant_wrapper"] == {
        "controlled_by_meshnet": False,
        "may_include_config_entry_id": True,
        "may_include_device_name_and_registry_id": True,
        "may_include_system_versions_and_timezone": True,
        "inspect_and_rename_before_sharing": True,
    }
    assert privacy["node_observability_analysis"] == {
        "maximum_analyzed_nodes": 1000,
        "truncation_reported": True,
        "omitted_nodes_deleted": False,
    }

    assert set(result["versions"]) >= {"meshnet", "meshtastic", "meshcore"}

    runtime = result["runtime"]
    assert runtime["configuration"] == {
        "node_timeout": 900,
        "history_days": 30,
        "gateway_count": 1,
    }
    assert runtime["lifecycle"]["gateway_generation"] == 3
    assert runtime["tasks"]["reconnect_task_count"] == 1
    assert runtime["gateways"][0]["protocol"] == "meshtastic"
    assert runtime["gateways"][0]["connected"] is True
    assert runtime["gateways"][0]["packets_received"] == 42
    assert runtime["gateways"][0]["client"]["interface_active"] is True
    assert runtime["dedupe"]["duplicate_packets"] == 2
    assert runtime["rate_limit"]["capacity"] == 5.0
    assert runtime["snapshot"]["mesh_health_score"] == 0.95
    assert runtime["store"]["packet_count"] == 42

    assert len(result["nodes"]) == 1
    _assert_thorough_safe_node(result["nodes"][0])
    _assert_no_private_values(result)
    json.dumps(result)
    _assert_no_radio_io(coordinator)


def test_config_entry_diagnostics_are_available_while_runtime_is_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry configuration and versions remain useful after a setup failure."""
    diagnostics = _load_diagnostics(monkeypatch)
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(version="2026.7.4"),
    )

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, _FakeEntry())
    )

    assert set(result) >= {
        "generated_at",
        "config_entry",
        "versions",
        "runtime",
        "nodes",
    }
    assert result["nodes"] == []
    assert isinstance(result["runtime"], dict)
    _assert_no_private_values(result)
    json.dumps(result)


def test_config_entry_diagnostics_bound_large_per_node_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathological mesh cannot create an unbounded diagnostics document."""
    diagnostics = _load_diagnostics(monkeypatch)
    coordinator = _FakeCoordinator()
    coordinator.snapshot.nodes = {
        f"node-{index:04d}": NodeState(
            node_key=f"node-{index:04d}",
            protocol="meshtastic",
            online=index % 2 == 0,
        )
        for index in range(1005)
    }
    hass = SimpleNamespace(data={DOMAIN: {_FakeEntry.entry_id: coordinator}})

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, _FakeEntry())
    )

    assert result["node_export"] == {
        "total_count": 1005,
        "exported_count": 1000,
        "truncated": True,
        "maximum_exported": 1000,
    }
    assert len(result["nodes"]) == 1000
    _assert_no_radio_io(coordinator)


def test_provider_supplied_metadata_and_dynamic_telemetry_keys_are_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge payload cannot smuggle ordinary PII through diagnostic fields."""
    diagnostics = _load_diagnostics(monkeypatch)
    coordinator = _FakeCoordinator()
    node = coordinator.snapshot.nodes["private-node-key"]
    node.hardware_model = "Private Owner's Bedroom Radio"
    node.firmware_version = "Private Owner Firmware"
    node.radio_type = "Private Owner Radio Type"
    node.role = "Private Owner Bedroom Role"
    node.sensors["Private_Owner_is_home"] = 1
    node.connectivity["Private_Owner_gateway_strength"] = 99
    hass = SimpleNamespace(data={DOMAIN: {_FakeEntry.entry_id: coordinator}})

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, _FakeEntry())
    )

    safe_node = result["nodes"][0]
    assert safe_node["hardware_model"] is None
    assert safe_node["firmware_version"] is None
    assert safe_node["radio_type"] is None
    assert safe_node["role"] is None
    assert safe_node["metadata_present"] == {
        "hardware_model": True,
        "firmware_version": True,
        "radio_type": True,
        "role": True,
    }
    assert safe_node["sensors"] == {"temperature": 23.5, "humidity": 48.0}
    assert safe_node["connectivity"] == {"rssi": -87, "snr": 6.25}
    _assert_no_private_values(result)
    _assert_no_radio_io(coordinator)


def test_device_diagnostics_include_safe_node_health_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _load_diagnostics(monkeypatch)
    coordinator = _FakeCoordinator()
    hass = SimpleNamespace(data={DOMAIN: {_FakeEntry.entry_id: coordinator}})
    device = SimpleNamespace(
        identifiers={
            (DOMAIN, "private-node-key"),
            ("other_integration", "unrelated-id"),
        }
    )

    result = asyncio.run(
        diagnostics.async_get_device_diagnostics(hass, _FakeEntry(), device)
    )

    node = _assert_device_classification(result, "node")
    _assert_thorough_safe_node(node)
    _assert_no_private_values(result)
    json.dumps(result)
    _assert_no_radio_io(coordinator)


def test_device_diagnostics_classify_hub_without_exposing_entry_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _load_diagnostics(monkeypatch)
    coordinator = _FakeCoordinator()
    hass = SimpleNamespace(data={DOMAIN: {_FakeEntry.entry_id: coordinator}})
    device = SimpleNamespace(identifiers={(DOMAIN, _FakeEntry.entry_id)})

    result = asyncio.run(
        diagnostics.async_get_device_diagnostics(hass, _FakeEntry(), device)
    )

    hub = _assert_device_classification(result, "hub")
    assert isinstance(hub, dict)
    assert hub["gateway_count"] == 1
    assert hub["node_count"] == 1
    assert hub["mesh_health_score"] == 0.95
    _assert_no_private_values(result)
    json.dumps(result)
    _assert_no_radio_io(coordinator)


def test_device_diagnostics_classify_gateway_with_safe_transport_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _load_diagnostics(monkeypatch)
    coordinator = _FakeCoordinator()
    hass = SimpleNamespace(data={DOMAIN: {_FakeEntry.entry_id: coordinator}})
    device = SimpleNamespace(identifiers={(DOMAIN, "private-gateway-id")})

    result = asyncio.run(
        diagnostics.async_get_device_diagnostics(hass, _FakeEntry(), device)
    )

    gateway = _assert_device_classification(result, "gateway")
    assert gateway["protocol"] == "meshtastic"
    assert gateway["transport"] == "tcp"
    assert gateway["connected"] is True
    assert gateway["last_connected"] == "2026-07-27T05:20:00+00:00"
    assert gateway["last_packet"] == "2026-07-27T05:29:00+00:00"
    assert gateway["packets_received"] == 42
    assert gateway["packets_sent"] == 4
    assert gateway["duplicate_packets"] == 2
    assert gateway["error_count"] == 1
    _assert_no_private_values(result)
    json.dumps(result)
    _assert_no_radio_io(coordinator)


def test_device_diagnostics_are_graceful_for_unloaded_or_unknown_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _load_diagnostics(monkeypatch)
    entry = _FakeEntry()
    unknown_device = SimpleNamespace(identifiers={(DOMAIN, "unknown-node")})

    unloaded = asyncio.run(
        diagnostics.async_get_device_diagnostics(
            SimpleNamespace(data={}), entry, unknown_device
        )
    )
    unknown = asyncio.run(
        diagnostics.async_get_device_diagnostics(
            SimpleNamespace(
                data={DOMAIN: {entry.entry_id: _FakeCoordinator()}}
            ),
            entry,
            unknown_device,
        )
    )

    for result in (unloaded, unknown):
        assert isinstance(result["schema_version"], int)
        assert result["device_type"] == "unknown"
        assert isinstance(result["reason"], str)
        assert result["reason"]
        assert not ({"hub", "gateway", "node"} & result.keys())
        _assert_no_private_values(result)
        json.dumps(result)
