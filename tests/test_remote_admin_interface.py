"""Acceptance tests for MeshNet's administrator-only remote-node interface.

The radio-level protocol tests live in ``test_remote_admin_safety.py``.  This
module specifies the narrower Home Assistant boundary before that boundary is
connected to WebSocket or panel code: exact identities, Bluetooth only,
copy-only controller public-key projection, preview/confirmation, one-shot
apply, and no security or raw-admin escape hatch.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.meshnet.models import GatewayConfig, GatewayStatus
from custom_components.meshnet.remote_admin import RemoteAdminError

TARGET = "!1234abcd"
GATEWAY_ID = "ble-gateway"
REVISION = "a" * 64
CONTROLLER_KEY = "base64:" + ("AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA=")


def _snapshot() -> dict:
    return {
        "available": True,
        "complete": True,
        "source": "remote_radio",
        "transport": "bluetooth",
        "revision": 3,
        "capabilities": {
            "read": True,
            "plan": True,
            "apply": True,
            "verification": True,
        },
        "controller": {
            "node_id": "!01020304",
            "short_name": "CTRL",
            "public_key": CONTROLLER_KEY,
        },
        "target": {
            "node_id": TARGET,
            "long_name": "Remote node",
            "short_name": "RMTE",
            "public_key_available": True,
            "remote_admin_eligible": True,
        },
        "categories": [
            {
                "key": "owner",
                "label": "Owner",
                "fields": [
                    {
                        "path": "owner.long_name",
                        "label": "Long name",
                        "type": "string",
                        "value": "Remote node",
                        "writable": True,
                    },
                    {
                        "path": "owner.short_name",
                        "label": "Short name",
                        "type": "string",
                        "value": "RMTE",
                        "writable": True,
                    },
                ],
            },
            {
                "key": "config.display",
                "label": "Display",
                "fields": [
                    {
                        "path": "config.display.flip_screen",
                        "label": "Flip screen",
                        "type": "boolean",
                        "value": False,
                        "writable": True,
                    }
                ],
            },
        ],
    }


def _manager(*, transport: str = "bluetooth", connected: bool = True):
    from custom_components.meshnet.remote_admin import RemoteAdminManager

    config = GatewayConfig(
        gateway_id=GATEWAY_ID,
        name="BLE gateway",
        protocol="meshtastic",
        transport=transport,
    )
    gateway = SimpleNamespace(
        config=config,
        status=GatewayStatus(
            gateway_id=GATEWAY_ID,
            name="BLE gateway",
            protocol="meshtastic",
            transport=transport,
            connected=connected,
        ),
        async_get_remote_settings_snapshot=AsyncMock(return_value=_snapshot()),
        async_apply_remote_settings_plan=AsyncMock(
            return_value={
                "status": "verified",
                "verified": ["owner.short_name"],
                "unverified": [],
                "target": {"node_id": TARGET, "short_name": "NEW"},
            }
        ),
    )
    coordinator = SimpleNamespace(gateways={GATEWAY_ID: gateway})
    return RemoteAdminManager(coordinator), gateway


def test_remote_get_is_exact_ble_only_and_exposes_copy_only_controller_key() -> None:
    async def run() -> None:
        manager, gateway = _manager()
        result = await manager.async_get(GATEWAY_ID, TARGET)

        assert result["schema_version"] == 1
        assert result["gateway_id"] == GATEWAY_ID
        assert result["target_node"] == TARGET
        assert len(result["revision"]) == 64
        assert result["controller"]["public_key"] == CONTROLLER_KEY
        assert result["controller"]["public_key_copy_only"] is True
        assert "private" not in json.dumps(result).casefold()
        gateway.async_get_remote_settings_snapshot.assert_awaited_once_with(TARGET)

        for invalid in ("Remote node", "^all", "!1234ABCd", "!00000000"):
            with pytest.raises(RemoteAdminError):
                await manager.async_get(GATEWAY_ID, invalid)
        assert gateway.async_get_remote_settings_snapshot.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["serial", "tcp", "mqtt", "rest"])
def test_remote_interface_never_falls_back_from_bluetooth(transport: str) -> None:
    async def run() -> None:
        manager, gateway = _manager(transport=transport)
        with pytest.raises(Exception) as raised:
            await manager.async_get(GATEWAY_ID, TARGET)
        assert getattr(raised.value, "code", None) == "remote_admin_requires_bluetooth"
        gateway.async_get_remote_settings_snapshot.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize(
    "path",
    [
        "config.security.private_key",
        "config.security.public_key",
        "config.security.admin_key",
        "config.bluetooth.fixed_pin",
        "channel.0.settings.psk",
        "admin.factory_reset_device",
        "raw_admin_message",
        "unknown.future_setting",
    ],
)
def test_preview_fails_closed_for_keys_secrets_destructive_and_unknown_paths(
    path: str,
) -> None:
    async def run() -> None:
        manager, gateway = _manager()
        loaded = await manager.async_get(GATEWAY_ID, TARGET)
        with pytest.raises(Exception) as raised:
            await manager.async_preview(
                GATEWAY_ID,
                TARGET,
                loaded["revision"],
                {path: "private-value-must-not-leak"},
            )
        assert getattr(raised.value, "code", None) == "remote_admin_changes_invalid"
        assert "private-value-must-not-leak" not in str(raised.value)
        gateway.async_apply_remote_settings_plan.assert_not_awaited()

    asyncio.run(run())


def test_preview_confirmation_apply_is_single_use_and_readback_verified() -> None:
    async def run() -> None:
        manager, gateway = _manager()
        loaded = await manager.async_get(GATEWAY_ID, TARGET)
        preview = await manager.async_preview(
            GATEWAY_ID,
            TARGET,
            loaded["revision"],
            {"owner.short_name": "NEW"},
        )

        assert preview["schema_version"] == 1
        assert preview["requires_confirmation"] is True
        assert preview["changes"] == [{"path": "owner.short_name", "label": "Short name"}]
        assert "NEW" not in json.dumps(preview)
        gateway.async_apply_remote_settings_plan.assert_not_awaited()

        with pytest.raises(Exception) as raised:
            await manager.async_apply(
                GATEWAY_ID,
                TARGET,
                loaded["revision"],
                preview["preview_id"],
                confirm_remote=False,
            )
        assert getattr(raised.value, "code", None) == "remote_admin_confirmation_required"
        gateway.async_apply_remote_settings_plan.assert_not_awaited()

        applied = await manager.async_apply(
            GATEWAY_ID,
            TARGET,
            loaded["revision"],
            preview["preview_id"],
            confirm_remote=True,
        )
        assert applied == {
            "schema_version": 1,
            "status": "verified",
            "gateway_id": GATEWAY_ID,
            "target_node": TARGET,
            "verified": ["owner.short_name"],
            "unverified": [],
        }
        gateway.async_apply_remote_settings_plan.assert_awaited_once_with(TARGET, {"owner.short_name": "NEW"})

        with pytest.raises(RemoteAdminError):
            await manager.async_apply(
                GATEWAY_ID,
                TARGET,
                loaded["revision"],
                preview["preview_id"],
                confirm_remote=True,
            )
        gateway.async_apply_remote_settings_plan.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["get", "apply"])
def test_provider_error_text_never_crosses_remote_admin_boundary(phase: str) -> None:
    """Stable local messages replace arbitrary provider exception details."""
    sentinel = "session_passkey=PRIVATE-PROVIDER-DETAIL"

    class ProviderError(RuntimeError):
        code = "remote_admin_session_rejected"
        public_message = sentinel

    async def run() -> None:
        manager, gateway = _manager()
        if phase == "get":
            gateway.async_get_remote_settings_snapshot.side_effect = ProviderError()
            operation = manager.async_get(GATEWAY_ID, TARGET)
        else:
            loaded = await manager.async_get(GATEWAY_ID, TARGET)
            preview = await manager.async_preview(
                GATEWAY_ID,
                TARGET,
                loaded["revision"],
                {"owner.short_name": "NEW"},
            )
            gateway.async_apply_remote_settings_plan.side_effect = ProviderError()
            operation = manager.async_apply(
                GATEWAY_ID,
                TARGET,
                loaded["revision"],
                preview["preview_id"],
                confirm_remote=True,
            )

        with pytest.raises(RemoteAdminError) as raised:
            await operation
        assert raised.value.code == "remote_admin_session_rejected"
        assert sentinel not in str(raised.value)
        assert str(raised.value) == (
            "The remote-admin session was rejected; load settings again"
        )

    asyncio.run(run())


def test_remote_admin_quiesce_cancels_active_work_and_fences_new_requests() -> None:
    """Reload/unload must own remote RF work before transports are stopped."""

    async def run() -> None:
        manager, gateway = _manager()
        started = asyncio.Event()
        never = asyncio.Event()

        async def blocked_get(_target: str):
            started.set()
            await never.wait()
            return _snapshot()

        gateway.async_get_remote_settings_snapshot.side_effect = blocked_get
        operation = asyncio.create_task(manager.async_get(GATEWAY_ID, TARGET))
        await started.wait()

        assert await manager.async_quiesce() is True
        with pytest.raises(asyncio.CancelledError):
            await operation
        with pytest.raises(RemoteAdminError) as raised:
            await manager.async_get(GATEWAY_ID, TARGET)
        assert raised.value.code == "remote_admin_unavailable"

        gateway.async_get_remote_settings_snapshot.side_effect = None
        gateway.async_get_remote_settings_snapshot.return_value = _snapshot()
        assert manager.resume() is True
        assert (await manager.async_get(GATEWAY_ID, TARGET))["target_node"] == TARGET

    asyncio.run(run())


def test_remote_admin_has_no_service_or_automatic_callsite() -> None:
    """Remote RF administration is administrator WebSocket/panel only."""
    services = open("custom_components/meshnet/services.yaml", encoding="utf-8").read()
    integration = open("custom_components/meshnet/__init__.py", encoding="utf-8").read()
    assert "remote_admin:" not in services
    assert "SERVICE_REMOTE_ADMIN" not in integration
