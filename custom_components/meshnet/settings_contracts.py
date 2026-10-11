"""Reviewed write contracts for Meshtastic radio settings.

Every Meshtastic setter replaces one complete protobuf section, so MeshNet only
writes a field after its semantics, bounds, and recovery behavior have been
reviewed.  This module is that review: one contract per field, shared by the
local gateway editor and the remote (over-the-mesh) editor.  A field that is
not listed here fails closed everywhere.  Security configuration, channel
keys, radio-parameter fields that can isolate a node, and hardware pin
assignments are deliberately absent.

The module imports nothing from the integration so the constants package and
the protocol client can both depend on it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

SCOPE_BOTH: Final = "both"
SCOPE_LOCAL: Final = "local"
SCOPE_REMOTE: Final = "remote"

POLICY_REMOTE: Final = "remote"

_CHANNEL_INDEX_RE = re.compile(r"^channel\.([0-9]+)\.")
_UINT32_MAX = 2**32 - 1


@dataclass(frozen=True, slots=True)
class WriteContract:
    """The reviewed write rules for one settings path."""

    scope: str = SCOPE_BOTH
    minimum: int | float | None = None
    maximum: int | float | None = None
    allow_zero: bool = False
    max_bytes: int | None = None
    allow_empty: bool = False
    warning: str | None = None
    local_reason: str | None = None
    remote_reason: str | None = None


def _c(**kwargs: object) -> WriteContract:
    return WriteContract(**kwargs)  # type: ignore[arg-type]


# Warning codes shown in a preview before the single confirmed write.
WARNING_ROLE = "role_change_alters_sleep_and_rebroadcast_behavior"
WARNING_REBROADCAST = "rebroadcast_mode_changes_how_this_node_relays_traffic"
WARNING_POWER_SAVING = "power_saving_sleeps_the_node_between_broadcasts"
WARNING_WIFI = "wifi_disables_bluetooth_on_esp32_nodes"
WARNING_BLUETOOTH_OFF = "disabling_bluetooth_removes_phone_access_until_re_enabled"
WARNING_DUTY_CYCLE = "overriding_duty_cycle_may_violate_regional_rules"
WARNING_RANGE_TEST = "range_test_sender_floods_the_mesh"
WARNING_OLED = "wrong_oled_type_blanks_the_screen"
WARNING_FIXED_POSITION = "fixed_position_keeps_the_last_known_position"
WARNING_CHANNEL = "channel_changes_alter_which_traffic_this_node_decodes"
WARNING_HOP_LIMIT = "hop_limit_changes_how_far_this_node_reaches"
WARNING_TX_POWER = "tx_power_changes_this_node_reach_and_regulatory_compliance"
WARNING_TX_DISABLED = "disabling_transmit_makes_the_gateway_listen_only"

# Explicit read-only reasons for fields that firmware offers but MeshNet will
# not write.  Anything not listed here or in WRITE_CONTRACTS uses the generic
# fail-closed reason.
REASON_RADIO_PARAMETERS = "radio_parameters_can_isolate_the_node_from_the_mesh"
REASON_SHUTDOWN = "can_shut_the_node_down_until_physically_woken"
REASON_HARDWARE_PIN = "hardware_pin_assignments_require_physical_access"
REASON_BITMASK = "bitmask_requires_a_dedicated_editor"
REASON_DEPRECATED = "deprecated_field_is_superseded_by_a_newer_setting"
REASON_CHANNEL_KEY = "channel_keys_require_local_access"
REASON_CHANNEL_ROLE = "channel_roles_require_the_official_app"
REASON_CHANNEL_IDENTITY = "channel_identity_is_derived_from_its_settings"
REASON_GPIO_ACCESS = "grants_arbitrary_gpio_access_over_the_mesh"
REASON_HAM = "licensed_ham_mode_requires_local_setup"
REASON_NETWORK_ADDRESS = "static_addresses_require_a_dedicated_editor"
REASON_LOCAL_ONLY = "setting_is_only_editable_on_the_physically_connected_gateway"
REASON_REMOTE_ONLY = "setting_can_disable_the_active_transport_of_this_gateway"
REASON_TX_DISABLED_REMOTE = "a_node_that_cannot_transmit_cannot_answer_over_the_mesh"
REASON_BLUETOOTH_PIN_REMOTE = "bluetooth_pin_is_set_with_local_access"
REASON_CREDENTIAL_REMOTE = "credentials_are_set_with_local_access"
REASON_PAIRING_MODE_LOCAL = "pairing_mode_change_can_break_the_active_bond"
REASON_POWER_SAVING_LOCAL = "power_saving_can_sleep_the_active_bluetooth_transport"
REASON_WIFI_LOCAL = "wifi_can_disable_the_active_bluetooth_transport"
REASON_GENERIC = "setting_requires_dedicated_semantic_validation"

_HARDWARE_PIN_FIELDS: Final = frozenset(
    {
        "button_gpio",
        "buzzer_gpio",
        "rx_gpio",
        "tx_gpio",
        "gps_en_gpio",
        "device_battery_ina_address",
        "rxd",
        "txd",
        "output",
        "output_vibra",
        "output_buzzer",
        "inputbroker_pin_a",
        "inputbroker_pin_b",
        "inputbroker_pin_press",
        "ptt_pin",
        "i2s_ws",
        "i2s_sd",
        "i2s_din",
        "i2s_sck",
        "monitor_pin",
    }
)

_EXPLICIT_READ_ONLY: Final[Mapping[str, str]] = {
    "owner.is_licensed": REASON_HAM,
    "config.device.is_managed": REASON_DEPRECATED,
    "config.position.gps_enabled": REASON_DEPRECATED,
    "config.position.gps_attempt_time": REASON_DEPRECATED,
    "config.position.position_flags": REASON_BITMASK,
    "config.power.on_battery_shutdown_after_secs": REASON_SHUTDOWN,
    "config.power.sds_secs": REASON_SHUTDOWN,
    "config.power.powermon_enables": REASON_BITMASK,
    "config.network.enabled_protocols": REASON_BITMASK,
    "config.network.ipv4_config.ip": REASON_NETWORK_ADDRESS,
    "config.network.ipv4_config.gateway": REASON_NETWORK_ADDRESS,
    "config.network.ipv4_config.subnet": REASON_NETWORK_ADDRESS,
    "config.network.ipv4_config.dns": REASON_NETWORK_ADDRESS,
    "config.lora.use_preset": REASON_RADIO_PARAMETERS,
    "config.lora.modem_preset": REASON_RADIO_PARAMETERS,
    "config.lora.bandwidth": REASON_RADIO_PARAMETERS,
    "config.lora.spread_factor": REASON_RADIO_PARAMETERS,
    "config.lora.coding_rate": REASON_RADIO_PARAMETERS,
    "config.lora.frequency_offset": REASON_RADIO_PARAMETERS,
    "config.lora.region": REASON_RADIO_PARAMETERS,
    "config.lora.channel_num": REASON_RADIO_PARAMETERS,
    "config.lora.override_frequency": REASON_RADIO_PARAMETERS,
    "config.lora.fem_lna_mode": REASON_HARDWARE_PIN,
    "config.lora.serial_hal_only": REASON_HARDWARE_PIN,
    "module.remote_hardware.allow_undefined_pin_access": REASON_GPIO_ACCESS,
    "channel.N.settings.psk": REASON_CHANNEL_KEY,
    "channel.N.settings.channel_num": REASON_CHANNEL_IDENTITY,
    "channel.N.settings.id": REASON_CHANNEL_IDENTITY,
    "channel.N.role": REASON_CHANNEL_ROLE,
}

WRITE_CONTRACTS: Final[Mapping[str, WriteContract]] = {
    # Owner. Byte limits match the firmware string buffers that the existing
    # planner already enforces.
    "owner.long_name": _c(max_bytes=40),
    "owner.short_name": _c(max_bytes=4),
    "owner.is_unmessagable": _c(),
    # Device.
    "config.device.role": _c(warning=WARNING_ROLE),
    "config.device.serial_enabled": _c(),
    "config.device.rebroadcast_mode": _c(warning=WARNING_REBROADCAST),
    "config.device.node_info_broadcast_secs": _c(minimum=3600, maximum=_UINT32_MAX),
    "config.device.double_tap_as_button_press": _c(),
    "config.device.disable_triple_click": _c(),
    "config.device.tzdef": _c(max_bytes=64, allow_empty=True),
    "config.device.led_heartbeat_disabled": _c(),
    "config.device.buzzer_mode": _c(),
    # Position. Zero means "firmware default" for every interval.
    "config.position.position_broadcast_secs": _c(minimum=32, maximum=_UINT32_MAX, allow_zero=True),
    "config.position.position_broadcast_smart_enabled": _c(),
    "config.position.fixed_position": _c(warning=WARNING_FIXED_POSITION),
    "config.position.gps_update_interval": _c(minimum=30, maximum=_UINT32_MAX, allow_zero=True),
    "config.position.broadcast_smart_minimum_distance": _c(minimum=0, maximum=_UINT32_MAX),
    "config.position.broadcast_smart_minimum_interval_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "config.position.gps_mode": _c(),
    # Power.
    "config.power.is_power_saving": _c(scope=SCOPE_REMOTE, warning=WARNING_POWER_SAVING),
    "config.power.adc_multiplier_override": _c(minimum=0, maximum=6),
    "config.power.wait_bluetooth_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "config.power.ls_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "config.power.min_wake_secs": _c(minimum=0, maximum=_UINT32_MAX),
    # Network.
    "config.network.wifi_enabled": _c(scope=SCOPE_REMOTE, warning=WARNING_WIFI),
    "config.network.wifi_ssid": _c(max_bytes=32, allow_empty=True),
    "config.network.wifi_psk": _c(
        scope=SCOPE_LOCAL,
        max_bytes=64,
        allow_empty=True,
        remote_reason=REASON_CREDENTIAL_REMOTE,
    ),
    "config.network.ntp_server": _c(max_bytes=32, allow_empty=True),
    "config.network.eth_enabled": _c(scope=SCOPE_REMOTE),
    "config.network.address_mode": _c(),
    "config.network.rsyslog_server": _c(max_bytes=32, allow_empty=True),
    "config.network.ipv6_enabled": _c(),
    # Display.
    "config.display.screen_on_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "config.display.gps_format": _c(),
    "config.display.auto_screen_carousel_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "config.display.compass_north_top": _c(),
    "config.display.flip_screen": _c(),
    "config.display.units": _c(),
    "config.display.oled": _c(warning=WARNING_OLED),
    "config.display.displaymode": _c(scope=SCOPE_REMOTE),
    "config.display.heading_bold": _c(),
    "config.display.wake_on_tap_or_motion": _c(),
    "config.display.compass_orientation": _c(),
    "config.display.use_12h_clock": _c(),
    "config.display.use_long_node_name": _c(),
    "config.display.enable_message_bubbles": _c(),
    # LoRa. Only fields that cannot move the node off the mesh's radio
    # parameters are writable; the rest are listed in _EXPLICIT_READ_ONLY.
    "config.lora.hop_limit": _c(minimum=1, maximum=7, warning=WARNING_HOP_LIMIT),
    "config.lora.tx_enabled": _c(
        scope=SCOPE_LOCAL,
        warning=WARNING_TX_DISABLED,
        remote_reason=REASON_TX_DISABLED_REMOTE,
    ),
    "config.lora.tx_power": _c(minimum=0, maximum=30, warning=WARNING_TX_POWER),
    "config.lora.override_duty_cycle": _c(warning=WARNING_DUTY_CYCLE),
    "config.lora.sx126x_rx_boosted_gain": _c(),
    "config.lora.pa_fan_disabled": _c(),
    "config.lora.ignore_mqtt": _c(),
    "config.lora.config_ok_to_mqtt": _c(),
    # Bluetooth. The local gateway keeps its PIN contract; a remote node's
    # radio can be toggled because the mesh link does not depend on it.
    "config.bluetooth.enabled": _c(scope=SCOPE_REMOTE, warning=WARNING_BLUETOOTH_OFF),
    "config.bluetooth.mode": _c(scope=SCOPE_REMOTE, local_reason=REASON_PAIRING_MODE_LOCAL),
    "config.bluetooth.fixed_pin": _c(scope=SCOPE_LOCAL, remote_reason=REASON_BLUETOOTH_PIN_REMOTE),
    # MQTT. Blocked over the local Bluetooth transport by the planner.
    "module.mqtt.enabled": _c(),
    "module.mqtt.address": _c(max_bytes=63, allow_empty=True),
    "module.mqtt.username": _c(
        scope=SCOPE_LOCAL,
        max_bytes=63,
        allow_empty=True,
        remote_reason=REASON_CREDENTIAL_REMOTE,
    ),
    "module.mqtt.password": _c(
        scope=SCOPE_LOCAL,
        max_bytes=31,
        allow_empty=True,
        remote_reason=REASON_CREDENTIAL_REMOTE,
    ),
    "module.mqtt.encryption_enabled": _c(),
    "module.mqtt.json_enabled": _c(),
    "module.mqtt.tls_enabled": _c(),
    "module.mqtt.root": _c(max_bytes=31, allow_empty=True),
    "module.mqtt.proxy_to_client_enabled": _c(),
    "module.mqtt.map_reporting_enabled": _c(),
    "module.mqtt.map_report_settings.publish_interval_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "module.mqtt.map_report_settings.position_precision": _c(minimum=0, maximum=32),
    "module.mqtt.map_report_settings.should_report_location": _c(),
    # Serial module. Blocked over the local Bluetooth transport by the planner.
    "module.serial.enabled": _c(),
    "module.serial.echo": _c(),
    "module.serial.baud": _c(),
    "module.serial.timeout": _c(minimum=0, maximum=_UINT32_MAX),
    "module.serial.mode": _c(),
    "module.serial.override_console_serial_port": _c(),
    # External notification.
    "module.external_notification.enabled": _c(),
    "module.external_notification.output_ms": _c(minimum=0, maximum=_UINT32_MAX),
    "module.external_notification.active": _c(),
    "module.external_notification.alert_message": _c(),
    "module.external_notification.alert_message_vibra": _c(),
    "module.external_notification.alert_message_buzzer": _c(),
    "module.external_notification.alert_bell": _c(),
    "module.external_notification.alert_bell_vibra": _c(),
    "module.external_notification.alert_bell_buzzer": _c(),
    "module.external_notification.use_pwm": _c(),
    "module.external_notification.nag_timeout": _c(minimum=0, maximum=_UINT32_MAX),
    "module.external_notification.use_i2s_as_buzzer": _c(),
    # Store and forward.
    "module.store_forward.enabled": _c(),
    "module.store_forward.heartbeat": _c(),
    "module.store_forward.records": _c(minimum=0, maximum=_UINT32_MAX),
    "module.store_forward.history_return_max": _c(minimum=0, maximum=_UINT32_MAX),
    "module.store_forward.history_return_window": _c(minimum=0, maximum=_UINT32_MAX),
    "module.store_forward.is_server": _c(),
    # Range test.
    "module.range_test.enabled": _c(),
    "module.range_test.sender": _c(minimum=0, maximum=_UINT32_MAX, warning=WARNING_RANGE_TEST),
    "module.range_test.save": _c(),
    "module.range_test.clear_on_reboot": _c(),
    # Telemetry.
    "module.telemetry.device_update_interval": _c(minimum=0, maximum=_UINT32_MAX),
    "module.telemetry.environment_update_interval": _c(minimum=0, maximum=_UINT32_MAX),
    "module.telemetry.environment_measurement_enabled": _c(),
    "module.telemetry.environment_screen_enabled": _c(),
    "module.telemetry.environment_display_fahrenheit": _c(),
    "module.telemetry.air_quality_enabled": _c(),
    "module.telemetry.air_quality_interval": _c(minimum=0, maximum=_UINT32_MAX),
    "module.telemetry.air_quality_screen_enabled": _c(),
    "module.telemetry.power_measurement_enabled": _c(),
    "module.telemetry.power_update_interval": _c(minimum=0, maximum=_UINT32_MAX),
    "module.telemetry.power_screen_enabled": _c(),
    "module.telemetry.health_measurement_enabled": _c(),
    "module.telemetry.health_update_interval": _c(minimum=0, maximum=_UINT32_MAX),
    "module.telemetry.health_screen_enabled": _c(),
    "module.telemetry.device_telemetry_enabled": _c(),
    # Canned messages.
    "module.canned_message.enabled": _c(),
    "module.canned_message.send_bell": _c(),
    "module.canned_message.allow_input_source": _c(max_bytes=15, allow_empty=True),
    "module.canned_message.rotary1_enabled": _c(),
    "module.canned_message.updown1_enabled": _c(),
    "module.canned_message.inputbroker_event_cw": _c(),
    "module.canned_message.inputbroker_event_ccw": _c(),
    "module.canned_message.inputbroker_event_press": _c(),
    # Audio.
    "module.audio.codec2_enabled": _c(),
    "module.audio.bitrate": _c(),
    # Remote hardware.
    "module.remote_hardware.enabled": _c(),
    # Neighbor info.
    "module.neighbor_info.enabled": _c(),
    "module.neighbor_info.update_interval": _c(minimum=0, maximum=_UINT32_MAX),
    "module.neighbor_info.transmit_over_lora": _c(),
    # Ambient lighting.
    "module.ambient_lighting.led_state": _c(),
    "module.ambient_lighting.current": _c(minimum=0, maximum=31),
    "module.ambient_lighting.red": _c(minimum=0, maximum=255),
    "module.ambient_lighting.green": _c(minimum=0, maximum=255),
    "module.ambient_lighting.blue": _c(minimum=0, maximum=255),
    # Detection sensor.
    "module.detection_sensor.enabled": _c(),
    "module.detection_sensor.minimum_broadcast_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "module.detection_sensor.state_broadcast_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "module.detection_sensor.send_bell": _c(),
    "module.detection_sensor.name": _c(max_bytes=19, allow_empty=True),
    "module.detection_sensor.detection_trigger_type": _c(),
    "module.detection_sensor.use_pullup": _c(),
    # Paxcounter.
    "module.paxcounter.enabled": _c(),
    "module.paxcounter.paxcounter_update_interval": _c(minimum=0, maximum=_UINT32_MAX),
    "module.paxcounter.wifi_threshold": _c(minimum=-128, maximum=127),
    "module.paxcounter.ble_threshold": _c(minimum=-128, maximum=127),
    # Status message.
    "module.statusmessage.node_status": _c(max_bytes=79, allow_empty=True),
    # Traffic management.
    "module.traffic_management.enabled": _c(),
    "module.traffic_management.position_dedup_enabled": _c(),
    "module.traffic_management.position_precision_bits": _c(minimum=0, maximum=32),
    "module.traffic_management.position_min_interval_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "module.traffic_management.nodeinfo_direct_response": _c(),
    "module.traffic_management.nodeinfo_direct_response_max_hops": _c(minimum=0, maximum=7),
    "module.traffic_management.rate_limit_enabled": _c(),
    "module.traffic_management.rate_limit_window_secs": _c(minimum=0, maximum=_UINT32_MAX),
    "module.traffic_management.rate_limit_max_packets": _c(minimum=0, maximum=_UINT32_MAX),
    "module.traffic_management.drop_unknown_enabled": _c(),
    "module.traffic_management.unknown_packet_threshold": _c(minimum=0, maximum=_UINT32_MAX),
    "module.traffic_management.exhaust_hop_telemetry": _c(),
    "module.traffic_management.exhaust_hop_position": _c(),
    "module.traffic_management.router_preserve_hops": _c(),
    # TAK.
    "module.tak.team": _c(),
    "module.tak.role": _c(),
    # Channels. The index is a wildcard; keys are never written.
    "channel.N.settings.name": _c(max_bytes=11, allow_empty=True, warning=WARNING_CHANNEL),
    "channel.N.settings.uplink_enabled": _c(),
    "channel.N.settings.downlink_enabled": _c(),
    "channel.N.settings.module_settings.position_precision": _c(minimum=0, maximum=32),
    "channel.N.settings.module_settings.is_muted": _c(),
}


def normalize_contract_path(path: str) -> str:
    """Collapse a channel index into the wildcard used by the contract table."""
    return _CHANNEL_INDEX_RE.sub("channel.N.", path, count=1)


def write_contract(path: str) -> WriteContract | None:
    """Return the reviewed contract for a path, or None when it fails closed."""
    if not isinstance(path, str):
        return None
    return WRITE_CONTRACTS.get(normalize_contract_path(path))


def write_allowed(path: str, *, policy: str) -> bool:
    """Return whether a contract permits writing under a transport policy."""
    contract = write_contract(path)
    if contract is None:
        return False
    if policy == POLICY_REMOTE:
        return contract.scope in {SCOPE_BOTH, SCOPE_REMOTE}
    return contract.scope in {SCOPE_BOTH, SCOPE_LOCAL}


def remote_write_allowed(path: str) -> bool:
    """Return whether a path may be written on a remote node over the mesh."""
    return write_allowed(path, policy=POLICY_REMOTE)


def explicit_read_only_reason(path: str, field_name: str, *, policy: str) -> str | None:
    """Return the reviewed reason a field is read-only, or None if writable."""
    normalized = normalize_contract_path(path)
    explicit = _EXPLICIT_READ_ONLY.get(normalized)
    if explicit is not None:
        return explicit
    if field_name in _HARDWARE_PIN_FIELDS:
        return REASON_HARDWARE_PIN
    contract = WRITE_CONTRACTS.get(normalized)
    if contract is None:
        return REASON_GENERIC
    if policy == POLICY_REMOTE:
        if contract.scope == SCOPE_LOCAL:
            return contract.remote_reason or REASON_LOCAL_ONLY
        return None
    if contract.scope == SCOPE_REMOTE:
        return contract.local_reason or REASON_REMOTE_ONLY
    return None


def preview_warning(path: str) -> str | None:
    """Return the review warning code attached to a path, if any."""
    contract = write_contract(path)
    return contract.warning if contract is not None else None


def section_of_path(path: str) -> str | None:
    """Return the loadable section key that owns a settings path."""
    if not isinstance(path, str):
        return None
    parts = path.split(".")
    if parts[0] == "owner" and len(parts) >= 2:
        return "owner"
    if parts[0] in {"config", "module", "channel"} and len(parts) >= 3:
        return f"{parts[0]}.{parts[1]}"
    return None


def _paths_for(policy: str) -> frozenset[str]:
    return frozenset(path for path in WRITE_CONTRACTS if write_allowed(path, policy=policy))


LOCAL_WRITABLE_PATHS: Final = _paths_for("bluetooth")
REMOTE_WRITABLE_PATHS: Final = _paths_for(POLICY_REMOTE)

# Remote sections that can be loaded one at a time over the mesh.  Security,
# session-key, and device-UI configuration are never requested.
REMOTE_CONFIG_SECTIONS: Final = (
    "device",
    "position",
    "power",
    "network",
    "display",
    "lora",
    "bluetooth",
)
REMOTE_MODULE_SECTIONS: Final[Mapping[str, str]] = {
    "mqtt": "MQTT_CONFIG",
    "serial": "SERIAL_CONFIG",
    "external_notification": "EXTNOTIF_CONFIG",
    "store_forward": "STOREFORWARD_CONFIG",
    "range_test": "RANGETEST_CONFIG",
    "telemetry": "TELEMETRY_CONFIG",
    "canned_message": "CANNEDMSG_CONFIG",
    "audio": "AUDIO_CONFIG",
    "remote_hardware": "REMOTEHARDWARE_CONFIG",
    "neighbor_info": "NEIGHBORINFO_CONFIG",
    "ambient_lighting": "AMBIENTLIGHTING_CONFIG",
    "detection_sensor": "DETECTIONSENSOR_CONFIG",
    "paxcounter": "PAXCOUNTER_CONFIG",
    "statusmessage": "STATUSMESSAGE_CONFIG",
    "traffic_management": "TRAFFICMANAGEMENT_CONFIG",
    "tak": "TAK_CONFIG",
}
REMOTE_CHANNEL_COUNT: Final = 8
REMOTE_DEFAULT_SECTIONS: Final = ("owner", "config.device")

REMOTE_SECTION_RE: Final = re.compile(
    r"^(owner|metadata|config\.(?:"
    + "|".join(REMOTE_CONFIG_SECTIONS)
    + r")|module\.(?:"
    + "|".join(REMOTE_MODULE_SECTIONS)
    + r")|channel\.[0-7])$"
)


def remote_section_catalog() -> tuple[str, ...]:
    """Return every section key a remote load may request, in display order."""
    return (
        "owner",
        "metadata",
        *(f"config.{name}" for name in REMOTE_CONFIG_SECTIONS),
        *(f"module.{name}" for name in REMOTE_MODULE_SECTIONS),
        *(f"channel.{index}" for index in range(REMOTE_CHANNEL_COUNT)),
    )


def is_remote_section(value: object) -> bool:
    """Return whether a value names exactly one loadable remote section."""
    return isinstance(value, str) and REMOTE_SECTION_RE.fullmatch(value) is not None
