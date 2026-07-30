"""Tests for MeshNet's server-owned gateway settings safety boundary."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import custom_components.meshnet.gateway_settings as settings_module
from custom_components.meshnet.gateway_settings import (
    GatewaySettingsConfirmationRequired,
    GatewaySettingsConflict,
    GatewaySettingsManager,
    GatewaySettingsPreviewExpired,
    GatewaySettingsUnavailable,
    GatewaySettingsValidationError,
    validate_changes_payload,
)


def _raw_settings() -> dict[str, Any]:
    return {
        "writable": True,
        "categories": [
            {
                "key": "identity",
                "label": "Identity",
                "fields": [
                    {
                        "path": "identity.long_name",
                        "label": "Long name",
                        "type": "string",
                        "value": "Gateway",
                        "max_length": 40,
                        "writable": True,
                    }
                ],
            },
            {
                "key": "radio",
                "label": "Radio",
                "fields": [
                    {
                        "path": "radio.tx_power",
                        "label": "TX power",
                        "type": "integer",
                        "value": 20,
                        "min": -9,
                        "max": 30,
                        "writable": True,
                    },
                    {
                        "path": "radio.region",
                        "label": "Region",
                        "type": "select",
                        "value": "US",
                        "options": [
                            {"value": "US", "label": "United States"},
                            {"value": "EU_868", "label": "Europe 868"},
                        ],
                        "writable": True,
                        "critical": True,
                        "requires_reconnect": True,
                    },
                ],
            },
            {
                "key": "network",
                "label": "Network",
                "fields": [
                    {
                        # This intentionally mislabels a secret as a string to
                        # prove the central sanitizer still removes its value.
                        "path": "network.password",
                        "label": "Wi-Fi password",
                        "type": "string",
                        "value": "never-return-this-secret",
                        "configured": True,
                        "allow_clear": True,
                        "writable": True,
                    }
                ],
            },
        ],
    }


class _Gateway:
    def __init__(
        self,
        gateway_id: str = "gateway-one",
        *,
        connected: bool = True,
        transport: str = "bluetooth",
    ) -> None:
        self.config = SimpleNamespace(
            gateway_id=gateway_id,
            name="Test gateway",
            protocol="meshtastic",
            transport=transport,
        )
        self.status = SimpleNamespace(connected=connected)
        self.raw = _raw_settings()
        self.apply_calls: list[dict[str, Any]] = []

    async def async_get_settings_snapshot(self) -> dict[str, Any]:
        return deepcopy(self.raw)

    async def async_apply_settings_plan(
        self, changes: dict[str, Any]
    ) -> dict[str, Any]:
        self.apply_calls.append(deepcopy(changes))
        fields = {
            field["path"]: field
            for category in self.raw["categories"]
            for field in category["fields"]
        }
        for path, value in changes.items():
            field = fields[path]
            if isinstance(value, dict):
                field["configured"] = value["operation"] == "replace"
            else:
                field["value"] = value
        return {"verified": list(changes)}


def _manager(*gateways: _Gateway) -> GatewaySettingsManager:
    return GatewaySettingsManager(
        SimpleNamespace(
            gateways={gateway.config.gateway_id: gateway for gateway in gateways}
        )
    )


def _get(manager: GatewaySettingsManager, gateway_id: str | None = None):
    return asyncio.run(manager.async_get(gateway_id))


def test_get_prefers_connected_local_gateway_and_redacts_secret_values() -> None:
    mqtt = _Gateway("mqtt", connected=True, transport="mqtt")
    local = _Gateway("local", connected=True)
    manager = _manager(mqtt, local)

    result = _get(manager)

    assert result["selected"]["gateway_id"] == "local"
    assert result["selected"]["writable"] is True
    password = next(
        field
        for category in result["selected"]["categories"]
        for field in category["fields"]
        if field["path"] == "network.password"
    )
    assert password["type"] == "secret"
    assert password["value"] is None
    assert password["configured"] is True
    assert "never-return-this-secret" not in json.dumps(result)
    assert len(result["selected"]["revision"]) == 64


def test_snapshot_warnings_are_exact_codes_not_provider_text() -> None:
    gateway = _Gateway()
    gateway.raw["warnings"] = ["private PIN 654321 provider failure"]
    gateway.raw["warning_codes"] = [
        "meshcore_commands_have_no_rollback",
        "unknown_provider_code",
    ]

    selected = _get(_manager(gateway))["selected"]

    assert selected["warnings"] == [
        "MeshCore has no settings transaction or guaranteed rollback. Each "
        "command is sent once and read back."
    ]
    assert "654321" not in json.dumps(selected)


def test_private_secret_state_changes_revision_without_leaking_material() -> None:
    gateway = _Gateway()
    raw_secret = "private-low-entropy-state"
    gateway.raw["_secret_revision_material"] = {
        "network.password": raw_secret
    }
    manager = _manager(gateway)

    first = _get(manager)["selected"]
    private_digest = manager._secret_revision_fingerprint(
        gateway.raw["_secret_revision_material"],
        secret_paths={"network.password"},
    )
    public_json = json.dumps(first)

    assert raw_secret not in public_json
    assert "_secret_revision_material" not in public_json
    assert private_digest is not None
    assert private_digest not in public_json

    gateway.raw["_secret_revision_material"] = {
        "network.password": "externally-replaced-state"
    }
    second = _get(manager)["selected"]
    assert second["revision"] != first["revision"]


def test_sanitized_field_safety_metadata_changes_revision() -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    first = _get(manager)["selected"]

    gateway.raw["categories"][1]["fields"][0]["max"] = 29
    second = _get(manager)["selected"]

    assert second["revision"] != first["revision"]


def test_malformed_writable_field_contracts_fail_read_only() -> None:
    """Bad provider metadata must never weaken central validation."""
    mutations = [
        ("radio.tx_power", {"min": 31, "max": 30}),
        ("radio.tx_power", {"step": 0}),
        ("radio.tx_power", {"value": True}),
        ("identity.long_name", {"max_length": 0}),
        (
            "radio.region",
            {
                "options": [
                    {"value": "US", "label": "United States"},
                    {"value": "US", "label": "Duplicate"},
                ]
            },
        ),
    ]
    for path, update in mutations:
        gateway = _Gateway()
        raw_field = next(
            field
            for category in gateway.raw["categories"]
            for field in category["fields"]
            if field["path"] == path
        )
        raw_field.update(update)

        selected = _get(_manager(gateway))["selected"]
        field = next(
            candidate
            for category in selected["categories"]
            for candidate in category["fields"]
            if candidate["path"] == path
        )

        assert field["writable"] is False
        assert "safety metadata" in field["read_only_reason"]


def test_capability_diagnostics_explain_contract_downgrades_without_schema_data() -> None:
    """Cache only counts and fixed states, never field or gateway details."""
    gateway = _Gateway("private-gateway-id")
    gateway.config.name = "Private bedroom radio"
    gateway.raw["read_only_reason"] = "private person at private.example.test"
    tx_power = gateway.raw["categories"][1]["fields"][0]
    tx_power.update({"min": 31, "max": 30})
    manager = _manager(gateway)

    selected = _get(manager)["selected"]
    diagnostics = manager.diagnostic_snapshot()
    capability = diagnostics["capability_observations"][0]

    assert selected["writable"] is True
    assert capability == {
        "diagnostic_id": "gateway_001",
        "protocol": "meshtastic",
        "transport": "bluetooth",
        "observed": True,
        "has_successful_snapshot": True,
        "last_read_outcome": "success",
        "capability_state": "writable",
        "connected": True,
        "source_available": True,
        "source_complete": True,
        "source_writable": True,
        "source_category_count": 3,
        "sanitized_category_count": 3,
        "source_field_count": 4,
        "sanitized_field_count": 4,
        "claimed_writable_field_count": 4,
        "schema_writable_field_count": 3,
        "editable_field_count": 3,
        "read_only_field_count": 1,
        "contract_downgraded_field_count": 1,
    }
    rendered = json.dumps(diagnostics)
    for private_value in (
        "private-gateway-id",
        "Private bedroom radio",
        "private person",
        "identity.long_name",
        "radio.tx_power",
        "never-return-this-secret",
    ):
        assert private_value not in rendered


@pytest.mark.parametrize(
    ("case", "expected_state"),
    [
        ("disconnected", "disconnected"),
        ("unavailable", "unavailable"),
        ("incomplete", "incomplete"),
        ("managed", "managed_mode"),
        ("rejected", "all_claimed_writable_fields_rejected"),
        ("provider_read_only", "no_writable_fields"),
    ],
)
def test_capability_diagnostics_distinguish_read_only_states(
    case: str,
    expected_state: str,
) -> None:
    gateway = _Gateway(connected=case != "disconnected")
    if case == "unavailable":
        gateway.raw["available"] = False
    elif case == "incomplete":
        gateway.raw["complete"] = False
    elif case == "managed":
        gateway.raw["writable"] = False
        gateway.raw["read_only_reason"] = (
            "managed_mode_rejects_local_admin_changes"
        )
        for category in gateway.raw["categories"]:
            for field in category["fields"]:
                field["writable"] = False
    elif case == "rejected":
        gateway.raw["categories"] = [
            {
                "key": "radio",
                "label": "Radio",
                "fields": [
                    {
                        "path": "radio.power",
                        "label": "Power",
                        "type": "integer",
                        "value": 20,
                        "min": 31,
                        "max": 30,
                        "writable": True,
                    }
                ],
            }
        ]
    elif case == "provider_read_only":
        gateway.raw["writable"] = False
        for category in gateway.raw["categories"]:
            for field in category["fields"]:
                field["writable"] = False

    manager = _manager(gateway)
    _get(manager)
    capability = manager.diagnostic_snapshot()["capability_observations"][0]

    assert capability["capability_state"] == expected_state
    assert capability["editable_field_count"] == 0


def test_capability_diagnostics_are_cached_only_and_invalidation_clears_them() -> None:
    gateway = _Gateway()
    original_getter = gateway.async_get_settings_snapshot
    gateway.async_get_settings_snapshot = AsyncMock(side_effect=original_getter)
    manager = _manager(gateway)

    before = manager.diagnostic_snapshot()
    assert before["capability_observations"][0]["observed"] is False
    gateway.async_get_settings_snapshot.assert_not_awaited()

    _get(manager)
    first = manager.diagnostic_snapshot()
    second = manager.diagnostic_snapshot()
    gateway.async_get_settings_snapshot.assert_awaited_once()
    assert first == second
    assert first["capability_data_is_cached_only"] is True

    manager.invalidate()
    cleared = manager.diagnostic_snapshot()
    assert cleared["capability_observations"][0]["observed"] is False
    gateway.async_get_settings_snapshot.assert_awaited_once()


@pytest.mark.parametrize(
    ("failure", "expected_outcome"),
    [
        (RuntimeError("private provider failure at bedroom.local"), "provider_error"),
        ("invalid private snapshot", "invalid_snapshot"),
    ],
)
def test_capability_diagnostics_reduce_read_failures_to_fixed_categories(
    failure: Any,
    expected_outcome: str,
) -> None:
    gateway = _Gateway("private-gateway-id")
    if isinstance(failure, Exception):
        gateway.async_get_settings_snapshot = AsyncMock(side_effect=failure)
    else:
        gateway.async_get_settings_snapshot = AsyncMock(return_value=failure)
    manager = _manager(gateway)

    with pytest.raises(GatewaySettingsUnavailable):
        _get(manager)
    diagnostics = manager.diagnostic_snapshot()
    capability = diagnostics["capability_observations"][0]

    assert capability["observed"] is False
    assert capability["has_successful_snapshot"] is False
    assert capability["last_read_outcome"] == expected_outcome
    assert diagnostics["settings_read_failure_counts"][expected_outcome] == 1
    rendered = json.dumps(diagnostics)
    assert "private-gateway-id" not in rendered
    assert "bedroom.local" not in rendered
    assert "invalid private snapshot" not in rendered


def test_capability_diagnostic_observations_are_bounded_and_identity_free() -> None:
    gateways = [_Gateway(f"private-gateway-{index:03d}") for index in range(70)]
    manager = _manager(*gateways)

    diagnostics = manager.diagnostic_snapshot()

    assert len(diagnostics["capability_observations"]) == 64
    assert diagnostics["capability_observation_truncated"] is True
    assert "private-gateway" not in json.dumps(diagnostics)


@pytest.mark.parametrize(
    "material",
    [
        "not-a-mapping",
        {"unknown.secret": "private"},
        {"network.password": object()},
        {1: "private", "network.password": "private"},
    ],
)
def test_invalid_private_secret_revision_material_fails_closed(
    material: Any,
) -> None:
    gateway = _Gateway()
    gateway.raw["_secret_revision_material"] = material

    with pytest.raises(GatewaySettingsUnavailable):
        _get(_manager(gateway))


def test_disconnected_gateway_is_read_only_even_if_adapter_claims_writable() -> None:
    gateway = _Gateway(connected=False)

    selected = _get(_manager(gateway))["selected"]

    assert selected["writable"] is False
    assert "Connect" in selected["read_only_reason"]


def test_preview_is_typed_redacted_and_orders_critical_changes_last() -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    selected = _get(manager)["selected"]

    preview = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "radio.region": "EU_868",
                "network.password": {
                    "operation": "replace",
                    "value": "private replacement",
                },
                "identity.long_name": "New name",
            },
        )
    )

    assert [change["path"] for change in preview["changes"]] == [
        "identity.long_name",
        "network.password",
        "radio.region",
    ]
    secret_change = preview["changes"][1]
    assert secret_change["before"] == "Configured"
    assert secret_change["after"] == "Will be replaced"
    assert secret_change["operation"] == "replace"
    assert "private replacement" not in json.dumps(preview)
    assert "private replacement" not in repr(manager._previews)
    assert preview["requires_critical_confirmation"] is True


def test_stale_revision_is_rejected_before_preview_or_apply() -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    selected = _get(manager)["selected"]

    with pytest.raises(GatewaySettingsConflict):
        asyncio.run(
            manager.async_preview(
                gateway_id=gateway.config.gateway_id,
                revision="0" * 64,
                changes={"identity.long_name": "Changed"},
            )
        )

    preview = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={"identity.long_name": "Changed"},
        )
    )
    gateway.raw["categories"][0]["fields"][0]["value"] = "External edit"
    with pytest.raises(GatewaySettingsConflict):
        asyncio.run(
            manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=preview["preview_id"],
                confirm_critical=False,
            )
        )
    assert gateway.apply_calls == []


def test_critical_confirmation_is_required_and_preview_is_single_use() -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    selected = _get(manager)["selected"]
    preview = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={"radio.region": "EU_868"},
        )
    )

    with pytest.raises(GatewaySettingsConfirmationRequired):
        asyncio.run(
            manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=preview["preview_id"],
                confirm_critical=False,
            )
        )
    with pytest.raises(GatewaySettingsPreviewExpired):
        asyncio.run(
            manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=preview["preview_id"],
                confirm_critical=True,
            )
        )
    assert gateway.apply_calls == []


def test_apply_uses_frozen_plan_once_and_verifies_readback() -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    selected = _get(manager)["selected"]
    preview = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "identity.long_name": "Changed",
                "network.password": {"operation": "clear"},
            },
        )
    )

    result = asyncio.run(
        manager.async_apply(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            preview_id=preview["preview_id"],
            confirm_critical=False,
        )
    )

    assert gateway.apply_calls == [
        {
            "identity.long_name": "Changed",
            "network.password": {"operation": "clear"},
        }
    ]
    assert result["status"] == "verified"
    assert result["verified"] == ["identity.long_name", "network.password"]
    assert result["unverified"] == []
    assert result["snapshot"]["revision"] != selected["revision"]
    with pytest.raises(GatewaySettingsPreviewExpired):
        asyncio.run(
            manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=preview["preview_id"],
                confirm_critical=False,
            )
        )


def test_verified_connection_update_is_persisted_but_never_returned() -> None:
    pin = "654321"

    class CredentialGateway(_Gateway):
        async def async_apply_settings_plan(
            self, changes: dict[str, Any]
        ) -> dict[str, Any]:
            self.raw["categories"][2]["fields"][0]["configured"] = True
            return {
                "verified": ["security.pin"],
                "connection_updates": {"pin": pin},
            }

    class Coordinator:
        def __init__(self, gateway: _Gateway) -> None:
            self.gateways = {gateway.config.gateway_id: gateway}
            self.saved: list[tuple[str, dict[str, str | None]]] = []

        async def async_persist_gateway_connection_updates(
            self, gateway_id: str, updates: dict[str, str | None]
        ) -> None:
            self.saved.append((gateway_id, deepcopy(updates)))

    gateway = CredentialGateway()
    # The central handoff only accepts the canonical connection-secret path.
    gateway.raw["categories"][2]["fields"][0]["path"] = "security.pin"
    gateway.raw["categories"][2]["fields"][0]["label"] = "Bluetooth PIN"
    coordinator = Coordinator(gateway)
    manager = GatewaySettingsManager(coordinator)
    selected = _get(manager)["selected"]
    preview = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "security.pin": {"operation": "replace", "value": pin}
            },
        )
    )

    result = asyncio.run(
        manager.async_apply(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            preview_id=preview["preview_id"],
            confirm_critical=False,
        )
    )

    assert coordinator.saved == [(gateway.config.gateway_id, {"pin": pin})]
    assert result["connection_recovery_required"] is False
    assert pin not in json.dumps(result)
    assert "connection_updates" not in result


def test_verified_pin_persistence_failure_requires_recovery() -> None:
    pin = "654321"

    class CredentialGateway(_Gateway):
        async def async_apply_settings_plan(
            self, changes: dict[str, Any]
        ) -> dict[str, Any]:
            self.raw["categories"][2]["fields"][0]["configured"] = True
            return {
                "verified": ["security.pin"],
                "connection_updates": {"pin": pin},
            }

    gateway = CredentialGateway()
    gateway.raw["categories"][2]["fields"][0]["path"] = "security.pin"
    gateway.raw["categories"][2]["fields"][0]["label"] = "Bluetooth PIN"
    coordinator = SimpleNamespace(
        gateways={gateway.config.gateway_id: gateway},
        async_persist_gateway_connection_updates=AsyncMock(
            side_effect=RuntimeError("private provider failure")
        ),
    )
    manager = GatewaySettingsManager(coordinator)

    async def run() -> dict[str, Any]:
        selected = (await manager.async_get())["selected"]
        preview = await manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "security.pin": {"operation": "replace", "value": pin}
            },
        )
        return await manager.async_apply(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            preview_id=preview["preview_id"],
            confirm_critical=False,
        )

    result = asyncio.run(run())

    assert result["connection_recovery_required"] is True
    assert len(result["warnings"]) == 1
    assert "could not save it" in result["warnings"][0]
    assert pin not in json.dumps(result)
    assert "private provider failure" not in json.dumps(result)


def test_untrusted_backend_warning_text_and_unverified_handoff_are_dropped() -> None:
    pin = "654321"

    class UntrustedGateway(_Gateway):
        async def async_apply_settings_plan(
            self, changes: dict[str, Any]
        ) -> dict[str, Any]:
            return {
                "verified": [],
                "warnings": [f"device rejected private PIN {pin}"],
                "connection_updates": {"pin": pin},
            }

    gateway = UntrustedGateway()
    gateway.raw["categories"][2]["fields"][0]["path"] = "security.pin"
    gateway.raw["categories"][2]["fields"][0]["label"] = "Bluetooth PIN"
    persisted = AsyncMock()
    coordinator = SimpleNamespace(
        gateways={gateway.config.gateway_id: gateway},
        async_persist_gateway_connection_updates=persisted,
    )
    manager = GatewaySettingsManager(coordinator)
    selected = _get(manager)["selected"]
    preview = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "security.pin": {"operation": "replace", "value": pin}
            },
        )
    )

    result = asyncio.run(
        manager.async_apply(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            preview_id=preview["preview_id"],
            confirm_critical=False,
        )
    )

    persisted.assert_not_awaited()
    assert result["verified"] == []
    assert result["unverified"] == ["security.pin"]
    assert result["connection_recovery_required"] is True
    assert len(result["warnings"]) == 1
    assert "not fully verified and saved" in result["warnings"][0]
    assert pin not in json.dumps(result)


@pytest.mark.parametrize("handoff", [None, {"pin": "123456"}])
def test_verified_pin_without_exact_handoff_requires_recovery(
    handoff: dict[str, str] | None,
) -> None:
    requested_pin = "654321"

    class IncompleteCredentialGateway(_Gateway):
        async def async_apply_settings_plan(
            self, changes: dict[str, Any]
        ) -> dict[str, Any]:
            self.raw["categories"][2]["fields"][0]["configured"] = True
            result: dict[str, Any] = {"verified": ["security.pin"]}
            if handoff is not None:
                result["connection_updates"] = handoff
            return result

    gateway = IncompleteCredentialGateway()
    gateway.raw["categories"][2]["fields"][0]["path"] = "security.pin"
    gateway.raw["categories"][2]["fields"][0]["label"] = "Bluetooth PIN"
    persisted = AsyncMock()
    coordinator = SimpleNamespace(
        gateways={gateway.config.gateway_id: gateway},
        async_persist_gateway_connection_updates=persisted,
    )
    manager = GatewaySettingsManager(coordinator)

    async def run() -> dict[str, Any]:
        selected = (await manager.async_get())["selected"]
        preview = await manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "security.pin": {
                    "operation": "replace",
                    "value": requested_pin,
                }
            },
        )
        return await manager.async_apply(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            preview_id=preview["preview_id"],
            confirm_critical=False,
        )

    result = asyncio.run(run())

    persisted.assert_not_awaited()
    assert result["connection_recovery_required"] is True
    assert len(result["warnings"]) == 1
    if handoff is None:
        assert "not fully verified and saved" in result["warnings"][0]
    else:
        assert "could not save it" in result["warnings"][0]
    assert requested_pin not in json.dumps(result)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("radio.tx_power", True),
        ("radio.tx_power", 31),
        ("radio.region", "NOT_A_REGION"),
        ("network.password", "plain secret"),
        ("unknown.field", "value"),
    ],
)
def test_preview_rejects_wrong_types_ranges_secrets_and_unknown_paths(
    path: str, value: Any
) -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    selected = _get(manager)["selected"]

    with pytest.raises(GatewaySettingsValidationError):
        asyncio.run(
            manager.async_preview(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                changes={path: value},
            )
        )


def test_preview_rejects_clearing_an_unconfigured_secret() -> None:
    gateway = _Gateway()
    gateway.raw["categories"][2]["fields"][0]["configured"] = False
    manager = _manager(gateway)
    selected = _get(manager)["selected"]

    with pytest.raises(GatewaySettingsValidationError):
        asyncio.run(
            manager.async_preview(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                changes={"network.password": {"operation": "clear"}},
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"__proto__.polluted": True},
        {"safe.path": {"operation": "replace", "value": ""}},
        {"safe.path": {"operation": "retry", "value": "secret"}},
        {"safe.path": float("nan")},
        {"safe.path": 2**53},
        {"safe.path": 2**80},
    ],
)
def test_websocket_change_payload_is_strictly_bounded(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_changes_payload(changes)


def test_invalidate_destroys_all_pending_previews() -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    selected = _get(manager)["selected"]
    preview = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={"identity.long_name": "Changed"},
        )
    )

    manager.invalidate()

    assert manager._previews == {}
    with pytest.raises(GatewaySettingsPreviewExpired):
        asyncio.run(
            manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=preview["preview_id"],
                confirm_critical=False,
            )
        )


def test_preview_has_active_ttl_callback_that_destroys_secret_plan() -> None:
    async def run() -> None:
        gateway = _Gateway()
        manager = _manager(gateway)
        selected = (await manager.async_get())["selected"]
        preview = await manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "network.password": {
                    "operation": "replace",
                    "value": "short-lived secret",
                }
            },
        )
        retained = manager._previews[preview["preview_id"]]
        assert retained.expiry_handle is not None

        manager._expire_preview(preview["preview_id"])

        assert manager._previews == {}
        assert retained.expiry_handle is None
        assert "short-lived secret" not in repr(manager._previews)
        with pytest.raises(GatewaySettingsPreviewExpired):
            await manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=preview["preview_id"],
                confirm_critical=False,
            )

    asyncio.run(run())


def test_new_preview_for_gateway_invalidates_older_secret_plan() -> None:
    gateway = _Gateway()
    manager = _manager(gateway)
    selected = _get(manager)["selected"]
    first = asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={
                "network.password": {
                    "operation": "replace",
                    "value": "first secret",
                }
            },
        )
    )
    asyncio.run(
        manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={"identity.long_name": "Second preview"},
        )
    )

    with pytest.raises(GatewaySettingsPreviewExpired):
        asyncio.run(
            manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=first["preview_id"],
                confirm_critical=False,
            )
        )
    assert "first secret" not in repr(manager._previews)


def test_quiesce_cancels_and_drains_an_active_apply_before_resume() -> None:
    async def run() -> None:
        started = asyncio.Event()

        class BlockingGateway(_Gateway):
            async def async_apply_settings_plan(
                self, changes: dict[str, Any]
            ) -> dict[str, Any]:
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("cancelled settings work resumed")

        gateway = BlockingGateway()
        manager = _manager(gateway)
        selected = (await manager.async_get())["selected"]
        preview = await manager.async_preview(
            gateway_id=gateway.config.gateway_id,
            revision=selected["revision"],
            changes={"identity.long_name": "Changed"},
        )
        apply_task = asyncio.create_task(
            manager.async_apply(
                gateway_id=gateway.config.gateway_id,
                revision=selected["revision"],
                preview_id=preview["preview_id"],
                confirm_critical=False,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        assert await manager.async_quiesce() is True
        with pytest.raises(asyncio.CancelledError):
            await apply_task
        assert manager.diagnostic_snapshot()["active_operation_count"] == 0
        with pytest.raises(GatewaySettingsUnavailable):
            await manager.async_get()

        assert manager.resume() is True
        assert (await manager.async_get())["selected"]["gateway_id"] == (
            gateway.config.gateway_id
        )

    asyncio.run(run())


def test_get_deadline_includes_adapter_read_and_clears_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abandoned settings read must not outlive the caller."""

    async def run() -> None:
        class BlockingGateway(_Gateway):
            async def async_get_settings_snapshot(self) -> dict[str, Any]:
                await asyncio.Event().wait()
                raise AssertionError("cancelled read resumed")

        monkeypatch.setattr(
            settings_module, "SETTINGS_READ_TIMEOUT_SECONDS", 0.05
        )
        manager = _manager(BlockingGateway())

        with pytest.raises(GatewaySettingsUnavailable):
            await manager.async_get()
        diagnostics = manager.diagnostic_snapshot()
        assert diagnostics["active_operation_count"] == 0
        assert diagnostics["settings_read_attempt_count"] == 1
        assert diagnostics["settings_read_success_count"] == 0
        assert diagnostics["settings_read_failure_counts"]["timeout"] == 1
        capability = diagnostics["capability_observations"][0]
        assert capability["last_read_outcome"] == "timeout"
        assert capability["has_successful_snapshot"] is False

    asyncio.run(run())


def test_preview_deadline_includes_lock_wait_and_creates_no_late_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock contention must not create a preview after the UI times out."""

    async def run() -> None:
        gateway = _Gateway()
        manager = _manager(gateway)
        selected = (await manager.async_get())["selected"]
        monkeypatch.setattr(
            settings_module, "SETTINGS_READ_TIMEOUT_SECONDS", 0.01
        )
        lock = manager._lock(gateway.config.gateway_id)
        await lock.acquire()
        try:
            with pytest.raises(GatewaySettingsUnavailable):
                await manager.async_preview(
                    gateway_id=gateway.config.gateway_id,
                    revision=selected["revision"],
                    changes={"identity.long_name": "Changed"},
                )
        finally:
            lock.release()

        await asyncio.sleep(0)
        assert manager._previews == {}
        assert manager.diagnostic_snapshot()["active_operation_count"] == 0

    asyncio.run(run())
