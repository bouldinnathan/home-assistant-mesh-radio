"""Home Assistant-backed smoke tests, skipped by the lightweight local suite."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from homeassistant.helpers.selector import (  # noqa: E402
    SelectSelector,
    TextSelector,
)

from custom_components.meshnet import config_flow as config_flow_module  # noqa: E402
from custom_components.meshnet.bluetooth_devices import BluetoothDevice  # noqa: E402
from custom_components.meshnet.bluetooth_pairing import (  # noqa: E402
    BluetoothDeviceNotFoundError,
    InvalidPinError,
    PairingCleanupIncompleteError,
    PairingError,
    PairingOwnershipPendingError,
    PairingRateLimitedError,
    PairingResult,
    PairingTimeoutError,
    ProvisionalBond,
)
from custom_components.meshnet.config_flow import (  # noqa: E402
    CONF_CONFIRM,
    CONF_GATEWAY,
    CONF_GATEWAYS_JSON,
    CONF_PAIRING_PIN,
    CONF_READY_TO_PAIR,
    CONF_REMOVE_BLUETOOTH_BOND,
    CONF_VERIFY_CONNECTION,
    BluetoothGuidedSetupRequiredError,
    BluetoothOwnershipChangeError,
    CannotConnectError,
    MeshNetConfigFlow,
    MeshNetOptionsFlow,
    _async_serial_field,
    _bluetooth_field,
    _gateway_schema,
    _pairing_error_key,
    _reconcile_advanced_bluetooth_ownership,
    _require_guided_setup_for_new_bluetooth,
    _serial_access,
    _serial_field,
    _strip_untrusted_bluetooth_ownership,
    _ui_transports,
    _validate_unique_gateways,
    async_validate_connection,
)
from custom_components.meshnet.const import (  # noqa: E402
    CONF_API_KEY,
    CONF_API_URL,
    CONF_BLE_ADDRESS,
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    CONF_BLUETOOTH_BOND_MANAGED,
    CONF_GATEWAYS,
    CONF_MQTT_TOPIC,
    CONF_SERIAL_PATH,
    CONF_TRANSPORT,
    DOMAIN,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_NATIVE,
    TRANSPORT_REST,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)
from custom_components.meshnet.serial_devices import SerialDevice  # noqa: E402

ADAPTER_ADDRESS = "00:11:22:33:44:55"


def test_supported_home_assistant_has_entry_owned_background_tasks() -> None:
    """The declared HA floor must support non-blocking transport startup."""
    from homeassistant.config_entries import ConfigEntry

    assert callable(getattr(ConfigEntry, "async_create_background_task", None))


def _keys(schema) -> set[str]:
    return {marker.schema for marker in schema.schema}


def _field(schema, key: str):
    return next(
        (marker, validator)
        for marker, validator in schema.schema.items()
        if marker.schema == key
    )


@pytest.mark.parametrize(
    ("protocol", "transport", "expected", "excluded"),
    [
        (
            PROTOCOL_MESHTASTIC,
            TRANSPORT_TCP,
            {"name", "host", "port", CONF_VERIFY_CONNECTION},
            {CONF_SERIAL_PATH, CONF_BLE_ADDRESS, CONF_MQTT_TOPIC, CONF_API_URL},
        ),
        (
            PROTOCOL_MESHTASTIC,
            TRANSPORT_SERIAL,
            {"name", CONF_SERIAL_PATH, CONF_VERIFY_CONNECTION},
            {"host", CONF_BLE_ADDRESS, CONF_MQTT_TOPIC, CONF_API_URL},
        ),
        (
            PROTOCOL_MESHCORE,
            TRANSPORT_BLUETOOTH,
            {"name", CONF_BLE_ADDRESS, "pin", CONF_VERIFY_CONNECTION},
            {"host", CONF_SERIAL_PATH, CONF_MQTT_TOPIC, CONF_API_URL},
        ),
        (
            PROTOCOL_MESHTASTIC,
            TRANSPORT_MQTT,
            {
                "name",
                CONF_MQTT_TOPIC,
                "publish_topic",
                "mqtt_node_id",
                CONF_VERIFY_CONNECTION,
            },
            {"host", CONF_SERIAL_PATH, CONF_BLE_ADDRESS, CONF_API_URL},
        ),
        (
            PROTOCOL_MESHCORE,
            TRANSPORT_REST,
            {
                "name",
                CONF_API_URL,
                CONF_API_KEY,
                "send_url",
                CONF_VERIFY_CONNECTION,
            },
            {"host", CONF_SERIAL_PATH, CONF_BLE_ADDRESS, CONF_MQTT_TOPIC},
        ),
    ],
)
def test_gateway_forms_only_show_relevant_fields(
    protocol: str,
    transport: str,
    expected: set[str],
    excluded: set[str],
) -> None:
    keys = _keys(_gateway_schema(protocol, transport))

    assert expected <= keys
    assert not (excluded & keys)


def test_legacy_native_alias_is_not_offered_as_a_separate_method() -> None:
    assert TRANSPORT_NATIVE not in _ui_transports(PROTOCOL_MESHCORE)


def test_options_flow_uses_modern_home_assistant_owned_config_entry() -> None:
    # HA 2024.11+ injects config_entry after construction. Passing it manually
    # became invalid on newer releases, so this constructor must stay argument-free.
    assert isinstance(MeshNetOptionsFlow(), MeshNetOptionsFlow)


def test_serial_picker_lists_devices_and_accepts_advanced_path() -> None:
    devices = [
        SerialDevice("/dev/serial/by-id/usb-A", "Radio A — /dev/serial/by-id/usb-A"),
        SerialDevice("/dev/serial/by-id/usb-B", "Radio B — /dev/serial/by-id/usb-B"),
    ]

    field, default = _serial_field(devices)

    assert isinstance(field, SelectSelector)
    assert field.config["custom_value"] is True
    assert [option["value"] for option in field.config["options"]] == [
        device.path for device in devices
    ]
    assert default == devices[0].path
    assert field("/dev/serial/by-id/usb-manual") == "/dev/serial/by-id/usb-manual"


def test_meshtastic_bluetooth_picker_hides_full_address_in_labels() -> None:
    field, default = _bluetooth_field(
        [BluetoothDevice("AA:BB:CC:DD:12:34", -48)]
    )

    assert isinstance(field, SelectSelector)
    assert field.config["custom_value"] is True
    assert field.config["options"][0]["value"] == "AA:BB:CC:DD:12:34"
    assert "AA:BB:CC:DD:12:34" not in field.config["options"][0]["label"]
    assert default == "AA:BB:CC:DD:12:34"


def test_meshtastic_bluetooth_picker_keeps_advanced_manual_mac() -> None:
    field, default = _bluetooth_field([])

    assert isinstance(field, TextSelector)
    assert default is None
    assert field("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"


def test_serial_picker_retains_disconnected_configured_path() -> None:
    current = "/dev/serial/by-id/usb-currently-unplugged"

    field, default = _serial_field(
        [SerialDevice("/dev/ttyUSB0", "USB serial device — /dev/ttyUSB0")],
        current=current,
    )

    assert isinstance(field, SelectSelector)
    assert field.config["options"][0]["value"] == current
    assert "not detected" in field.config["options"][0]["label"]
    assert default == current


def test_serial_picker_without_devices_still_allows_manual_path() -> None:
    field, default = _serial_field([])

    assert isinstance(field, TextSelector)
    assert default is None
    assert field("/dev/serial/by-id/usb-manual") == "/dev/serial/by-id/usb-manual"


def test_serial_schema_uses_picker_and_detected_default() -> None:
    picker, default = _serial_field(
        [SerialDevice("/dev/ttyACM7", "USB serial device — /dev/ttyACM7")]
    )
    schema = _gateway_schema(
        PROTOCOL_MESHTASTIC,
        TRANSPORT_SERIAL,
        serial_field=picker,
        serial_default=default,
    )

    marker, field = _field(schema, CONF_SERIAL_PATH)
    assert field is picker
    assert marker.default() == "/dev/ttyACM7"


def test_home_assistant_scans_local_serial_ports_in_executor() -> None:
    device = SerialDevice("/dev/ttyUSB3", "USB serial device — /dev/ttyUSB3")
    hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=[device]))

    field, default = asyncio.run(
        _async_serial_field(hass, transport=TRANSPORT_SERIAL)
    )

    assert isinstance(field, SelectSelector)
    assert default == device.path
    hass.async_add_executor_job.assert_awaited_once()


def test_serial_validation_uses_exact_selected_path_in_executor() -> None:
    path = "/dev/serial/by-id/usb-RAKwireless_RAK4631_ABC-if00"
    hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(return_value=(True, True))
    )

    asyncio.run(
        async_validate_connection(
            hass,
            {CONF_TRANSPORT: TRANSPORT_SERIAL, CONF_SERIAL_PATH: path},
        )
    )

    hass.async_add_executor_job.assert_awaited_once_with(_serial_access, path)


@pytest.mark.parametrize("access", [(False, False), (True, False), (False, True)])
def test_serial_validation_rejects_missing_read_or_write_access(access) -> None:
    hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=access))

    with pytest.raises(CannotConnectError):
        asyncio.run(
            async_validate_connection(
                hass,
                {
                    CONF_TRANSPORT: TRANSPORT_SERIAL,
                    CONF_SERIAL_PATH: "/dev/ttyUSB0",
                },
            )
        )


def test_gateway_step_keeps_manual_path_after_failed_connection(
    monkeypatch,
) -> None:
    manual_path = "/dev/serial/by-id/usb-manual-radio"
    validator = AsyncMock(side_effect=CannotConnectError)
    monkeypatch.setattr(config_flow_module, "async_validate_connection", validator)
    flow = MeshNetConfigFlow()
    flow._transport = TRANSPORT_SERIAL
    flow.hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(return_value=[])
    )

    result = asyncio.run(
        flow.async_step_gateway(
            {
                "name": "Manual USB radio",
                CONF_SERIAL_PATH: manual_path,
                CONF_VERIFY_CONNECTION: True,
            }
        )
    )

    marker, _ = _field(result["data_schema"], CONF_SERIAL_PATH)
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "cannot_connect"}
    assert marker.default() == manual_path
    assert flow._gateways == []
    validator.assert_awaited_once()


def test_gateway_step_can_save_offline_manual_path(monkeypatch) -> None:
    manual_path = "/dev/serial/by-id/usb-offline-radio"
    validator = AsyncMock()
    monkeypatch.setattr(config_flow_module, "async_validate_connection", validator)
    flow = MeshNetConfigFlow()
    flow._transport = TRANSPORT_SERIAL
    flow.hass = SimpleNamespace()

    result = asyncio.run(
        flow.async_step_gateway(
            {
                "name": "Offline USB radio",
                CONF_SERIAL_PATH: manual_path,
                CONF_VERIFY_CONNECTION: False,
            }
        )
    )

    assert result["step_id"] == "more"
    assert flow._gateways[0][CONF_SERIAL_PATH] == manual_path
    validator.assert_not_awaited()


def test_options_add_can_save_offline_manual_path(monkeypatch) -> None:
    manual_path = "/dev/serial/by-id/usb-options-radio"
    validator = AsyncMock()
    monkeypatch.setattr(config_flow_module, "async_validate_connection", validator)
    saved: dict = {}
    flow = MeshNetOptionsFlow()
    flow._transport = TRANSPORT_SERIAL
    flow.hass = SimpleNamespace()
    monkeypatch.setattr(flow, "_gateways", lambda: [])
    monkeypatch.setattr(
        flow,
        "_save",
        lambda **updates: saved.update(updates) or {"type": "create_entry"},
    )

    result = asyncio.run(
        flow.async_step_add_details(
            {
                "name": "Options USB radio",
                CONF_SERIAL_PATH: manual_path,
                CONF_VERIFY_CONNECTION: False,
            }
        )
    )

    assert result["type"] == "create_entry"
    assert saved["gateways"][0][CONF_SERIAL_PATH] == manual_path
    validator.assert_not_awaited()


def test_meshtastic_bluetooth_requires_pairing_instead_of_offline_save() -> None:
    keys = _keys(_gateway_schema(PROTOCOL_MESHTASTIC, TRANSPORT_BLUETOOTH))

    assert {"name", CONF_BLE_ADDRESS} <= keys
    assert CONF_VERIFY_CONNECTION not in keys
    assert "pin" not in keys


class _FakePairingAttempt:
    def __init__(self, *, requires_pin: bool, result: PairingResult) -> None:
        self.requires_pin = requires_pin
        self.result = None if requires_pin else result
        self._result = result
        self.submitted_pin: str | None = None
        self.submit_count = 0
        self.cancelled = False
        self.provisional_bond: ProvisionalBond | None = None
        self.provisional_bonds: tuple[ProvisionalBond, ...] = ()
        self.retired_bonds: tuple[ProvisionalBond, ...] = ()

    async def async_submit_pin(self, pin: str) -> PairingResult:
        self.submit_count += 1
        self.submitted_pin = pin
        self.requires_pin = False
        self.result = self._result
        return self._result

    async def async_cancel(self) -> None:
        self.cancelled = True


class _FakePairingManager:
    def __init__(self, attempt: _FakePairingAttempt) -> None:
        self.attempt = attempt
        self.addresses: list[str] = []
        self.forgotten: list[str] = []
        self.released: list[tuple[str, str, str]] = []
        self.forget_error: PairingError | None = None
        self.begin_error: PairingError | None = None

    async def async_begin(self, address: str) -> _FakePairingAttempt:
        self.addresses.append(address)
        if self.begin_error is not None:
            raise self.begin_error
        return self.attempt

    async def async_forget_current_bond(
        self,
        address: str,
        *,
        adapter: str,
        adapter_address: str,
        user_confirmed: bool,
    ) -> None:
        assert user_confirmed is True
        if self.forget_error is not None:
            raise self.forget_error
        self.forgotten.append((adapter, adapter_address, address))

    def release_created(
        self,
        address: str,
        *,
        adapter: str,
        adapter_address: str,
    ) -> bool:
        self.released.append((adapter, adapter_address, address))
        return True


def _pairing_hass() -> SimpleNamespace:
    return SimpleNamespace(data={}, async_create_task=asyncio.create_task)


def test_meshtastic_pairing_without_pin_saves_only_verified_metadata(
    monkeypatch,
) -> None:
    async def run() -> None:
        pairing_result = PairingResult(
            address="AA:BB:CC:DD:EE:FF",
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            bond_created=True,
        )
        attempt = _FakePairingAttempt(
            requires_pin=False, result=pairing_result
        )
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow._protocol = PROTOCOL_MESHTASTIC
        flow._transport = TRANSPORT_BLUETOOTH
        flow.hass = _pairing_hass()

        intro = await flow.async_step_gateway(
            {"name": "BLE radio", CONF_BLE_ADDRESS: "aa:bb:cc:dd:ee:ff"}
        )
        assert intro["step_id"] == "pair_intro"

        progress = await flow.async_step_pair_intro(
            {CONF_READY_TO_PAIR: True}
        )
        assert progress["step_id"] == "pairing"
        assert flow._pairing_begin_task is not None
        await flow._pairing_begin_task
        done = await flow.async_step_pairing()
        assert done["step_id"] == "pair_finish"

        finished = await flow.async_step_pair_finish()
        assert finished["type"] == "create_entry"
        assert finished["data"]["gateways"] == flow._gateways
        assert flow._provisional_bonds == {
            ("hci0", ADAPTER_ADDRESS, pairing_result.address)
        }
        assert manager.addresses == ["AA:BB:CC:DD:EE:FF"]
        gateway = flow._gateways[0]
        assert gateway[CONF_BLE_ADDRESS] == pairing_result.address
        assert gateway["options"] == {
            CONF_BLUETOOTH_ADAPTER: "hci0",
            CONF_BLUETOOTH_ADAPTER_ADDRESS: ADAPTER_ADDRESS,
            CONF_BLUETOOTH_BOND_MANAGED: True,
        }
        assert "pin" not in repr(gateway).lower()
        flow.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [
                SimpleNamespace(data=finished["data"], options={})
            ]
            if domain == DOMAIN
            else []
        )
        flow.async_remove()
        await asyncio.sleep(0)
        assert manager.forgotten == []
        assert manager.released == [
            ("hci0", ADAPTER_ADDRESS, pairing_result.address)
        ]
        assert flow._provisional_bonds == set()

    asyncio.run(run())


def test_meshtastic_random_pin_is_masked_and_never_saved(monkeypatch) -> None:
    async def run() -> None:
        pairing_result = PairingResult(
            address="AA:BB:CC:DD:EE:01",
            adapter="hci1",
            adapter_address=ADAPTER_ADDRESS,
            bond_created=True,
        )
        attempt = _FakePairingAttempt(
            requires_pin=True, result=pairing_result
        )
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow._protocol = PROTOCOL_MESHTASTIC
        flow._transport = TRANSPORT_BLUETOOTH
        flow.hass = _pairing_hass()

        await flow.async_step_gateway(
            {"name": "Screen radio", CONF_BLE_ADDRESS: pairing_result.address}
        )
        await flow.async_step_pair_intro({CONF_READY_TO_PAIR: True})
        assert flow._pairing_begin_task is not None
        await flow._pairing_begin_task
        ready = await flow.async_step_pairing()
        assert ready["step_id"] == "pair_pin"

        pin_form = await flow.async_step_pair_pin()
        _marker, pin_selector = _field(pin_form["data_schema"], CONF_PAIRING_PIN)
        assert isinstance(pin_selector, TextSelector)
        assert pin_selector.config["type"] == "password"

        secret_input = {CONF_PAIRING_PIN: "000123"}
        progress = await flow.async_step_pair_pin(secret_input)
        assert progress["step_id"] == "pairing_submit"
        assert secret_input == {}
        assert flow._pairing_submit_task is not None
        await flow._pairing_submit_task
        verified = await flow.async_step_pairing_submit()
        assert verified["step_id"] == "pair_finish"
        finished = await flow.async_step_pair_finish()
        assert finished["type"] == "create_entry"
        assert flow._provisional_bonds == {
            ("hci1", ADAPTER_ADDRESS, pairing_result.address)
        }

        assert attempt.submitted_pin == "000123"
        serialized_gateway = repr(flow._gateways[0])
        assert "000123" not in serialized_gateway
        assert CONF_PAIRING_PIN not in serialized_gateway
        assert "pin" not in flow._gateways[0].get("options", {})

    asyncio.run(run())


def test_options_add_persists_verified_pairing_before_flow_removal(
    monkeypatch,
) -> None:
    async def run() -> None:
        pairing_result = PairingResult(
            "AA:BB:CC:DD:EE:09", "hci0", ADAPTER_ADDRESS, True
        )
        attempt = _FakePairingAttempt(
            requires_pin=False, result=pairing_result
        )
        manager = _FakePairingManager(attempt)
        saved: dict = {}
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetOptionsFlow()
        flow._protocol = PROTOCOL_MESHTASTIC
        flow._transport = TRANSPORT_BLUETOOTH
        flow.hass = _pairing_hass()
        monkeypatch.setattr(flow, "_gateways", lambda: [])
        monkeypatch.setattr(
            flow,
            "_save",
            lambda **updates: saved.update(updates)
            or {"type": "create_entry"},
        )

        intro = await flow.async_step_add_details(
            {"name": "Options BLE", CONF_BLE_ADDRESS: pairing_result.address}
        )
        assert intro["step_id"] == "pair_intro"
        await flow.async_step_pair_intro({CONF_READY_TO_PAIR: True})
        assert flow._pairing_begin_task is not None
        await flow._pairing_begin_task
        await flow.async_step_pairing()
        result = await flow.async_step_pair_finish()

        assert result["type"] == "create_entry"
        assert saved["gateways"][0]["options"] == {
            CONF_BLUETOOTH_ADAPTER: "hci0",
            CONF_BLUETOOTH_ADAPTER_ADDRESS: ADAPTER_ADDRESS,
            CONF_BLUETOOTH_BOND_MANAGED: True,
        }
        assert flow._provisional_bonds == {
            ("hci0", ADAPTER_ADDRESS, pairing_result.address)
        }
        flow.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [
                SimpleNamespace(
                    data={}, options={CONF_GATEWAYS: saved["gateways"]}
                )
            ]
            if domain == DOMAIN
            else []
        )
        flow.async_remove()
        await asyncio.sleep(0)
        assert manager.forgotten == []
        assert manager.released == [
            ("hci0", ADAPTER_ADDRESS, pairing_result.address)
        ]
        assert flow._provisional_bonds == set()

    asyncio.run(run())


def test_abandoned_successful_pairing_preserves_external_bond(monkeypatch) -> None:
    async def run() -> None:
        pairing_result = PairingResult(
            "AA:BB:CC:DD:EE:10", "hci0", ADAPTER_ADDRESS, True
        )
        attempt = _FakePairingAttempt(
            requires_pin=False, result=pairing_result
        )
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow._protocol = PROTOCOL_MESHTASTIC
        flow._transport = TRANSPORT_BLUETOOTH
        flow.hass = _pairing_hass()
        created_tasks: list[asyncio.Task] = []

        def create_task(coro):
            task = asyncio.create_task(coro)
            created_tasks.append(task)
            return task

        flow.hass.async_create_task = create_task

        await flow.async_step_gateway(
            {"name": "Abandoned radio", CONF_BLE_ADDRESS: pairing_result.address}
        )
        await flow.async_step_pair_intro({CONF_READY_TO_PAIR: True})
        assert flow._pairing_begin_task is not None
        await flow._pairing_begin_task
        await flow.async_step_pairing()

        flow.async_remove()
        await created_tasks[-1]

        assert attempt.cancelled is True
        assert manager.forgotten == []
        assert manager.released == [
            ("hci0", ADAPTER_ADDRESS, pairing_result.address)
        ]

    asyncio.run(run())


def test_failed_verification_and_rollback_releases_ambiguous_proof(
    monkeypatch,
) -> None:
    async def run() -> None:
        bond = ProvisionalBond(
            "AA:BB:CC:DD:EE:17", "hci0", ADAPTER_ADDRESS
        )
        attempt = _FakePairingAttempt(
            requires_pin=False,
            result=PairingResult(
                bond.address, bond.adapter, bond.adapter_address, False
            ),
        )
        manager = _FakePairingManager(attempt)
        manager.begin_error = PairingCleanupIncompleteError(bond)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow._protocol = PROTOCOL_MESHTASTIC
        flow._transport = TRANSPORT_BLUETOOTH
        flow.hass = _pairing_hass()

        await flow.async_step_gateway(
            {"name": "Rollback radio", CONF_BLE_ADDRESS: bond.address}
        )
        await flow.async_step_pair_intro({CONF_READY_TO_PAIR: True})
        assert flow._pairing_begin_task is not None
        with pytest.raises(PairingCleanupIncompleteError):
            await flow._pairing_begin_task

        result = await flow.async_step_pairing()
        assert result["step_id"] == "gateway"
        assert flow._provisional_bonds == {
            (bond.adapter, bond.adapter_address, bond.address)
        }

        await flow._async_cleanup_removed_pairing_flow()

        assert manager.forgotten == []
        assert manager.released == [
            (bond.adapter, bond.adapter_address, bond.address)
        ]
        assert flow._provisional_bonds == set()

    asyncio.run(run())


def test_busy_pairing_flow_never_inherits_other_flows_cleanup_authority(
    monkeypatch,
) -> None:
    async def run() -> None:
        address = "AA:BB:CC:DD:EE:18"
        attempt = _FakePairingAttempt(
            requires_pin=False,
            result=PairingResult(
                address, "hci0", ADAPTER_ADDRESS, False
            ),
        )
        manager = _FakePairingManager(attempt)
        manager.begin_error = PairingOwnershipPendingError(
            "another pairing flow is active"
        )
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow._protocol = PROTOCOL_MESHTASTIC
        flow._transport = TRANSPORT_BLUETOOTH
        flow.hass = _pairing_hass()

        await flow.async_step_gateway(
            {"name": "Busy radio", CONF_BLE_ADDRESS: address}
        )
        await flow.async_step_pair_intro({CONF_READY_TO_PAIR: True})
        assert flow._pairing_begin_task is not None
        with pytest.raises(PairingOwnershipPendingError):
            await flow._pairing_begin_task

        result = await flow.async_step_pairing()
        assert result["step_id"] == "gateway"
        assert flow._provisional_bonds == set()

        await flow._async_cleanup_removed_pairing_flow()
        assert manager.forgotten == []
        assert manager.released == []

    asyncio.run(run())


def test_unfinalized_create_entry_result_keeps_bond_provisional(
    monkeypatch,
) -> None:
    async def run() -> None:
        address = "AA:BB:CC:DD:EE:15"
        attempt = _FakePairingAttempt(
            requires_pin=False,
            result=PairingResult(address, "hci0", ADAPTER_ADDRESS, True),
        )
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow.hass = _pairing_hass()
        flow._provisional_bonds.add(("hci0", ADAPTER_ADDRESS, address))
        created_tasks: list[asyncio.Task] = []

        def create_task(coro):
            task = asyncio.create_task(coro)
            created_tasks.append(task)
            return task

        flow.hass.async_create_task = create_task

        flow.async_remove()
        await created_tasks[-1]

        assert manager.forgotten == []
        assert manager.released == [("hci0", ADAPTER_ADDRESS, address)]
        assert flow._provisional_bonds == set()

    asyncio.run(run())


def test_home_assistant_flow_manager_abort_preserves_external_bond(
    monkeypatch, tmp_path
) -> None:
    async def run() -> None:
        from homeassistant.config_entries import ConfigEntries
        from homeassistant.core import HomeAssistant

        result = PairingResult(
            "AA:BB:CC:DD:EE:14", "hci0", ADAPTER_ADDRESS, True
        )
        attempt = _FakePairingAttempt(requires_pin=False, result=result)
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        hass = HomeAssistant(str(tmp_path))
        config_entries = ConfigEntries(hass, {})
        hass.config_entries = config_entries
        flow = MeshNetConfigFlow()
        flow.hass = hass
        flow.handler = "meshnet"
        flow.flow_id = "meshnet_pairing_abort_test"
        flow.context = {"source": "user"}
        flow.init_data = None
        flow._pairing_attempt = attempt
        flow._pairing_result = result
        config_entries.flow._async_add_flow_progress(flow)

        config_entries.flow.async_abort(flow.flow_id)
        await hass.async_block_till_done()

        assert attempt.cancelled is True
        assert manager.forgotten == []
        assert manager.released == [
            ("hci0", ADAPTER_ADDRESS, result.address)
        ]

    asyncio.run(run())


def test_home_assistant_flow_manager_success_commits_before_cleanup(
    monkeypatch, tmp_path
) -> None:
    async def run() -> None:
        from homeassistant.config_entries import ConfigEntries
        from homeassistant.core import HomeAssistant

        pairing_result = PairingResult(
            "AA:BB:CC:DD:EE:16", "hci0", ADAPTER_ADDRESS, True
        )
        attempt = _FakePairingAttempt(
            requires_pin=False, result=pairing_result
        )
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        hass = HomeAssistant(str(tmp_path))
        config_entries = ConfigEntries(hass, {})
        hass.config_entries = config_entries
        monkeypatch.setattr(
            config_entries, "async_setup", AsyncMock(return_value=True)
        )
        flow = MeshNetConfigFlow()
        flow.hass = hass
        flow.handler = DOMAIN
        flow.flow_id = "meshnet_pairing_success_test"
        flow.context = {"source": "user"}
        flow.init_data = None
        flow._pairing_gateway = {
            "gateway_id": "new_radio",
            "name": "New radio",
            "protocol": PROTOCOL_MESHTASTIC,
            "transport": TRANSPORT_BLUETOOTH,
            CONF_BLE_ADDRESS: pairing_result.address,
        }
        flow._pairing_result = pairing_result
        flow.cur_step = flow.async_show_progress_done(
            next_step_id="pair_finish"
        )
        config_entries.flow._async_add_flow_progress(flow)

        result = await config_entries.flow.async_configure(flow.flow_id)
        await hass.async_block_till_done()

        assert result["type"] == "create_entry"
        assert len(config_entries.async_entries(DOMAIN)) == 1
        assert manager.forgotten == []
        assert manager.released == [
            ("hci0", ADAPTER_ADDRESS, pairing_result.address)
        ]
        stored = config_entries.async_entries(DOMAIN)[0].data[CONF_GATEWAYS][0]
        assert stored["options"] == {
            CONF_BLUETOOTH_ADAPTER: "hci0",
            CONF_BLUETOOTH_ADAPTER_ADDRESS: ADAPTER_ADDRESS,
            CONF_BLUETOOTH_BOND_MANAGED: True,
        }

    asyncio.run(run())


def test_home_assistant_flow_manager_finishes_after_pin_progress_poll(
    monkeypatch, tmp_path
) -> None:
    """Reproduce the frontend PIN submit and progress-poll transition."""

    async def run() -> None:
        from homeassistant.config_entries import ConfigEntries
        from homeassistant.core import HomeAssistant

        pairing_result = PairingResult(
            "AA:BB:CC:DD:EE:17", "hci0", ADAPTER_ADDRESS, True
        )

        class DeferredPinAttempt(_FakePairingAttempt):
            def __init__(self) -> None:
                super().__init__(requires_pin=True, result=pairing_result)
                self.pin_received = asyncio.Event()
                self.release_result = asyncio.Event()

            async def async_submit_pin(self, pin: str) -> PairingResult:
                self.submit_count += 1
                self.submitted_pin = pin
                self.pin_received.set()
                await self.release_result.wait()
                self.requires_pin = False
                self.result = self._result
                return self._result

        attempt = DeferredPinAttempt()
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        hass = HomeAssistant(str(tmp_path))
        config_entries = ConfigEntries(hass, {})
        hass.config_entries = config_entries
        monkeypatch.setattr(
            config_entries, "async_setup", AsyncMock(return_value=True)
        )
        flow = MeshNetConfigFlow()
        flow.hass = hass
        flow.handler = DOMAIN
        flow.flow_id = "meshnet_pin_progress_poll_test"
        flow.context = {"source": "user"}
        flow.init_data = None
        flow._pairing_gateway = {
            "gateway_id": "screen_radio",
            "name": "Screen radio",
            "protocol": PROTOCOL_MESHTASTIC,
            "transport": TRANSPORT_BLUETOOTH,
            CONF_BLE_ADDRESS: pairing_result.address,
        }
        flow._pairing_return_step = "gateway"
        flow._pairing_attempt = attempt
        flow.cur_step = await flow.async_step_pair_pin()
        config_entries.flow._async_add_flow_progress(flow)

        progress = await config_entries.flow.async_configure(
            flow.flow_id, {CONF_PAIRING_PIN: "000123"}
        )
        assert progress["type"] == "progress"
        assert progress["step_id"] == "pairing_submit"
        await attempt.pin_received.wait()

        # This is the poll issued while the frontend displays
        # "Loading next step for MeshNet". It must remain bounded to the same
        # progress task and must not start a second PIN submission.
        pending = await config_entries.flow.async_configure(flow.flow_id)
        assert pending["type"] == "progress"
        assert pending["step_id"] == "pairing_submit"
        assert attempt.submit_count == 1

        # Home Assistant's progress-task callback must consume
        # SHOW_PROGRESS_DONE. The frontend then polls that next step without
        # resubmitting the secret, which must invoke pair_finish.
        attempt.release_result.set()
        await hass.async_block_till_done()

        assert flow.cur_step is not None
        assert flow.cur_step["type"] == "progress_done"
        assert flow.cur_step["step_id"] == "pair_finish"
        finished = await config_entries.flow.async_configure(flow.flow_id)
        await hass.async_block_till_done()

        assert finished["type"] == "create_entry"
        assert len(config_entries.async_entries(DOMAIN)) == 1
        assert config_entries.flow.async_progress() == []
        assert attempt.submitted_pin == "000123"
        assert manager.released == [
            ("hci0", ADAPTER_ADDRESS, pairing_result.address)
        ]
        stored = config_entries.async_entries(DOMAIN)[0].data[CONF_GATEWAYS][0]
        assert CONF_PAIRING_PIN not in repr(stored)
        assert stored["options"] == {
            CONF_BLUETOOTH_ADAPTER: "hci0",
            CONF_BLUETOOTH_ADAPTER_ADDRESS: ADAPTER_ADDRESS,
            CONF_BLUETOOTH_BOND_MANAGED: True,
        }

    asyncio.run(run())


def test_preexisting_bond_is_not_removed_when_pairing_flow_is_abandoned(
    monkeypatch,
) -> None:
    async def run() -> None:
        pairing_result = PairingResult(
            "AA:BB:CC:DD:EE:11", "hci0", ADAPTER_ADDRESS, False
        )
        attempt = _FakePairingAttempt(
            requires_pin=False, result=pairing_result
        )
        manager = _FakePairingManager(attempt)
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow.hass = _pairing_hass()
        flow._pairing_attempt = attempt
        flow._pairing_result = pairing_result

        await flow._async_cleanup_removed_pairing_flow()

        assert manager.forgotten == []
        assert manager.released == []

    asyncio.run(run())


def test_duplicate_pairing_confirmation_reuses_one_backend_task(monkeypatch) -> None:
    async def run() -> None:
        pairing_result = PairingResult(
            "AA:BB:CC:DD:EE:12", "hci0", ADAPTER_ADDRESS, False
        )
        manager = _FakePairingManager(
            _FakePairingAttempt(requires_pin=False, result=pairing_result)
        )
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )
        flow = MeshNetConfigFlow()
        flow.hass = _pairing_hass()
        flow._pairing_gateway = {CONF_BLE_ADDRESS: pairing_result.address}
        flow._pairing_return_step = "gateway"

        first = await flow.async_step_pair_intro({CONF_READY_TO_PAIR: True})
        second = await flow.async_step_pair_intro({CONF_READY_TO_PAIR: True})
        assert first["step_id"] == second["step_id"] == "pairing"
        assert flow._pairing_begin_task is not None
        await flow._pairing_begin_task

        assert manager.addresses == [pairing_result.address]

    asyncio.run(run())


def test_duplicate_pin_submission_reuses_first_secret_task() -> None:
    async def run() -> None:
        attempt = _FakePairingAttempt(
            requires_pin=True,
            result=PairingResult(
                "AA:BB:CC:DD:EE:13", "hci0", ADAPTER_ADDRESS, True
            ),
        )
        flow = MeshNetConfigFlow()
        flow.hass = _pairing_hass()
        flow._pairing_attempt = attempt

        first_input = {CONF_PAIRING_PIN: "000123"}
        second_input = {CONF_PAIRING_PIN: "999999"}
        first = await flow.async_step_pair_pin(first_input)
        second = await flow.async_step_pair_pin(second_input)

        assert first["step_id"] == second["step_id"] == "pairing_submit"
        assert first_input == second_input == {}
        assert flow._pairing_submit_task is not None
        await flow._pairing_submit_task
        assert attempt.submit_count == 1
        assert attempt.submitted_pin == "000123"

    asyncio.run(run())


@pytest.mark.parametrize("pin", ["12345", "1234567", "１２３４５６", "12A456"])
def test_meshtastic_pairing_rejects_non_ascii_six_digit_pin(pin: str) -> None:
    async def run() -> None:
        attempt = _FakePairingAttempt(
            requires_pin=True,
            result=PairingResult(
                "AA:BB:CC:DD:EE:01", "hci0", ADAPTER_ADDRESS, True
            ),
        )
        flow = MeshNetConfigFlow()
        flow._pairing_attempt = attempt

        response = await flow.async_step_pair_pin({CONF_PAIRING_PIN: pin})

        assert response["errors"] == {CONF_PAIRING_PIN: "invalid_pin"}
        assert attempt.submitted_pin is None

    asyncio.run(run())


def test_pairing_errors_map_to_stable_non_secret_translations() -> None:
    assert (
        _pairing_error_key(PairingRateLimitedError())
        == "pairing_rate_limited"
    )
    assert _pairing_error_key(PairingTimeoutError()) == "pairing_timeout"
    assert (
        _pairing_error_key(BluetoothDeviceNotFoundError())
        == "local_adapter_required"
    )
    assert _pairing_error_key(InvalidPinError()) == "invalid_pin"
    assert (
        _pairing_error_key(PairingOwnershipPendingError())
        == "pairing_ownership_pending"
    )


def _owned_bluetooth_gateway(address: str = "AA:BB:CC:DD:EE:FF") -> dict:
    return {
        "gateway_id": "owned_radio",
        "name": "Owned radio",
        "protocol": PROTOCOL_MESHTASTIC,
        "transport": TRANSPORT_BLUETOOTH,
        CONF_BLE_ADDRESS: address,
        "options": {
            CONF_BLUETOOTH_ADAPTER: "hci0",
            CONF_BLUETOOTH_ADAPTER_ADDRESS: ADAPTER_ADDRESS,
            CONF_BLUETOOTH_BOND_MANAGED: True,
        },
    }


def test_duplicate_bluetooth_addresses_are_rejected_across_gateway_ids() -> None:
    first = _owned_bluetooth_gateway()
    second = {
        **first,
        "gateway_id": "same_radio_again",
        "name": "Same radio again",
        "options": {},
    }

    with pytest.raises(ValueError, match="Bluetooth addresses"):
        _validate_unique_gateways([first, second])


def test_advanced_editor_requires_guided_pairing_for_new_bluetooth() -> None:
    with pytest.raises(BluetoothGuidedSetupRequiredError):
        _require_guided_setup_for_new_bluetooth(
            [], [_owned_bluetooth_gateway()]
        )


@pytest.mark.parametrize(
    ("managed", "remove_bond", "expected_forget"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_options_remove_gateway_respects_owned_bond_boundary(
    monkeypatch, managed: bool, remove_bond: bool, expected_forget: bool
) -> None:
    async def run() -> None:
        gateway = _owned_bluetooth_gateway()
        if not managed:
            gateway["options"].pop(CONF_BLUETOOTH_BOND_MANAGED)
        attempt = _FakePairingAttempt(
            requires_pin=False,
            result=PairingResult(
                gateway[CONF_BLE_ADDRESS], "hci0", ADAPTER_ADDRESS, False
            ),
        )
        manager = _FakePairingManager(attempt)
        saved: dict = {}
        flow = MeshNetOptionsFlow()
        flow.hass = _pairing_hass()
        monkeypatch.setattr(flow, "_gateways", lambda: [gateway])
        monkeypatch.setattr(
            flow,
            "_save",
            lambda **updates: saved.update(updates)
            or {"type": "create_entry"},
        )
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )

        form = await flow.async_step_remove_gateway()
        marker, _validator = _field(
            form["data_schema"], CONF_REMOVE_BLUETOOTH_BOND
        )
        assert marker.default() is False

        result = await flow.async_step_remove_gateway(
            {
                CONF_GATEWAY: gateway["gateway_id"],
                CONF_CONFIRM: True,
                CONF_REMOVE_BLUETOOTH_BOND: remove_bond,
            }
        )

        assert result["type"] == "create_entry"
        assert saved["gateways"] == []
        assert bool(manager.forgotten) is expected_forget
        assert bool(manager.released) is (managed and not remove_bond)

    asyncio.run(run())


def test_options_cleanup_failure_keeps_gateway_for_safe_retry(monkeypatch) -> None:
    async def run() -> None:
        gateway = _owned_bluetooth_gateway()
        attempt = _FakePairingAttempt(
            requires_pin=False,
            result=PairingResult(
                gateway[CONF_BLE_ADDRESS], "hci0", ADAPTER_ADDRESS, False
            ),
        )
        manager = _FakePairingManager(attempt)
        manager.forget_error = PairingError("simulated safe failure")
        saved: dict = {}
        flow = MeshNetOptionsFlow()
        flow.hass = _pairing_hass()
        monkeypatch.setattr(flow, "_gateways", lambda: [gateway])
        monkeypatch.setattr(
            flow,
            "_save",
            lambda **updates: saved.update(updates)
            or {"type": "create_entry"},
        )
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: manager,
        )

        result = await flow.async_step_remove_gateway(
            {
                CONF_GATEWAY: gateway["gateway_id"],
                CONF_CONFIRM: True,
                CONF_REMOVE_BLUETOOTH_BOND: True,
            }
        )

        assert result["type"] == "form"
        assert result["errors"] == {"base": "bluetooth_cleanup_failed"}
        assert saved == {}

    asyncio.run(run())


def test_yaml_cannot_claim_ownership_of_a_preexisting_bond() -> None:
    cleaned = _strip_untrusted_bluetooth_ownership(
        _owned_bluetooth_gateway()
    )

    assert "options" not in cleaned


def test_advanced_editor_preserves_real_owned_bond_metadata() -> None:
    current = _owned_bluetooth_gateway()
    proposed = {**current, "name": "Renamed radio", "options": {}}

    reconciled = _reconcile_advanced_bluetooth_ownership(
        [current], [proposed]
    )

    assert reconciled[0]["name"] == "Renamed radio"
    assert reconciled[0]["options"] == current["options"]


@pytest.mark.parametrize("mutation", ["remove", "address", "forge"])
def test_advanced_editor_cannot_orphan_or_forge_owned_bond(mutation: str) -> None:
    current = _owned_bluetooth_gateway()
    if mutation == "remove":
        proposed = []
    elif mutation == "address":
        proposed = [
            {**current, CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:01"}
        ]
    else:
        proposed = [
            {
                **_owned_bluetooth_gateway("AA:BB:CC:DD:EE:01"),
                "gateway_id": "forged_radio",
            }
        ]

    with pytest.raises(BluetoothOwnershipChangeError):
        _reconcile_advanced_bluetooth_ownership([current], proposed)


def test_yaml_import_path_rejects_meshtastic_bluetooth() -> None:
    async def run() -> None:
        flow = MeshNetConfigFlow()

        result = await flow.async_step_import(
            {CONF_GATEWAYS: [_owned_bluetooth_gateway()]}
        )

        assert result["type"] == "abort"
        assert result["reason"] == "bluetooth_requires_gui"

    asyncio.run(run())


def test_advanced_flow_path_rejects_new_meshtastic_bluetooth(
    monkeypatch,
) -> None:
    async def run() -> None:
        flow = MeshNetOptionsFlow()
        flow.hass = _pairing_hass()
        monkeypatch.setattr(flow, "_gateways", lambda: [])

        result = await flow.async_step_advanced(
            {
                CONF_GATEWAYS_JSON: json.dumps(
                    [_owned_bluetooth_gateway()]
                )
            }
        )

        assert result["type"] == "form"
        assert result["errors"] == {
            CONF_GATEWAYS_JSON: "bluetooth_requires_gui"
        }

    asyncio.run(run())


def test_owned_same_address_edit_preserves_exact_bond_without_repairing(
    monkeypatch,
) -> None:
    async def run() -> None:
        gateway = _owned_bluetooth_gateway()
        saved: dict = {}
        flow = MeshNetOptionsFlow()
        flow.hass = _pairing_hass()
        flow._selected_gateway_id = gateway["gateway_id"]
        flow._protocol = PROTOCOL_MESHTASTIC
        flow._transport = TRANSPORT_BLUETOOTH
        monkeypatch.setattr(flow, "_gateways", lambda: [gateway])
        monkeypatch.setattr(
            flow,
            "_save",
            lambda **updates: saved.update(updates)
            or {"type": "create_entry"},
        )
        monkeypatch.setattr(
            config_flow_module,
            "_async_pairing_manager",
            lambda _hass: pytest.fail("owned edit tried to pair again"),
        )

        result = await flow.async_step_edit_details(
            {
                "name": "Renamed owned radio",
                CONF_BLE_ADDRESS: gateway[CONF_BLE_ADDRESS],
            }
        )

        assert result["type"] == "create_entry"
        edited = saved[CONF_GATEWAYS][0]
        assert edited["name"] == "Renamed owned radio"
        assert edited["options"] == gateway["options"]

    asyncio.run(run())
