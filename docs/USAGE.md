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

- App-like broadcast, channel, and direct conversation threads with a composer
- Gateway online state
- Node online/last-seen state with favorites-aware sorting
- Recent messages
- A draggable, moving 20/50/100-node cached-evidence graph whose spring
  lengths use valid GPS distance only after an edge has independent route
  evidence, with the great-circle distance labeled in miles
- A link to Home Assistant's native Map
- RF heat based on RSSI or SNR

On the main **Mesh** view, drag a side-card handle to reorder cards or use its
accessible earlier/later buttons. Resize with the corner handle or its keyboard
arrow controls. Card order and size live only in the current panel instance;
MeshNet does not write them to browser storage, Home Assistant, or a radio.
Press **Reset**, or detach/navigate away from the panel, to restore the default
layout.

For the simplest test, open the sidebar panel, leave **Delivery** on
**Broadcast** and **Gateway** on **Automatic**, enter a short message, and press
**Send**. For a direct message, choose **Delivery → Direct** and select a cached
node, or press **Message** beside a node. The dropdown submits the node's
canonical identifier, so you do not need to copy a node number or short name
manually.

Every node selector—including both message composers, remote administration,
traceroute, and NeighborInfo—shows favorites first and then valid last-seen
times from newest to oldest. A star and last-seen label make that ordering
visible; missing or malformed timestamps follow valid ones.

Meshtastic reactions are attached only when their `reply_id` matches the
original message's exact on-air packet ID. Reactions whose original message is
outside the loaded history remain visible as unresolved instead of being
guessed onto a nearby message. A five-second panel refresh preserves the
current scroll position; when you are already near the bottom it follows the
newest messages.

## Nodes, Favorites, Map, And Topology

The default node order is **Favorites + last seen**. You can instead choose
**Last seen** or **Name**. Missing or malformed timestamps sort after valid
timestamps and node identifiers provide a stable final tie-breaker.

To mark nodes as favorites without creating MeshNet-owned residue:

1. In Home Assistant, open **Settings → Areas, labels & zones → Labels**.
2. Create a label named exactly `MeshNet Favorite`.
3. Add that label to any MeshNet node device you want pinned first.

MeshNet only reads that one label from the Home Assistant device registry. It
never creates, changes, or removes labels, and uninstalling MeshNet leaves the
user-owned label under Home Assistant's normal control.

The panel's **Map** link opens Home Assistant's native Map. A MeshNet node is
eligible only when it has both finite latitude and longitude inside valid
geographic ranges. Nodes without valid cached coordinates remain in the node
list but do not get a location tracker. For Meshtastic, the protocol's unset
`(0, 0)` position is treated as missing. Precision bits remain separate from an
explicit meter accuracy and are never presented to Home Assistant as meters.

The topology deliberately requires cached evidence. A solid gateway edge
means locally received, non-MQTT packet/node data explicitly reported zero hops
and retained the gateway that observed it. A received, explicit MeshCore
route/path may be shown only when every endpoint resolves to an exact cached
identifier. Nodes sharing a gateway are not assumed to be connected. When no
defensible evidence exists the panel displays **No cached connection evidence
yet**. Edges are explicitly labeled as last received, cached evidence rather
than a live route; MeshNet does not currently expire a received MeshCore
route/path on a guessed schedule.

Locally received Meshtastic NeighborInfo may add a node-to-node edge when both
identities resolve exactly and the report is no more than one hour old. MQTT,
ambiguous, malformed, stale, and implausibly future-dated neighbor reports do
not create graph edges. Unsolicited and legacy reports are marked `passive`.
Reports returned by the explicit request below are marked `manual_request`, so
the graph never describes solicited evidence as passive.

An edge label is the Haversine distance in miles, not an RF range estimate.
For a gateway endpoint MeshNet uses the exact controller radio's cached GPS
first, then Home Assistant's configured Home location in the browser, then a
neutral graph length. MeshNet does not write a fixed radio position. If the
controller should advertise a fixed GPS position, configure it deliberately
with an official Meshtastic client; otherwise keep Home Assistant's Home
location accurate for the clearly labeled fallback.

MeshNet does not run traceroute automatically, on refresh, on startup, or to
fill the graph. Administrators may explicitly request one unicast traceroute
from the MeshNet panel when using a connected Meshtastic Bluetooth gateway.
The backend permits at most one manual traceroute across the entire MeshNet
integration per minute, reserves that global cooldown before transmitting, never
retries, and exposes no traceroute service or automation action. The panel
reloads the persisted cooldown and last bounded result after Home Assistant or
the browser restarts.

Administrators may explicitly request NeighborInfo from one exact known node
through a connected Meshtastic Bluetooth gateway. MeshNet submits one unicast
application packet, with no MeshNet retry, only after atomically reserving persisted 180-second integration-wide
and same-target floors shared by every gateway. It never
polls, batches, broadcasts, schedules, or retries this operation. This
experimental path is verified on Meshtastic firmware 2.7.26 and requires the
target's Neighbor Info module to be enabled. Older firmware may reject it or
time out. The response is a cached zero-hop report with at most ten neighbors,
not a live scan; an empty valid response and a timeout are different outcomes.
A timeout is treated as unknown and consumes both reservations.

The **NeighborInfo** button beside **Message** in a node row is only a shortcut
to the guarded controls. It selects the exact node and reveals the existing
flow; it does not load status or transmit RF. Explicitly use **Load persisted
status**, then **Request NeighborInfo**, review the warning, and **Confirm
request** before one request can leave the radio.

## Remote Node Administration

For a non-critical Meshtastic node that should accept this controller:

1. Open **MeshNet** in the Home Assistant sidebar and use its **Remote node
   administration** card with a connected Meshtastic Bluetooth gateway.
2. Select the exact target and press **Load / Test access**.
3. Copy the displayed controller public key. It is copy-only; MeshNet never
   handles the controller private key.
4. Connect the official Meshtastic app or CLI locally to the target and place
   that public key in a free Security Admin Key slot.
5. Reconnect MeshNet, load the target again, edit an offered owner/display
   field, Preview, review the warning, confirm, and Apply once.

The target must have a valid public key in the controller NodeDB. Bluetooth
configuration, channel keys, SecurityConfig, admin-key slots, resets, firmware,
files, position mutation, and raw admin commands are excluded. A timeout is an
unknown outcome: inspect the radio locally instead of pressing Apply again.
The full trust model and recovery table are in
[Advanced Mesh Operations](ADVANCED_MESH_OPERATIONS.md).

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
- `sensor.<gateway>_failure_count`
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
Identity-shaped text is resolved as an identity before names, so a node cannot
capture a message by advertising an ID, MAC, or public key as its name. The
sidebar dropdown remains safest because it uses the cached canonical identity.

Direct messages require a target; every other message type rejects one. Message
text is limited to 237 UTF-8 bytes, channel indexes to `0`–`7`, and priority to
`normal`, `high`, or `emergency`. If supplied, `gateway_id` must exactly match a
configured gateway using the target protocol. MeshNet queues for a matching
offline radio instead of sending through a connected radio of another protocol.

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
The complete message envelope is validated before its timer is registered.
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
| `meshnet_message_status` | A send attempt is queued, sent, blocked, or fails safely |
| `meshnet_gateway_status` | A gateway connection or failure state really changes |

`meshnet_packet` is a deprecated, metadata-only compatibility event. It and
the versioned status events omit raw provider payloads, credentials, and
exception strings.

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
- Manual traceroute cooldowns and sanitized results

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
