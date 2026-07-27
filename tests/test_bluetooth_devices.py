"""Tests for cached Meshtastic Bluetooth discovery."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from custom_components.meshnet.bluetooth_devices import (
    MESHTASTIC_SERVICE_UUID,
    BluetoothDevice,
    async_discover_meshtastic_devices,
    bluetooth_select_options,
    is_meshtastic_service_info,
    meshtastic_devices_from_service_info,
    normalize_bluetooth_address,
)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("aa:bb:0c:0d:ee:ff", "AA:BB:0C:0D:EE:FF"),
        (" AA:BB:0c:0d:ee:ff ", "AA:BB:0C:0D:EE:FF"),
    ],
)
def test_normalize_bluetooth_address(raw: str, canonical: str) -> None:
    assert normalize_bluetooth_address(raw) == canonical


@pytest.mark.parametrize(
    "address",
    [
        "AABBCCDDEEFF",
        "AA-BB-CC-DD-EE-FF",
        "AA:BB-CC:DD:EE:FF",
        "AA:BB:CC:DD:EE",
        "GG:BB:CC:DD:EE:FF",
        "00:00:00:00:00:00",
        "FF:FF:FF:FF:FF:FF",
        "not-a-device",
        "",
    ],
)
def test_normalize_bluetooth_address_rejects_noncanonical_input(
    address: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_bluetooth_address(address)


def test_meshtastic_filter_accepts_uuid_lists_and_service_data() -> None:
    upper = MESHTASTIC_SERVICE_UUID.upper()

    assert is_meshtastic_service_info(
        SimpleNamespace(service_uuids=[upper], service_data={})
    )
    assert is_meshtastic_service_info(
        SimpleNamespace(service_uuids=[], service_data={upper: b"advertisement"})
    )
    assert not is_meshtastic_service_info(
        SimpleNamespace(
            service_uuids=["0000180f-0000-1000-8000-00805f9b34fb"],
            service_data={},
        )
    )


def test_discovery_filters_deduplicates_and_sorts_by_signal() -> None:
    infos = [
        _service_info("AA:BB:CC:DD:EE:02", -80),
        _service_info("aa:bb:cc:dd:ee:01", -70),
        _service_info("AA:BB:CC:DD:EE:02", -40),
        _service_info(
            "AA:BB:CC:DD:EE:03",
            -20,
            service_uuid="0000180f-0000-1000-8000-00805f9b34fb",
        ),
        _service_info("invalid", -10),
    ]

    devices = meshtastic_devices_from_service_info(infos)

    assert devices == [
        BluetoothDevice("AA:BB:CC:DD:EE:02", -40),
        BluetoothDevice("AA:BB:CC:DD:EE:01", -70),
    ]


def test_labels_do_not_copy_advertised_names_or_scanner_sources() -> None:
    info = _service_info("AA:BB:CC:DD:12:34", -51)
    info.name = "A Person's Secret Radio Name"
    info.source = "private-proxy-identifier"

    (device,) = meshtastic_devices_from_service_info([info])

    assert device.label == "Meshtastic radio …:12:34 — -51 dBm"
    assert info.name not in device.label
    assert info.source not in device.label
    assert device.address not in device.label


def test_options_preserve_an_undiscovered_current_address() -> None:
    options, default = bluetooth_select_options(
        [BluetoothDevice("AA:BB:CC:DD:EE:02", -45)],
        current="aa:bb:cc:dd:ee:01",
    )

    assert options[0] == {
        "value": "AA:BB:CC:DD:EE:01",
        "label": "Currently configured (not detected) — radio …:EE:01",
    }
    assert options[1]["value"] == "AA:BB:CC:DD:EE:02"
    assert default == "AA:BB:CC:DD:EE:01"


def test_options_default_to_strongest_discovered_radio() -> None:
    options, default = bluetooth_select_options(
        [
            BluetoothDevice("AA:BB:CC:DD:EE:01", -75),
            BluetoothDevice("AA:BB:CC:DD:EE:02", -45),
        ]
    )

    assert [option["value"] for option in options] == [
        "AA:BB:CC:DD:EE:02",
        "AA:BB:CC:DD:EE:01",
    ]
    assert default == "AA:BB:CC:DD:EE:02"


def test_options_deduplicate_caller_records_using_strongest_signal() -> None:
    options, default = bluetooth_select_options(
        [
            BluetoothDevice("AA:BB:CC:DD:EE:01", -45),
            BluetoothDevice("AA:BB:CC:DD:EE:01", -85),
        ]
    )

    assert options == [
        {
            "value": "AA:BB:CC:DD:EE:01",
            "label": "Meshtastic radio …:EE:01 — -45 dBm",
        }
    ]
    assert default == "AA:BB:CC:DD:EE:01"


def test_async_discovery_uses_home_assistant_connectable_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_infos = [_service_info("AA:BB:CC:DD:EE:01", -55)]
    calls: list[tuple[object, bool]] = []
    bluetooth = ModuleType("homeassistant.components.bluetooth")

    def async_discovered_service_info(hass: object, *, connectable: bool):
        calls.append((hass, connectable))
        return service_infos

    bluetooth.async_discovered_service_info = async_discovered_service_info  # type: ignore[attr-defined]
    components = ModuleType("homeassistant.components")
    components.bluetooth = bluetooth  # type: ignore[attr-defined]
    homeassistant = ModuleType("homeassistant")
    homeassistant.components = components  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.bluetooth", bluetooth
    )
    hass = object()

    devices = asyncio.run(async_discover_meshtastic_devices(hass))

    assert devices == [BluetoothDevice("AA:BB:CC:DD:EE:01", -55)]
    assert calls == [(hass, True)]


def _service_info(
    address: str,
    rssi: int,
    *,
    service_uuid: str = MESHTASTIC_SERVICE_UUID,
) -> SimpleNamespace:
    return SimpleNamespace(
        address=address,
        rssi=rssi,
        service_uuids=[service_uuid],
        service_data={},
    )
