"""Constants for the MeshNet integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "meshnet"
NAME: Final = "MeshNet"
VERSION: Final = "0.5.5"
DATA_BLUETOOTH_PAIRING: Final = f"{DOMAIN}_bluetooth_pairing"

PLATFORMS: Final = ["sensor", "binary_sensor", "device_tracker"]

CONF_GATEWAYS: Final = "gateways"
CONF_GATEWAY_ID: Final = "gateway_id"
CONF_GATEWAY_NAME: Final = "gateway_name"
CONF_PROTOCOL: Final = "protocol"
CONF_TRANSPORT: Final = "transport"
CONF_SERIAL_PATH: Final = "serial_path"
CONF_BLE_ADDRESS: Final = "ble_address"
CONF_BLUETOOTH_ADAPTER: Final = "bluetooth_adapter"
CONF_BLUETOOTH_ADAPTER_ADDRESS: Final = "bluetooth_adapter_address"
CONF_BLUETOOTH_BOND_MANAGED: Final = "bluetooth_bond_managed"
CONF_MQTT_TOPIC: Final = "mqtt_topic"
CONF_API_URL: Final = "api_url"
CONF_API_KEY: Final = "api_key"
CONF_NODE_TIMEOUT: Final = "node_timeout"
CONF_HISTORY_DAYS: Final = "history_days"
CONF_PACKET_CAPTURE: Final = "packet_capture"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_NODE_TIMEOUT: Final = 900
DEFAULT_HISTORY_DAYS: Final = 30
DEFAULT_SCAN_INTERVAL: Final = 30
# Match Meshtastic's decoded JSON branch without subscribing to raw protobuf
# topics under ``msh/<region>/2/e/...``.
DEFAULT_MESHTASTIC_MQTT_TOPIC: Final = "msh/+/2/json/#"
DEFAULT_MESHCORE_MQTT_TOPIC: Final = "meshcore/+/+/packets"
DEFAULT_DATABASE_NAME: Final = "meshnet.sqlite3"

PROTOCOL_MESHTASTIC: Final = "meshtastic"
PROTOCOL_MESHCORE: Final = "meshcore"
PROTOCOLS: Final = [PROTOCOL_MESHTASTIC, PROTOCOL_MESHCORE]

TRANSPORT_SERIAL: Final = "serial"
TRANSPORT_TCP: Final = "tcp"
TRANSPORT_BLUETOOTH: Final = "bluetooth"
TRANSPORT_MQTT: Final = "mqtt"
TRANSPORT_REST: Final = "rest"
TRANSPORT_NATIVE: Final = "native"
TRANSPORTS: Final = [
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_REST,
    TRANSPORT_NATIVE,
]

MESSAGE_TYPE_DIRECT: Final = "direct"
MESSAGE_TYPE_GROUP: Final = "group"
MESSAGE_TYPE_BROADCAST: Final = "broadcast"
MESSAGE_TYPE_EMERGENCY: Final = "emergency"

ATTR_GATEWAY: Final = "gateway"
ATTR_GATEWAY_ID: Final = "gateway_id"
ATTR_NODE_KEY: Final = "node_key"
ATTR_TARGET_NODE: Final = "target_node"
ATTR_MESSAGE: Final = "message"
ATTR_CHANNEL: Final = "channel"
ATTR_PRIORITY: Final = "priority"
ATTR_MESSAGE_TYPE: Final = "message_type"
ATTR_WHEN: Final = "when"
ATTR_RAW: Final = "raw"

EVENT_PACKET: Final = "meshnet_packet"
EVENT_MESSAGE_RECEIVED: Final = "meshnet_message_received"
EVENT_MESSAGE_SENT: Final = "meshnet_message_sent"

STORAGE_SCHEMA_VERSION: Final = 1

DIAGNOSTIC_REDACT: Final = [
    CONF_API_KEY,
    "access_token",
    "address",
    "adapter_address",
    "api_url",
    "area_id",
    "authorization",
    "ble_address",
    "bluetooth_address",
    "bluetooth_adapter_address",
    "channel",
    "client_secret",
    "config_entry_id",
    "contacts",
    "credential",
    "credentials",
    "device_id",
    "device_ieee",
    "device_name",
    "email",
    "entry_id",
    "entity_id",
    "errors",
    "friendly_name",
    "from",
    "from_id",
    "gateway_id",
    "gateway_name",
    "host",
    "hostname",
    "id",
    "ieee",
    "ip_address",
    "key",
    "last_gateway_id",
    "latitude",
    "location",
    "long_name",
    "longitude",
    "mac",
    "message",
    "message_id",
    "mqtt_password",
    "mqtt_topic",
    "name",
    "network_key",
    "neighbors",
    "node_id",
    "node_key",
    "packet_id",
    "password",
    "passphrase",
    "path",
    "payload",
    "pin",
    "position",
    "private_key",
    "provider_id",
    "public_key",
    "raw",
    "receiver",
    "refresh_token",
    "sender",
    "send_url",
    "serial_path",
    "secret",
    "secret_key",
    "short_name",
    "ssid",
    "target_node",
    "text",
    "title",
    "to",
    "to_id",
    "token",
    "topic",
    "unique_id",
    "url",
    "user_name",
    "username",
    "wifi_password",
]
