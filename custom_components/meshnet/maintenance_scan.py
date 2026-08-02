"""Low-priority, traffic-aware NeighborInfo maintenance scheduling.

This module deliberately knows nothing about Home Assistant, gateways, nodes,
or durable storage.  Its owner injects three narrow callbacks:

* choose one opaque candidate while honoring the active cycle's exclusions;
* submit one NeighborInfo request for that candidate; and
* report whether legitimate foreground work is active.

The owner is also responsible for recording observed radio activity through
``record_activity``.  Candidate validation, durable airtime reservation, and
protocol correlation stay at their existing boundaries outside this module.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

_QUIESCE_TIMEOUT_SECONDS = 5.0
_TASK_NAME = "MeshNet idle NeighborInfo maintenance"

type MaintenanceCandidate = Any
type CandidateCallback = Callable[
    [frozenset[MaintenanceCandidate]],
    Awaitable[MaintenanceCandidate | None],
]
type RequestCallback = Callable[[MaintenanceCandidate], Awaitable[Any]]
type BusyCallback = Callable[[], bool]
type MonotonicCallback = Callable[[], float]
type TaskFactory = Callable[
    [Coroutine[Any, Any, None], str], asyncio.Task[None]
]

_OUTCOMES = frozenset(
    {
        "not_started",
        "disabled",
        "fenced",
        "not_due",
        "request_spacing",
        "traffic_deferred",
        "busy_deferred",
        "busy_check_failed",
        "no_candidate",
        "candidate_failed",
        "duplicate_candidate",
        "request_succeeded",
        "request_failed",
        "request_cancelled",
        "cycle_complete",
        "internal_error",
    }
)


@dataclass(frozen=True, slots=True)
class MaintenanceScanConfig:
    """Validated timing and budget controls for one maintenance scheduler."""

    enabled: bool
    interval_seconds: float
    quiet_seconds: float
    max_requests: int
    request_spacing_seconds: float = 60.0
    tick_seconds: float = 15.0

    def __post_init__(self) -> None:
        """Reject ambiguous or unsafe scheduler configuration."""
        if not isinstance(self.enabled, bool):
            raise ValueError("maintenance enabled must be boolean")
        for name in (
            "interval_seconds",
            "quiet_seconds",
            "request_spacing_seconds",
            "tick_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"maintenance {name} must be positive and finite")
        if (
            isinstance(self.max_requests, bool)
            or not isinstance(self.max_requests, int)
            or self.max_requests < 1
        ):
            raise ValueError("maintenance max_requests must be a positive integer")


def _default_task_factory(
    target: Coroutine[Any, Any, None], name: str
) -> asyncio.Task[None]:
    """Create the scheduler's single background owner."""
    return asyncio.create_task(target, name=name)


class MaintenanceScanScheduler:
    """Run bounded NeighborInfo cycles only after observed radio silence."""

    def __init__(
        self,
        config: MaintenanceScanConfig,
        *,
        next_candidate: CandidateCallback,
        request_neighbor_info: RequestCallback,
        is_busy: BusyCallback,
        monotonic: MonotonicCallback = time.monotonic,
        task_factory: TaskFactory = _default_task_factory,
    ) -> None:
        self.config = config
        self._next_candidate = next_candidate
        self._request_neighbor_info = request_neighbor_info
        self._is_busy = is_busy
        self._monotonic = monotonic
        self._task_factory = task_factory

        now = self._read_monotonic()
        self._clock_floor = now
        self._last_activity = now
        self._next_cycle_due = now + float(config.interval_seconds)
        self._next_request_due = self._next_cycle_due
        self._last_request: float | None = None

        self._accepting = True
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._cycle_active = False
        self._cycle_request_count = 0
        self._attempted_candidates: set[MaintenanceCandidate] = set()

        self._last_outcome = "not_started"
        self._activity_count = 0
        self._activity_generation = 0
        self._cycle_started_count = 0
        self._cycle_completed_count = 0
        self._request_attempt_count = 0
        self._request_success_count = 0
        self._request_failure_count = 0
        self._request_cancellation_count = 0
        self._no_candidate_count = 0
        self._candidate_failure_count = 0
        self._duplicate_candidate_count = 0
        self._traffic_deferral_count = 0
        self._busy_deferral_count = 0
        self._busy_check_failure_count = 0
        self._runner_failure_count = 0

    @property
    def enabled(self) -> bool:
        """Return whether the operator opted into maintenance RF requests."""
        return self.config.enabled

    @property
    def accepting(self) -> bool:
        """Return whether new scheduler work is allowed."""
        return self._accepting

    def start(self) -> bool:
        """Start, or retain, the scheduler's single background task."""
        if not self.config.enabled:
            self._last_outcome = "disabled"
            return False
        if not self._accepting:
            self._last_outcome = "fenced"
            return False
        task = self._task
        if task is not None and not task.done():
            return True
        if task is not None:
            self._consume_task_result(task)
        target = self._run()
        try:
            task = self._task_factory(target, _TASK_NAME)
        except BaseException:
            target.close()
            raise
        if not isinstance(task, asyncio.Task):
            target.close()
            raise TypeError("maintenance task factory must return an asyncio Task")
        self._task = task
        task.add_done_callback(self._background_done)
        return True

    @property
    def activity_generation(self) -> int:
        """Return the token changed by every observed foreground activity."""
        return self._activity_generation

    def record_activity(self) -> int:
        """Record radio activity and return its new monotonic generation."""
        now = self._now()
        self._last_activity = max(self._last_activity, now)
        self._activity_count += 1
        self._activity_generation += 1
        self._wakeup.set()
        return self._activity_generation

    async def async_tick(self) -> str:
        """Evaluate one scheduler tick and submit at most one request."""
        if self._tick_lock.locked():
            self._last_outcome = "busy_deferred"
            self._busy_deferral_count += 1
            return self._last_outcome
        async with self._tick_lock:
            return await self._async_tick_locked()

    async def _async_tick_locked(self) -> str:
        """Evaluate one serialized tick."""
        if not self.config.enabled:
            return self._set_outcome("disabled")
        if not self._accepting:
            return self._set_outcome("fenced")

        now = self._now()
        if not self._cycle_active:
            if now < self._next_cycle_due:
                return self._set_outcome("not_due")
            if self._traffic_is_recent(now):
                self._traffic_deferral_count += 1
                return self._set_outcome("traffic_deferred")
            busy = self._foreground_is_busy()
            if busy is None:
                return self._set_outcome("busy_check_failed")
            if busy:
                self._busy_deferral_count += 1
                return self._set_outcome("busy_deferred")
            self._start_cycle(now)

        if self._cycle_request_count >= self.config.max_requests:
            self._complete_cycle(now)
            return self._set_outcome("cycle_complete")
        if now < self._next_request_due:
            return self._set_outcome("request_spacing")
        if self._traffic_is_recent(now):
            self._traffic_deferral_count += 1
            return self._set_outcome("traffic_deferred")
        busy = self._foreground_is_busy()
        if busy is None:
            return self._set_outcome("busy_check_failed")
        if busy:
            self._busy_deferral_count += 1
            return self._set_outcome("busy_deferred")

        activity_generation = self._activity_generation
        try:
            candidate = await self._next_candidate(
                frozenset(self._attempted_candidates)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._candidate_failure_count += 1
            self._complete_cycle(self._now())
            return self._set_outcome("candidate_failed")

        if candidate is None:
            self._no_candidate_count += 1
            self._complete_cycle(self._now())
            return self._set_outcome("no_candidate")
        try:
            duplicate = candidate in self._attempted_candidates
            if not duplicate:
                self._attempted_candidates.add(candidate)
        except Exception:
            self._candidate_failure_count += 1
            self._complete_cycle(self._now())
            return self._set_outcome("candidate_failed")
        if duplicate:
            self._duplicate_candidate_count += 1
            self._complete_cycle(self._now())
            return self._set_outcome("duplicate_candidate")

        # Candidate discovery can yield to normal radio work.  The candidate
        # remains excluded for the rest of this cycle even when a late gate
        # rejects it, so a timeout, busy race, or zero-RF rejection is never
        # retried. Recheck both gates immediately before crossing the injected
        # RF boundary; there is intentionally no await between that check and
        # invoking the callback.
        now = self._now()
        if (
            self._activity_generation != activity_generation
            or self._traffic_is_recent(now)
        ):
            self._traffic_deferral_count += 1
            return self._set_outcome("traffic_deferred")
        busy = self._foreground_is_busy()
        if busy is None:
            return self._set_outcome("busy_check_failed")
        if busy:
            self._busy_deferral_count += 1
            return self._set_outcome("busy_deferred")

        self._request_attempt_count += 1
        self._cycle_request_count += 1
        self._last_request = now
        self._next_request_due = now + float(self.config.request_spacing_seconds)
        try:
            await self._request_neighbor_info(candidate)
        except asyncio.CancelledError:
            self._request_cancellation_count += 1
            self._last_outcome = "request_cancelled"
            raise
        except Exception:
            self._request_failure_count += 1
            outcome = "request_failed"
        else:
            self._request_success_count += 1
            outcome = "request_succeeded"

        if self._cycle_request_count >= self.config.max_requests:
            self._complete_cycle(self._now())
        return self._set_outcome(outcome)

    async def async_quiesce(self) -> bool:
        """Fence new work and cancel the single background owner."""
        self._accepting = False
        self._wakeup.set()
        task = self._task
        if task is None:
            self._clear_cycle()
            return True
        if task is asyncio.current_task():
            return False
        if task.done():
            self._consume_task_result(task)
            self._clear_cycle()
            return True
        if task.cancelling() == 0:
            task.cancel()
        done, _pending = await asyncio.wait(
            {task}, timeout=_QUIESCE_TIMEOUT_SECONDS
        )
        if task not in done:
            return False
        self._consume_task_result(task)
        self._clear_cycle()
        return True

    def resume(self) -> bool:
        """Resume only after the previous scheduler owner has fully drained."""
        task = self._task
        if task is not None and not task.done():
            return False
        if task is not None:
            self._consume_task_result(task)
        self._task = None
        self._accepting = True
        now = self._now()
        self._last_activity = now
        self._next_cycle_due = now + float(self.config.interval_seconds)
        self._next_request_due = self._next_cycle_due
        self._last_request = None
        self._clear_cycle()
        self._wakeup.clear()
        if not self.config.enabled:
            self._last_outcome = "disabled"
            return True
        self._last_outcome = "not_started"
        return self.start()

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return fixed aggregate scheduler state without candidate identity."""
        now = self._now()
        task = self._task
        return {
            "schema_version": 1,
            "enabled": self.config.enabled,
            "accepting": self._accepting,
            "task_state": self._task_state(task),
            "cycle_active": self._cycle_active,
            "cycle_request_count": self._cycle_request_count,
            "interval_seconds": float(self.config.interval_seconds),
            "quiet_seconds": float(self.config.quiet_seconds),
            "request_spacing_seconds": float(
                self.config.request_spacing_seconds
            ),
            "tick_seconds": float(self.config.tick_seconds),
            "max_requests_per_cycle": self.config.max_requests,
            "next_cycle_in_seconds": self._remaining(self._next_cycle_due, now),
            "next_request_in_seconds": (
                self._remaining(self._next_request_due, now)
                if self._cycle_active
                else None
            ),
            "last_activity_age_seconds": self._age(self._last_activity, now),
            "last_request_age_seconds": (
                self._age(self._last_request, now)
                if self._last_request is not None
                else None
            ),
            "last_outcome": (
                self._last_outcome
                if self._last_outcome in _OUTCOMES
                else "internal_error"
            ),
            "activity_count": self._activity_count,
            "cycle_started_count": self._cycle_started_count,
            "cycle_completed_count": self._cycle_completed_count,
            "request_attempt_count": self._request_attempt_count,
            "request_success_count": self._request_success_count,
            "request_failure_count": self._request_failure_count,
            "request_cancellation_count": self._request_cancellation_count,
            "no_candidate_count": self._no_candidate_count,
            "candidate_failure_count": self._candidate_failure_count,
            "duplicate_candidate_count": self._duplicate_candidate_count,
            "traffic_deferral_count": self._traffic_deferral_count,
            "busy_deferral_count": self._busy_deferral_count,
            "busy_check_failure_count": self._busy_check_failure_count,
            "runner_failure_count": self._runner_failure_count,
            "candidate_identity_exposed": False,
            "automatic_traceroute_supported": False,
            "automatic_retry_supported": False,
        }

    async def _run(self) -> None:
        """Own all recurring scheduler work in one cancellable task."""
        try:
            while self._accepting:
                self._wakeup.clear()
                try:
                    async with asyncio.timeout(float(self.config.tick_seconds)):
                        await self._wakeup.wait()
                except TimeoutError:
                    pass
                if not self._accepting:
                    return
                try:
                    await self.async_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._runner_failure_count += 1
                    self._last_outcome = "internal_error"
                    self._complete_cycle(self._now())
        except asyncio.CancelledError:
            raise

    def _start_cycle(self, now: float) -> None:
        self._cycle_active = True
        self._cycle_request_count = 0
        self._attempted_candidates.clear()
        self._next_request_due = now
        self._cycle_started_count += 1

    def _complete_cycle(self, now: float) -> None:
        self._cycle_active = False
        self._cycle_request_count = 0
        self._attempted_candidates.clear()
        self._next_cycle_due = now + float(self.config.interval_seconds)
        self._next_request_due = self._next_cycle_due
        self._cycle_completed_count += 1

    def _clear_cycle(self) -> None:
        self._cycle_active = False
        self._cycle_request_count = 0
        self._attempted_candidates.clear()

    def _traffic_is_recent(self, now: float) -> bool:
        return now - self._last_activity < float(self.config.quiet_seconds)

    def _foreground_is_busy(self) -> bool | None:
        try:
            return bool(self._is_busy())
        except Exception:
            self._busy_check_failure_count += 1
            return None

    def _set_outcome(self, outcome: str) -> str:
        self._last_outcome = outcome if outcome in _OUTCOMES else "internal_error"
        return self._last_outcome

    def _now(self) -> float:
        value = self._read_monotonic()
        if value < self._clock_floor:
            return self._clock_floor
        self._clock_floor = value
        return value

    def _read_monotonic(self) -> float:
        value = self._monotonic()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RuntimeError("maintenance monotonic clock returned invalid time")
        return float(value)

    def _background_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            self._runner_failure_count += 1
            self._last_outcome = "internal_error"

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return

    @staticmethod
    def _task_state(task: asyncio.Task[None] | None) -> str:
        if task is None:
            return "not_created"
        if task.cancelled():
            return "cancelled"
        if task.done():
            return "finished"
        if task.cancelling():
            return "cancelling"
        return "pending"

    @staticmethod
    def _remaining(when: float, now: float) -> float:
        return round(max(0.0, when - now), 3)

    @staticmethod
    def _age(when: float, now: float) -> float:
        return round(max(0.0, now - when), 3)


# A descriptive alias for callers that prefer the feature-specific name.
IdleNeighborInfoMaintenanceScheduler = MaintenanceScanScheduler
