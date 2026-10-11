"""Reviewed help text for every Meshtastic setting the panel can show.

Each entry explains what a setting does, its unit, and what a zero or empty
value means.  Bounds are not repeated here: the panel composes the allowed
range from the write contract so the two can never disagree.  Channel paths
use the ``channel.N.`` wildcard from :mod:`settings_contracts`.
"""

from __future__ import annotations

from typing import Final

from .settings_contracts import normalize_contract_path

HELP_MAX_LENGTH: Final = 512
UNIT_MAX_LENGTH: Final = 32

_FIRMWARE_DEFAULT = "0 keeps the firmware default"

SETTING_HELP: Final[dict[str, str]] = {
    # Owner.
    "owner.id": "The node's Meshtastic ID, derived from its hardware address. Shown for reference.",
    "owner.long_name": "The full name other nodes and apps show for this node. Up to 40 bytes of UTF-8.",
    "owner.short_name": (
        "The short name shown on small screens and in compact lists. Up to 4 bytes, "
        "so most emoji count as one character."
    ),
    "owner.macaddr": "The hardware address the firmware reports for this node.",
    "owner.hw_model": "The hardware model the firmware was built for. Reported by the firmware and not editable.",
    "owner.is_licensed": (
        "Licensed amateur-radio (HAM) mode. It disables encryption and uses the call sign as the "
        "name, so it is set up locally with the official app."
    ),
    "owner.role": "A copy of the device role kept in the owner record. Change the role under Device instead.",
    "owner.public_key": "The node's public key for encrypted direct messages and remote administration.",
    "owner.is_unmessagable": (
        "Marks this node as not accepting direct messages, so apps hide it from recipient lists. "
        "Useful for sensors and infrastructure nodes."
    ),
    # Device.
    "config.device.role": (
        "How the node behaves on the mesh: CLIENT for normal use, ROUTER or REPEATER for "
        "infrastructure, and the sleepy, tracker, and sensor roles for power saving. Routers and "
        "repeaters relay more traffic and sleep differently."
    ),
    "config.device.serial_enabled": (
        "Enables the serial console on the USB port. Turn it off to free the port for the Serial module."
    ),
    "config.device.button_gpio": "GPIO pin number of the user button. 0 uses the board default.",
    "config.device.buzzer_gpio": "GPIO pin number of the buzzer. 0 uses the board default.",
    "config.device.rebroadcast_mode": (
        "Which packets this node relays for others: ALL, ALL_SKIP_DECODING, LOCAL_ONLY (known "
        "channels), KNOWN_ONLY (known nodes), NONE, or CORE_PORTNUMS_ONLY."
    ),
    "config.device.node_info_broadcast_secs": (
        "Seconds between NodeInfo broadcasts that announce this node's name and key. The firmware "
        "minimum is 3600 (1 hour); the default is 10800 (3 hours)."
    ),
    "config.device.double_tap_as_button_press": (
        "Treats a double tap on the accelerometer as a button press on boards with motion sensing."
    ),
    "config.device.is_managed": "Deprecated. Managed mode now lives in the Security section.",
    "config.device.disable_triple_click": (
        "Disables the triple-click shortcut that toggles the GPS on nodes with a button."
    ),
    "config.device.tzdef": (
        "POSIX time-zone string for the on-screen clock, for example EST5EDT,M3.2.0,M11.1.0. "
        "Up to 64 bytes; empty keeps UTC."
    ),
    "config.device.led_heartbeat_disabled": (
        "Stops the status LED from blinking a heartbeat. Saves a little power and stray light."
    ),
    "config.device.buzzer_mode": (
        "What the buzzer is used for: all sounds, notifications only, system sounds only, or disabled."
    ),
    # Position.
    "config.position.position_broadcast_secs": (
        f"Seconds between position broadcasts. {_FIRMWARE_DEFAULT} of 900 (15 minutes); "
        "otherwise the minimum is 32. Shorter intervals add mesh traffic."
    ),
    "config.position.position_broadcast_smart_enabled": (
        "Smart broadcast sends a position only after the node has moved far enough and enough time "
        "has passed, instead of on a fixed timer."
    ),
    "config.position.fixed_position": (
        "Uses a fixed position instead of the GPS. The node keeps its last known position until a "
        "new one is set from an app."
    ),
    "config.position.gps_enabled": "Deprecated. Use GPS mode instead.",
    "config.position.gps_update_interval": (
        f"Seconds between GPS fixes. {_FIRMWARE_DEFAULT} of 120; otherwise the minimum is 30. "
        "Longer intervals save power."
    ),
    "config.position.gps_attempt_time": "Deprecated. The firmware no longer uses this value.",
    "config.position.position_flags": (
        "Bitmask of extra position fields (altitude, satellites, heading, speed) included in "
        "broadcasts. Needs a dedicated editor."
    ),
    "config.position.rx_gpio": "GPIO pin for GPS receive. 0 uses the board default.",
    "config.position.tx_gpio": "GPIO pin for GPS transmit. 0 uses the board default.",
    "config.position.broadcast_smart_minimum_distance": (
        f"Meters the node must move before smart broadcast sends a new position. {_FIRMWARE_DEFAULT} of 100."
    ),
    "config.position.broadcast_smart_minimum_interval_secs": (
        f"Minimum seconds between smart-broadcast positions, even while moving. {_FIRMWARE_DEFAULT} of 30."
    ),
    "config.position.gps_en_gpio": "GPIO pin that powers the GPS on and off. 0 uses the board default.",
    "config.position.gps_mode": (
        "Whether the GPS is enabled, disabled, or not present. NOT_PRESENT also removes the GPS "
        "from the device menus."
    ),
    # Power.
    "config.power.is_power_saving": (
        "Puts the node into deep sleep between broadcasts. Bluetooth and the screen are unavailable "
        "while it sleeps, so use it for solar and battery nodes only."
    ),
    "config.power.on_battery_shutdown_after_secs": (
        "Seconds on battery power before the node shuts itself down. 0 disables. A node that has "
        "shut down needs a button press to wake."
    ),
    "config.power.adc_multiplier_override": (
        "Corrects the battery voltage reading. 0 uses the board's calibrated value; corrections are "
        "typically between 1 and 3."
    ),
    "config.power.wait_bluetooth_secs": (
        f"Seconds to keep Bluetooth on after waking so a phone can connect. {_FIRMWARE_DEFAULT}."
    ),
    "config.power.sds_secs": (
        "Seconds of super deep sleep before the node wakes again. Large values leave a node "
        "unreachable for a long time."
    ),
    "config.power.ls_secs": (
        f"Seconds the node stays in light sleep before waking to listen. {_FIRMWARE_DEFAULT}."
    ),
    "config.power.min_wake_secs": (
        f"Minimum seconds the node stays awake after waking. {_FIRMWARE_DEFAULT}."
    ),
    "config.power.device_battery_ina_address": "I2C address of an INA battery monitor. 0 means none.",
    "config.power.powermon_enables": "Bitmask of power-monitor debug events. Needs a dedicated editor.",
    # Network.
    "config.network.wifi_enabled": (
        "Turns Wi-Fi on. On ESP32 boards Wi-Fi and Bluetooth cannot run together, so the Bluetooth "
        "radio turns off while Wi-Fi is enabled."
    ),
    "config.network.wifi_ssid": "Name of the Wi-Fi network to join. Up to 32 bytes.",
    "config.network.wifi_psk": "Password of the Wi-Fi network. Up to 64 characters; set locally and never shown.",
    "config.network.ntp_server": (
        "Host name of the NTP time server. Up to 32 bytes; empty keeps the firmware default."
    ),
    "config.network.eth_enabled": "Turns the Ethernet port on for boards that have one.",
    "config.network.address_mode": "How the node gets its IP address: DHCP or a static address.",
    "config.network.ipv4_config.ip": "Static IPv4 address, stored as a number. Needs a dedicated editor.",
    "config.network.ipv4_config.gateway": "Static IPv4 gateway, stored as a number. Needs a dedicated editor.",
    "config.network.ipv4_config.subnet": "Static IPv4 subnet mask, stored as a number. Needs a dedicated editor.",
    "config.network.ipv4_config.dns": "Static IPv4 DNS server, stored as a number. Needs a dedicated editor.",
    "config.network.rsyslog_server": (
        "Host name or address of a syslog server that receives the node's logs. Up to 32 bytes; "
        "empty disables."
    ),
    "config.network.enabled_protocols": (
        "Bitmask of network discovery protocols such as mDNS. Needs a dedicated editor."
    ),
    "config.network.ipv6_enabled": "Turns IPv6 on for the network interface.",
    # Display.
    "config.display.screen_on_secs": (
        f"Seconds the screen stays on after the last button press. {_FIRMWARE_DEFAULT}."
    ),
    "config.display.gps_format": (
        "How coordinates are shown on screen: decimal degrees, DMS, UTM, MGRS, OLC, or OSGR."
    ),
    "config.display.auto_screen_carousel_secs": (
        "Seconds between automatic screen pages. 0 disables the carousel."
    ),
    "config.display.compass_north_top": (
        "Keeps north at the top of the compass instead of rotating it with the heading."
    ),
    "config.display.flip_screen": "Rotates the screen 180 degrees for boards mounted upside down.",
    "config.display.units": "Metric or imperial units for distances and temperatures on screen.",
    "config.display.oled": (
        "The OLED controller type. Pick the one that matches the hardware; a wrong type can blank "
        "the screen until it is corrected."
    ),
    "config.display.displaymode": (
        "The screen layout: DEFAULT, TWOCOLOR, INVERTED, or COLOR. COLOR can disable Bluetooth on "
        "some boards."
    ),
    "config.display.heading_bold": "Shows the heading text in bold for readability.",
    "config.display.wake_on_tap_or_motion": (
        "Wakes the screen when the node is tapped or moved on boards with an accelerometer."
    ),
    "config.display.compass_orientation": "Rotates the compass to match how the board is mounted.",
    "config.display.use_12h_clock": "Shows the clock in 12-hour format instead of 24-hour.",
    "config.display.use_long_node_name": "Shows long node names on screen where space allows.",
    "config.display.enable_message_bubbles": "Shows messages as chat bubbles on screen.",
    # LoRa.
    "config.lora.use_preset": (
        "Uses a modem preset instead of explicit bandwidth, spread factor, and coding rate. Every "
        "node on a mesh must match."
    ),
    "config.lora.modem_preset": (
        "The radio preset that trades speed for range, shared by the whole mesh. Nodes with "
        "different presets cannot hear each other."
    ),
    "config.lora.bandwidth": "Channel bandwidth in kHz when not using a preset. Must match the mesh.",
    "config.lora.spread_factor": "LoRa spreading factor, 7 to 12, when not using a preset. Must match the mesh.",
    "config.lora.coding_rate": "LoRa coding rate denominator, 5 to 8, when not using a preset. Must match the mesh.",
    "config.lora.frequency_offset": "Offset in Hz added to the channel frequency to correct crystal drift.",
    "config.lora.region": (
        "The regulatory region that sets the frequencies and duty cycle. A wrong region puts the "
        "node on a different band where nobody hears it."
    ),
    "config.lora.hop_limit": (
        "Maximum hops a packet from this node may travel, 1 to 7. The default is 3. Higher values "
        "reach farther but use more airtime on every relay."
    ),
    "config.lora.tx_enabled": (
        "Lets the radio transmit. Off makes the node listen-only, so it cannot answer over the mesh."
    ),
    "config.lora.tx_power": (
        "Transmit power in dBm, 0 to 30. 0 uses the regional maximum. Higher values must stay "
        "within local rules and the hardware's limit."
    ),
    "config.lora.channel_num": (
        "Explicit frequency slot number within the region. 0 picks the slot from the primary "
        "channel name."
    ),
    "config.lora.override_duty_cycle": (
        "Ignores the regional duty-cycle limit. This may break local rules."
    ),
    "config.lora.sx126x_rx_boosted_gain": (
        "Enables the receiver's boosted-gain mode on SX126x radios for a little more sensitivity "
        "at a higher power draw."
    ),
    "config.lora.override_frequency": (
        "Transmits on this exact frequency in MHz instead of the slot. 0 disables the override."
    ),
    "config.lora.pa_fan_disabled": "Turns off the power-amplifier fan on boards that have one.",
    "config.lora.ignore_incoming": "Node numbers whose packets this node ignores. Needs a dedicated editor.",
    "config.lora.ignore_mqtt": "Ignores packets that arrived through MQTT instead of over the air.",
    "config.lora.config_ok_to_mqtt": (
        "Marks this node's packets as allowed to be forwarded to MQTT by other nodes."
    ),
    "config.lora.fem_lna_mode": (
        "Front-end module LNA setting for boards with an external amplifier. Needs physical access."
    ),
    "config.lora.serial_hal_only": (
        "Uses only the serial HAL for the radio driver. Hardware-specific; needs physical access."
    ),
    # Bluetooth.
    "config.bluetooth.enabled": (
        "Turns the Bluetooth radio on. Off removes phone access until it is enabled again."
    ),
    "config.bluetooth.mode": (
        "Pairing mode: RANDOM_PIN shows a code on screen, FIXED_PIN uses the configured PIN, "
        "NO_PIN pairs without one."
    ),
    "config.bluetooth.fixed_pin": "The 6-digit PIN used with FIXED_PIN mode. Set locally and never shown.",
    # Security.
    "config.security.public_key": "The node's public key. Security settings need a recovery workflow.",
    "config.security.private_key": "The node's private key. Never shown or sent anywhere.",
    "config.security.admin_key": (
        "Public keys of the nodes allowed to administer this one remotely, up to three. Managed "
        "with the official app."
    ),
    "config.security.is_managed": (
        "Managed mode locks local configuration so only remote administrators can change settings."
    ),
    "config.security.serial_enabled": "Enables the serial console; this is the security copy of the device setting.",
    "config.security.debug_log_api_enabled": "Lets connected clients read the debug log over the API.",
    "config.security.admin_channel_enabled": (
        "Allows the legacy admin channel in addition to key-based administration."
    ),
    # MQTT.
    "module.mqtt.enabled": "Connects the node to an MQTT broker to bridge mesh traffic over the internet.",
    "module.mqtt.address": (
        "Broker host name or address, optionally with :port. Up to 63 bytes; empty uses the public "
        "Meshtastic broker."
    ),
    "module.mqtt.username": "Broker user name. Up to 63 bytes; set locally.",
    "module.mqtt.password": "Broker password. Up to 31 characters; set locally and never shown.",
    "module.mqtt.encryption_enabled": "Sends packets to the broker still encrypted instead of decrypted.",
    "module.mqtt.json_enabled": "Also publishes decoded packets as JSON topics.",
    "module.mqtt.tls_enabled": "Uses TLS for the broker connection.",
    "module.mqtt.root": "Root topic prefix for this node's MQTT traffic. Up to 31 bytes; empty uses msh.",
    "module.mqtt.proxy_to_client_enabled": (
        "Lets a connected phone app carry the MQTT connection on the node's behalf."
    ),
    "module.mqtt.map_reporting_enabled": "Publishes this node's position to the Meshtastic map through MQTT.",
    "module.mqtt.map_report_settings.publish_interval_secs": (
        f"Seconds between map reports. {_FIRMWARE_DEFAULT} of 900 (15 minutes)."
    ),
    "module.mqtt.map_report_settings.position_precision": (
        "Bits of position precision in map reports, 0 to 32. 32 is exact; smaller values blur the "
        "location, with 10 to 19 typical."
    ),
    "module.mqtt.map_report_settings.should_report_location": "Includes the node's location in map reports.",
    # Serial module.
    "module.serial.enabled": (
        "Enables the Serial module on a spare UART so external devices can send and receive mesh data."
    ),
    "module.serial.echo": "Echoes received serial bytes back to the sender.",
    "module.serial.rxd": "GPIO pin for serial receive.",
    "module.serial.txd": "GPIO pin for serial transmit.",
    "module.serial.baud": "Serial speed. BAUD_DEFAULT uses the firmware default.",
    "module.serial.timeout": (
        f"Milliseconds to wait before sending buffered serial bytes over the mesh. {_FIRMWARE_DEFAULT}."
    ),
    "module.serial.mode": (
        "What the serial port carries: DEFAULT, SIMPLE text, PROTO protobufs, TEXTMSG, NMEA, "
        "CALTOPO, or WS85."
    ),
    "module.serial.override_console_serial_port": "Uses the main USB console port for the Serial module.",
    # External notification.
    "module.external_notification.enabled": (
        "Enables external alerts on an LED, buzzer, or vibration motor when messages arrive."
    ),
    "module.external_notification.output_ms": (
        f"Milliseconds each alert pulse stays on. {_FIRMWARE_DEFAULT} of 1000."
    ),
    "module.external_notification.output": "GPIO pin for the LED or relay output.",
    "module.external_notification.output_vibra": "GPIO pin for the vibration motor.",
    "module.external_notification.output_buzzer": "GPIO pin for the buzzer.",
    "module.external_notification.active": "Whether the output pin is active-high.",
    "module.external_notification.alert_message": "Alerts on the main output when a text message arrives.",
    "module.external_notification.alert_message_vibra": "Alerts on the vibration motor when a text message arrives.",
    "module.external_notification.alert_message_buzzer": "Alerts on the buzzer when a text message arrives.",
    "module.external_notification.alert_bell": "Alerts on the main output when a message contains the bell character.",
    "module.external_notification.alert_bell_vibra": "Alerts on the vibration motor when a message contains the bell character.",
    "module.external_notification.alert_bell_buzzer": "Alerts on the buzzer when a message contains the bell character.",
    "module.external_notification.use_pwm": "Drives the buzzer with PWM tones instead of a plain on/off signal.",
    "module.external_notification.nag_timeout": (
        "Seconds to keep repeating an alert until a button is pressed. 0 alerts once."
    ),
    "module.external_notification.use_i2s_as_buzzer": (
        "Plays alert tones on the I2S audio output instead of a buzzer pin."
    ),
    # Store and forward.
    "module.store_forward.enabled": (
        "Enables Store and Forward so a server node with PSRAM keeps recent messages for clients "
        "that were asleep."
    ),
    "module.store_forward.heartbeat": "Sends a periodic heartbeat announcing the Store and Forward server.",
    "module.store_forward.records": (
        "Number of messages the server keeps. 0 lets the firmware size the buffer from available memory."
    ),
    "module.store_forward.history_return_max": (
        f"Maximum messages returned to a client in one request. {_FIRMWARE_DEFAULT}."
    ),
    "module.store_forward.history_return_window": (
        f"Minutes of history a client may request. {_FIRMWARE_DEFAULT}."
    ),
    "module.store_forward.is_server": "Acts as the Store and Forward server. Needs a board with PSRAM.",
    # Range test.
    "module.range_test.enabled": "Enables the Range Test module, which sends or records test packets.",
    "module.range_test.sender": (
        "Seconds between test packets this node sends. 0 makes it a receiver only. A sender floods "
        "the mesh, so turn it off after testing."
    ),
    "module.range_test.save": "Saves received test packets to a CSV file on the node.",
    "module.range_test.clear_on_reboot": "Deletes the saved range test file when the node reboots.",
    # Telemetry.
    "module.telemetry.device_update_interval": (
        f"Seconds between device metrics broadcasts (battery, voltage, uptime). {_FIRMWARE_DEFAULT} "
        "of 1800 (30 minutes)."
    ),
    "module.telemetry.environment_update_interval": (
        f"Seconds between environment sensor broadcasts. {_FIRMWARE_DEFAULT} of 1800 (30 minutes)."
    ),
    "module.telemetry.environment_measurement_enabled": (
        "Reads attached environment sensors such as temperature and humidity."
    ),
    "module.telemetry.environment_screen_enabled": "Shows environment readings on the screen.",
    "module.telemetry.environment_display_fahrenheit": "Shows temperatures in Fahrenheit on screen.",
    "module.telemetry.air_quality_enabled": "Reads attached air quality sensors.",
    "module.telemetry.air_quality_interval": (
        f"Seconds between air quality broadcasts. {_FIRMWARE_DEFAULT} of 1800 (30 minutes)."
    ),
    "module.telemetry.air_quality_screen_enabled": "Shows air quality readings on the screen.",
    "module.telemetry.power_measurement_enabled": "Reads attached power sensors such as an INA219.",
    "module.telemetry.power_update_interval": (
        f"Seconds between power metrics broadcasts. {_FIRMWARE_DEFAULT} of 1800 (30 minutes)."
    ),
    "module.telemetry.power_screen_enabled": "Shows power readings on the screen.",
    "module.telemetry.health_measurement_enabled": "Reads attached health sensors such as heart rate.",
    "module.telemetry.health_update_interval": (
        f"Seconds between health metrics broadcasts. {_FIRMWARE_DEFAULT} of 1800 (30 minutes)."
    ),
    "module.telemetry.health_screen_enabled": "Shows health readings on the screen.",
    "module.telemetry.device_telemetry_enabled": (
        "Broadcasts device metrics at all. Off stops battery and uptime reports."
    ),
    # Canned messages.
    "module.canned_message.rotary1_enabled": "Enables a rotary encoder as the input device.",
    "module.canned_message.inputbroker_pin_a": "GPIO pin A of the rotary encoder.",
    "module.canned_message.inputbroker_pin_b": "GPIO pin B of the rotary encoder.",
    "module.canned_message.inputbroker_pin_press": "GPIO pin of the rotary encoder's press switch.",
    "module.canned_message.inputbroker_event_cw": "Key event sent when the encoder turns clockwise.",
    "module.canned_message.inputbroker_event_ccw": "Key event sent when the encoder turns counter-clockwise.",
    "module.canned_message.inputbroker_event_press": "Key event sent when the encoder is pressed.",
    "module.canned_message.updown1_enabled": "Enables an up/down button pad as the input device.",
    "module.canned_message.enabled": (
        "Enables the Canned Message module so pre-written messages can be sent from the device."
    ),
    "module.canned_message.allow_input_source": (
        "Name of the input source allowed to trigger canned messages. Up to 15 bytes; empty allows any."
    ),
    "module.canned_message.send_bell": "Adds the bell character to canned messages so recipients are alerted.",
    # Audio.
    "module.audio.codec2_enabled": "Enables Codec2 voice over the mesh on supported boards.",
    "module.audio.ptt_pin": "GPIO pin of the push-to-talk button.",
    "module.audio.bitrate": "Codec2 bitrate. Lower rates use less airtime.",
    "module.audio.i2s_ws": "I2S word-select pin.",
    "module.audio.i2s_sd": "I2S serial data output pin.",
    "module.audio.i2s_din": "I2S data input pin.",
    "module.audio.i2s_sck": "I2S clock pin.",
    # Remote hardware.
    "module.remote_hardware.enabled": "Enables remote GPIO control over the mesh.",
    "module.remote_hardware.allow_undefined_pin_access": (
        "Lets remote nodes drive any GPIO, not only the listed pins. Dangerous on most boards."
    ),
    "module.remote_hardware.available_pins": (
        "The GPIO pins remote nodes may control. Needs a dedicated editor."
    ),
    # Neighbor info.
    "module.neighbor_info.enabled": "Broadcasts the list of nodes heard directly, which maps the topology.",
    "module.neighbor_info.update_interval": (
        f"Seconds between neighbor info broadcasts. {_FIRMWARE_DEFAULT} of 21600 (6 hours); the "
        "firmware raises shorter values to its minimum."
    ),
    "module.neighbor_info.transmit_over_lora": "Sends neighbor info over LoRa, not only to MQTT.",
    # Ambient lighting.
    "module.ambient_lighting.led_state": "Turns the RGB ambient LED on.",
    "module.ambient_lighting.current": "LED current level, 0 to 31.",
    "module.ambient_lighting.red": "Red component of the LED color, 0 to 255.",
    "module.ambient_lighting.green": "Green component of the LED color, 0 to 255.",
    "module.ambient_lighting.blue": "Blue component of the LED color, 0 to 255.",
    # Detection sensor.
    "module.detection_sensor.enabled": (
        "Enables the Detection Sensor module, which reports a GPIO change such as a door switch."
    ),
    "module.detection_sensor.minimum_broadcast_secs": (
        f"Minimum seconds between detection broadcasts. {_FIRMWARE_DEFAULT}."
    ),
    "module.detection_sensor.state_broadcast_secs": (
        "Seconds between periodic state reports even without a change. 0 disables them."
    ),
    "module.detection_sensor.send_bell": "Adds the bell character to detection messages.",
    "module.detection_sensor.name": "Friendly name sent with detections. Up to 19 bytes.",
    "module.detection_sensor.monitor_pin": "GPIO pin to watch.",
    "module.detection_sensor.detection_trigger_type": "Which edge or level of the pin counts as a detection.",
    "module.detection_sensor.use_pullup": "Enables the internal pull-up resistor on the monitored pin.",
    # Paxcounter.
    "module.paxcounter.enabled": "Counts nearby Wi-Fi and Bluetooth devices and reports the count.",
    "module.paxcounter.paxcounter_update_interval": (
        f"Seconds between count reports. {_FIRMWARE_DEFAULT}."
    ),
    "module.paxcounter.wifi_threshold": (
        f"Weakest Wi-Fi signal in dBm that still counts. {_FIRMWARE_DEFAULT} of -80."
    ),
    "module.paxcounter.ble_threshold": (
        f"Weakest Bluetooth signal in dBm that still counts. {_FIRMWARE_DEFAULT} of -80."
    ),
    # Status message.
    "module.statusmessage.node_status": "Short status text broadcast with this node's info. Up to 79 bytes.",
    # Traffic management.
    "module.traffic_management.enabled": (
        "Enables traffic management, which trims repeated and excess packets this node relays."
    ),
    "module.traffic_management.position_dedup_enabled": (
        "Drops repeated position packets from the same node within the minimum interval."
    ),
    "module.traffic_management.position_precision_bits": (
        "Precision bits kept when comparing positions for deduplication, 0 to 32."
    ),
    "module.traffic_management.position_min_interval_secs": (
        f"Minimum seconds between relayed positions from one node. {_FIRMWARE_DEFAULT}."
    ),
    "module.traffic_management.nodeinfo_direct_response": (
        "Answers NodeInfo requests directly instead of rebroadcasting them."
    ),
    "module.traffic_management.nodeinfo_direct_response_max_hops": (
        "Maximum hops for direct NodeInfo answers, 0 to 7."
    ),
    "module.traffic_management.rate_limit_enabled": (
        "Limits how many packets one node may send through this relay within a window."
    ),
    "module.traffic_management.rate_limit_window_secs": (
        f"Length of the rate-limit window in seconds. {_FIRMWARE_DEFAULT}."
    ),
    "module.traffic_management.rate_limit_max_packets": (
        f"Packets allowed from one node per window. {_FIRMWARE_DEFAULT}."
    ),
    "module.traffic_management.drop_unknown_enabled": (
        "Drops packets from nodes this node has never received a NodeInfo from."
    ),
    "module.traffic_management.unknown_packet_threshold": (
        f"Packets from an unknown node tolerated before dropping. {_FIRMWARE_DEFAULT}."
    ),
    "module.traffic_management.exhaust_hop_telemetry": (
        "Sets the remaining hop count of relayed telemetry to zero so it travels no farther."
    ),
    "module.traffic_management.exhaust_hop_position": (
        "Sets the remaining hop count of relayed positions to zero so they travel no farther."
    ),
    "module.traffic_management.router_preserve_hops": (
        "Lets router nodes keep the original hop count on packets they relay."
    ),
    # TAK.
    "module.tak.team": "ATAK team color reported by this node.",
    "module.tak.role": "ATAK role reported by this node.",
    # Channels.
    "channel.N.index": "The channel slot, 0 to 7. It is chosen by the section, not edited here.",
    "channel.N.settings.channel_num": "Legacy channel number; the firmware derives it from the settings.",
    "channel.N.settings.psk": "The channel's pre-shared key. Set with local access using the official app.",
    "channel.N.settings.name": (
        "The channel name. Up to 11 bytes. On the primary channel it also picks the frequency slot "
        "when no explicit slot is set."
    ),
    "channel.N.settings.id": "Unique channel identifier assigned by the firmware.",
    "channel.N.settings.uplink_enabled": "Forwards this channel's packets to MQTT.",
    "channel.N.settings.downlink_enabled": "Delivers packets arriving from MQTT onto this channel.",
    "channel.N.settings.module_settings.position_precision": (
        "Bits of position precision shared on this channel, 0 to 32. 0 shares no position; 32 is exact."
    ),
    "channel.N.settings.module_settings.is_muted": "Mutes notifications for this channel on the device.",
    "channel.N.role": "PRIMARY, SECONDARY, or DISABLED. Managed with the official app.",
    # On-device UI (read-only in MeshNet).
    "device_ui.version": "Version of the on-device UI configuration.",
    "device_ui.screen_brightness": "Screen brightness level.",
    "device_ui.screen_timeout": "Seconds before the device screen turns off.",
    "device_ui.screen_lock": "Locks the device screen until the PIN is entered.",
    "device_ui.settings_lock": "Locks the device settings menu.",
    "device_ui.pin_code": "PIN that unlocks the device screen.",
    "device_ui.theme": "Light or dark on-device theme.",
    "device_ui.alert_enabled": "Shows alert pop-ups on the device.",
    "device_ui.banner_enabled": "Shows banners on the device.",
    "device_ui.ring_tone_id": "The ringtone used for alerts on the device.",
    "device_ui.language": "Language of the on-device UI.",
    "device_ui.node_filter.unknown_switch": "Hides unknown nodes in the device node list.",
    "device_ui.node_filter.offline_switch": "Hides offline nodes in the device node list.",
    "device_ui.node_filter.public_key_switch": "Shows only nodes with a public key in the device node list.",
    "device_ui.node_filter.hops_away": "Hides nodes farther away than this many hops in the device node list.",
    "device_ui.node_filter.position_switch": "Shows only nodes with a position in the device node list.",
    "device_ui.node_filter.node_name": "Shows only nodes whose name contains this text in the device node list.",
    "device_ui.node_filter.channel": "Shows only nodes on this channel in the device node list.",
    "device_ui.node_highlight.chat_switch": "Highlights nodes that sent messages in the device node list.",
    "device_ui.node_highlight.position_switch": "Highlights nodes with a position in the device node list.",
    "device_ui.node_highlight.telemetry_switch": "Highlights nodes with telemetry in the device node list.",
    "device_ui.node_highlight.iaq_switch": "Highlights nodes with air quality data in the device node list.",
    "device_ui.node_highlight.node_name": "Highlights nodes whose name contains this text in the device node list.",
    "device_ui.calibration_data": "Touch screen calibration data.",
    "device_ui.compass_mode": "How the on-device compass is drawn.",
    "device_ui.screen_rgb_color": "RGB accent color of the device screen.",
    "device_ui.is_clockface_analog": "Shows an analog clock face on the device.",
    "device_ui.gps_format": "Coordinate format in the on-device UI.",
    # Local node info and firmware metadata (read-only).
    "local_info.reboot_count": "How many times the node has rebooted.",
    "local_info.min_app_version": "Oldest app version the firmware accepts.",
    "local_info.max_channels": "Number of channel slots the firmware supports.",
    "metadata.firmware_version": "Firmware version running on the node.",
    "metadata.device_state_version": "Version of the saved device state format.",
    "metadata.can_shutdown": "Whether the firmware supports a software shutdown.",
    "metadata.has_wifi": "Whether the hardware has Wi-Fi.",
    "metadata.has_bluetooth": "Whether the hardware has Bluetooth.",
    "metadata.has_ethernet": "Whether the hardware has Ethernet.",
    "metadata.has_eth": "Whether the hardware has Ethernet.",
    "metadata.role": "The device role the firmware reports.",
    "metadata.position_flags": "Position flags bitmask the firmware reports.",
    "metadata.hw_model": "Hardware model the firmware reports.",
    "metadata.has_remote_hardware": "Whether the firmware includes the Remote Hardware module.",
    "metadata.has_pkc": "Whether the firmware supports public-key cryptography for direct messages.",
    "metadata.excluded_modules": "Bitmask of modules excluded from this firmware build.",
}

_SECONDS = "seconds"
SETTING_UNITS: Final[dict[str, str]] = {
    "config.device.node_info_broadcast_secs": _SECONDS,
    "config.position.position_broadcast_secs": _SECONDS,
    "config.position.gps_update_interval": _SECONDS,
    "config.position.gps_attempt_time": _SECONDS,
    "config.position.broadcast_smart_minimum_distance": "meters",
    "config.position.broadcast_smart_minimum_interval_secs": _SECONDS,
    "config.power.on_battery_shutdown_after_secs": _SECONDS,
    "config.power.wait_bluetooth_secs": _SECONDS,
    "config.power.sds_secs": _SECONDS,
    "config.power.ls_secs": _SECONDS,
    "config.power.min_wake_secs": _SECONDS,
    "config.display.screen_on_secs": _SECONDS,
    "config.display.auto_screen_carousel_secs": _SECONDS,
    "config.lora.bandwidth": "kHz",
    "config.lora.frequency_offset": "Hz",
    "config.lora.hop_limit": "hops",
    "config.lora.tx_power": "dBm",
    "config.lora.override_frequency": "MHz",
    "module.mqtt.map_report_settings.publish_interval_secs": _SECONDS,
    "module.mqtt.map_report_settings.position_precision": "bits",
    "module.serial.timeout": "milliseconds",
    "module.external_notification.output_ms": "milliseconds",
    "module.external_notification.nag_timeout": _SECONDS,
    "module.store_forward.records": "messages",
    "module.store_forward.history_return_max": "messages",
    "module.store_forward.history_return_window": "minutes",
    "module.range_test.sender": _SECONDS,
    "module.telemetry.device_update_interval": _SECONDS,
    "module.telemetry.environment_update_interval": _SECONDS,
    "module.telemetry.air_quality_interval": _SECONDS,
    "module.telemetry.power_update_interval": _SECONDS,
    "module.telemetry.health_update_interval": _SECONDS,
    "module.neighbor_info.update_interval": _SECONDS,
    "module.detection_sensor.minimum_broadcast_secs": _SECONDS,
    "module.detection_sensor.state_broadcast_secs": _SECONDS,
    "module.paxcounter.paxcounter_update_interval": _SECONDS,
    "module.paxcounter.wifi_threshold": "dBm",
    "module.paxcounter.ble_threshold": "dBm",
    "module.traffic_management.position_precision_bits": "bits",
    "module.traffic_management.position_min_interval_secs": _SECONDS,
    "module.traffic_management.nodeinfo_direct_response_max_hops": "hops",
    "module.traffic_management.rate_limit_window_secs": _SECONDS,
    "module.traffic_management.rate_limit_max_packets": "packets",
    "module.traffic_management.unknown_packet_threshold": "packets",
    "channel.N.settings.module_settings.position_precision": "bits",
    "device_ui.screen_timeout": _SECONDS,
    "device_ui.node_filter.hops_away": "hops",
}


def _help_key(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    normalized = normalize_contract_path(path)
    # The on-device UI record is projected as its own category, but newer
    # firmware also nests it under Config; both spellings share one entry.
    if normalized.startswith("config.device_ui."):
        normalized = normalized[len("config."):]
    return normalized


def setting_help(path: str) -> str | None:
    """Return the reviewed help sentence for a settings path, if any."""
    key = _help_key(path)
    return SETTING_HELP.get(key) if key is not None else None


def setting_unit(path: str) -> str | None:
    """Return the display unit for a numeric settings path, if any."""
    key = _help_key(path)
    return SETTING_UNITS.get(key) if key is not None else None
