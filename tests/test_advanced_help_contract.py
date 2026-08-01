"""Keep the advanced operator guide aligned with radio-safety invariants."""

from __future__ import annotations

from pathlib import Path

GUIDE = Path("docs/ADVANCED_MESH_OPERATIONS.md")
README = Path("README.md")
EXAMPLE_AUTOMATIONS = Path("examples/automations.yaml")


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
    assert "one manual traceroute across the entire MeshNet integration every 60 seconds" in guide
    assert "There is no traceroute Home Assistant service" in guide
    assert "There is no broadcast, batch, scheduled, automatic, or retry mode" in guide


def test_advanced_guide_separates_evidence_distance_and_signal_strength() -> None:
    """Graph presentation cannot manufacture network topology."""
    guide = _guide()

    assert "Physical proximity alone never creates an edge" in guide
    assert "SNR/RSSI never substitutes for GPS" in guide
    assert "Home Assistant location fallback" in guide
    assert "20, 50, or 100 most recently heard nodes" in guide


def test_hacs_readme_has_basic_message_and_telemetry_instructions() -> None:
    """The HACS landing page must explain normal operation without doc hunting."""
    readme = README.read_text(encoding="utf-8")

    for required in (
        "Messages and sensor data quick start",
        "meshnet.broadcast_message",
        "meshnet.send_message",
        "meshnet_message_received",
        "Receive and log radio telemetry",
        "Home Assistant Recorder",
        "Online",
        "Last heard",
    ):
        assert required in readme


def test_published_automation_examples_use_real_targets_and_event_fields() -> None:
    """Public examples must not depend on rejected identities or absent fields."""
    examples = EXAMPLE_AUTOMATIONS.read_text(encoding="utf-8")

    assert "meshtastic:security" not in examples
    assert "trigger.event.data.priority" not in examples
    assert "trigger.event.data.message_type" not in examples
    assert "trigger.event.data.delivery" in examples
