"""Home Assistant-backed smoke tests, skipped by the lightweight local suite."""

from __future__ import annotations

import asyncio
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
from custom_components.meshnet.config_flow import (  # noqa: E402
    CONF_VERIFY_CONNECTION,
    CannotConnectError,
    MeshNetConfigFlow,
    MeshNetOptionsFlow,
    _async_serial_field,
    _gateway_schema,
    _serial_access,
    _serial_field,
    _ui_transports,
    async_validate_connection,
)
from custom_components.meshnet.const import (  # noqa: E402
    CONF_API_KEY,
    CONF_API_URL,
    CONF_BLE_ADDRESS,
    CONF_MQTT_TOPIC,
    CONF_SERIAL_PATH,
    CONF_TRANSPORT,
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
