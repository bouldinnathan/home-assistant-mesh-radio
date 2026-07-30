"""Redact sensitive MeshNet websocket traffic before HA debug logging.

Home Assistant's websocket server logs the decoded command before dispatching
it when websocket debug logging is enabled.  Its generic redactor cannot know
that ``changes.<setting path>.value`` is a write-only radio secret or that a
MeshNet send command contains private message and identity data, so MeshNet
installs this narrow LogRecord filter before registering its panel API.

Home Assistant also logs serialized outbound websocket result bytes. Sensitive
MeshNet result messages carry a fixed server-added sentinel. The filter detects
that literal without decoding or traversing arbitrary response JSON, then
replaces the complete log record before logging can format the private payload.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

_SETTINGS_PREVIEW_TYPE = "meshnet/settings/preview"
_REMOTE_SETTINGS_GET_TYPE = "meshnet/remote_settings/get"
_REMOTE_SETTINGS_PREVIEW_TYPE = "meshnet/remote_settings/preview"
_REMOTE_SETTINGS_APPLY_TYPE = "meshnet/remote_settings/apply"
_TRACEROUTE_TYPE = "meshnet/traceroute"
_SEND_MESSAGE_TYPE = "meshnet/send_message"
_CALL_SERVICE_TYPE = "call_service"
_MESHNET_DOMAIN = "meshnet"
_SENSITIVE_MESHNET_SERVICES = frozenset(
    {
        "send_message",
        "broadcast_message",
        "schedule_message",
        "refresh_gateway",
    }
)
_SENSITIVE_SERVICE_FIELDS = frozenset({"message", "target_node", "gateway_id", "channel"})
_SENSITIVE_COMMAND_FIELDS = {
    _SETTINGS_PREVIEW_TYPE: frozenset({"changes"}),
    _REMOTE_SETTINGS_GET_TYPE: frozenset({"gateway_id", "target_node"}),
    _REMOTE_SETTINGS_PREVIEW_TYPE: frozenset({"gateway_id", "target_node", "changes"}),
    _REMOTE_SETTINGS_APPLY_TYPE: frozenset({"gateway_id", "target_node"}),
    _TRACEROUTE_TYPE: frozenset({"gateway_id", "target_node"}),
    _SEND_MESSAGE_TYPE: frozenset({"message", "target_node", "gateway_id", "channel"}),
}
_REDACTED = "<redacted by MeshNet>"
_REDACTION_GUARD_MESSAGE = "WebSocket command omitted by MeshNet redaction guard"
_OUTBOUND_REDACTION_GUARD_MESSAGE = "WebSocket result omitted by MeshNet privacy guard"
# This marker is protocol metadata added only by ``sensitive_result_message``.
# It is not accepted from any MeshNet websocket command or copied from a result.
_OUTBOUND_SENTINEL_KEY = "__meshnet_private_response__"
_OUTBOUND_SENTINEL_VALUE = "meshnet-server-redact-v1"
_OUTBOUND_SENTINEL_TEXT = f'"{_OUTBOUND_SENTINEL_KEY}":"{_OUTBOUND_SENTINEL_VALUE}"'
_OUTBOUND_SENTINEL_BYTES = _OUTBOUND_SENTINEL_TEXT.encode("ascii")
_OUTBOUND_LOG_MARKER = "Sending %s"
_CORE_SERVICE_LOG_MESSAGES = frozenset(
    {
        "Invalid data for service call %s.%s: %s",
        "Error executing service: %s",
        "Service was cancelled: %s",
    }
)
_CORE_SERVICE_REDACTION_GUARD_MESSAGE = "Private MeshNet service call payload omitted by privacy guard"
_MAX_SEQUENCE_DEPTH = 4
_MAX_INSPECTED_ITEMS = 512
_MAX_SENSITIVE_COMMAND_FIELDS = 64
_WEBSOCKET_LOGGER_NAMES = (
    # Current and minimum supported Home Assistant releases use the first
    # name.  The additional exact namespaces keep the boundary fail-safe if
    # the adapter logger moves within websocket_api.
    "homeassistant.components.websocket_api.http.connection",
    "homeassistant.components.websocket_api.connection",
    "homeassistant.components.websocket_api.http",
)
_CORE_LOGGER_NAMES = ("homeassistant.core",)


class _RedactionTraversalLimit(Exception):
    """Signal that an unusual log argument must be replaced in full."""


def _omit_record(record: logging.LogRecord, message: str) -> None:
    """Replace a record completely, including a possibly sensitive traceback."""
    record.msg = message
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None


def _redact_meshnet_service_command(value: Mapping[Any, Any]) -> tuple[Any, bool]:
    """Redact one exact HA call_service envelope for a private MeshNet send."""
    if (
        value.get("type") != _CALL_SERVICE_TYPE
        or value.get("domain") != _MESHNET_DOMAIN
        or value.get("service") not in _SENSITIVE_MESHNET_SERVICES
    ):
        return value, False
    if len(value) > _MAX_SENSITIVE_COMMAND_FIELDS:
        raise _RedactionTraversalLimit
    service_data = value.get("service_data")
    if not isinstance(service_data, Mapping) or isinstance(service_data, (str, bytes)):
        # This is malformed, but it still belongs to an exact private send
        # service. Omitting the record is safer than rendering its payload.
        raise _RedactionTraversalLimit
    if len(service_data) > _MAX_SENSITIVE_COMMAND_FIELDS:
        raise _RedactionTraversalLimit
    safe_data = dict(service_data)
    for field in _SENSITIVE_SERVICE_FIELDS:
        if field in safe_data:
            safe_data[field] = _REDACTED
    redacted = dict(value)
    redacted["service_data"] = safe_data
    return redacted, True


def _is_sensitive_meshnet_service_call(value: Any) -> bool:
    """Recognize HA's ServiceCall object without importing Home Assistant."""
    return (
        getattr(value, "domain", None) == _MESHNET_DOMAIN
        and getattr(value, "service", None) in _SENSITIVE_MESHNET_SERVICES
    )


def _redact_core_service_record(record: logging.LogRecord) -> bool:
    """Omit service payloads that HA core otherwise renders on failures."""
    if (
        record.name not in _CORE_LOGGER_NAMES
        or not isinstance(record.msg, str)
        or record.msg not in _CORE_SERVICE_LOG_MESSAGES
        or not isinstance(record.args, tuple)
    ):
        return False
    if (
        record.msg == "Invalid data for service call %s.%s: %s"
        and len(record.args) == 3
        and record.args[0] == _MESHNET_DOMAIN
        and record.args[1] in _SENSITIVE_MESHNET_SERVICES
    ) or (
        record.msg in {"Error executing service: %s", "Service was cancelled: %s"}
        and len(record.args) == 1
        and _is_sensitive_meshnet_service_call(record.args[0])
    ):
        _omit_record(record, _CORE_SERVICE_REDACTION_GUARD_MESSAGE)
        return True
    return False


def _redact_command_bounded(
    value: Any,
    *,
    depth: int,
    remaining_items: list[int],
) -> tuple[Any, bool]:
    """Inspect only HA's bounded command/batch containers.

    A decoded websocket command is a root mapping. A batch is a root list of
    command mappings, normally reached through ``LogRecord.args``. Mapping
    values are deliberately never traversed: doing so would put arbitrary
    client JSON on the shared Home Assistant logging call stack.
    """
    if isinstance(value, Mapping):
        service_command, service_changed = _redact_meshnet_service_command(value)
        if service_changed:
            return service_command, True
        sensitive_fields = _SENSITIVE_COMMAND_FIELDS.get(value.get("type"))
        if sensitive_fields is not None:
            # Real MeshNet commands have fewer than ten top-level fields. Do
            # not duplicate an attacker-sized decoded mapping on Home
            # Assistant's shared logging path merely to redact four values.
            if len(value) > _MAX_SENSITIVE_COMMAND_FIELDS:
                raise _RedactionTraversalLimit
            redacted = dict(value)
            for field in sensitive_fields:
                if field in redacted:
                    redacted[field] = _REDACTED
            return redacted, True
        return value, False
    if isinstance(value, (tuple, list)):
        if depth >= _MAX_SEQUENCE_DEPTH or len(value) > remaining_items[0]:
            raise _RedactionTraversalLimit
        remaining_items[0] -= len(value)
        items = []
        changed = False
        for item in value:
            safe_item, item_changed = _redact_command_bounded(
                item,
                depth=depth + 1,
                remaining_items=remaining_items,
            )
            items.append(safe_item)
            changed = changed or item_changed
        if not changed:
            return value, False
        return (tuple(items) if isinstance(value, tuple) else items), True
    return value, False


def _redact_command(value: Any) -> tuple[Any, bool]:
    """Return a detached structure only for a sensitive MeshNet command."""
    return _redact_command_bounded(
        value,
        depth=0,
        remaining_items=[_MAX_INSPECTED_ITEMS],
    )


def _contains_outbound_sentinel_bounded(
    value: Any,
    *,
    depth: int,
    remaining_items: list[int],
) -> bool:
    """Find only MeshNet's fixed outbound marker without parsing payload JSON."""
    if isinstance(value, bytes):
        return _OUTBOUND_SENTINEL_BYTES in value
    if isinstance(value, str):
        return _OUTBOUND_SENTINEL_TEXT in value
    if isinstance(value, Mapping):
        return value.get(_OUTBOUND_SENTINEL_KEY) == _OUTBOUND_SENTINEL_VALUE
    if isinstance(value, (tuple, list)):
        if depth >= _MAX_SEQUENCE_DEPTH or len(value) > remaining_items[0]:
            raise _RedactionTraversalLimit
        remaining_items[0] -= len(value)
        return any(
            _contains_outbound_sentinel_bounded(
                item,
                depth=depth + 1,
                remaining_items=remaining_items,
            )
            for item in value
        )
    return False


def _contains_outbound_sentinel(value: Any) -> bool:
    """Return whether one bounded HA log argument contains our result marker."""
    return _contains_outbound_sentinel_bounded(
        value,
        depth=0,
        remaining_items=[_MAX_INSPECTED_ITEMS],
    )


def sensitive_result_message(message_id: int, result: Any) -> dict[str, Any]:
    """Build one HA-compatible result tagged for pre-log omission.

    The caller passes this mapping to ``connection.send_message`` instead of
    ``connection.send_result``. Home Assistant serializes and sends the normal
    ``id/type/success/result`` shape; clients ignore the additional top-level
    marker. The result itself is retained by reference and is never modified.
    """
    return {
        "id": message_id,
        "type": "result",
        "success": True,
        "result": result,
        _OUTBOUND_SENTINEL_KEY: _OUTBOUND_SENTINEL_VALUE,
    }


def send_sensitive_result(connection: Any, message_id: int, result: Any) -> None:
    """Send an HA-compatible result whose debug log is omitted in full."""
    connection.send_message(sensitive_result_message(message_id, result))


class _MeshNetWebSocketRedactionFilter(logging.Filter):
    """Remove secrets, message content, and identifiers from matching records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if _redact_core_service_record(record):
                return True
            # This filter is also installed on HA core solely for the three
            # exact service records above. Never traverse or rewrite unrelated
            # core logs.
            if record.name in _CORE_LOGGER_NAMES:
                return True
            if (
                isinstance(record.msg, str)
                and _OUTBOUND_LOG_MARKER in record.msg
                and _contains_outbound_sentinel(record.args)
            ):
                _omit_record(record, _OUTBOUND_REDACTION_GUARD_MESSAGE)
                return True
            safe_args, changed = _redact_command(record.args)
            if changed:
                record.args = safe_args
        except Exception:
            # A logging filter must never be able to interrupt Home Assistant.
            # If a nonstandard container exceeds the narrow HA command shape
            # (or raises while being inspected), replace the complete record
            # with fixed text instead of risking either a leak or an exception.
            _omit_record(record, _REDACTION_GUARD_MESSAGE)
        return True


_FILTER = _MeshNetWebSocketRedactionFilter()


def install_websocket_secret_redaction() -> None:
    """Install the idempotent, process-local filter on HA websocket loggers."""
    for name in _WEBSOCKET_LOGGER_NAMES:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
    for name in _CORE_LOGGER_NAMES:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)


__all__ = [
    "install_websocket_secret_redaction",
    "send_sensitive_result",
    "sensitive_result_message",
]
