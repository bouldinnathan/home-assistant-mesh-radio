from __future__ import annotations

import pytest

from custom_components.meshnet.config_helpers import (
    DEFAULT_TCP_PORTS,
    gateway_from_form,
    supported_transports,
    validate_gateway_dict,
)
from custom_components.meshnet.const import (
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_MQTT,
    TRANSPORT_REST,
    TRANSPORT_TCP,
)


def test_supported_transports_are_filtered_by_protocol() -> None:
    assert TRANSPORT_REST not in supported_transports(PROTOCOL_MESHTASTIC)
    assert TRANSPORT_REST in supported_transports(PROTOCOL_MESHCORE)


def test_tcp_form_applies_protocol_default_port_and_stable_shape() -> None:
    gateway = gateway_from_form(
        PROTOCOL_MESHTASTIC,
        TRANSPORT_TCP,
        {"name": "Roof radio", "host": "radio.local"},
    )

    assert gateway["gateway_id"].startswith("roof_radio_")
    assert gateway["port"] == DEFAULT_TCP_PORTS[PROTOCOL_MESHTASTIC]
    assert gateway["host"] == "radio.local"


def test_meshcore_tcp_never_guesses_a_firmware_specific_port() -> None:
    with pytest.raises(ValueError, match="requires port"):
        gateway_from_form(
            PROTOCOL_MESHCORE,
            TRANSPORT_TCP,
            {"name": "MeshCore LAN", "host": "meshcore.local"},
        )


def test_mqtt_form_keeps_bridge_send_settings_in_options() -> None:
    gateway = gateway_from_form(
        PROTOCOL_MESHTASTIC,
        TRANSPORT_MQTT,
        {
            "name": "MQTT gateway",
            "mqtt_topic": "msh/US/2/json/#",
            "publish_topic": "msh/US/2/json/mqtt/",
            "mqtt_node_id": "305419896",
        },
    )

    assert gateway["mqtt_topic"] == "msh/US/2/json/#"
    assert gateway["options"] == {
        "publish_topic": "msh/US/2/json/mqtt/",
        "mqtt_node_id": "305419896",
    }


@pytest.mark.parametrize("port", [0, 65536, "not-a-number"])
def test_invalid_tcp_ports_are_rejected(port: object) -> None:
    with pytest.raises(ValueError):
        validate_gateway_dict(
            {
                "gateway_id": "bad",
                "name": "Bad",
                "protocol": PROTOCOL_MESHTASTIC,
                "transport": TRANSPORT_TCP,
                "host": "radio.local",
                "port": port,
            }
        )


def test_unsupported_protocol_transport_pair_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not support"):
        validate_gateway_dict(
            {
                "gateway_id": "bad",
                "name": "Bad",
                "protocol": PROTOCOL_MESHTASTIC,
                "transport": TRANSPORT_REST,
                "api_url": "http://radio.local/state",
            }
        )


def test_rest_requires_an_http_url() -> None:
    with pytest.raises(ValueError, match="http"):
        validate_gateway_dict(
            {
                "gateway_id": "bad",
                "name": "Bad",
                "protocol": PROTOCOL_MESHCORE,
                "transport": TRANSPORT_REST,
                "api_url": "radio.local/state",
            }
        )
