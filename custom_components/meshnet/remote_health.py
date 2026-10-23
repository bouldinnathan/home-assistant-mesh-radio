"""Bounded firmware comparisons for explicitly maintained remote nodes."""

from __future__ import annotations

import re

_RELEASE = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_BUILD = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.[0-9a-fA-F]{6,40})?")


def approved_firmware(value: object) -> str:
    """Accept an optional explicit release baseline, never an implicit latest."""
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("minimum_firmware must be a release such as 2.7.11")
    value = value.strip()
    if value and _RELEASE.fullmatch(value) is None:
        raise ValueError("minimum_firmware must be a release such as 2.7.11")
    return value.removeprefix("v")


def firmware_status(installed: object, minimum: str) -> dict[str, str | None]:
    """Compare numeric releases; unrecognized/custom builds remain unknown."""
    minimum = approved_firmware(minimum)
    match = _BUILD.fullmatch(installed) if isinstance(installed, str) and len(installed) <= 80 else None
    version = installed if match else None
    status = "not_checked" if not minimum else "unknown"
    if minimum and match:
        baseline = _RELEASE.fullmatch(minimum)
        assert baseline is not None
        status = "outdated" if tuple(map(int, match.groups())) < tuple(map(int, baseline.groups())) else "current"
    return {"firmware_version": version, "minimum_firmware": minimum or None, "firmware_status": status}
