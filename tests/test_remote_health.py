"""Remote maintenance observes exact nodes and never retries uncertain writes."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.meshnet.remote_admin import RemoteAdminError, RemoteAdminManager
from custom_components.meshnet.remote_health import approved_firmware, firmware_status
from custom_components.meshnet.remote_services import register_remote_services
from custom_components.meshnet.store import MeshStore

TARGET = "!1234abcd"


@pytest.mark.parametrize(("installed", "minimum", "status"), [
    ("2.7.11.abcdef0", "2.7.11", "current"),
    ("2.9.1", "2.10.0", "outdated"),
    ("2.10.0", "2.9.1", "current"),
    ("v2.7.11", "v2.7.11", "current"),
    ("2.7.11-alpha", "2.7.11", "unknown"),
    ("my secret custom build", "2.7.11", "unknown"),
    (None, "2.7.11", "unknown"),
    ("2.7.11", "", "not_checked"),
])
def test_firmware_comparison_is_numeric_and_unknown_is_not_current(installed, minimum, status) -> None:
    result = firmware_status(installed, minimum)
    assert result["firmware_status"] == status
    if status == "unknown":
        assert result["firmware_version"] is None


@pytest.mark.parametrize("value", [True, 2.7, "latest", "2.7", "2.7.11-beta", "2.7.11.abcdef0", "9" * 100])
def test_firmware_baseline_requires_an_explicit_release(value) -> None:
    with pytest.raises(ValueError):
        approved_firmware(value)


def _manager():
    gateway = SimpleNamespace(
        config=SimpleNamespace(protocol="meshtastic", transport="bluetooth"),
        status=SimpleNamespace(connected=True),
        async_check_remote_node=AsyncMock(return_value={"firmware_version": "2.7.11.abcdef0"}),
    )
    coordinator = SimpleNamespace(
        gateways={"gateway": gateway},
        store=SimpleNamespace(async_reserve_remote_probe=AsyncMock(return_value=True)),
    )
    return RemoteAdminManager(coordinator), gateway, coordinator


def test_health_is_fresh_bounded_and_reports_firmware_separately() -> None:
    async def run():
        manager, gateway, coordinator = _manager()
        result = await manager.async_check("gateway", TARGET, minimum_firmware="2.8.0")
        assert result["status"] == "responding"
        assert result["firmware_status"] == "outdated"
        assert result["target_node"] == TARGET
        gateway.async_check_remote_node.assert_awaited_once_with(TARGET)
        coordinator.store.async_reserve_remote_probe.assert_awaited_once()
        coordinator.store.async_reserve_remote_probe.return_value = False
        result = await manager.async_check("gateway", TARGET)
        assert result["status"] == "not_checked"
        assert result["reason"] == "remote_admin_rate_limited"
        assert gateway.async_check_remote_node.await_count == 1
        assert not manager._snapshots  # Health reads never authorize a settings draft.

    asyncio.run(run())


@pytest.mark.parametrize(("code", "status"), [
    ("remote_admin_no_response", "no_response"),
    ("remote_admin_no_route", "no_response"),
    ("remote_admin_controller_unauthorized", "not_checked"),
    ("remote_admin_session_rejected", "not_checked"),
    ("remote_admin_duty_cycle_limited", "not_checked"),
])
def test_health_distinguishes_missing_reply_from_controller_failure(code, status) -> None:
    async def run():
        manager, gateway, _ = _manager()
        gateway.async_check_remote_node.side_effect = RemoteAdminError(code)
        result = await manager.async_check("gateway", TARGET)
        assert result["status"] == status
        assert result["reason"] == code
        gateway.async_check_remote_node.assert_awaited_once()

    asyncio.run(run())


def test_health_rejects_bad_targets_offline_gateways_and_unload_without_rf() -> None:
    async def run():
        manager, gateway, _ = _manager()
        for target in ("all", "!ffffffff", "!00000000", "!1234ABCd"):
            with pytest.raises(RemoteAdminError):
                await manager.async_check("gateway", target)
        gateway.status.connected = False
        assert (await manager.async_check("gateway", TARGET))["status"] == "not_checked"
        gateway.status.connected = True
        assert await manager.async_quiesce()
        assert (await manager.async_check("gateway", TARGET))["status"] == "not_checked"
        gateway.async_check_remote_node.assert_not_awaited()

    asyncio.run(run())


def test_health_never_echoes_provider_details() -> None:
    async def run():
        manager, gateway, _ = _manager()
        gateway.async_check_remote_node.side_effect = RuntimeError("PRIVATE PROVIDER PAYLOAD")
        result = await manager.async_check("gateway", TARGET)
        assert result["status"] == "not_checked"
        assert "PRIVATE" not in json.dumps(result)
        gateway.async_check_remote_node.side_effect = None
        gateway.async_check_remote_node.return_value = {"firmware_version": "SECRET", "private_key": "SECRET"}
        assert "SECRET" not in json.dumps(await manager.async_check("gateway", TARGET))

    asyncio.run(run())


def _services(monkeypatch):
    pytest.importorskip("voluptuous")
    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = str
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.Unauthorized = type("Unauthorized", (Exception,), {})
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.config_validation", cv)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)
    registrations = {}
    coordinator = SimpleNamespace(
        gateways={"gateway": object()},
        store=SimpleNamespace(async_reserve_remote_reboot=AsyncMock(return_value=True)),
        async_remote_reboot=AsyncMock(return_value={"status": "unconfirmed"}),
        async_check_remote_node=AsyncMock(return_value={"status": "responding"}),
    )

    def register(domain, name, handler, **kwargs):
        assert domain == "meshnet"
        registrations[name] = (handler, kwargs["schema"])

    hass = SimpleNamespace(
        services=SimpleNamespace(async_register=register),
        data={"meshnet": {"entry": coordinator}},
        auth=SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True))),
    )
    register_remote_services(hass, "optional")
    return registrations, coordinator, hass, exceptions


def test_remote_actions_require_exact_target_admin_and_explicit_confirmation(monkeypatch) -> None:
    async def run():
        services, coordinator, hass, errors = _services(monkeypatch)
        import voluptuous as vol

        handler, schema = services["reboot_remote_node"]
        good = {"gateway_id": "gateway", "target_node": TARGET, "confirm_remote": True}
        for bad in ({}, {**good, "target_node": "all"}, {**good, "confirm_remote": 1}, {**good, "confirm_remote": False}):
            with pytest.raises(vol.Invalid):
                schema(bad)
        call = SimpleNamespace(data=schema(good), context=SimpleNamespace(user_id="admin"), return_response=True)
        hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=False)
        with pytest.raises(errors.Unauthorized):
            await handler(call)
        coordinator.store.async_reserve_remote_reboot.assert_not_awaited()
        coordinator.async_remote_reboot.assert_not_awaited()
        hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=True)
        assert await handler(call) == {"status": "unconfirmed"}
        coordinator.async_remote_reboot.assert_awaited_once_with(gateway_id="gateway", target_node=TARGET, confirm_remote=True)
        coordinator.store.async_reserve_remote_reboot.return_value = False
        with pytest.raises(errors.HomeAssistantError, match="last hour"):
            await handler(call)
        assert coordinator.async_remote_reboot.await_count == 1
        # A system automation has no user; its explicit configuration is authorization.
        call.context.user_id = None
        coordinator.store.async_reserve_remote_reboot.return_value = True
        coordinator.async_remote_reboot.side_effect = RuntimeError("PRIVATE KEY")
        with pytest.raises(errors.HomeAssistantError) as raised:
            await handler(call)
        assert "PRIVATE" not in str(raised.value)
        assert coordinator.async_remote_reboot.await_count == 2
        coordinator.store.async_reserve_remote_reboot.side_effect = RuntimeError("DATABASE PATH")
        with pytest.raises(errors.HomeAssistantError):
            await handler(call)
        assert coordinator.async_remote_reboot.await_count == 2

    asyncio.run(run())


def test_remote_health_action_is_admin_only_and_never_selects_another_gateway(monkeypatch) -> None:
    async def run():
        services, coordinator, hass, errors = _services(monkeypatch)
        handler, schema = services["check_remote_node"]
        coordinator.async_check_remote_node.return_value = {
            "status": "responding", "target_node": TARGET, "gateway_id": "gateway", "unexpected": "PRIVATE"
        }
        call = SimpleNamespace(
            data=schema({"gateway_id": "gateway", "target_node": TARGET, "minimum_firmware": "2.8.0"}),
            context=SimpleNamespace(user_id="admin"), return_response=True,
        )
        assert await handler(call) == {"status": "responding"}
        coordinator.async_check_remote_node.assert_awaited_once_with(gateway_id="gateway", target_node=TARGET, minimum_firmware="2.8.0")
        hass.auth.async_get_user.return_value = None
        with pytest.raises(errors.Unauthorized):
            await handler(call)
        call.context.user_id = None
        call.data["gateway_id"] = "missing"
        with pytest.raises(errors.HomeAssistantError):
            await handler(call)
        assert coordinator.async_check_remote_node.await_count == 1

    asyncio.run(run())


def test_reboot_reservation_is_atomic_durable_and_shared_across_gateways(tmp_path) -> None:
    async def run():
        path = tmp_path / "mesh.sqlite3"
        now = datetime.now(UTC)
        store = MeshStore(path)
        await store.async_open()
        results = await asyncio.gather(
            store.async_reserve_remote_reboot("gateway1", TARGET, now=now),
            store.async_reserve_remote_reboot("gateway2", TARGET, now=now),
        )
        assert sorted(results) == [False, True]
        await store.async_close()
        store = MeshStore(path)
        await store.async_open()
        assert not await store.async_reserve_remote_reboot("gateway3", TARGET, now=now + timedelta(minutes=59))
        assert await store.async_reserve_remote_reboot("gateway3", TARGET, now=now + timedelta(hours=1))
        await store.async_close()

    asyncio.run(run())


def test_health_probes_share_durable_metadata_floor(tmp_path) -> None:
    async def run():
        path = tmp_path / "mesh.sqlite3"
        now = datetime.now(UTC)
        store = MeshStore(path)
        await store.async_open()
        assert await store.async_reserve_remote_probe(now=now)
        assert not await store.async_reserve_remote_probe(now=now + timedelta(seconds=59))
        assert not (await store.async_reserve_traceroute("gateway", TARGET, now=now))["reserved"]
        await store.async_close()
        store = MeshStore(path)
        await store.async_open()
        assert not await store.async_reserve_remote_probe(now=now)
        assert (await store.async_reserve_traceroute("gateway", TARGET, now=now + timedelta(minutes=1)))["reserved"]
        assert not await store.async_reserve_remote_probe(now=now + timedelta(minutes=1))
        await store.async_close()

    asyncio.run(run())
