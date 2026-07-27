"""USB serial-device discovery helpers with no Home Assistant imports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from glob import glob

DEFAULT_BY_ID_DIRECTORY = "/dev/serial/by-id"
DEFAULT_FALLBACK_PATTERNS = ("/dev/ttyACM*", "/dev/ttyUSB*")


@dataclass(frozen=True, slots=True)
class SerialDevice:
    """A serial path and the user-facing label for it."""

    path: str
    label: str


def discover_serial_devices(
    by_id_directory: str = DEFAULT_BY_ID_DIRECTORY,
    fallback_patterns: tuple[str, ...] = DEFAULT_FALLBACK_PATTERNS,
) -> list[SerialDevice]:
    """Return stable USB serial paths first, followed by unmatched tty paths."""
    devices: list[SerialDevice] = []
    seen_targets: set[str] = set()

    try:
        names = sorted(os.listdir(by_id_directory), key=str.casefold)
    except OSError:
        names = []

    for name in names:
        path = os.path.join(by_id_directory, name)
        _append_device(devices, seen_targets, path, stable=True)

    fallback_paths = {
        path for pattern in fallback_patterns for path in glob(pattern)
    }
    for path in sorted(fallback_paths, key=str.casefold):
        _append_device(devices, seen_targets, path, stable=False)

    return devices


def _append_device(
    devices: list[SerialDevice],
    seen_targets: set[str],
    path: str,
    *,
    stable: bool,
) -> None:
    """Append one usable, non-duplicate device path."""
    if not os.path.exists(path) or os.path.isdir(path):
        return
    target = os.path.realpath(path)
    if target in seen_targets:
        return
    seen_targets.add(target)
    devices.append(SerialDevice(path=path, label=_device_label(path, stable=stable)))


def _device_label(path: str, *, stable: bool) -> str:
    """Build a readable label while retaining the exact stored path."""
    if not stable:
        return f"USB serial device — {path}"
    name = os.path.basename(path).removeprefix("usb-").replace("_", " ")
    return f"{name} — {path}"
