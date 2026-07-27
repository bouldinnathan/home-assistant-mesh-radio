"""Cached Meshtastic Bluetooth discovery helpers.

Discovery answers only whether Home Assistant has a recent advertisement for a
Meshtastic GATT service.  It deliberately does not claim that the advertisement
came from a local BlueZ adapter or that the device can be paired.  The pairing
backend must enforce those properties against BlueZ before requesting a PIN.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, TypedDict
from uuid import UUID

MESHTASTIC_SERVICE_UUID: Final = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"

_MAC_ADDRESS_RE: Final = re.compile(
    r"^(?P<a>[0-9A-Fa-f]{2}):"
    r"(?P<b>[0-9A-Fa-f]{2}):"
    r"(?P<c>[0-9A-Fa-f]{2}):"
    r"(?P<d>[0-9A-Fa-f]{2}):"
    r"(?P<e>[0-9A-Fa-f]{2}):"
    r"(?P<f>[0-9A-Fa-f]{2})$"
)
_INVALID_DEVICE_ADDRESSES: Final = {
    "00:00:00:00:00:00",
    "FF:FF:FF:FF:FF:FF",
}


class BluetoothSelectOption(TypedDict):
    """A Home Assistant selector-compatible option without an HA import."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class BluetoothDevice:
    """A discovered Meshtastic radio, identified only by its BLE address."""

    address: str
    rssi: int | None = None

    def __post_init__(self) -> None:
        """Keep every record canonical, including records made by callers."""
        object.__setattr__(self, "address", normalize_bluetooth_address(self.address))
        if self.rssi is not None and isinstance(self.rssi, bool):
            raise ValueError("Bluetooth RSSI must be an integer or None")
        if self.rssi is not None and not isinstance(self.rssi, int):
            raise ValueError("Bluetooth RSSI must be an integer or None")

    @property
    def label(self) -> str:
        """Return a useful label without exposing an advertised device name."""
        base = f"Meshtastic radio …:{self.address[-5:]}"
        if self.rssi is None:
            return base
        return f"{base} — {self.rssi} dBm"


def normalize_bluetooth_address(address: str) -> str:
    """Validate and return a canonical upper-case, colon-separated MAC.

    BlueZ device paths used by Home Assistant represent 48-bit Bluetooth
    addresses.  Platform-specific UUIDs, compact hexadecimal strings, and
    mixed separators are rejected rather than being guessed.
    """
    if not isinstance(address, str):
        raise ValueError("Bluetooth address must be a string")
    match = _MAC_ADDRESS_RE.fullmatch(address.strip())
    if match is None:
        raise ValueError("Bluetooth address must contain six hexadecimal octets")
    canonical = ":".join(match.group(name).upper() for name in "abcdef")
    if canonical in _INVALID_DEVICE_ADDRESSES:
        raise ValueError("Bluetooth address is not a device address")
    return canonical


def is_meshtastic_service_info(service_info: Any) -> bool:
    """Return whether cached discovery data advertises Meshtastic's service."""
    candidates: list[Any] = list(
        getattr(service_info, "service_uuids", None) or ()
    )
    service_data = getattr(service_info, "service_data", None)
    if isinstance(service_data, dict):
        candidates.extend(service_data)

    for candidate in candidates:
        try:
            if str(UUID(str(candidate))) == MESHTASTIC_SERVICE_UUID:
                return True
        except (AttributeError, TypeError, ValueError):
            continue
    return False


def meshtastic_devices_from_service_info(
    service_infos: Iterable[Any],
) -> list[BluetoothDevice]:
    """Filter, canonicalize, and deterministically deduplicate advertisements."""
    strongest_by_address: dict[str, BluetoothDevice] = {}
    for service_info in service_infos:
        if not is_meshtastic_service_info(service_info):
            continue
        try:
            address = normalize_bluetooth_address(service_info.address)
        except (AttributeError, ValueError):
            continue

        raw_rssi = getattr(service_info, "rssi", None)
        rssi = (
            raw_rssi
            if isinstance(raw_rssi, int) and not isinstance(raw_rssi, bool)
            else None
        )
        device = BluetoothDevice(address=address, rssi=rssi)
        existing = strongest_by_address.get(address)
        if existing is None or _rssi_rank(device.rssi) > _rssi_rank(existing.rssi):
            strongest_by_address[address] = device

    return sorted(
        strongest_by_address.values(),
        key=lambda device: (-_rssi_rank(device.rssi), device.address),
    )


async def async_discover_meshtastic_devices(hass: Any) -> list[BluetoothDevice]:
    """Collect Meshtastic radios from Home Assistant's cached BLE discoveries.

    ``connectable=True`` narrows the cache to advertisements exposed through a
    connectable scanner.  That scanner can still be a Bluetooth proxy, so this
    function's result must not be treated as proof of local pairing support.
    """
    from homeassistant.components import bluetooth

    service_infos = bluetooth.async_discovered_service_info(
        hass, connectable=True
    )
    return meshtastic_devices_from_service_info(service_infos)


def bluetooth_select_options(
    devices: Iterable[BluetoothDevice],
    *,
    current: str | None = None,
) -> tuple[list[BluetoothSelectOption], str | None]:
    """Return selector options and a default while retaining an absent current.

    The dictionaries are accepted by ``SelectOptionDict``.  The config flow is
    responsible for setting ``custom_value=True`` so an advanced manual MAC is
    always possible.
    """
    normalized_current = (
        normalize_bluetooth_address(current) if current is not None else None
    )
    deduplicated: dict[str, BluetoothDevice] = {}
    for device in devices:
        existing = deduplicated.get(device.address)
        if existing is None or _rssi_rank(device.rssi) > _rssi_rank(existing.rssi):
            deduplicated[device.address] = device
    ordered_devices = sorted(
        deduplicated.values(),
        key=lambda device: (-_rssi_rank(device.rssi), device.address),
    )
    options: list[BluetoothSelectOption] = [
        {"value": device.address, "label": device.label}
        for device in ordered_devices
    ]

    if normalized_current and normalized_current not in deduplicated:
        options.insert(
            0,
            {
                "value": normalized_current,
                "label": (
                    "Currently configured (not detected) — "
                    f"radio …:{normalized_current[-5:]}"
                ),
            },
        )

    default = normalized_current or (
        ordered_devices[0].address if ordered_devices else None
    )
    return options, default


def _rssi_rank(rssi: int | None) -> int:
    """Rank a missing RSSI below every valid Bluetooth RSSI."""
    return rssi if rssi is not None else -10_000
