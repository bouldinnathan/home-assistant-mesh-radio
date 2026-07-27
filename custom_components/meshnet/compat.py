"""Compatibility helpers for supported Home Assistant releases."""

from __future__ import annotations

from typing import Any


def percentage_unit(homeassistant_const: Any) -> Any:
    """Return the percentage unit used by the installed Home Assistant version."""
    unit_of_ratio = getattr(homeassistant_const, "UnitOfRatio", None)
    if unit_of_ratio is not None:
        return unit_of_ratio("%")
    return homeassistant_const.PERCENTAGE
