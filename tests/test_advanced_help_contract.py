"""Keep the advanced operator guide aligned with radio-safety invariants."""

from __future__ import annotations

from pathlib import Path

GUIDE = Path("docs/ADVANCED_MESH_OPERATIONS.md")


def _guide() -> str:
    return " ".join(GUIDE.read_text(encoding="utf-8").split())


def test_advanced_guide_covers_every_requested_operator_surface() -> None:
    """The help file is the implementation and acceptance contract."""
    guide = _guide()

    for heading in (
        "Remote Administration and Keys",
        "Messages",
        "Manual Traceroute",
        "Moving, Distance-Aware Graph",
        "Home Assistant Automations and Network Failures",
        "Passive Weather and Sensor Data",
        "Privacy, Recovery, and Uninstall",
        "Acceptance and Regression Contract",
    ):
        assert heading in guide


def test_advanced_guide_fixes_key_and_traceroute_safety_boundaries() -> None:
    """Dangerous shortcuts must not silently return in a later release."""
    guide = _guide()

    assert "does not provide a private-key text box" in guide
    assert "every SecurityConfig write" in guide
    assert "raw AdminMessage passthrough" in guide
    assert "reserved in SQLite before the radio write" in guide
    assert "one manual traceroute across the entire MeshNet integration every 3,600 seconds" in guide
    assert "There is no traceroute Home Assistant service" in guide
    assert "There is no broadcast, batch, scheduled, automatic, or retry mode" in guide


def test_advanced_guide_separates_evidence_distance_and_signal_strength() -> None:
    """Graph presentation cannot manufacture network topology."""
    guide = _guide()

    assert "Physical proximity alone never creates an edge" in guide
    assert "SNR/RSSI never substitutes for GPS" in guide
    assert "Home Assistant location fallback" in guide
    assert "20, 50, or 100 most recently heard nodes" in guide
