"""Home Assistant WebSocket boundary tests for reviewed remote administration."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import Unauthorized  # noqa: E402

from custom_components.meshnet.websocket_api import (  # noqa: E402
    websocket_remote_settings_apply,
    websocket_remote_settings_get,
    websocket_remote_settings_preview,
    websocket_traceroute_status,
)


def _hass(coordinator):
    return SimpleNamespace(data={"meshnet": {"entry": coordinator}})


def test_remote_websocket_schemas_are_exact_and_admin_only() -> None:
    coordinator = SimpleNamespace(
        async_remote_settings_get=AsyncMock(),
        async_remote_settings_preview=AsyncMock(),
        async_remote_settings_apply=AsyncMock(),
    )
    hass = _hass(coordinator)
    connection = SimpleNamespace(
        user=SimpleNamespace(is_admin=False),
        send_result=MagicMock(),
        send_error=MagicMock(),
    )
    messages = (
        (
            websocket_remote_settings_get,
            {
                "id": 1,
                "type": "meshnet/remote_settings/get",
                "gateway_id": "ble-gateway",
                "target_node": "!1234abcd",
            },
        ),
        (
            websocket_remote_settings_preview,
            {
                "id": 2,
                "type": "meshnet/remote_settings/preview",
                "gateway_id": "ble-gateway",
                "target_node": "!1234abcd",
                "revision": "a" * 64,
                "changes": {"owner.short_name": "NEW"},
            },
        ),
        (
            websocket_remote_settings_apply,
            {
                "id": 3,
                "type": "meshnet/remote_settings/apply",
                "gateway_id": "ble-gateway",
                "target_node": "!1234abcd",
                "revision": "a" * 64,
                "preview_id": "p" * 43,
                "confirm_remote": True,
            },
        ),
    )
    for handler, message in messages:
        with pytest.raises(Unauthorized):
            handler(hass, connection, message)
    coordinator.async_remote_settings_get.assert_not_awaited()
    coordinator.async_remote_settings_preview.assert_not_awaited()
    coordinator.async_remote_settings_apply.assert_not_awaited()


def test_traceroute_status_is_admin_only_and_performs_no_rf() -> None:
    """Reading the global cooldown delegates only to durable storage."""

    async def run() -> None:
        status = {
            "schema_version": 1,
            "scope": "integration",
            "reserved": True,
            "status": "cooldown",
            "gateway_id": "ble-gateway",
            "target_node": "meshtastic:!1234abcd",
            "remaining_seconds": 3599,
        }
        store = SimpleNamespace(
            async_get_global_traceroute_status=AsyncMock(return_value=status)
        )
        coordinator = SimpleNamespace(
            store=store,
            async_manual_traceroute=AsyncMock(),
        )
        hass = _hass(coordinator)
        denied = SimpleNamespace(
            user=SimpleNamespace(is_admin=False),
            send_message=MagicMock(),
            send_error=MagicMock(),
        )
        message = {"id": 4, "type": "meshnet/traceroute/status"}

        with pytest.raises(Unauthorized):
            websocket_traceroute_status(hass, denied, message)
        store.async_get_global_traceroute_status.assert_not_awaited()
        coordinator.async_manual_traceroute.assert_not_awaited()

        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())
        await websocket_traceroute_status.__wrapped__.__wrapped__(
            hass, connection, message
        )

        store.async_get_global_traceroute_status.assert_awaited_once_with()
        coordinator.async_manual_traceroute.assert_not_awaited()
        envelope = connection.send_message.call_args.args[0]
        assert envelope["id"] == 4
        assert envelope["type"] == "result"
        assert envelope["success"] is True
        assert envelope["result"] == status

    asyncio.run(run())


def test_traceroute_status_failure_uses_fixed_redacted_error() -> None:
    """Storage failures cannot echo provider details through the status endpoint."""

    async def run() -> None:
        sentinel = "private-key at /dev/serial/by-id/private-device"
        store = SimpleNamespace(
            async_get_global_traceroute_status=AsyncMock(
                side_effect=RuntimeError(sentinel)
            )
        )
        coordinator = SimpleNamespace(store=store, async_manual_traceroute=AsyncMock())
        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())

        await websocket_traceroute_status.__wrapped__.__wrapped__(
            _hass(coordinator),
            connection,
            {"id": 5, "type": "meshnet/traceroute/status"},
        )

        connection.send_message.assert_not_called()
        connection.send_error.assert_called_once_with(
            5,
            "traceroute_status_failed",
            "MeshNet could not load the traceroute status",
        )
        coordinator.async_manual_traceroute.assert_not_awaited()
        assert sentinel not in json.dumps(connection.send_error.call_args_list, default=str)

    asyncio.run(run())


def test_traceroute_status_missing_integration_uses_fixed_error() -> None:
    """An unloaded integration cannot expose framework exception details."""

    async def run() -> None:
        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())

        await websocket_traceroute_status.__wrapped__.__wrapped__(
            SimpleNamespace(data={}),
            connection,
            {"id": 6, "type": "meshnet/traceroute/status"},
        )

        connection.send_message.assert_not_called()
        connection.send_error.assert_called_once_with(
            6,
            "traceroute_status_failed",
            "MeshNet could not load the traceroute status",
        )

    asyncio.run(run())


def test_traceroute_status_rejects_extra_values_without_echoing_them() -> None:
    """Unexpected status input is rejected inside the redacted handler boundary."""

    async def run() -> None:
        sentinel = "private-key-must-never-reach-validation-logs"
        store = SimpleNamespace(async_get_global_traceroute_status=AsyncMock())
        coordinator = SimpleNamespace(store=store)
        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())

        await websocket_traceroute_status.__wrapped__.__wrapped__(
            _hass(coordinator),
            connection,
            {
                "id": 7,
                "type": "meshnet/traceroute/status",
                "private_key": sentinel,
            },
        )

        store.async_get_global_traceroute_status.assert_not_awaited()
        connection.send_message.assert_not_called()
        connection.send_error.assert_called_once_with(
            7,
            "traceroute_status_failed",
            "MeshNet could not load the traceroute status",
        )
        assert sentinel not in json.dumps(connection.send_error.call_args_list, default=str)

    asyncio.run(run())


def test_remote_handlers_delegate_exactly_and_never_echo_provider_details() -> None:
    async def run() -> None:
        coordinator = SimpleNamespace(
            async_remote_settings_get=AsyncMock(return_value={"schema_version": 1, "controller": {}}),
            async_remote_settings_preview=AsyncMock(return_value={"schema_version": 1, "preview_id": "p" * 43}),
            async_remote_settings_apply=AsyncMock(return_value={"schema_version": 1, "status": "verified"}),
        )
        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())
        hass = _hass(coordinator)

        await websocket_remote_settings_get.__wrapped__.__wrapped__(
            hass,
            connection,
            {
                "id": 10,
                "type": "meshnet/remote_settings/get",
                "gateway_id": "ble-gateway",
                "target_node": "!1234abcd",
            },
        )
        coordinator.async_remote_settings_get.assert_awaited_once_with(
            gateway_id="ble-gateway", target_node="!1234abcd"
        )

        await websocket_remote_settings_preview.__wrapped__.__wrapped__(
            hass,
            connection,
            {
                "id": 11,
                "type": "meshnet/remote_settings/preview",
                "gateway_id": "ble-gateway",
                "target_node": "!1234abcd",
                "revision": "a" * 64,
                "changes": {"owner.short_name": "NEW"},
            },
        )
        coordinator.async_remote_settings_preview.assert_awaited_once_with(
            gateway_id="ble-gateway",
            target_node="!1234abcd",
            revision="a" * 64,
            changes={"owner.short_name": "NEW"},
        )

        await websocket_remote_settings_apply.__wrapped__.__wrapped__(
            hass,
            connection,
            {
                "id": 12,
                "type": "meshnet/remote_settings/apply",
                "gateway_id": "ble-gateway",
                "target_node": "!1234abcd",
                "revision": "a" * 64,
                "preview_id": "p" * 43,
                "confirm_remote": True,
            },
        )
        coordinator.async_remote_settings_apply.assert_awaited_once_with(
            gateway_id="ble-gateway",
            target_node="!1234abcd",
            revision="a" * 64,
            preview_id="p" * 43,
            confirm_remote=True,
        )
        serialized = json.dumps(connection.send_message.call_args_list, default=str)
        assert "private_key" not in serialized
        assert "session_passkey" not in serialized

    asyncio.run(run())


def test_malformed_remote_preview_is_rejected_without_value_echo() -> None:
    async def run() -> None:
        sentinel = "private-value-must-not-be-rendered"
        coordinator = SimpleNamespace(async_remote_settings_preview=AsyncMock())
        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())
        await websocket_remote_settings_preview.__wrapped__.__wrapped__(
            _hass(coordinator),
            connection,
            {
                "id": 20,
                "type": "meshnet/remote_settings/preview",
                "gateway_id": "ble-gateway",
                "target_node": "!1234abcd",
                "revision": "a" * 64,
                "changes": {"config.security.private_key": {"value": sentinel}},
            },
        )
        coordinator.async_remote_settings_preview.assert_not_awaited()
        connection.send_error.assert_called_once_with(
            20,
            "remote_admin_changes_invalid",
            "One or more remote settings changes are invalid",
        )
        assert sentinel not in json.dumps(connection.send_error.call_args_list, default=str)

    asyncio.run(run())
