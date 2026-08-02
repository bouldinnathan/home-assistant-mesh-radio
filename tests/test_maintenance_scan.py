"""Dependency-light tests for low-priority NeighborInfo maintenance."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from custom_components.meshnet.maintenance_scan import (
    MaintenanceScanConfig,
    MaintenanceScanScheduler,
)


class FakeClock:
    """Small monotonic clock controlled directly by each test."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _config(**updates: Any) -> MaintenanceScanConfig:
    values = {
        "enabled": True,
        "interval_seconds": 3600,
        "quiet_seconds": 60,
        "max_requests": 2,
        "request_spacing_seconds": 60,
        "tick_seconds": 15,
    }
    values.update(updates)
    return MaintenanceScanConfig(**values)


def _scheduler(
    clock: FakeClock,
    *,
    candidates: list[Any] | None = None,
    request: Callable[[Any], Awaitable[Any]] | None = None,
    busy: Callable[[], bool] | None = None,
    config: MaintenanceScanConfig | None = None,
) -> tuple[MaintenanceScanScheduler, list[Any]]:
    pending = list(candidates or [])
    requested: list[Any] = []

    async def next_candidate(_excluded: frozenset[Any]) -> Any | None:
        return pending.pop(0) if pending else None

    async def default_request(candidate: Any) -> None:
        requested.append(candidate)

    scheduler = MaintenanceScanScheduler(
        config or _config(),
        next_candidate=next_candidate,
        request_neighbor_info=request or default_request,
        is_busy=busy or (lambda: False),
        monotonic=clock,
    )
    return scheduler, requested


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"enabled": 1}, "enabled"),
        ({"interval_seconds": 0}, "interval_seconds"),
        ({"quiet_seconds": float("nan")}, "quiet_seconds"),
        ({"request_spacing_seconds": -1}, "request_spacing_seconds"),
        ({"tick_seconds": True}, "tick_seconds"),
        ({"max_requests": 0}, "max_requests"),
    ],
)
def test_configuration_rejects_ambiguous_or_unbounded_values(
    updates: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _config(**updates)


def test_disabled_scheduler_never_creates_a_task_or_calls_callbacks() -> None:
    async def run() -> None:
        clock = FakeClock()
        scheduler, requested = _scheduler(
            clock,
            candidates=["private-node"],
            config=_config(enabled=False),
        )

        assert scheduler.start() is False
        clock.advance(100_000)
        assert await scheduler.async_tick() == "disabled"
        assert requested == []
        diagnostics = scheduler.diagnostic_snapshot()
        assert diagnostics["enabled"] is False
        assert diagnostics["task_state"] == "not_created"
        assert diagnostics["request_attempt_count"] == 0

    asyncio.run(run())


def test_first_cycle_waits_full_interval_spaces_requests_and_never_catches_up() -> None:
    async def run() -> None:
        clock = FakeClock()
        scheduler, requested = _scheduler(
            clock,
            candidates=["node-a", "node-b", "node-c"],
        )

        clock.advance(3599)
        assert await scheduler.async_tick() == "not_due"
        assert requested == []

        clock.advance(1)
        assert await scheduler.async_tick() == "request_succeeded"
        assert requested == ["node-a"]
        assert await scheduler.async_tick() == "request_spacing"
        assert requested == ["node-a"]

        clock.advance(59)
        assert await scheduler.async_tick() == "request_spacing"
        clock.advance(1)
        assert await scheduler.async_tick() == "request_succeeded"
        assert requested == ["node-a", "node-b"]

        diagnostics = scheduler.diagnostic_snapshot()
        assert diagnostics["cycle_active"] is False
        assert diagnostics["cycle_completed_count"] == 1
        assert diagnostics["next_cycle_in_seconds"] == 3600

        # Missing many theoretical intervals creates one new cycle and at most
        # one request on this tick; there is no catch-up burst.
        clock.advance(50_000)
        assert await scheduler.async_tick() == "request_succeeded"
        assert requested == ["node-a", "node-b", "node-c"]

    asyncio.run(run())


def test_recent_activity_and_foreground_work_defer_before_selection() -> None:
    async def run() -> None:
        clock = FakeClock()
        busy = False
        pending = ["node-a"]
        requested: list[str] = []

        async def next_candidate(_excluded: frozenset[Any]) -> str | None:
            return pending[0] if pending else None

        async def request(candidate: Any) -> None:
            requested.append(str(candidate))
            pending.remove(candidate)

        scheduler = MaintenanceScanScheduler(
            _config(),
            next_candidate=next_candidate,
            request_neighbor_info=request,
            is_busy=lambda: busy,
            monotonic=clock,
        )
        clock.advance(3600)
        scheduler.record_activity()
        assert await scheduler.async_tick() == "traffic_deferred"
        assert requested == []

        clock.advance(60)
        busy = True
        assert await scheduler.async_tick() == "busy_deferred"
        assert requested == []

        busy = False
        assert await scheduler.async_tick() == "request_succeeded"
        assert requested == ["node-a"]
        diagnostics = scheduler.diagnostic_snapshot()
        assert diagnostics["traffic_deferral_count"] == 1
        assert diagnostics["busy_deferral_count"] == 1

    asyncio.run(run())


def test_activity_during_candidate_selection_rechecks_before_rf() -> None:
    async def run() -> None:
        clock = FakeClock()
        requested: list[str] = []
        exclusions: list[frozenset[Any]] = []
        scheduler: MaintenanceScanScheduler

        async def next_candidate(excluded: frozenset[Any]) -> str:
            exclusions.append(excluded)
            if not excluded:
                assert scheduler.record_activity() == 1
                return "node-a"
            return "node-b"

        async def request(candidate: Any) -> None:
            requested.append(str(candidate))

        scheduler = MaintenanceScanScheduler(
            _config(),
            next_candidate=next_candidate,
            request_neighbor_info=request,
            is_busy=lambda: False,
            monotonic=clock,
        )
        clock.advance(3600)
        assert await scheduler.async_tick() == "traffic_deferred"
        assert requested == []
        assert scheduler.diagnostic_snapshot()["request_attempt_count"] == 0
        assert scheduler.activity_generation == 1

        clock.advance(60)
        assert await scheduler.async_tick() == "request_succeeded"
        assert requested == ["node-b"]
        assert exclusions == [frozenset(), frozenset({"node-a"})]

    asyncio.run(run())


def test_busy_race_keeps_selected_candidate_excluded_for_cycle() -> None:
    async def run() -> None:
        clock = FakeClock()
        requested: list[str] = []
        exclusions: list[frozenset[Any]] = []
        busy_checks = iter((False, False, True, False, False))

        async def next_candidate(excluded: frozenset[Any]) -> str:
            exclusions.append(excluded)
            return "node-a" if not excluded else "node-b"

        async def request(candidate: Any) -> None:
            requested.append(str(candidate))

        scheduler = MaintenanceScanScheduler(
            _config(),
            next_candidate=next_candidate,
            request_neighbor_info=request,
            is_busy=lambda: next(busy_checks),
            monotonic=clock,
        )
        clock.advance(3600)
        assert await scheduler.async_tick() == "busy_deferred"
        assert requested == []

        assert await scheduler.async_tick() == "request_succeeded"
        assert requested == ["node-b"]
        assert exclusions == [frozenset(), frozenset({"node-a"})]

    asyncio.run(run())


def test_failed_request_is_not_retried_when_provider_returns_same_candidate() -> None:
    async def run() -> None:
        clock = FakeClock()
        calls: list[str] = []

        async def next_candidate(_excluded: frozenset[Any]) -> str:
            return "node-private-value"

        async def request(candidate: Any) -> None:
            calls.append(str(candidate))
            raise RuntimeError("private endpoint and node-private-value")

        scheduler = MaintenanceScanScheduler(
            _config(),
            next_candidate=next_candidate,
            request_neighbor_info=request,
            is_busy=lambda: False,
            monotonic=clock,
        )
        clock.advance(3600)
        assert await scheduler.async_tick() == "request_failed"
        clock.advance(60)
        assert await scheduler.async_tick() == "duplicate_candidate"
        assert calls == ["node-private-value"]

        diagnostics = scheduler.diagnostic_snapshot()
        assert diagnostics["request_attempt_count"] == 1
        assert diagnostics["request_failure_count"] == 1
        assert diagnostics["duplicate_candidate_count"] == 1
        assert "node-private-value" not in repr(diagnostics)
        assert "private endpoint" not in repr(diagnostics)
        assert diagnostics["automatic_retry_supported"] is False

    asyncio.run(run())


def test_no_candidate_or_candidate_failure_ends_cycle_until_next_interval() -> None:
    async def run() -> None:
        clock = FakeClock()
        candidate_calls = 0

        async def next_candidate(_excluded: frozenset[Any]) -> None:
            nonlocal candidate_calls
            candidate_calls += 1
            if candidate_calls == 1:
                raise RuntimeError("private candidate failure")
            return None

        scheduler = MaintenanceScanScheduler(
            _config(),
            next_candidate=next_candidate,
            request_neighbor_info=lambda _candidate: asyncio.sleep(0),
            is_busy=lambda: False,
            monotonic=clock,
        )
        clock.advance(3600)
        assert await scheduler.async_tick() == "candidate_failed"
        assert candidate_calls == 1
        assert await scheduler.async_tick() == "not_due"
        clock.advance(3600)
        assert await scheduler.async_tick() == "no_candidate"
        assert candidate_calls == 2

    asyncio.run(run())


def test_unhashable_candidate_fails_closed_before_request() -> None:
    async def run() -> None:
        clock = FakeClock()
        requested: list[Any] = []

        async def next_candidate(_excluded: frozenset[Any]) -> Any:
            return ["private-node-id"]

        async def request(candidate: Any) -> None:
            requested.append(candidate)

        scheduler = MaintenanceScanScheduler(
            _config(),
            next_candidate=next_candidate,
            request_neighbor_info=request,
            is_busy=lambda: False,
            monotonic=clock,
        )
        clock.advance(3600)
        assert await scheduler.async_tick() == "candidate_failed"
        assert requested == []
        assert "private-node-id" not in repr(scheduler.diagnostic_snapshot())

    asyncio.run(run())


def test_monotonic_rollback_cannot_reduce_activity_age_or_trigger_early_scan() -> None:
    async def run() -> None:
        clock = FakeClock(100)
        scheduler, requested = _scheduler(clock, candidates=["node-a"])
        scheduler.record_activity()

        clock.value = 1
        scheduler.record_activity()
        diagnostics = scheduler.diagnostic_snapshot()
        assert diagnostics["last_activity_age_seconds"] == 0
        assert diagnostics["next_cycle_in_seconds"] == 3600

        clock.value = 3699
        assert await scheduler.async_tick() == "not_due"
        assert requested == []
        clock.value = 3700
        assert await scheduler.async_tick() == "request_succeeded"
        assert requested == ["node-a"]

    asyncio.run(run())


def test_start_is_single_flight_and_quiesce_resume_rearms_first_interval() -> None:
    async def run() -> None:
        clock = FakeClock()
        created: list[asyncio.Task[None]] = []

        def task_factory(
            target: Awaitable[None], name: str
        ) -> asyncio.Task[None]:
            task = asyncio.create_task(target, name=name)
            created.append(task)
            return task

        async def next_candidate(_excluded: frozenset[Any]) -> None:
            return None

        scheduler = MaintenanceScanScheduler(
            _config(tick_seconds=3600),
            next_candidate=next_candidate,
            request_neighbor_info=lambda _candidate: asyncio.sleep(0),
            is_busy=lambda: False,
            monotonic=clock,
            task_factory=task_factory,
        )
        assert scheduler.start() is True
        assert scheduler.start() is True
        assert len(created) == 1
        assert scheduler.diagnostic_snapshot()["task_state"] == "pending"

        assert await scheduler.async_quiesce() is True
        assert scheduler.accepting is False
        assert created[0].done()

        clock.advance(50_000)
        assert scheduler.resume() is True
        assert len(created) == 2
        assert scheduler.diagnostic_snapshot()["next_cycle_in_seconds"] == 3600
        assert await scheduler.async_quiesce() is True

    asyncio.run(run())


def test_quiesce_cancels_and_drains_an_active_request() -> None:
    async def run() -> None:
        clock = FakeClock()
        request_started = asyncio.Event()
        request_cancelled = asyncio.Event()

        async def next_candidate(_excluded: frozenset[Any]) -> str:
            return "node-a"

        async def request(_candidate: Any) -> None:
            request_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                request_cancelled.set()

        scheduler = MaintenanceScanScheduler(
            _config(tick_seconds=0.001),
            next_candidate=next_candidate,
            request_neighbor_info=request,
            is_busy=lambda: False,
            monotonic=clock,
        )
        assert scheduler.start() is True
        clock.advance(3600)
        await asyncio.wait_for(request_started.wait(), timeout=1)

        assert await scheduler.async_quiesce() is True
        assert request_cancelled.is_set()
        assert scheduler.accepting is False
        diagnostics = scheduler.diagnostic_snapshot()
        assert diagnostics["task_state"] == "cancelled"
        assert diagnostics["request_cancellation_count"] == 1

    asyncio.run(run())


def test_diagnostics_are_a_fixed_identity_free_schema() -> None:
    clock = FakeClock()
    scheduler, _requested = _scheduler(clock, candidates=["secret-node-id"])
    diagnostics = scheduler.diagnostic_snapshot()

    assert set(diagnostics) == {
        "schema_version",
        "enabled",
        "accepting",
        "task_state",
        "cycle_active",
        "cycle_request_count",
        "interval_seconds",
        "quiet_seconds",
        "request_spacing_seconds",
        "tick_seconds",
        "max_requests_per_cycle",
        "next_cycle_in_seconds",
        "next_request_in_seconds",
        "last_activity_age_seconds",
        "last_request_age_seconds",
        "last_outcome",
        "activity_count",
        "cycle_started_count",
        "cycle_completed_count",
        "request_attempt_count",
        "request_success_count",
        "request_failure_count",
        "request_cancellation_count",
        "no_candidate_count",
        "candidate_failure_count",
        "duplicate_candidate_count",
        "traffic_deferral_count",
        "busy_deferral_count",
        "busy_check_failure_count",
        "runner_failure_count",
        "candidate_identity_exposed",
        "automatic_traceroute_supported",
        "automatic_retry_supported",
    }
    assert diagnostics["candidate_identity_exposed"] is False
    assert diagnostics["automatic_traceroute_supported"] is False
    assert "secret-node-id" not in repr(diagnostics)
