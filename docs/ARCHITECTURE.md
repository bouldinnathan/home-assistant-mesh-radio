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
