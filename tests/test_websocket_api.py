"""Home Assistant-backed tests for MeshNet's privacy-minimal panel snapshot."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

pytest.importorskip("homeassistant")

import voluptuous as vol  # noqa: E402
from homeassistant.exceptions import Unauthorized  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402

from custom_components.meshnet import websocket_api as websocket_api_module  # noqa: E402
from custom_components.meshnet.models import (  # noqa: E402
    GatewayStatus,
    MeshSnapshot,
    MessageRecord,
    NodeState,
)
from custom_components.meshnet.websocket_api import (  # noqa: E402
    _FAVORITE_LABEL_NAME,
    _async_panel_operation,
    _panel_node,
    _snapshot_with_panel_metadata,
    websocket_messages,
    websocket_panel_log,
    websocket_send_message,
    websocket_settings_apply,
    websocket_settings_get,
    websocket_settings_preview,
    websocket_snapshot,
)

ENTRY_ID = "entry-id"
FAVORITE_NODE = "meshtastic:1"
OTHER_NODE = "meshtastic:2"


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id=ENTRY_ID),
        snapshot=MeshSnapshot(
            nodes={
                FAVORITE_NODE: NodeState(
                    node_key=FAVORITE_NODE,
                    protocol="meshtastic",
                ),
                OTHER_NODE: NodeState(
                    node_key=OTHER_NODE,
                    protocol="meshtastic",
                ),
            }
        ),
    )


def _assert_panel_metadata(result: dict, *, favorite_configured: bool) -> dict:
    metadata = result["panel_metadata"]
    assert metadata["favorite_label_configured"] is favorite_configured
    assert datetime.fromisoformat(metadata["last_snapshot_generated_at"])
    assert metadata["projection_schema_version"] == 2
    assert metadata["telemetry"]["schema_version"] == 1
    return metadata


def test_panel_identity_safety_handles_legacy_protocol_case() -> None:
    """Legacy protocol casing cannot bypass Meshtastic identity validation."""
    node = NodeState(
        node_key="meshtastic:!11111111",
        protocol="  Meshtastic  ",
        node_id="!22222222",
    )

    assert _panel_node(node, identity_valid=True)["identity_valid"] is False


def test_panel_node_projects_only_bounded_passive_neighbor_evidence() -> None:
    """Provider routing data cannot inject graph nodes or hide MQTT provenance."""
    node = NodeState(
        node_key="meshtastic:!01020304",
        protocol="meshtastic",
        node_id="!01020304",
        location={
            "latitude": 41.0,
            "longitude": -87.0,
            "precision_bits": 12,
            "private_note": "home",
        },
        routing={
            "neighbors": [
                "!11121314",
                "!21222324",
                "not-an-id",
                {"private": "value"},
            ],
            "neighbor_count": 999,
            "neighbors_updated_at": "2026-07-30T12:00:00+00:00",
            "neighbors_via_mqtt": False,
            "private_route_data": "secret",
        },
    )

    projected = _panel_node(node, identity_valid=True)

    assert projected["location"] == {
        "latitude": 41.0,
        "longitude": -87.0,
        "precision_bits": 12,
    }
    assert projected["routing"] == {
        "neighbors": ["!11121314", "!21222324"],
        "neighbor_count": 2,
        "neighbors_updated_at": "2026-07-30T12:00:00+00:00",
        "neighbors_via_mqtt": False,
    }
    assert "secret" not in json.dumps(projected)


def test_messages_websocket_redacts_legacy_raw_provider_metadata() -> None:
    async def run() -> None:
        message = MessageRecord(
            message_id="visible-message",
            protocol="meshtastic",
            gateway_id="visible-gateway",
            sender="visible-sender",
            receiver=None,
            channel="0",
            text="visible text",
            raw={
                "status": "queued",
                "last_error": "token=private at /dev/private",
                "provider_id": "private-provider-id",
                "last_error_code": "send_failed",
            },
        )
        coordinator = _coordinator()
        coordinator.store = SimpleNamespace(
            async_recent_messages=AsyncMock(return_value=[message])
        )
        hass = SimpleNamespace(data={"meshnet": {ENTRY_ID: coordinator}})
        connection = SimpleNamespace(send_message=MagicMock())
        handler = websocket_messages.__wrapped__.__wrapped__

        await handler(
            hass,
            connection,
            {"id": 11, "type": "meshnet/messages", "limit": 100},
        )

        envelope = connection.send_message.call_args.args[0]
        assert envelope["id"] == 11
        assert envelope["type"] == "result"
        assert envelope["success"] is True
        payload = envelope["result"]
        assert payload[0]["raw"] == {
            "status": "queued",
            "last_error_code": "send_failed",
        }
        serialized = json.dumps(payload)
        assert "token=private" not in serialized
        assert "/dev/private" not in serialized
        assert "private-provider-id" not in serialized

    import asyncio

    asyncio.run(run())


def test_sensitive_websocket_handlers_return_fixed_errors_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        private_error = (
            "database /config/private.sqlite for node !12345678 failed"
        )
        coordinator = _coordinator()
        coordinator.store = SimpleNamespace(
            async_recent_messages=AsyncMock(
                side_effect=RuntimeError(private_error)
            )
        )
        coordinator.async_send_message = AsyncMock(
            side_effect=RuntimeError(private_error)
        )
        hass = SimpleNamespace(data={"meshnet": {ENTRY_ID: coordinator}})
        connection = SimpleNamespace(
            send_message=MagicMock(),
            send_error=MagicMock(),
        )

        def failed_snapshot(*_args: object) -> dict:
            raise RuntimeError(private_error)

        monkeypatch.setattr(
            websocket_api_module,
            "_snapshot_with_panel_metadata",
            failed_snapshot,
        )

        await websocket_snapshot.__wrapped__.__wrapped__(
            hass,
            connection,
            {"id": 40, "type": "meshnet/snapshot"},
        )
        await websocket_messages.__wrapped__.__wrapped__(
            hass,
            connection,
            {"id": 41, "type": "meshnet/messages", "limit": 100},
        )
        await websocket_send_message.__wrapped__.__wrapped__(
            hass,
            connection,
            {
                "id": 42,
                "type": "meshnet/send_message",
                "message": "safe caller message",
                "target_node": "!12345678",
                "priority": "normal",
                "message_type": "direct",
            },
        )

        assert connection.send_error.call_args_list == [
            call(40, "snapshot_failed", "MeshNet could not load the panel snapshot"),
            call(41, "messages_failed", "MeshNet could not load message history"),
            call(42, "send_failed", "MeshNet could not submit the message"),
        ]
        connection.send_message.assert_not_called()
        serialized = json.dumps(
            {
                "errors": connection.send_error.call_args_list,
                "telemetry": coordinator.panel_telemetry.snapshot(),
            },
            default=str,
        )
        assert private_error not in serialized
        assert "/config/private.sqlite" not in serialized
        assert "!12345678" not in serialized

    import asyncio

    asyncio.run(run())


def test_snapshot_marks_every_node_false_when_favorite_label_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An optional missing label must not hide nodes or access device state."""
    label_lookup = MagicMock(return_value=None)
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=label_lookup,
        ),
    )
    device_registry_get = MagicMock(
        side_effect=AssertionError("device registry must not be read")
    )
    monkeypatch.setattr(dr, "async_get", device_registry_get)
    coordinator = _coordinator()

    result = _snapshot_with_panel_metadata(object(), coordinator)

    assert label_lookup.call_args.args == (_FAVORITE_LABEL_NAME,)
    device_registry_get.assert_not_called()
    _assert_panel_metadata(result, favorite_configured=False)
    assert result["nodes"][FAVORITE_NODE]["favorite"] is False
    assert result["nodes"][OTHER_NODE]["favorite"] is False
    assert not hasattr(coordinator.snapshot.nodes[FAVORITE_NODE], "favorite")


def test_snapshot_reads_favorites_with_legacy_device_lookup_without_leaking_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA 2025.1 through 2026.7 use the legacy identifier lookup."""
    private_label_id = "private-ha-label-id"
    private_device_id = "private-ha-device-id"
    favorite_label = SimpleNamespace(
        label_id=private_label_id,
        name=_FAVORITE_LABEL_NAME,
    )
    label_lookup = MagicMock(return_value=favorite_label)
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=label_lookup,
        ),
    )

    lookup_calls: list[set[tuple[str, str]]] = []

    class LegacyDeviceRegistry:
        def async_get_device(self, *, identifiers):
            lookup_calls.append(identifiers)
            node_key = next(iter(identifiers))[1]
            if node_key != FAVORITE_NODE:
                return None
            return SimpleNamespace(
                id=private_device_id,
                labels={private_label_id},
            )

        def async_update_device(self, *_args, **_kwargs):
            raise AssertionError("favorite projection must remain read-only")

    monkeypatch.setattr(dr, "async_get", lambda _hass: LegacyDeviceRegistry())

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    _assert_panel_metadata(result, favorite_configured=True)
    assert result["nodes"][FAVORITE_NODE]["favorite"] is True
    assert result["nodes"][OTHER_NODE]["favorite"] is False
    assert lookup_calls == [
        {("meshnet", FAVORITE_NODE)},
        {("meshnet", OTHER_NODE)},
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert private_label_id not in serialized
    assert private_device_id not in serialized
    assert _FAVORITE_LABEL_NAME not in serialized


def test_snapshot_inherits_favorite_from_retained_identity_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projected node stays favorite when its older HA device was labeled."""
    favorite_label = SimpleNamespace(label_id="favorite-label")
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=MagicMock(return_value=favorite_label),
        ),
    )
    alias_key = "mac:aabbccddeeff"
    lookup_calls: list[set[tuple[str, str]]] = []

    class LegacyDeviceRegistry:
        def async_get_device(self, *, identifiers):
            lookup_calls.append(identifiers)
            node_key = next(iter(identifiers))[1]
            labels = {favorite_label.label_id} if node_key == alias_key else set()
            return SimpleNamespace(labels=labels)

    monkeypatch.setattr(dr, "async_get", lambda _hass: LegacyDeviceRegistry())
    coordinator = _coordinator()
    coordinator.node_alias_keys = lambda node_key: (
        (node_key, alias_key) if node_key == FAVORITE_NODE else (node_key,)
    )

    result = _snapshot_with_panel_metadata(object(), coordinator)

    assert result["nodes"][FAVORITE_NODE]["favorite"] is True
    assert result["nodes"][OTHER_NODE]["favorite"] is False
    assert lookup_calls == [
        {("meshnet", FAVORITE_NODE)},
        {("meshnet", alias_key)},
        {("meshnet", OTHER_NODE)},
    ]


def test_snapshot_prefers_config_entry_scoped_device_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use Home Assistant's scoped identifier API as soon as it is available."""
    favorite_label = SimpleNamespace(label_id="favorite-label")
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=MagicMock(return_value=favorite_label),
        ),
    )

    scoped_calls: list[tuple[tuple[str, str], str]] = []

    class ScopedDeviceRegistry:
        def async_get_device_by_identifier(self, identifier, config_entry_id):
            scoped_calls.append((identifier, config_entry_id))
            labels = (
                {favorite_label.label_id}
                if identifier == ("meshnet", FAVORITE_NODE)
                else set()
            )
            return SimpleNamespace(labels=labels)

        def async_get_device(self, **_kwargs):
            raise AssertionError("legacy lookup must not run when scoped API exists")

        def async_update_device(self, *_args, **_kwargs):
            raise AssertionError("favorite projection must remain read-only")

    monkeypatch.setattr(dr, "async_get", lambda _hass: ScopedDeviceRegistry())

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    assert result["nodes"][FAVORITE_NODE]["favorite"] is True
    assert result["nodes"][OTHER_NODE]["favorite"] is False
    assert scoped_calls == [
        (("meshnet", FAVORITE_NODE), ENTRY_ID),
        (("meshnet", OTHER_NODE), ENTRY_ID),
    ]


def test_snapshot_degrades_safely_when_optional_registry_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A favorites problem must never make the core mesh snapshot unavailable."""
    monkeypatch.setattr(
        lr,
        "async_get",
        MagicMock(side_effect=RuntimeError("private registry detail")),
    )

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    metadata = _assert_panel_metadata(result, favorite_configured=False)
    schema_stats = metadata["telemetry"]["operations"]["snapshot_schema"]
    assert schema_stats["failure_count"] == 1
    assert schema_stats["error_type_counts"] == {"RuntimeError": 1}
    assert "private registry detail" not in json.dumps(metadata)
    assert all(not node["favorite"] for node in result["nodes"].values())


def test_snapshot_retains_configured_flag_when_device_registry_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient device read failure must not misreport the label as absent."""
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_label_by_name=MagicMock(
                return_value=SimpleNamespace(label_id="favorite-label")
            ),
        ),
    )
    monkeypatch.setattr(
        dr,
        "async_get",
        MagicMock(side_effect=RuntimeError("private device registry detail")),
    )

    result = _snapshot_with_panel_metadata(object(), _coordinator())

    metadata = _assert_panel_metadata(result, favorite_configured=True)
    schema_stats = metadata["telemetry"]["operations"]["snapshot_schema"]
    assert schema_stats["failure_count"] == 1
    assert "private device registry detail" not in json.dumps(metadata)
    assert all(not node["favorite"] for node in result["nodes"].values())


def test_snapshot_includes_only_exact_bounded_node_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get_label_by_name=lambda _name: None),
    )
    coordinator = _coordinator()
    coordinator.panel_node_provenance = MagicMock(
        return_value={
            "total_node_count": 305,
            "retained_node_record_count": 473,
            "collapsed_alias_record_count": 168,
            "resolved_identity_group_count": 150,
            "unresolved_identity_group_count": 2,
            "unresolved_identity_node_count": 4,
            "invalid_identity_record_count": 3,
            "analyzed_node_count": 305,
            "omitted_node_count": 0,
            "current_session_node_count": 191,
            "cached_only_node_count": 114,
            "online_node_count": 24,
            "located_node_count": 175,
            "located_offline_node_count": 163,
            "mqtt_node_count": 42,
            "mqtt_unknown_node_count": 114,
            "identity_collision_group_count": 2,
            "identity_collision_node_count": 4,
            "private_node_name": "must not escape",
        }
    )

    result = _snapshot_with_panel_metadata(object(), coordinator)

    metadata = _assert_panel_metadata(result, favorite_configured=False)
    assert {
        key: metadata[key]
        for key in (
            "total_node_count",
            "retained_node_record_count",
            "collapsed_alias_record_count",
            "resolved_identity_group_count",
            "unresolved_identity_group_count",
            "unresolved_identity_node_count",
            "invalid_identity_record_count",
            "analyzed_node_count",
            "omitted_node_count",
            "current_session_node_count",
            "cached_only_node_count",
            "online_node_count",
            "located_node_count",
            "located_offline_node_count",
            "mqtt_node_count",
            "mqtt_unknown_node_count",
            "identity_collision_group_count",
            "identity_collision_node_count",
        )
    } == {
        "total_node_count": 305,
        "retained_node_record_count": 473,
        "collapsed_alias_record_count": 168,
        "resolved_identity_group_count": 150,
        "unresolved_identity_group_count": 2,
        "unresolved_identity_node_count": 4,
        "invalid_identity_record_count": 3,
        "analyzed_node_count": 305,
        "omitted_node_count": 0,
        "current_session_node_count": 191,
        "cached_only_node_count": 114,
        "online_node_count": 24,
        "located_node_count": 175,
        "located_offline_node_count": 163,
        "mqtt_node_count": 42,
        "mqtt_unknown_node_count": 114,
        "identity_collision_group_count": 2,
        "identity_collision_node_count": 4,
    }
    assert "private_node_name" not in metadata
    assert "must not escape" not in json.dumps(metadata)


def test_snapshot_rejects_invalid_provenance_without_losing_core_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get_label_by_name=lambda _name: None),
    )
    coordinator = _coordinator()
    coordinator.panel_node_provenance = MagicMock(
        return_value={key: -1 for key in (
            "total_node_count",
            "analyzed_node_count",
            "omitted_node_count",
            "current_session_node_count",
            "cached_only_node_count",
            "online_node_count",
            "located_node_count",
            "located_offline_node_count",
            "mqtt_node_count",
            "mqtt_unknown_node_count",
            "identity_collision_group_count",
            "identity_collision_node_count",
        )}
    )

    result = _snapshot_with_panel_metadata(object(), coordinator)

    metadata = _assert_panel_metadata(result, favorite_configured=False)
    assert "total_node_count" not in metadata
    stats = metadata["telemetry"]["operations"]["snapshot_schema"]
    assert stats["failure_count"] == 1
    assert stats["error_type_counts"] == {"SchemaError": 1}


def test_panel_snapshot_omits_raw_provider_state_and_unused_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The five-second panel poll must stay bounded and exclude raw SDK data."""
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get_label_by_name=lambda _name: None),
    )
    coordinator = _coordinator()
    node = coordinator.snapshot.nodes[FAVORITE_NODE]
    node.long_name = "Visible node"
    node.connectivity = {"snr": -4.5, "private_metric": "raw-node-secret"}
    node.power = {"battery_level": 99, "private_power": "raw-power-secret"}
    node.raw = {"endpoint": "https://private-node.local", "token": "raw-token"}
    coordinator.snapshot.gateways = {
        "private-gateway": GatewayStatus(
            gateway_id="private-gateway",
            name="Visible gateway",
            protocol="meshtastic",
            transport="bluetooth",
            errors=["raw-gateway-secret"],
            detail={"private": "raw-detail-secret"},
        )
    }
    coordinator.snapshot.recent_messages = [
        MessageRecord(
            message_id="visible-message-id",
            protocol="meshtastic",
            gateway_id="private-gateway",
            sender="visible-sender",
            receiver=None,
            channel="0",
            text="Visible message",
            raw={
                "status": "sent",
                "last_error": "raw-message-error-secret",
                "provider_id": "raw-provider-secret",
            },
        )
    ]

    result = _snapshot_with_panel_metadata(object(), coordinator)

    projected_node = result["nodes"][FAVORITE_NODE]
    assert projected_node["connectivity"] == {"snr": -4.5}
    assert "power" not in projected_node
    assert "raw" not in projected_node
    assert result["gateways"]["private-gateway"] == {
        "gateway_id": "private-gateway",
        "name": "Visible gateway",
        "protocol": "meshtastic",
        "transport": "bluetooth",
        "connected": False,
    }
    assert result["recent_messages"][0]["raw"] == {"status": "sent"}
    coordinator.snapshot.recent_messages[0].raw["status"] = ["sent"]
    assert _snapshot_with_panel_metadata(object(), coordinator)[
        "recent_messages"
    ][0]["raw"] == {}
    serialized = json.dumps(result, sort_keys=True)
    for secret in (
        "raw-node-secret",
        "raw-power-secret",
        "raw-token",
        "raw-gateway-secret",
        "raw-detail-secret",
        "raw-message-error-secret",
        "raw-provider-secret",
    ):
        assert secret not in serialized


def test_panel_snapshot_caps_recurring_projection_without_mutating_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A huge retained database must not create unbounded five-second work."""
    monkeypatch.setattr(websocket_api_module, "MAX_PANEL_NODES", 1)
    monkeypatch.setattr(websocket_api_module, "MAX_PANEL_GATEWAYS", 1)
    monkeypatch.setattr(
        lr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get_label_by_name=lambda _name: None),
    )
    coordinator = _coordinator()
    coordinator.snapshot.gateways = {
        f"gateway-{index}": GatewayStatus(
            gateway_id=f"gateway-{index}",
            name=f"Gateway {index}",
            protocol="meshtastic",
            transport="bluetooth",
        )
        for index in range(2)
    }
    coordinator.panel_node_provenance = MagicMock(
        return_value={
            "total_node_count": 2,
            "analyzed_node_count": 1,
            "omitted_node_count": 1,
            "current_session_node_count": 1,
            "cached_only_node_count": 0,
            "online_node_count": 0,
            "located_node_count": 0,
            "located_offline_node_count": 0,
            "mqtt_node_count": 0,
            "mqtt_unknown_node_count": 1,
            "identity_collision_group_count": 0,
            "identity_collision_node_count": 0,
        }
    )

    result = _snapshot_with_panel_metadata(object(), coordinator)

    assert list(result["nodes"]) == [FAVORITE_NODE]
    assert list(result["gateways"]) == ["gateway-0"]
    assert result["panel_metadata"]["omitted_node_count"] == 1
    assert len(coordinator.snapshot.nodes) == 2
    assert len(coordinator.snapshot.gateways) == 2


def test_instrumented_panel_operation_tracks_failure_then_recovery() -> None:
    async def run() -> None:
        coordinator = SimpleNamespace()
        private_error = "message from private-node at 41.1234,-87.5678"
        failing = AsyncMock(side_effect=RuntimeError(private_error))
        with pytest.raises(RuntimeError, match="private-node"):
            await _async_panel_operation(coordinator, "snapshot", failing)

        successful = AsyncMock(return_value="snapshot")
        assert (
            await _async_panel_operation(coordinator, "snapshot", successful)
            == "snapshot"
        )

        telemetry = coordinator.panel_telemetry.snapshot()
        stats = telemetry["operations"]["snapshot"]
        assert stats["request_count"] == 2
        assert stats["failure_count"] == 1
        assert stats["success_count"] == 1
        assert stats["recovery_count"] == 1
        assert stats["consecutive_failure_count"] == 0
        assert private_error not in json.dumps(telemetry)

    import asyncio

    asyncio.run(run())


def test_panel_log_schema_rejects_unknown_or_identifying_fields() -> None:
    schema = websocket_panel_log._ws_schema
    valid = schema(
        {
            "id": 1,
            "type": "meshnet/panel_log",
            "operation": "render",
            "category": "internal",
            "error_type": "TypeError",
            "error_code": "render_failed",
        }
    )
    assert valid["occurrence"] == 1
    assert valid["consecutive"] == 1

    invalid_reports = (
        {**valid, "message": "private message contents"},
        {**valid, "operation": "node_12345678"},
        {**valid, "category": "person@example.com"},
        {**valid, "error_type": "PrivateNodeError"},
        {**valid, "error_code": "private_location_41_1234"},
        {**valid, "occurrence": True},
        {**valid, "consecutive": 0},
        {**valid, "consecutive": 1_000_001},
    )
    for report in invalid_reports:
        with pytest.raises(vol.Invalid):
            schema(report)


def test_panel_reporting_failure_is_guarded_and_does_not_recurse() -> None:
    async def run() -> None:
        coordinator = _coordinator()
        hass = SimpleNamespace(data={"meshnet": {ENTRY_ID: coordinator}})
        connection = SimpleNamespace(
            send_result=MagicMock(
                side_effect=RuntimeError(
                    "private-node at https://private-host.local failed"
                )
            ),
            send_error=MagicMock(),
        )
        handler = websocket_panel_log.__wrapped__.__wrapped__
        await handler(
            hass,
            connection,
            {
                "id": 9,
                "type": "meshnet/panel_log",
                "operation": "global_error",
                "category": "internal",
                "error_type": "Error",
                "error_code": "unexpected_error",
                "occurrence": 1,
                "consecutive": 1,
            },
        )

        connection.send_error.assert_called_once_with(
            9,
            "reporting_failed",
            "MeshNet could not accept the panel failure report",
        )
        telemetry = coordinator.panel_telemetry.snapshot()
        assert telemetry["operations"]["global_error"]["failure_count"] == 1
        assert telemetry["operations"]["reporting"]["failure_count"] == 1
        serialized = json.dumps(telemetry)
        assert "private-node" not in serialized
        assert "private-host" not in serialized

    import asyncio

    asyncio.run(run())


def test_panel_reporting_cancellation_is_counted_and_propagated() -> None:
    async def run() -> None:
        import asyncio

        coordinator = _coordinator()
        hass = SimpleNamespace(data={"meshnet": {ENTRY_ID: coordinator}})
        connection = SimpleNamespace(
            send_result=MagicMock(side_effect=asyncio.CancelledError),
        )
        handler = websocket_panel_log.__wrapped__.__wrapped__

        with pytest.raises(asyncio.CancelledError):
            await handler(
                hass,
                connection,
                {
                    "id": 10,
                    "type": "meshnet/panel_log",
                    "operation": "render",
                    "category": "internal",
                    "error_type": "Error",
                    "error_code": "render_failed",
                    "occurrence": 1,
                    "consecutive": 1,
                },
            )

        telemetry = coordinator.panel_telemetry.snapshot()
        reporting = telemetry["operations"]["reporting"]
        assert reporting["request_count"] == 1
        assert reporting["failure_count"] == 1
        assert reporting["error_type_counts"] == {"CancelledError": 1}

    import asyncio

    asyncio.run(run())


def test_settings_preview_schema_defers_secret_validation_to_admin_handler() -> None:
    revision = "a" * 64
    preview = websocket_settings_preview._ws_schema(
        {
            "id": 20,
            "type": "meshnet/settings/preview",
            "gateway_id": "gateway-one",
            "revision": revision,
            "changes": {
                "identity.long_name": "New name",
                "network.password": {
                    "operation": "replace",
                    "value": "write-only value",
                },
            },
        }
    )
    assert preview["changes"]["identity.long_name"] == "New name"

    apply = websocket_settings_apply._ws_schema(
        {
            "id": 21,
            "type": "meshnet/settings/apply",
            "gateway_id": "gateway-one",
            "revision": revision,
            "preview_id": "p" * 43,
        }
    )
    assert apply["confirm_critical"] is False

    malformed = (
        {**preview, "revision": "short"},
        {**preview, "changes": {"__proto__.polluted": True}},
        {
            **preview,
            "changes": {
                "network.password": {"operation": "retry", "value": "secret"}
            },
        },
        {**preview, "raw_command": "factory_reset"},
    )
    for message in malformed:
        # Home Assistant's generic voluptuous error logger does not know that
        # nested `changes.<path>.value` is secret. The decorator accepts the
        # envelope so the authorized handler can reject it with fixed text and
        # without ever rendering the payload.
        assert websocket_settings_preview._ws_schema(message) == message


def test_settings_preview_handler_rejects_malformed_secret_payload_privately() -> None:
    async def run() -> None:
        private_value = "never-render-this-secret"
        coordinator = _coordinator()
        coordinator.async_gateway_settings_preview = AsyncMock()
        hass = SimpleNamespace(data={"meshnet": {ENTRY_ID: coordinator}})
        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())
        handler = websocket_settings_preview.__wrapped__.__wrapped__

        await handler(
            hass,
            connection,
            {
                "id": 22,
                "type": "meshnet/settings/preview",
                "gateway_id": "gateway-one",
                "revision": "a" * 64,
                "changes": {
                    "network.password": {
                        "operation": "retry",
                        "value": private_value,
                    }
                },
            },
        )

        coordinator.async_gateway_settings_preview.assert_not_awaited()
        connection.send_message.assert_not_called()
        connection.send_error.assert_called_once_with(
            22,
            "settings_invalid",
            "One or more settings changes are invalid",
        )
        serialized = json.dumps(
            {
                "errors": connection.send_error.call_args_list,
                "telemetry": coordinator.panel_telemetry.snapshot(),
            },
            default=str,
        )
        assert private_value not in serialized

    import asyncio

    asyncio.run(run())


def test_settings_websocket_handlers_require_admin_before_gateway_access() -> None:
    """The real HA decorator must reject non-admins before parsing or I/O."""
    coordinator = _coordinator()
    coordinator.async_gateway_settings_get = AsyncMock()
    coordinator.async_gateway_settings_preview = AsyncMock()
    coordinator.async_gateway_settings_apply = AsyncMock()
    hass = SimpleNamespace(data={"meshnet": {ENTRY_ID: coordinator}})
    connection = SimpleNamespace(
        user=SimpleNamespace(is_admin=False),
        send_result=MagicMock(),
        send_error=MagicMock(),
    )
    messages = (
        (
            websocket_settings_get,
            {"id": 23, "type": "meshnet/settings/get"},
        ),
        (
            websocket_settings_preview,
            {
                "id": 24,
                "type": "meshnet/settings/preview",
                "gateway_id": "gateway-one",
                "revision": "a" * 64,
                "changes": {
                    "security.pin": {
                        "operation": "retry",
                        "value": "must-never-reach-the-handler",
                    }
                },
            },
        ),
        (
            websocket_settings_apply,
            {
                "id": 25,
                "type": "meshnet/settings/apply",
                "gateway_id": "gateway-one",
                "revision": "a" * 64,
                "preview_id": "p" * 43,
                "confirm_critical": True,
            },
        ),
    )

    for handler, message in messages:
        with pytest.raises(Unauthorized):
            handler(hass, connection, message)

    coordinator.async_gateway_settings_get.assert_not_awaited()
    coordinator.async_gateway_settings_preview.assert_not_awaited()
    coordinator.async_gateway_settings_apply.assert_not_awaited()
    connection.send_result.assert_not_called()
    connection.send_error.assert_not_called()


def test_settings_websocket_handlers_delegate_without_exposing_provider_errors() -> None:
    async def run() -> None:
        from custom_components.meshnet.gateway_settings import (
            GatewaySettingsUnavailable,
        )

        coordinator = _coordinator()
        coordinator.async_gateway_settings_get = AsyncMock(
            return_value={"gateways": [], "selected": {}}
        )
        coordinator.async_gateway_settings_preview = AsyncMock(
            return_value={"preview_id": "safe-preview"}
        )
        coordinator.async_gateway_settings_apply = AsyncMock(
            side_effect=GatewaySettingsUnavailable(
                "private password at 41.1234,-87.5678"
            )
        )
        hass = SimpleNamespace(data={"meshnet": {ENTRY_ID: coordinator}})
        connection = SimpleNamespace(send_message=MagicMock(), send_error=MagicMock())

        get_handler = websocket_settings_get.__wrapped__.__wrapped__
        await get_handler(
            hass,
            connection,
            {"id": 30, "type": "meshnet/settings/get", "gateway_id": "gateway-one"},
        )
        coordinator.async_gateway_settings_get.assert_awaited_once_with("gateway-one")
        get_envelope = connection.send_message.call_args.args[0]
        assert get_envelope["id"] == 30
        assert get_envelope["result"] == {"gateways": [], "selected": {}}

        preview_handler = websocket_settings_preview.__wrapped__.__wrapped__
        await preview_handler(
            hass,
            connection,
            {
                "id": 31,
                "type": "meshnet/settings/preview",
                "gateway_id": "gateway-one",
                "revision": "a" * 64,
                "changes": {"identity.long_name": "New name"},
            },
        )
        coordinator.async_gateway_settings_preview.assert_awaited_once_with(
            gateway_id="gateway-one",
            revision="a" * 64,
            changes={"identity.long_name": "New name"},
        )
        preview_envelope = connection.send_message.call_args.args[0]
        assert preview_envelope["id"] == 31
        assert preview_envelope["result"] == {"preview_id": "safe-preview"}

        apply_handler = websocket_settings_apply.__wrapped__.__wrapped__
        await apply_handler(
            hass,
            connection,
            {
                "id": 32,
                "type": "meshnet/settings/apply",
                "gateway_id": "gateway-one",
                "revision": "a" * 64,
                "preview_id": "p" * 43,
                "confirm_critical": True,
            },
        )
        connection.send_error.assert_called_once_with(
            32,
            "settings_unavailable",
            "Live settings are unavailable for this gateway",
        )
        serialized = json.dumps(
            {
                "result_calls": connection.send_message.call_args_list,
                "error_calls": connection.send_error.call_args_list,
                "telemetry": coordinator.panel_telemetry.snapshot(),
            },
            default=str,
        )
        assert "private password" not in serialized
        assert "41.1234" not in serialized

    import asyncio

    asyncio.run(run())
