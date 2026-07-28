# Configuration

MeshNet can be configured from the guided Home Assistant UI or imported from
YAML. The UI path is recommended: it filters connection methods by protocol,
displays only relevant fields, and tests connectivity. Meshtastic Bluetooth is
GUI-only because its local bond must be verified and tracked safely.

## Configuration Methods

Recommended:

```text
Settings -> Devices & Services -> Add Integration -> MeshNet
```

Advanced YAML import:

```yaml
meshnet:
  node_timeout: 900
  history_days: 30
  gateways:
    meshtastic_wifi_1:
      gateway_id: meshtastic_wifi_1
      name: Meshtastic WiFi 1
      protocol: meshtastic
      transport: tcp
      host: 192.0.2.50
      port: 4403
```

After Home Assistant imports YAML into a config entry, use **Configure** to add, edit, or remove gateways with forms. The Advanced JSON editor remains available only as an escape hatch for custom bridge fields.

YAML and Advanced JSON cannot add or replace a Meshtastic Bluetooth gateway.
Use the guided Add form for a new endpoint. Edit can rename a paired gateway,
but changing its radio address requires **Remove gateway**, then Add. This
prevents an unverified or forged originally-paired marker from authorizing
current-bond cleanup for a different address.

## Global Options

| Option | Default | Meaning |
| --- | --- | --- |
| `node_timeout` | `900` | Seconds before a node is marked offline after its last packet |
| `history_days` | `30` | Days of message and packet history to keep in `meshnet.sqlite3` |
| `scan_interval` | `30` | Polling interval for transports that poll, such as REST |

## Gateway Fields

Every gateway needs:

| Field | Example | Meaning |
| --- | --- | --- |
| `gateway_id` | `meshtastic_wifi_1` | Stable unique ID. Do not rename casually. |
| `name` | `Meshtastic WiFi 1` | Friendly name shown in Home Assistant. |
| `protocol` | `meshtastic` | `meshtastic` or `meshcore`. |
| `transport` | `tcp` | Transport used to reach the gateway. |

Transport-specific required fields:

| Transport | Required fields | Optional fields |
| --- | --- | --- |
| `tcp` | `host` | `port` |
| `serial` | `serial_path` | `options.baudrate`, `options.debug` |
| `bluetooth` | `ble_address` | `options.pin` (MeshCore only) |
| `mqtt` | Home Assistant MQTT integration, `mqtt_topic` | `options.publish_topic`, `options.mqtt_node_id` |
| `rest` | `api_url` | `api_key`, `options.send_url` |

Supported combinations:

| Protocol | Transports |
| --- | --- |
| `meshtastic` | `tcp`, `serial`, `bluetooth`, `mqtt` |
| `meshcore` | `tcp`, `serial`, `bluetooth`, `mqtt`, `rest` |

Invalid combinations are rejected by the config flow.

## Direct Meshtastic Bluetooth (version 0.5)

> [!NOTE]
> This section describes the version 0.5 behavior. Install a version 0.5 build
> before expecting these controls in Home Assistant.

Meshtastic Bluetooth setup uses a local Linux BlueZ adapter. Home Assistant
Bluetooth proxies are not supported for pairing or for the subsequent direct
Meshtastic connection.

The stored pairing record includes the controller's stable Bluetooth address.
MeshNet resolves the current `hciN` identity and a fresh Home Assistant
`BLEDevice` through that exact controller for every connection attempt, so
other valid local adapters may remain powered when the radio resolves through
one unambiguous local controller. An `hciN` rename after reboot does not
authorize a different controller.

This path is fully local after installation. It does not configure or require
MQTT, a broker, Internet access, radio Wi-Fi, or a LAN connection. Home
Assistant owns one persistent BLE connection and reconnects with bounded
backoff if an established link is lost.

Before starting:

1. Enable Bluetooth on the Meshtastic radio and disable Wi-Fi if its firmware
   does not permit both at the same time.
2. Close the Meshtastic phone app and disconnect other Bluetooth clients. A
   radio normally serves only one Bluetooth client at a time.
3. Place the radio near a local Bluetooth adapter attached to the Home
   Assistant host.

In the MeshNet form:

1. Choose a discovered Meshtastic radio from the dropdown. The advanced option
   accepts only a canonical Bluetooth MAC address such as
   `AA:BB:CC:DD:EE:FF`.
2. Select **Start pairing**.
3. For a screened radio configured with `RANDOM_PIN`, enter the six-digit code
   that appears on the radio. For a screenless radio, enter its configured fixed
   PIN. A factory fixed PIN may be `123456`; change that default in Meshtastic
   before using Bluetooth for regular operation.
4. Submit within about 50 seconds. Start again if the request expires.

The PIN entry is password-masked. MeshNet passes it only to the temporary
pairing operation and never saves it in the Home Assistant config entry,
diagnostics, or logs. Do not place a Meshtastic Bluetooth PIN in YAML or in
`options.pin`; that option is for MeshCore SDK connections only.

MeshNet registers a temporary, application-scoped BlueZ agent for the exact
selected device. It does not become the system default agent. On Home Assistant
OS, normal pairing therefore does not require a root shell or `bluetoothctl`.

The first verified Bluetooth gateway is saved immediately with safe global
defaults, rather than leaving a provisional bond at the Add-another/settings
screens. Use **Configure** afterward to add gateways or change global options.

The Meshtastic radio normally allows only one Bluetooth client. Close the
Android/iOS app, web client, and any BLE command-line client while MeshNet is
connected. Keeping an app merely in the background may leave its link active.

Deleting a gateway, config entry, or HACS package preserves external BlueZ
state. BlueZ has no bond-generation identifier, so MeshNet cannot prove that a
same-address bond was not recreated by another app after initial setup.

For deliberate cleanup, choose **Configure → Remove gateway**, enable **Remove
this radio's current Bluetooth bond (may disconnect other apps)**, and confirm.
The option is off by default. It removes the current address-scoped bond, not a
cryptographically identifiable historical generation. If BlueZ is unavailable,
the gateway is kept so the explicitly requested operation can be retried.

## Meshtastic TCP

Use this when the Meshtastic radio exposes the TCP API over WiFi.

```yaml
meshnet:
  gateways:
    meshtastic_wifi_1:
      gateway_id: meshtastic_wifi_1
      name: Meshtastic WiFi 1
      protocol: meshtastic
      transport: tcp
      host: 192.0.2.50
      port: 4403
```

Validate from the Home Assistant host:

```bash
nc -z -w 3 192.0.2.50 4403
echo $?
```

Expected:

```text
0
```

## Meshtastic USB Serial

The GUI lists local USB serial devices visible to Home Assistant. Prefer a stable `/dev/serial/by-id/` selection. If the desired mapping is not listed, type the exact path visible inside Home Assistant.

```yaml
meshnet:
  gateways:
    meshtastic_usb_1:
      gateway_id: meshtastic_usb_1
      name: Meshtastic USB 1
      protocol: meshtastic
      transport: serial
      serial_path: /dev/serial/by-id/usb-YOUR_MESHTASTIC_DEVICE
```

Find devices:

```bash
ls -l /dev/serial/by-id/
```

## MeshCore TCP

MeshCore TCP ports vary by firmware or bridge. Confirm the port before configuring it.

```yaml
meshnet:
  gateways:
    meshcore_wifi_1:
      gateway_id: meshcore_wifi_1
      name: MeshCore WiFi 1
      protocol: meshcore
      transport: tcp
      host: 192.0.2.51
      port: 12345
      options:
        debug: false
```

If your MeshCore gateway requires a PIN:

```yaml
options:
  pin: "REPLACE_WITH_DEVICE_PIN"
  debug: false
```

## MeshCore USB Serial

```yaml
meshnet:
  gateways:
    meshcore_usb_1:
      gateway_id: meshcore_usb_1
      name: MeshCore USB 1
      protocol: meshcore
      transport: serial
      serial_path: /dev/serial/by-id/usb-YOUR_MESHCORE_DEVICE
      options:
        baudrate: 115200
        debug: false
```

## MQTT JSON

MeshNet consumes JSON, not raw protobuf MQTT packets. Meshtastic firmware can publish its supported packet types on the official `/json/` branch. MeshCore requires a compatible external JSON bridge.

Meshtastic:

```yaml
meshnet:
  gateways:
    meshtastic_mqtt_1:
      gateway_id: meshtastic_mqtt_1
      name: Meshtastic MQTT 1
      protocol: meshtastic
      transport: mqtt
      mqtt_topic: msh/+/2/json/#
      options:
        # Exact downlink topic configured on the Meshtastic gateway.
        publish_topic: msh/US/2/json/mqtt/
        # Decimal node ID of the gateway which transmits downlinks.
        mqtt_node_id: "305419896"
```

MeshCore:

```yaml
meshnet:
  gateways:
    meshcore_mqtt_1:
      gateway_id: meshcore_mqtt_1
      name: MeshCore MQTT 1
      protocol: meshcore
      transport: mqtt
      mqtt_topic: meshcore/+/+/packets
      options:
        publish_topic: meshcore/homeassistant/commands
```

MeshCore MQTT uses a project-specific JSON bridge contract and publishes send requests to `<publish_topic>/send`. Omit `publish_topic` for receive-only operation; sending is rejected instead of guessing a command topic.

For receive-only Meshtastic MQTT, omit `publish_topic` and `mqtt_node_id`. Never subscribe MeshNet to broad `msh/#`: that includes raw protobuf topics which are intentionally not JSON-decoded.

Validate MQTT in Home Assistant before using either MQTT transport:

```text
Settings -> Devices & Services -> MQTT
```

## MeshCore REST

Use REST only when a MeshCore bridge exposes JSON state and accepts JSON send requests.

```yaml
meshnet:
  gateways:
    meshcore_rest_1:
      gateway_id: meshcore_rest_1
      name: MeshCore REST 1
      protocol: meshcore
      transport: rest
      api_url: http://192.0.2.51:8080/meshcore/state
      api_key: !secret meshcore_api_key
      options:
        send_url: http://192.0.2.51:8080/meshcore/send
```

In `secrets.yaml`:

```yaml
meshcore_api_key: "REPLACE_WITH_REAL_KEY"
```

## Docker Serial Paths

On the host:

```bash
ls -l /dev/serial/by-id/
```

In `docker-compose.yml`:

```yaml
devices:
  - /dev/serial/by-id/usb-YOUR_MESHTASTIC_DEVICE:/dev/meshtastic0
```

In MeshNet:

```yaml
serial_path: /dev/meshtastic0
```

Validate inside the container:

```bash
docker compose exec homeassistant ls -l /dev/meshtastic0
```

## Services

MeshNet registers these Home Assistant services:

| Service | Purpose |
| --- | --- |
| `meshnet.send_message` | Send a direct, group, broadcast, or emergency message |
| `meshnet.broadcast_message` | Send a broadcast message |
| `meshnet.schedule_message` | Schedule a message at an ISO timestamp with timezone |
| `meshnet.refresh_gateway` | Refresh one or all gateways |

Example:

```yaml
action: meshnet.send_message
data:
  gateway_id: meshtastic_wifi_1
  target_node: "!12345678"
  message: "Generator battery low"
  channel: "0"
  priority: high
  message_type: direct
```

For Meshtastic direct Bluetooth, `target_node` accepts a full `!` node ID, an
integer node number, or one exact unique cached short/long name. Numeric names
must be quoted in YAML. Partial, fuzzy, unknown, and ambiguous name matches are
rejected. Specify the matching `gateway_id` when manually using a name with a
multi-gateway entry. The MeshNet sidebar composer avoids manual identifiers by
listing cached nodes in a dropdown.

## Entity Model

Immediately after setup:

- Summary sensors appear on the MeshNet hub device.
- Gateway devices appear for each configured gateway.
- Gateway online and packet sensors appear.

After packets arrive:

- Node devices appear.
- Node battery, RSSI, SNR, hop, telemetry, and location entities appear when those values are present.
- GPS-capable nodes create `device_tracker` entities after latitude and longitude are received.

## Stable IDs

Do not rename these unless you intentionally want new Home Assistant devices/entities:

- `gateway_id`
- Node public key
- Node MAC address
- Node ID when MAC/public key is unavailable

Changing `gateway_id` also affects queued outbound messages and historical attribution.
