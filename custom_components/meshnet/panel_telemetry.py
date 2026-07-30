"""Privacy-safe, bounded telemetry for the MeshNet sidebar panel."""

from __future__ import annotations

import logging
import math
from collections import Counter, deque
from datetime import UTC, datetime
from typing import Any

PANEL_OPERATIONS = frozenset(
    {
        "snapshot",
        "messages",
        "send_message",
        "settings_get",
        "settings_preview",
        "settings_apply",
        "remote_settings_get",
        "remote_settings_preview",
        "remote_settings_apply",
        "traceroute",
        "render",
        "poll",
        "snapshot_schema",
        "snapshot_timeout",
        "post_send_refresh",
        "event_handler",
        "global_error",
        "unhandled_rejection",
        "invalid_recipient",
        "reporting",
    }
)

PANEL_ERROR_CATEGORIES = frozenset(
    {
        "authentication",
        "availability",
        "connection",
        "data",
        "internal",
        "lifecycle",
        "network",
        "permission",
        "timeout",
        "unknown",
        "validation",
    }
)

PANEL_ERROR_TYPES = frozenset(
    {
        "AbortError",
        "CancelledError",
        "ConnectionError",
        "DOMException",
        "Error",
        "HomeAssistantError",
        "InvalidAuth",
        "NetworkError",
        "NotFoundError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "SchemaError",
        "ServiceValidationError",
        "SyntaxError",
        "TimeoutError",
        "TypeError",
        "Unauthorized",
        "ValueError",
        "WebSocketError",
        "other_error",
        "unknown_error",
    }
)

PANEL_ERROR_CODES = frozenset(
    {
        "callback_failed",
        "connection_failed",
        "favorite_device_lookup_failed",
        "favorite_registry_failed",
        "handler_failed",
        "invalid_recipient",
        "invalid_response",
        "invalid_schema",
        "message_load_failed",
        "operation_cancelled",
        "operation_failed",
        "poll_failed",
        "post_send_refresh_failed",
        "provenance_failed",
        "render_failed",
        "report_failed",
        "send_failed",
        "snapshot_failed",
        "settings_load_failed",
        "settings_preview_failed",
        "settings_apply_failed",
        "remote_settings_load_failed",
        "remote_settings_preview_failed",
        "remote_settings_apply_failed",
        "traceroute_failed",
        "timeout",
        "unavailable",
        "unexpected_error",
        "websocket_failed",
    }
)

_DURATION_BUCKETS = (
    "under_10ms",
    "under_50ms",
    "under_250ms",
    "under_1s",
    "under_5s",
    "5s_or_more",
    "unknown",
)
_EVENT_LIMIT = 100


def _safe_choice(value: Any, allowed: frozenset[str], fallback: str) -> str:
    """Return only a fixed, non-identifying vocabulary item."""
    return value if isinstance(value, str) and value in allowed else fallback


def safe_panel_operation(value: Any) -> str:
    """Normalize an operation without retaining caller-controlled text."""
    return _safe_choice(value, PANEL_OPERATIONS, "reporting")


def safe_error_category(value: Any) -> str:
    """Normalize an error category without retaining caller-controlled text."""
    return _safe_choice(value, PANEL_ERROR_CATEGORIES, "unknown")


def safe_error_type(value: Any) -> str:
    """Normalize an exception type without retaining caller-controlled text."""
    return _safe_choice(value, PANEL_ERROR_TYPES, "other_error")


def safe_error_code(value: Any) -> str:
    """Normalize an error code without retaining caller-controlled text."""
    return _safe_choice(value, PANEL_ERROR_CODES, "unexpected_error")


def classify_exception(error: BaseException) -> tuple[str, str]:
    """Classify an exception using its type only, never its message."""
    error_type = safe_error_type(type(error).__name__)
    if isinstance(error, TimeoutError):
        category = "timeout"
    elif isinstance(error, PermissionError):
        category = "permission"
    elif isinstance(error, (ConnectionError, OSError)):
        category = "connection"
    elif isinstance(error, (TypeError, ValueError)):
        category = "validation"
    else:
        category = "internal"
    return category, error_type


def _duration_bucket(duration_seconds: Any) -> str:
    """Return a bounded latency bucket without retaining exact timing."""
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
        or duration_seconds < 0
    ):
        return "unknown"
    milliseconds = float(duration_seconds) * 1000
    if milliseconds < 10:
        return "under_10ms"
    if milliseconds < 50:
        return "under_50ms"
    if milliseconds < 250:
        return "under_250ms"
    if milliseconds < 1000:
        return "under_1s"
    if milliseconds < 5000:
        return "under_5s"
    return "5s_or_more"


def _new_operation_stats() -> dict[str, Any]:
    return {
        "request_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "recovery_count": 0,
        "consecutive_failure_count": 0,
        "error_category_counts": Counter(),
        "error_type_counts": Counter(),
        "duration_bucket_counts": Counter(),
    }


class PanelTelemetry:
    """Collect complete counters and a bounded identity-free failure history."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        event_limit: int = _EVENT_LIMIT,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._operations = {operation: _new_operation_stats() for operation in sorted(PANEL_OPERATIONS)}
        self._failure_events: deque[dict[str, Any]] = deque(maxlen=max(1, min(int(event_limit), _EVENT_LIMIT)))
        self._sequence = 0
        self._failure_signatures: Counter[tuple[str, str, str, str]] = Counter()

    def record_request(self, operation: Any) -> None:
        """Count one operation request."""
        operation = safe_panel_operation(operation)
        self._operations[operation]["request_count"] += 1

    def record_success(self, operation: Any, *, duration_seconds: Any = None) -> None:
        """Count success and recovery after any consecutive failures."""
        operation = safe_panel_operation(operation)
        stats = self._operations[operation]
        stats["success_count"] += 1
        stats["duration_bucket_counts"][_duration_bucket(duration_seconds)] += 1
        consecutive = stats["consecutive_failure_count"]
        if consecutive:
            stats["recovery_count"] += 1
            stats["consecutive_failure_count"] = 0
            self._logger.debug(
                "MeshNet panel operation recovered operation=%s previous_consecutive=%d",
                operation,
                consecutive,
            )

    def record_failure(
        self,
        operation: Any,
        *,
        category: Any,
        error_type: Any,
        error_code: Any,
        duration_seconds: Any = None,
        occurrence: Any = None,
        consecutive: Any = None,
    ) -> None:
        """Count and retain one failure using only fixed safe classifications."""
        operation = safe_panel_operation(operation)
        category = safe_error_category(category)
        error_type = safe_error_type(error_type)
        error_code = safe_error_code(error_code)
        duration = _duration_bucket(duration_seconds)
        stats = self._operations[operation]
        stats["failure_count"] += 1
        stats["consecutive_failure_count"] += 1
        stats["error_category_counts"][category] += 1
        stats["error_type_counts"][error_type] += 1
        stats["duration_bucket_counts"][duration] += 1

        signature = (operation, category, error_type, error_code)
        self._failure_signatures[signature] += 1
        signature_occurrence = self._failure_signatures[signature]
        self._sequence += 1
        event: dict[str, Any] = {
            "sequence": self._sequence,
            "recorded_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "category": category,
            "error_type": error_type,
            "error_code": error_code,
            "occurrence": signature_occurrence,
            "consecutive": stats["consecutive_failure_count"],
            "duration_bucket": duration,
        }
        reported_occurrence = _bounded_report_count(occurrence)
        reported_consecutive = _bounded_report_count(consecutive)
        if reported_occurrence is not None:
            event["reported_occurrence"] = reported_occurrence
        if reported_consecutive is not None:
            event["reported_consecutive"] = reported_consecutive
        self._failure_events.append(event)

        self._logger.debug(
            "MeshNet panel failure operation=%s category=%s error_type=%s "
            "error_code=%s occurrence=%d consecutive=%d duration_bucket=%s",
            operation,
            category,
            error_type,
            error_code,
            signature_occurrence,
            stats["consecutive_failure_count"],
            duration,
        )
        if _should_warn(signature_occurrence):
            self._logger.warning(
                "MeshNet panel failure operation=%s category=%s error_type=%s "
                "error_code=%s occurrence=%d consecutive=%d",
                operation,
                category,
                error_type,
                error_code,
                signature_occurrence,
                stats["consecutive_failure_count"],
            )

    def snapshot(self) -> dict[str, Any]:
        """Return a detached, JSON-safe aggregate and bounded failure ring."""
        operations: dict[str, Any] = {}
        totals = Counter()
        total_categories: Counter[str] = Counter()
        total_types: Counter[str] = Counter()
        total_durations: Counter[str] = Counter()
        for operation, raw in sorted(self._operations.items()):
            stats = {
                "request_count": raw["request_count"],
                "success_count": raw["success_count"],
                "failure_count": raw["failure_count"],
                "recovery_count": raw["recovery_count"],
                "consecutive_failure_count": raw["consecutive_failure_count"],
                "error_category_counts": dict(sorted(raw["error_category_counts"].items())),
                "error_type_counts": dict(sorted(raw["error_type_counts"].items())),
                "duration_bucket_counts": {
                    bucket: raw["duration_bucket_counts"].get(bucket, 0) for bucket in _DURATION_BUCKETS
                },
            }
            operations[operation] = stats
            for key in (
                "request_count",
                "success_count",
                "failure_count",
                "recovery_count",
            ):
                totals[key] += stats[key]
            total_categories.update(raw["error_category_counts"])
            total_types.update(raw["error_type_counts"])
            total_durations.update(raw["duration_bucket_counts"])
        return {
            "schema_version": 1,
            "event_capacity": self._failure_events.maxlen,
            "failure_event_count": len(self._failure_events),
            "totals": {
                **dict(totals),
                "error_category_counts": dict(sorted(total_categories.items())),
                "error_type_counts": dict(sorted(total_types.items())),
                "duration_bucket_counts": {bucket: total_durations.get(bucket, 0) for bucket in _DURATION_BUCKETS},
            },
            "operations": operations,
            "failure_events": [dict(event) for event in self._failure_events],
        }


def _bounded_report_count(value: Any) -> int | None:
    """Accept only the same bounded integer shape as the WebSocket schema."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 1_000_000 else None


def _should_warn(occurrence: int) -> bool:
    """Warn on the first three, powers of two, and periodic milestones."""
    return occurrence <= 3 or occurrence & (occurrence - 1) == 0 or occurrence % 100 == 0
