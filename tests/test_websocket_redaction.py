"""Tests for pre-dispatch websocket secret redaction."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from custom_components.meshnet.websocket_redaction import (
    _MeshNetWebSocketRedactionFilter,
    install_websocket_secret_redaction,
    sensitive_result_message,
)


def _preview(secret: str) -> dict:
    return {
        "id": 7,
        "type": "meshnet/settings/preview",
        "gateway_id": "gateway-one",
        "revision": "a" * 64,
        "changes": {
            "security.pin": {"operation": "replace", "value": secret}
        },
    }


def _send_command(message: str) -> dict:
    return {
        "id": 9,
        "type": "meshnet/send_message",
        "message": message,
        "target_node": "!12345678",
        "gateway_id": "private-gateway",
        "channel": "private-channel",
        "priority": "normal",
        "message_type": "direct",
    }


def _service_command(message: str) -> dict:
    return {
        "id": 10,
        "type": "call_service",
        "domain": "meshnet",
        "service": "send_message",
        "service_data": {
            "message": message,
            "target_node": "!12345678",
            "gateway_id": "private-gateway",
            "channel": "private-channel",
            "priority": "normal",
        },
    }


def test_filter_redacts_single_and_batched_preview_without_mutating_command() -> None:
    secret = "654321"
    command = _preview(secret)
    unrelated = {"id": 8, "type": "meshnet/snapshot"}
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "%s: Received %s",
        ("connection", [unrelated, command]),
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    rendered = record.getMessage()
    assert secret not in rendered
    assert "<redacted by MeshNet>" in rendered
    assert command["changes"]["security.pin"]["value"] == secret
    assert unrelated in record.args[1]


def test_filter_leaves_unrelated_records_unchanged() -> None:
    args = ("connection", {"id": 8, "type": "meshnet/snapshot"})
    record = logging.LogRecord("test", logging.DEBUG, __file__, 1, "%s %s", args, None)

    _MeshNetWebSocketRedactionFilter().filter(record)

    assert record.args is args


def test_filter_redacts_send_message_and_identifiers_without_mutation() -> None:
    command = _send_command("private household message")
    original = dict(command)
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "%s: Received %s",
        ("connection", command),
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    rendered = record.getMessage()
    for private_value in (
        "private household message",
        "!12345678",
        "private-gateway",
        "private-channel",
    ):
        assert private_value not in rendered
    assert rendered.count("<redacted by MeshNet>") == 4
    assert command == original


def test_filter_redacts_send_message_inside_batch() -> None:
    command = _send_command("batched private message")
    record = logging.LogRecord(
        "test",
        logging.DEBUG,
        __file__,
        1,
        "%s",
        ([{"id": 1, "type": "meshnet/snapshot"}, command],),
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    assert "batched private message" not in record.getMessage()
    assert command["message"] == "batched private message"


def test_filter_redacts_meshnet_call_service_payload_without_mutation() -> None:
    command = _service_command("private service message")
    original_data = dict(command["service_data"])
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "%s: Received %s",
        ("connection", command),
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    rendered = record.getMessage()
    for private_value in (
        "private service message",
        "!12345678",
        "private-gateway",
        "private-channel",
    ):
        assert private_value not in rendered
    assert command["service_data"] == original_data


def test_filter_leaves_other_call_service_payload_unchanged() -> None:
    command = _service_command("not a MeshNet service")
    command["domain"] = "other"
    args = ("connection", command)
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "%s: Received %s",
        args,
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True
    assert record.args is args


def test_filter_redacts_meshnet_refresh_gateway_identity() -> None:
    command = {
        "id": 11,
        "type": "call_service",
        "domain": "meshnet",
        "service": "refresh_gateway",
        "service_data": {"gateway_id": "private-gateway"},
    }
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "%s: Received %s",
        ("connection", command),
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True
    assert "private-gateway" not in record.getMessage()


def test_sensitive_result_is_ha_compatible_and_does_not_mutate_payload() -> None:
    result = {
        "nodes": {"private-node": {"latitude": 12.3, "longitude": 45.6}},
        "recent_messages": [{"text": "private result message"}],
    }

    tagged = sensitive_result_message(27, result)

    assert tagged["id"] == 27
    assert tagged["type"] == "result"
    assert tagged["success"] is True
    assert tagged["result"] is result
    assert result["recent_messages"][0]["text"] == "private result message"
    marker_fields = set(tagged) - {"id", "type", "success", "result"}
    assert len(marker_fields) == 1


def test_filter_omits_tagged_outbound_mapping_in_full() -> None:
    tagged = sensitive_result_message(
        27,
        {"node_id": "!12345678", "text": "private outbound mapping"},
    )
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "[abc123] %s: Sending %s",
        ("Admin from private-host", tagged),
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    assert record.args == ()
    assert record.getMessage() == "WebSocket result omitted by MeshNet privacy guard"


def test_filter_omits_serialized_outbound_bytes_string_and_batch() -> None:
    tagged = sensitive_result_message(
        28,
        {"target_node": "!abcdef12", "text": "private serialized result"},
    )
    serialized = json.dumps(tagged, separators=(",", ":"))
    normal = json.dumps(
        {"id": 1, "type": "result", "success": True, "result": "public"},
        separators=(",", ":"),
    )
    values = (
        serialized,
        serialized.encode(),
        f"[{normal},{serialized}]".encode(),
    )

    for value in values:
        record = logging.LogRecord(
            "homeassistant.components.websocket_api.http.connection",
            logging.DEBUG,
            __file__,
            1,
            "%s: Sending %s",
            ("Admin from private-host", value),
            None,
        )

        assert _MeshNetWebSocketRedactionFilter().filter(record) is True
        assert "private serialized result" not in record.getMessage()
        assert record.getMessage() == (
            "WebSocket result omitted by MeshNet privacy guard"
        )


def test_outbound_marker_cannot_turn_an_incoming_command_into_outbound_log() -> None:
    tagged = sensitive_result_message(29, {"text": "private"})
    marker_field = next(
        key for key in tagged if key not in {"id", "type", "success", "result"}
    )
    incoming = {
        "id": 30,
        "type": "unrelated/integration",
        marker_field: tagged[marker_field],
    }
    args = ("Admin from private-host", incoming)
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "%s: Received %s",
        args,
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    assert record.args is args


def test_filter_leaves_untagged_outbound_result_unchanged() -> None:
    outbound = b'{"id":1,"type":"result","success":true,"result":"public"}'
    args = ("connection", outbound)
    record = logging.LogRecord(
        "homeassistant.components.websocket_api.http.connection",
        logging.DEBUG,
        __file__,
        1,
        "%s: Sending %s",
        args,
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    assert record.args is args


def test_filter_does_not_descend_into_unrelated_command_values() -> None:
    """Arbitrary decoded JSON must not recurse on HA's shared logger path."""
    nested: dict = {"leaf": True}
    for _index in range(2_000):
        nested = {"nested": nested}
    args = (
        "connection",
        {"id": 8, "type": "unrelated/integration", "payload": nested},
    )
    record = logging.LogRecord("test", logging.DEBUG, __file__, 1, "%s %s", args, None)

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    assert record.args is args


def test_filter_fails_closed_for_cyclic_or_oversized_sequence() -> None:
    """Unusual containers become fixed text without breaking logging."""
    cyclic: list = []
    cyclic.append(cyclic)
    record = logging.LogRecord(
        "test", logging.DEBUG, __file__, 1, "%s", (cyclic,), None
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    assert record.args == ()
    assert record.getMessage() == "WebSocket command omitted by MeshNet redaction guard"


def test_filter_fails_closed_without_copying_oversized_sensitive_mapping() -> None:
    """An admin command cannot make the shared logger clone a huge mapping."""
    command = _send_command("private message")
    command.update({f"unused_{index}": index for index in range(65)})
    record = logging.LogRecord(
        "test", logging.DEBUG, __file__, 1, "%s", (command,), None
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True

    assert record.args == ()
    assert record.getMessage() == "WebSocket command omitted by MeshNet redaction guard"


def test_filter_omits_ha_core_invalid_private_service_payload() -> None:
    private_data = _service_command("invalid private service message")[
        "service_data"
    ]
    record = logging.LogRecord(
        "homeassistant.core",
        logging.DEBUG,
        __file__,
        1,
        "Invalid data for service call %s.%s: %s",
        ("meshnet", "send_message", private_data),
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True
    assert "invalid private service message" not in record.getMessage()
    assert record.args == ()


def test_filter_omits_ha_core_private_service_exception_and_traceback() -> None:
    private_call = SimpleNamespace(
        domain="meshnet",
        service="schedule_message",
        data={"message": "private failed service message"},
    )
    private_error = RuntimeError("private provider exception")
    exception_info = (RuntimeError, private_error, None)
    record = logging.LogRecord(
        "homeassistant.core",
        logging.ERROR,
        __file__,
        1,
        "Error executing service: %s",
        (private_call,),
        exception_info,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True
    assert record.args == ()
    assert record.exc_info is None
    assert "private provider exception" not in record.getMessage()
    assert "private failed service message" not in record.getMessage()


def test_filter_leaves_other_ha_core_service_failure_unchanged() -> None:
    other_call = SimpleNamespace(
        domain="other", service="send_message", data={"message": "public"}
    )
    args = (other_call,)
    record = logging.LogRecord(
        "homeassistant.core",
        logging.ERROR,
        __file__,
        1,
        "Error executing service: %s",
        args,
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True
    assert record.args is args


def test_filter_never_traverses_unrelated_ha_core_log_arguments() -> None:
    oversized = tuple(range(1_000))
    args = (oversized,)
    record = logging.LogRecord(
        "homeassistant.core",
        logging.DEBUG,
        __file__,
        1,
        "Unrelated core data: %s",
        args,
        None,
    )

    assert _MeshNetWebSocketRedactionFilter().filter(record) is True
    assert record.args is args


def test_install_is_idempotent_and_real_logger_never_emits_secret(caplog) -> None:
    logger = logging.getLogger(
        "homeassistant.components.websocket_api.http.connection"
    )
    install_websocket_secret_redaction()
    install_websocket_secret_redaction()
    matching_filters = [
        item for item in logger.filters
        if isinstance(item, _MeshNetWebSocketRedactionFilter)
    ]
    assert len(matching_filters) == 1

    secret = "never-log-this-pin"
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.debug("%s: Received %s", "connection", _preview(secret))

    assert secret not in caplog.text
    assert "<redacted by MeshNet>" in caplog.text
