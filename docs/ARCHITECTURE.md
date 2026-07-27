# Architecture

MeshNet turns one Home Assistant instance into the operating surface for mixed mesh radio networks. The integration keeps provider-specific details at the edges and exposes normalized gateways, nodes, packets, messages, sensors, trackers, events, services, diagnostics, and a sidebar panel.

## High-Level Flow

```text
Meshtastic / MeshCore gateways
        |
        v
Provider clients
        |
        v
Normalized models
        |
        v
MeshNetCoordinator
        |
        +--> SQLite cache
        +--> Home Assistant entities
        +--> Home Assistant events
        +--> Websocket API and admin panel
        +--> Services for outbound messages
```

## Gateway Clients

Files:

```text
custom_components/meshnet/meshtastic_client.py
custom_components/meshnet/meshcore_client.py
custom_components/meshnet/gateway.py
```

Responsibilities:

- Connect to one configured transport.
- Translate provider packet shapes into `MeshPacket`.
- Translate node/contact/telemetry shapes into `NodeState`.
- Send outbound messages through the provider API.
- Report `GatewayStatus` back to the coordinator.

Supported transports are validated before runtime setup.

## Meshtastic Bluetooth Pairing Boundary (version 0.4)

Bluetooth discovery and pairing are separate trust decisions:

```text
Home Assistant discovery cache
        |
        v
Meshtastic UUID filter -> dropdown / canonical manual MAC
        |
        v
Local BlueZ device verification
        |
        v
Temporary app-scoped Agent1 -> exact Device1.Pair
        |
        v
Re-read bonded state -> begin Meshtastic SDK connection
```

`custom_components/meshnet/bluetooth_devices.py` filters cached advertisements
for the Meshtastic service UUID, canonicalizes MAC addresses, and constructs the
selection list. A cached advertisement is not proof that a device belongs to a
local adapter because Home Assistant Bluetooth proxies also populate discovery.
The pairing backend therefore requires the exact device on a local BlueZ
adapter before changing bond state. Proxies are rejected.

Meshtastic 2.7.11 does not expose controller selection. Pairing and runtime
therefore require the verified physical controller to be the sole powered local
adapter. MeshNet stores both its current `hciN` name and stable Adapter1 address,
then checks the stable identity before constructing the SDK interface.

For each request, MeshNet opens a bounded pairing session, registers a temporary
application-scoped BlueZ agent, and invokes pairing for only the selected device
on that connection. It never requests the system-default agent role. Agent
callbacks for any other BlueZ device fail closed. The PIN handoff and pairing
prompt expire after about 50 seconds, the whole transaction has a 75-second
limit, and cancellation/error paths unregister the agent and release the
connection.

Pairing and PIN submissions reuse an existing task so a duplicate browser
request cannot start another transaction. If verification fails after Pair,
rollback runs immediately on the active D-Bus connection and is bounded. BlueZ
failure or process shutdown can still leave external daemon state.

When Pair succeeds but rollback cannot be verified, MeshNet retains the exact
Device1 path, stable controller identity, and BlueZ owner only long enough to
block overlapping MeshNet flows. Flow removal releases that process-local proof
without delayed deletion. BlueZ has no bond-generation identifier, so an old
address/path cannot safely authorize `RemoveDevice` after another client may
have recreated the bond.

The PIN is transient: it is password-masked in the form and is not included in
gateway options, persistence, diagnostics, or logging. After pairing, MeshNet
re-reads BlueZ state before starting the provider client.

MeshNet records the radio address, current `hciN` name, and stable controller
address when it pairs a radio, but the record proves only historical setup.
Entry deletion and HACS removal do not change BlueZ, and ending an already-idle
flow cannot start delayed deletion. Canceling an active pairing transaction may
immediately roll back its uncommitted bond using the exact Device1 and stable
controller guards. The Configure removal form can perform a warned, explicitly
confirmed deletion of the current same-address bond; the option defaults off
because other clients may share or have recreated it.

## Normalized Models

File:

```text
custom_components/meshnet/models.py
```

Core models:

- `GatewayConfig`
- `GatewayStatus`
- `MeshPacket`
- `NodeState`
- `MessageRecord`
- `MeshSnapshot`

Provider clients should normalize into these models before data reaches Home Assistant entities.

## Coordinator

File:

```text
custom_components/meshnet/coordinator.py
```

The coordinator owns:

- Gateway lifecycle
- Merged node state
- Gateway status
- Packet deduplication
- SQLite persistence
- Outbound message queue replay
- Home Assistant events
- Mesh health score calculation

The coordinator uses `always_update=True` so periodic changes such as stale-node marking and health score updates are published to entities.

## Persistence

File:

```text
custom_components/meshnet/store.py
```

Database:

```text
<HA_CONFIG_DIR>/meshnet.sqlite3
```

Tables:

- `nodes`
- `messages`
- `packets`
- `routes`

Queued outbound messages are stored in `messages` with `direction = tx` and `raw.status = queued`. They are replayed when a connected gateway becomes available.

## Deduplication

File:

```text
custom_components/meshnet/dedupe.py
```

Packets are deduplicated by provider packet ID when available. If no packet ID exists, the fingerprint hashes normalized packet content plus a five-second timestamp bucket. This lets the same radio packet arrive through multiple gateways without duplicating message history.

## Entities

Files:

```text
custom_components/meshnet/sensor.py
custom_components/meshnet/binary_sensor.py
custom_components/meshnet/device_tracker.py
custom_components/meshnet/entities/
```

Entity groups:

- Mesh summary sensors
- Gateway online and packet sensors
- Node online, battery, RF, telemetry, and routing sensors
- GPS `device_tracker` entities when latitude and longitude are present

Gateway entities are created immediately from configuration. Node entities are created after node data arrives.

## Services And Events

Services:

- `meshnet.send_message`
- `meshnet.broadcast_message`
- `meshnet.schedule_message`
- `meshnet.refresh_gateway`

Events:

- `meshnet_packet`
- `meshnet_message_received`
- `meshnet_message_sent`

Message sends use a token bucket to reduce radio flooding.

## Websocket API And Panel

Files:

```text
custom_components/meshnet/websocket_api.py
custom_components/meshnet/frontend/meshnet-panel.js
```

Websocket commands:

- `meshnet/snapshot`
- `meshnet/messages`
- `meshnet/send_message`

All MeshNet websocket commands require an authenticated Home Assistant admin user. The sidebar panel is admin-only.

## Setup Tools

Scripts:

```text
setup.sh
install.sh
verify_setup.sh
uninstall.sh
```

Purpose:

- `setup.sh` detects the environment, checks devices, generates config, optionally installs the custom component, and writes rollback metadata.
- `install.sh` reads `.env` and calls `setup.sh`.
- `verify_setup.sh` checks staged files, installed files, TCP endpoints, serial devices, and Home Assistant config validation when available.
- `uninstall.sh` removes the component path recorded by setup metadata.

## Design Constraints

- Keep gateway IDs stable.
- Prefer normalized model tests over provider-specific assumptions.
- Keep setup scripts safe and idempotent.
- Do not silently edit Home Assistant YAML.
- Require admin access for UI/API paths that can transmit messages.
- Restrict Bluetooth pairing to a verified local BlueZ device and a temporary,
  non-default agent.
- Never persist or log a Bluetooth pairing PIN.

## Isolation Boundary

HACS custom integrations run inside the Home Assistant Core process. Strict
pairing validation and bounded cleanup reduce the Bluetooth attack surface, but
they do not isolate a crash or dependency failure from Home Assistant. True
process isolation requires a separate Home Assistant App or sidecar with a
small inter-process contract, such as MQTT.
