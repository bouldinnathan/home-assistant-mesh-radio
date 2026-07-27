"""Home Assistant-backed smoke tests, skipped by the lightweight local suite."""

from __future__ import annotations

import pytest


pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from custom_components.meshnet.config_flow import (  # noqa: E402
    CONF_VERIFY_CONNECTION,
    MeshNetOptionsFlow,
    _gateway_schema,
    _ui_transports,
)
from custom_components.meshnet.const import (  # noqa: E402
    CONF_API_KEY,
    CONF_API_URL,
    CONF_BLE_ADDRESS,
    CONF_MQTT_TOPIC,
    CONF_SERIAL_PATH,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_NATIVE,
    TRANSPORT_REST,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)


def _keys(schema) -> set[str]:
    return {marker.schema for marker in schema.schema}


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
