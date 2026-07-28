"""Tests for the bounded, privacy-safe MeshNet panel telemetry."""

from __future__ import annotations

import json

from custom_components.meshnet.panel_telemetry import PanelTelemetry


class _Logger:
    def __init__(self) -> None:
        self.debug_calls: list[tuple[object, ...]] = []
        self.warning_calls: list[tuple[object, ...]] = []

    def debug(self, *args: object) -> None:
        self.debug_calls.append(args)

    def warning(self, *args: object) -> None:
        self.warning_calls.append(args)


def test_failure_ring_is_bounded_while_aggregate_counts_remain_complete() -> None:
    logger = _Logger()
    telemetry = PanelTelemetry(logger)

    for _ in range(105):
        telemetry.record_request("snapshot")
        telemetry.record_failure(
            "snapshot",
            category="connection",
            error_type="ConnectionError",
            error_code="snapshot_failed",
            duration_seconds=0.025,
        )

    snapshot = telemetry.snapshot()
    stats = snapshot["operations"]["snapshot"]
    assert snapshot["event_capacity"] == 100
    assert snapshot["failure_event_count"] == 100
    assert len(snapshot["failure_events"]) == 100
    assert snapshot["failure_events"][0]["sequence"] == 6
    assert snapshot["failure_events"][-1]["sequence"] == 105
    assert stats["request_count"] == 105
    assert stats["failure_count"] == 105
    assert stats["consecutive_failure_count"] == 105
    assert stats["error_category_counts"] == {"connection": 105}
    assert stats["error_type_counts"] == {"ConnectionError": 105}
    assert stats["duration_bucket_counts"]["under_50ms"] == 105
    assert len(logger.debug_calls) == 105
    assert len(logger.warning_calls) < len(logger.debug_calls)


def test_success_records_recovery_and_resets_consecutive_failures() -> None:
    telemetry = PanelTelemetry(_Logger())
    telemetry.record_request("poll")
    telemetry.record_failure(
        "poll",
        category="network",
        error_type="NetworkError",
        error_code="poll_failed",
        duration_seconds=1.5,
    )
    telemetry.record_request("poll")
    telemetry.record_success("poll", duration_seconds=0.005)

    stats = telemetry.snapshot()["operations"]["poll"]
    assert stats["request_count"] == 2
    assert stats["success_count"] == 1
    assert stats["failure_count"] == 1
    assert stats["recovery_count"] == 1
    assert stats["consecutive_failure_count"] == 0
    assert stats["duration_bucket_counts"]["under_10ms"] == 1
    assert stats["duration_bucket_counts"]["under_5s"] == 1


def test_untrusted_values_are_never_retained_or_logged() -> None:
    logger = _Logger()
    telemetry = PanelTelemetry(logger)
    private_values = (
        "person@example.com",
        "node_12345678",
        "https://private-host.local/path?token=secret",
        "41.1234,-87.5678",
        "private message contents",
    )
    telemetry.record_failure(
        private_values[0],
        category=private_values[1],
        error_type=private_values[2],
        error_code=private_values[3],
        occurrence=private_values[4],
        consecutive=private_values[4],
    )

    serialized = json.dumps(
        {
            "snapshot": telemetry.snapshot(),
            "debug": logger.debug_calls,
            "warning": logger.warning_calls,
        },
        sort_keys=True,
        default=str,
    )
    assert all(value not in serialized for value in private_values)
    event = telemetry.snapshot()["failure_events"][0]
    assert event["operation"] == "reporting"
    assert event["category"] == "unknown"
    assert event["error_type"] == "other_error"
    assert event["error_code"] == "unexpected_error"
    assert "reported_occurrence" not in event
    assert "reported_consecutive" not in event


def test_client_occurrence_metadata_is_bounded_and_does_not_change_counts() -> None:
    telemetry = PanelTelemetry(_Logger())
    telemetry.record_failure(
        "global_error",
        category="internal",
        error_type="Error",
        error_code="unexpected_error",
        occurrence=999_999,
        consecutive=12,
    )

    snapshot = telemetry.snapshot()
    event = snapshot["failure_events"][0]
    assert event["reported_occurrence"] == 999_999
    assert event["reported_consecutive"] == 12
    assert event["occurrence"] == 1
    assert event["consecutive"] == 1
    assert snapshot["operations"]["global_error"]["failure_count"] == 1
