from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from custom_components.meshnet.meshtastic_ble import MeshtasticBluetoothTransport


def test_supported_home_assistant_exposes_per_scanner_resolver() -> None:
    """Keep the exact-controller safety boundary in the HA compatibility job."""
    bluetooth = pytest.importorskip("homeassistant.components.bluetooth")

    assert callable(getattr(bluetooth, "async_scanner_devices_by_address", None))


def _transport() -> MeshtasticBluetoothTransport:
    transport = object.__new__(MeshtasticBluetoothTransport)
    transport._hass = object()
    transport._address = "AA:BB:CC:DD:EE:FF"
    transport._adapter = "hci0"
    transport._adapter_address = "00:11:22:33:44:55"
    transport._resolution_attempts = 0
    transport._resolution_successes = 0
    transport._last_resolution_result = "not_started"
    return transport


def _candidate(
    *,
    adapter: str | None = "hci0",
    source: str | None = "00:11:22:33:44:55",
    path: str | None = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
    address: str = "AA:BB:CC:DD:EE:FF",
):
    scanner = SimpleNamespace(adapter=adapter, source=source)
    details = {} if path is None else {"path": path}
    device = SimpleNamespace(address=address, details=details)
    return SimpleNamespace(scanner=scanner, ble_device=device)


def _install_bluetooth_api(monkeypatch, candidates) -> None:
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    bluetooth.async_scanner_devices_by_address = (
        lambda _hass, _address, *, connectable: candidates if connectable else []
    )
    components = types.ModuleType("homeassistant.components")
    components.bluetooth = bluetooth
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.components = components
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.bluetooth",
        bluetooth,
    )


def test_resolver_returns_fresh_device_from_exact_local_adapter(monkeypatch) -> None:
    candidate = _candidate()
    _install_bluetooth_api(monkeypatch, [candidate])
    transport = _transport()

    assert transport._resolve_local_device() is candidate.ble_device
    assert transport._resolution_attempts == 1
    assert transport._resolution_successes == 1
    assert transport._last_resolution_result == "matched_verified_local_adapter"


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(adapter="hci1", source=None, path="/org/bluez/hci1/dev_x"),
        _candidate(adapter=None, source="00:11:22:33:44:66", path=None),
        _candidate(adapter=None, source=None, path=None),
        _candidate(address="AA:BB:CC:DD:EE:00"),
        # Contradictory stable identity rejects even when hci0 also appears.
        _candidate(adapter="hci0", source="00:11:22:33:44:66"),
    ],
)
def test_resolver_rejects_wrong_proxy_or_metadata_less_candidate(
    monkeypatch,
    candidate,
) -> None:
    _install_bluetooth_api(monkeypatch, [candidate])
    transport = _transport()

    assert transport._resolve_local_device() is None
    assert transport._resolution_successes == 0
    assert transport._last_resolution_result == "selected_adapter_not_visible"


def test_resolver_rejects_ambiguous_selected_adapter_candidates(monkeypatch) -> None:
    _install_bluetooth_api(monkeypatch, [_candidate(), _candidate()])
    transport = _transport()

    assert transport._resolve_local_device() is None
    assert transport._last_resolution_result == "selected_adapter_ambiguous"
