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
custom_components/meshnet/meshtastic_settings.py
custom_components/meshnet/meshcore_settings.py
```

Responsibilities:

- Connect to one configured transport.
- Translate provider packet shapes into `MeshPacket`.
- Translate node/contact/telemetry shapes into `NodeState`.
- Send outbound messages through the provider API.
- Report `GatewayStatus` back to the coordinator.

Supported transports are validated before runtime setup.

## Meshtastic Bluetooth Boundary (version 0.5)

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
Re-read bonded state -> begin bounded async Meshtastic connection
```

`custom_components/meshnet/bluetooth_devices.py` filters cached advertisements
for the Meshtastic service UUID, canonicalizes MAC addresses, and constructs the
selection list. A cached advertisement is not proof that a device belongs to a
local adapter because Home Assistant Bluetooth proxies also populate discovery.
The pairing backend therefore requires the exact device on a local BlueZ
adapter before changing bond state. Proxies are rejected.

MeshNet stores both the selected controller's current `hciN` name and stable
Adapter1 address. Pairing and runtime resolve the current identity from that
stable address and use the exact local Device1/scanner path. Other valid local
controllers may remain powered when the radio has one unambiguous ownership
path; a proxy, wrong controller, missing controller, or ambiguous identity
fails closed.

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

The runtime is a Bluetooth-only adaptation of the official asynchronous
Meshtastic Home Assistant transport. It reuses the installed Meshtastic
protobuf definitions but does not instantiate the synchronous SDK
`BLEInterface`, its private event-loop thread, or its global pubsub lifecycle.
For every attempt, MeshNet obtains a fresh Home Assistant `BLEDevice` from the
selected local scanner, connects with `bleak-retry-connector`, validates the
three Meshtastic GATT characteristics, subscribes to FromNum, sends
`want_config`, and actively drains FromRadio until configuration completes.

One supervisor owns the connection, reader, configuration, and fast post-active
reconnect tasks. Initial setup has hard connect and configuration deadlines.
The coordinator also schedules its single-flight, jittered stop-before-start
watchdog after either an initial failure or a connected-status loss; a recovered
status cancels that watchdog. Stop cancels and awaits subordinate tasks, then
performs bounded GATT teardown before the endpoint lease is released. A timed-out
ATT operation is never retried on its uncertain link, and reads and writes share
one bounded GATT-operation lock. No MQTT, broker, Internet, Wi-Fi, or LAN path is
part of this transport.

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

Meshtastic persistence retains provider records under their original keys, but
the coordinator publishes a reversible effective-node projection. Only exact,
valid 32-bit routing IDs with one consistently observed MAC/public-key proof
bundle are collapsed. A MAC-only observation and a public-key-only observation
are not combined into proof that no source record supplied. This projection is
shared by the panel, entities, Map, topology, and health calculations; raw
SQLite rows are neither rewritten nor deleted. New
NodeDB and packet callbacks use deterministic proof-aware keys. An observation
that carries a MAC and/or public key hashes every available proof together with
its routing ID, so one conflicting proof or ID cannot overwrite another before
the projection evaluates it. A MAC or public key shared by different routing
IDs is retained as unresolved evidence, and direct sends through either record
are blocked. Retained-key redirects preserve favorites and direct-message
compatibility. Malformed, conflicting, or unbound complementary evidence always
remains separate.

Config-entry setup opens persistence, restores cached state, and builds gateway
objects synchronously. Actual radio SDK startup and queued-message replay run in
a Home Assistant entry-owned background task. This is a deliberate isolation
boundary: Meshtastic's synchronous BLE constructor may spend minutes waiting for
discovery and configuration, but it cannot keep config-entry creation or the
frontend's final setup request open. Home Assistant cancels the background
waiter on unload, while each provider's shutdown path handles its transport.
Startup, reconnect, polling, direct-send, and outbox work is retained and
cancelled with finite waits. Provider callbacks carry a gateway-generation
token, so work from a replaced or unloaded gateway cannot publish state or
write storage. SQLite close is likewise deferred behind any executor operation
that already owns the connection; even a connect result that arrives after
cancelled setup keeps an exact close owner while config-entry unload remains
bounded.
Meshtastic send and refresh executor work is retained against the exact SDK
interface, and interface close waits behind that work without making Home
Assistant unload wait indefinitely. Failed entry setup rolls back forwarded
platforms and closes the coordinator; a failed platform unload leaves the live
coordinator in place for Home Assistant to retry safely.

## Gateway Settings Boundary

Files:

```text
custom_components/meshnet/gateway_settings.py
custom_components/meshnet/meshtastic_settings.py
custom_components/meshnet/meshcore_settings.py
custom_components/meshnet/sensitive_logging.py
```

Gateway settings use a protocol-neutral, typed contract between the admin-only
panel and provider clients:

```text
live provider read
        |
        v
sanitized typed schema + revision
        |
        v
browser-memory draft
        |
        v
server validation + single-use redacted preview
        |
        v
serialized one-shot provider writes (critical operations last)
        |
        v
fresh provider read + per-field verification
```

The coordinator owns one `GatewaySettingsManager`. The manager accepts only an
exact configured gateway ID, serializes operations per gateway, bounds field
counts and values, rejects duplicate or unsafe paths, and keeps at most one
five-minute preview per gateway in process memory. A preview is tied to a
server revision, expires after use, and is invalidated by a replacement
preview, coordinator reload, or shutdown. Provider exception text and submitted
secret values are never part of the public websocket result.

Provider adapters implement `async_get_settings_snapshot()` and
`async_apply_settings_plan()`. MeshCore local companion operations run under its
single native-command lock. Meshtastic Bluetooth admin operations share the
transport send/settings locks and correlate internal ADMIN_APP and routing
responses without publishing their payloads as mesh packets. Sensitive SDK
logger namespaces are suppressed for the complete credential-bearing read and
write scope and restored afterward.

A settings timeout is an unknown device state, not a retry signal. No provider
write is retried. A successful response is followed by live readback; a result
distinguishes verified and unverified fields. A MeshCore plan stops before its
next command as soon as one acknowledged write cannot be verified. When a
MeshCore Bluetooth PIN is verified, the provider returns a private handoff to
the coordinator. The coordinator updates only that gateway's connection option
so a reload can use the new PIN; the handoff is consumed before any public
response is built.

This boundary addresses accidental and unverified writes, not process
isolation. Settings are intentionally persistent radio state and cannot be
rolled back by uninstalling an in-process Home Assistant integration.

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
- GPS `device_tracker` entities only when latitude and longitude are finite and
  inside valid geographic ranges

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
- `meshnet_message_status`
- `meshnet_gateway_status`

Message sends use a token bucket to reduce radio flooding. Versioned status
events and action responses expose correlation and stable outcomes without raw
packet dictionaries, message text, credentials, or provider exceptions.

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
- `meshnet/traceroute` (admin-only, explicit Meshtastic Bluetooth unicast)
- `meshnet/settings/get`
- `meshnet/settings/preview`
- `meshnet/settings/apply`
- `meshnet/remote_settings/get`
- `meshnet/remote_settings/preview`
- `meshnet/remote_settings/apply`

All MeshNet websocket commands require an authenticated Home Assistant admin
user. The sidebar panel is admin-only. Local and remote settings commands
expose a typed, sanitized schema and preview token; there is no raw
provider-command websocket endpoint. Remote administration is additionally
Meshtastic-Bluetooth-only, exact-target-only, allowlisted to owner/display
fields, and fenced from SecurityConfig, keys, PSKs, destructive commands, and
Home Assistant services.

The traceroute command reserves a SQLite cooldown before any provider call. It
permits one manual traceroute across the integration every 3,600 seconds;
changing the gateway or destination cannot bypass that global floor, and a
timeout consumes the reservation. Its admin-only status command reads the
persisted cooldown and bounded last result without sending RF. No service,
poller, reconnect task, or automatic graph path can invoke traceroute.

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
- Never persist or log a Meshtastic BlueZ pairing PIN. Persist a changed
  MeshCore connection PIN only after the physically connected radio verifies
  it, and never return or log its value.
- Do not expose raw radio commands, generic remote RF administration, reset,
  firmware flashing, key/PSK mutation, SecurityConfig, or private-key
  import/export. Reviewed remote owner/display fields use a separate
  preview/confirmation/readback boundary.
- Treat a settings-write timeout as an unknown outcome and never retry it
  automatically.
- Treat topology as cached passive evidence: never infer node-to-node links
  from a shared gateway observation.
- Do not expose automatic traceroute. The admin-only manual WebSocket command
  uses a backend-persisted integration-wide floor of 3,600 seconds, with no
  startup, polling, refresh, graph-fill, service, or retry trigger.

## Isolation Boundary

HACS custom integrations run inside the Home Assistant Core process. Strict
pairing validation and bounded cleanup reduce the Bluetooth attack surface, but
they do not isolate a crash or dependency failure from Home Assistant. True
process isolation requires a separate Home Assistant App or sidecar with a
small inter-process contract, such as MQTT.
