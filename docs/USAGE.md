# Usage

This guide covers normal operation after MeshNet is installed and configured.

## Open MeshNet

Home Assistant UI:

```text
Settings -> Devices & Services -> MeshNet
```

Admin-only sidebar panel:

```text
MeshNet
```

The panel shows:

- A message composer with cached recipient and gateway dropdowns
- Gateway online state
- Node online state
- Recent messages
- Basic topology view
- RF heat based on RSSI or SNR

For the simplest test, open the sidebar panel, leave **Recipient** on
**Broadcast**, leave **Gateway** on **Automatic**, enter a short message, and
press **Send**. For a direct message, choose a cached node from the recipient
dropdown. The dropdown submits the node's canonical identifier, so you do not
need to copy a node number or short name manually.

## Verify Gateway Status

In Home Assistant:

```text
Settings -> Devices & Services -> MeshNet -> Devices
```

Expected gateway entities:

- `binary_sensor.<gateway>_online`
- `sensor.<gateway>_last_connected`
- `sensor.<gateway>_last_packet`
- `sensor.<gateway>_packets_received`
- `sensor.<gateway>_packets_sent`
- `sensor.<gateway>_duplicate_packets`
- `sensor.<gateway>_error_count`

If a gateway is offline, run:

```bash
./verify_setup.sh --config-dir /config
```

## Refresh A Gateway

Use Developer Tools -> Actions:

```yaml
action: meshnet.refresh_gateway
data:
  gateway_id: meshtastic_wifi_1
```

Refresh all gateways:

```yaml
action: meshnet.refresh_gateway
data: {}
```

## Send A Message

The sidebar composer is the recommended interactive interface. Developer Tools
-> Actions and automations can use the same backend with YAML.

Broadcast:

```yaml
action: meshnet.broadcast_message
data:
  gateway_id: meshtastic_wifi_1
  message: "Test from Home Assistant"
  channel: "0"
  priority: normal
```

Direct:

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

For Meshtastic, `target_node` may be a full node ID such as `!12345678`, an
integer node number, or an exact unique short/long name already present in the
local Bluetooth node cache. Quote a numeric short name in YAML. Name matching
is case-insensitive but never fuzzy; an unknown or duplicated name is rejected.
When more than one gateway is configured, supply the matching `gateway_id` for
a manually entered name. The sidebar dropdown remains safer because it uses the
cached canonical ID and can select the node's last connected gateway.

Schedule:

```yaml
action: meshnet.schedule_message
data:
  gateway_id: meshtastic_wifi_1
  when: "2026-05-16T21:00:00-05:00"
  message: "Nightly radio check"
  channel: "0"
  message_type: broadcast
```

Scheduled timestamps must include a timezone.
Pending schedules are in memory and are canceled when the MeshNet entry unloads
or Home Assistant restarts.

## Known Good Radio Test

Use a harmless broadcast on a test channel:

```yaml
action: meshnet.broadcast_message
data:
  gateway_id: meshtastic_wifi_1
  message: "MeshNet test"
  channel: "0"
  priority: normal
```

Expected:

- The service call succeeds.
- `sensor.<gateway>_packets_sent` increments.
- A `meshnet_message_sent` event is fired.
- The recent message list shows the outbound message.

If the gateway is offline, MeshNet queues the message and replays it when a connected gateway is available.

## Automations

Example: alert when a gateway goes offline.

```yaml
alias: MeshNet gateway offline
trigger:
  - platform: state
    entity_id: binary_sensor.meshtastic_wifi_1_online
    to: "off"
    for: "00:05:00"
action:
  - service: persistent_notification.create
    data:
      title: MeshNet gateway offline
      message: Meshtastic WiFi 1 has been offline for 5 minutes.
```

Example: alert on low average battery.

```yaml
alias: MeshNet low average battery
trigger:
  - platform: numeric_state
    entity_id: sensor.meshnet_average_battery
    below: 25
action:
  - service: persistent_notification.create
    data:
      title: Mesh node batteries low
      message: Average MeshNet battery is below 25 percent.
```

## Events

MeshNet fires these Home Assistant events:

| Event | When |
| --- | --- |
| `meshnet_packet` | A packet is received and not deduplicated |
| `meshnet_message_received` | A received packet contains message text |
| `meshnet_message_sent` | A message send succeeds, including queued replay |

Listen in:

```text
Developer Tools -> Events
```

Event name:

```text
meshnet_message_received
```

## Diagnostics

Download diagnostics:

```text
Settings -> Devices & Services -> MeshNet -> three-dot menu -> Download diagnostics
```

Diagnostics include:

- MeshNet, Meshtastic, MeshCore, Bluetooth, D-Bus, and SQLite versions
- Redacted config-entry structure and safe gateway capability flags
- Coordinator update health and background startup/reconnect/send/outbox state
- Per-gateway protocol, transport, connection timestamps, packet counters,
  categorized errors, repair aggregates, and provider-client lifecycle state
- Meshtastic startup phase and elapsed time, synchronous serial/TCP constructor
  ownership, async Bluetooth GATT/configuration/reconnect task state,
  last-start outcome, and identity-free local adapter validation
- Per-node hardware, firmware, role, online state, last-heard time, and safe
  cached connectivity, power, radio, routing-hop, and environmental telemetry
- Mesh-wide online/offline, protocol, role, hardware, firmware, location-
  presence, gateway-reachability, and telemetry-presence summaries
- Deduplication and transmit-rate limiter state
- SQLite schema/runtime versions, journal mode, table/protocol/direction/outbox
  counts, age ranges, executor/close state, and database/WAL size totals
- Entity/device registry totals and per-domain available/unknown/unavailable
  state-health counts

The download is built exclusively from cached state. It never connects, pairs,
scans, refreshes, or transmits through a radio. Identifiers, names, network
addresses, serial paths, URLs, MQTT topics, credentials, message content, raw
packets/provider data, precise locations, and occupancy-related values are
omitted or redacted. Home Assistant also supplies its normal system,
integration-manifest, setup-time, and custom-component metadata around the
MeshNet report. That Home Assistant-controlled wrapper and downloaded filename
contain the config-entry ID; device filenames may additionally contain the
device name and registry ID. Inspect and rename the complete file before
sharing it.

The same action is available from MeshNet hub, gateway, and node device pages;
device downloads select the relevant cached detail automatically. An
administrator account is required. Mesh-wide summaries always cover the full
snapshot; config-entry downloads cap the per-node detail list at 1,000 cached
nodes and report whether that list was truncated, preventing an unusually large
mesh from exhausting Home Assistant memory while building the JSON file.

## Database

MeshNet stores durable state here:

```text
<HA_CONFIG_DIR>/meshnet.sqlite3
```

It stores:

- Nodes
- Messages
- Packets
- Route placeholders

Pruning follows `history_days`.

## Restart After Changes

Restart after changing installed component files or YAML import configuration:

```bash
ha core restart
```

Docker:

```bash
docker compose restart homeassistant
```

Most integration options changed from the UI reload automatically.
