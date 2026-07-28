"""Repository-level guardrails for radio operations that must stay manual."""

from __future__ import annotations

from pathlib import Path

RUNTIME_PATHS = tuple(
    path
    for path in sorted(Path("custom_components/meshnet").rglob("*"))
    if path.is_file() and path.suffix in {".js", ".json", ".py", ".yaml"}
)


def test_runtime_exposes_no_traceroute_transmit_path() -> None:
    """Topology refreshes must never cause an active traceroute transmission."""
    runtime = "\n".join(path.read_text(encoding="utf-8") for path in RUNTIME_PATHS)
    forbidden_active_tokens = (
        "TRACEROUTE_APP",
        "meshnet/traceroute",
        "meshnet.trace_route",
        "SERVICE_TRACEROUTE",
        "async_traceroute",
        "send_traceroute",
        "sendTraceRoute",
        "request_traceroute",
        "requestTraceRoute",
    )

    assert all(token not in runtime for token in forbidden_active_tokens)


def test_future_manual_traceroute_cooldown_is_documented() -> None:
    """Keep the one-hour backend cooldown invariant visible to maintainers."""
    architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    usage = Path("docs/USAGE.md").read_text(encoding="utf-8")

    assert "3,600 seconds per gateway and" in " ".join(architecture.split())
    assert "at least one hour per gateway and destination" in " ".join(usage.split())
