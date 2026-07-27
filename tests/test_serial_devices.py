"""Tests for USB serial-device discovery."""

from __future__ import annotations

from pathlib import Path

from custom_components.meshnet.serial_devices import discover_serial_devices


def _device(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_discovery_prefers_stable_paths_and_deduplicates_tty_aliases(
    tmp_path: Path,
) -> None:
    dev = tmp_path / "dev"
    tty_usb = _device(dev / "ttyUSB0")
    tty_acm = _device(dev / "ttyACM2")
    by_id = dev / "serial" / "by-id"
    by_id.mkdir(parents=True)
    stable = by_id / "usb-RAKwireless_RAK4631_ABC-if00"
    stable.symlink_to(tty_usb)
    (by_id / "usb-broken").symlink_to(dev / "missing")

    devices = discover_serial_devices(
        str(by_id),
        (str(dev / "ttyACM*"), str(dev / "ttyUSB*")),
    )

    assert [device.path for device in devices] == [str(stable), str(tty_acm)]
    assert "RAKwireless RAK4631 ABC-if00" in devices[0].label
    assert str(stable) in devices[0].label


def test_discovery_without_by_id_returns_every_tty_path_sorted(
    tmp_path: Path,
) -> None:
    dev = tmp_path / "dev"
    paths = [
        _device(dev / "ttyUSB2"),
        _device(dev / "ttyACM0"),
        _device(dev / "ttyUSB10"),
    ]

    devices = discover_serial_devices(
        str(dev / "missing-by-id"),
        (str(dev / "ttyACM*"), str(dev / "ttyUSB*")),
    )

    assert [device.path for device in devices] == sorted(
        (str(path) for path in paths), key=str.casefold
    )
    assert all(device.label.startswith("USB serial device — ") for device in devices)


def test_discovery_with_no_visible_devices_is_empty(tmp_path: Path) -> None:
    devices = discover_serial_devices(
        str(tmp_path / "missing-by-id"),
        (str(tmp_path / "ttyACM*"), str(tmp_path / "ttyUSB*")),
    )

    assert devices == []
