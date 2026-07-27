"""Tests for Home Assistant compatibility helpers."""

from enum import StrEnum
from types import SimpleNamespace

from custom_components.meshnet.compat import percentage_unit


class FakeUnitOfRatio(StrEnum):
    """Stand in for the Home Assistant 2026.7+ ratio unit enum."""

    PERCENTAGE = "%"


def test_percentage_unit_uses_ratio_enum_when_available() -> None:
    """Current Home Assistant releases use UnitOfRatio for sensor units."""
    result = percentage_unit(
        SimpleNamespace(UnitOfRatio=FakeUnitOfRatio, PERCENTAGE="legacy")
    )

    assert result is FakeUnitOfRatio.PERCENTAGE


def test_percentage_unit_falls_back_for_older_home_assistant() -> None:
    """The declared Home Assistant 2025.1.4 floor still exposes PERCENTAGE."""
    result = percentage_unit(SimpleNamespace(PERCENTAGE="%"))

    assert result == "%"
