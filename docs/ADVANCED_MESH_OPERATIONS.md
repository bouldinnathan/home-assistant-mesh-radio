# Advanced Mesh Operations

This guide is the operator contract for MeshNet's advanced local mesh tools.
It explains what each feature does, what radio traffic it creates, which data
Home Assistant retains, and the safety rules that the backend enforces even if
the browser is bypassed.

MeshNet is designed to keep working without Internet access, cloud services,
MQTT, or Wi-Fi. A supported radio still needs a local connection to Home
Assistant. The first advanced implementation uses the persistent Meshtastic
Bluetooth transport because that is the transport for which MeshNet can own,
correlate, cancel, and verify every request without blocking Home Assistant.

## Safety Summary

| Operation | Creates RF traffic | Automatic | Initial transport | Durable state |
| --- | ---: | ---: | --- | --- |
| Read cached nodes, messages, map, graph, or telemetry | No | Yes, from received packets | Any supported transport | Existing history cache |
| Send a text message | Yes | Only when an automation explicitly calls it | Supported sending transports | Message/outbox history |
| Load remote settings | Yes | Never | Meshtastic Bluetooth | No settings or session keys |
| Apply remote settings | Yes | Never | Meshtastic Bluetooth | Sanitized result only |
| Run a traceroute | Yes | Never | Meshtastic Bluetooth | Cooldown and sanitized route |
| Request NeighborInfo manually | Yes | Never | Meshtastic Bluetooth | Cooldowns and sanitized report |
| Run NeighborInfo maintenance | Yes | Only after explicit opt-in and an idle-time gate | Meshtastic Bluetooth | Cooldowns, aggregate scheduler state, and sanitized reports |
| Reorder or resize main Mesh cards | No | Never | Not applicable | Validated browser-only layout |
| Change, move, filter, or sort the graph | No | Browser animation only | Not applicable | Force positions in panel memory; rolling-day GPS observations in local SQLite |

The following rules are invariants, not UI suggestions:

- Reading, refreshing, polling, filtering, sorting, mapping, graph animation,
  and changing graph layout or limit never transmit a traceroute,
  NeighborInfo request, or remote-admin packet.
- A manual traceroute is unicast to one exact Meshtastic node. Broadcast,
  self, ambiguous, malformed, and unknown targets are rejected.
- A traceroute cooldown is reserved in SQLite before the radio write. A
  failure, timeout, restart, second browser, or simultaneous request cannot
  bypass the one-minute limit.
- An RF timeout has an unknown outcome. MeshNet never automatically retries a
  traceroute, NeighborInfo request, or remote setting write after a timeout.
- All advanced commands require a Home Assistant administrator.
- Node numbers and cryptographic identity are routing facts. Names are display
  labels and are never used to merge or silently retarget nodes.
- Graph distance never creates an edge. An edge exists only when MeshNet has
  received direct, route, or fresh NeighborInfo evidence. Solicited
  NeighborInfo is marked `manual_request`, opt-in maintenance evidence is
  marked `maintenance_scan`, and unsolicited and legacy evidence is marked
  `passive`. Manual traceroute results stay separate and never mutate the
  graph.
- SNR and RSSI are signal observations, not physical-distance estimates.
- Encrypted traffic that the connected radio cannot legitimately decrypt is
  not inspected, guessed, retained, or exposed.

## Main Mesh Workspace

Each side card on the main **Mesh** view can be reordered with a drag handle or
the accessible earlier/later buttons. A corner handle supports pointer resizing
and keyboard arrow resizing. Order and size are saved in a versioned browser
record so they survive navigation and reloads. Its exact schema accepts only
the fixed card-ID permutation and allowlisted bounded integer dimensions; it
cannot contain messages, node or gateway data, settings drafts, credentials,
or keys. It is never written to Home Assistant state or configuration, SQLite,
or radio firmware. Invalid or inaccessible browser storage fails closed to the
in-memory defaults. **Reset** restores the default and deletes the browser
record; use it before uninstall if the browser record must also be removed.

Both direct-message recipient selectors and the remote-administration,
traceroute, and NeighborInfo target selectors share one deterministic order:
favorites first, then valid last-seen timestamps from newest to oldest, with
stable name/identity tie-breakers. Favorite stars and last-seen labels are
visible in each selector. Invalid, ambiguous, and duplicate identities are not
offered as advanced radio-operation targets.

Traceroute applies two additional selected-gateway checks: the gateway's own
node and nodes not observed during the active radio session are omitted. The
backend repeats those checks before cooldown reservation, so a stale browser
cannot turn a rejected preflight into RF traffic.

The node-row **NeighborInfo** button performs no status request and transmits no
RF packet. It revalidates and selects one exact cached Meshtastic identity,
reveals the existing NeighborInfo card, and focuses **Load persisted status**.
An administrator must still load status, press **Request NeighborInfo**, review
the warning, and press **Confirm request**. The backend cooldown reservation
and validation remain authoritative even if the browser is bypassed.

## Remote Administration and Keys

### What PKI remote administration means

Meshtastic firmware 2.5 and later authorizes remote administration by storing
the controlling radio's public key in one of the target radio's three Admin
Key slots. The controlling radio keeps its own private key and performs the
cryptographic operation. Home Assistant does not need the private key.

MeshNet therefore uses this model:

1. Home Assistant connects locally to the controller radio over Bluetooth.
2. MeshNet offers a copy-only view of that radio's existing public key.
3. The operator locally provisions that public key into an Admin Key slot on
   each target, using the official app or CLI while physically connected to
   the target.
4. The target must also have supplied a valid public key in the controller's
   node database. Merely knowing a node name is insufficient.
5. MeshNet asks the controller radio to send authenticated remote Admin
   messages. The controller radio, not Home Assistant, uses its private key.

Up to three controlling public keys may be stored by a target. Removing or
rotating those keys must be done with the official local tools in the initial
release.

### Why private-key import is deliberately absent

MeshNet does not provide a private-key text box, file upload, service field, or
WebSocket field. It never exports a private key from the radio. A private key,
admin session passkey, or channel PSK must never enter browser state, SQLite,
the Home Assistant Store, logs, diagnostics, repairs, events, action responses,
or exception text.

This is both safer and simpler than making Home Assistant a key vault. It also
preserves the official trust model: possession of the physical controller
radio is part of administrative authority.

Meshtastic's SecurityConfig is excluded from generic remote reads and writes.
An official security-config read can include the private key, and a partial
security-config write can regenerate a keypair. MeshNet returns only derived,
non-secret capability flags such as "public key available" and "remote admin
eligible." It does not return the SecurityConfig payload.

### Provisioning a target

To authorize the MeshNet controller radio:

1. In MeshNet, open **Remote administration** and select the connected
   Meshtastic Bluetooth gateway.
2. Copy the controller public key. Confirm that the displayed node number and
   short name match the physical controller.
3. Connect the official Meshtastic app or CLI locally to the target radio.
4. Add the copied public key to one free **Security > Admin Key** slot.
5. Save, reconnect to the controller, and wait for the target's NodeInfo/public
   key to be present in the controller's node database.
6. In MeshNet, select the target and use **Test access**. This performs an
   explicit, read-only request; it does not change the target.

Do not enable Managed Mode until remote read and write have both been tested
on a non-critical node. A wrong radio region, channel, LoRa preset, key, role,
or power setting can make a remote node unreachable.

### Supported remote settings

The remote editor follows the same safe shape as the local gateway editor:

- The target and gateway are selected by exact stable identity.
- Settings are fetched only after an explicit Load/Test access action.
- Remote loads are never part of the five-second panel snapshot poll.
- Only documented scalar and enum fields in an allowlist are displayed.
- Fields unsupported by the target firmware are omitted; unknown fields fail
  closed instead of being guessed or exposed as editable controls.
- Values are validated against the protobuf type, enum, numeric range, byte
  limit, and additional MeshNet safety constraints.
- Changes remain in the current browser tab until Preview.
- Preview produces a bounded diff and a short-lived, single-use token held in
  process memory.
- Every remote write requires a separate confirmation after Preview.
- One target may have only one admin operation in flight.
- A write uses the target's eight-byte session passkey only in memory, for at
  most the firmware's session lifetime, and never returns it to the UI.
- The write is sent once. MeshNet then requests the changed section again and
  reports Verified, Mismatch, Rejected, or Unknown outcome.

The safe initial allowlist covers the owner long/short name and reviewed
non-secret DisplayConfig preferences that the existing settings planner can
represent and verify. Bluetooth configuration is intentionally excluded from
the first remote editor because changing another node's pairing behavior can
create an avoidable local-access lockout. The following remain excluded even
when the firmware offers them:

- every SecurityConfig write, including public/private/admin keys, Managed
  Mode, legacy admin, serial security, and debug-security controls;
- channel PSKs and arbitrary channel replacement;
- factory reset, node database reset, shutdown, reboot, and device deletion;
- OTA/DFU, filesystem operations, backup/restore, and firmware mutation;
- fixed-position commands that could publish a home's precise location;
- contacts, key verification, ignored-node lists, and arbitrary protobufs;
- raw AdminMessage passthrough and remote-admin automation services.

Use the official app or CLI locally for excluded operations. This division is
intentional: the Home Assistant integration remains a validated operator
surface, not a root console for radios.

### Remote-admin errors

MeshNet presents stable categories without echoing packet contents or keys:

| Result | Meaning | Safe next action |
| --- | --- | --- |
| Target public key unavailable | Controller NodeDB lacks the target key | Wait for NodeInfo or provision a verified contact locally |
| Controller unauthorized | Target does not accept this controller public key | Provision the exact displayed key locally on the target |
| Session expired or rejected | The short-lived admin session is invalid | Explicitly load settings again; no write is retried |
| No route / no response | The target could not be reached or did not answer | Check passive last-seen data and try later |
| Duty-cycle or rate limit | Firmware refused additional RF traffic | Wait; do not repeatedly submit the action |
| Readback mismatch | Target answered but the requested value was not observed | Stop and inspect the target locally |
| Unknown outcome | A write may have left Home Assistant but was not verified | Do not repeat blindly; inspect/reconnect first |

## Messages

### Conversation layout

The **Messages** tab provides three kinds of conversation:

- **Broadcast / Primary**: packets sent or received on channel index 0.
- **Other channels**: separate threads for configured/observed channel indices
  1 through 7. The initial UI labels them `Channel N`; it never exposes PSKs.
- **Direct**: one thread per exact peer identity, with long name and short name
  used only as labels. If User/NodeInfo has not arrived, `!xxxxxxxx` is the
  correct label and the record is not merged with a guessed node.

Message history is loaded from the bounded `meshnet/messages` WebSocket API,
not the abbreviated panel snapshot. Malformed records are skipped. Text is
HTML-escaped, UTF-8 validated, and bounded to the radio payload limit.

Meshtastic reactions carry the original packet's numeric `reply_id`. MeshNet
projects that as `meshtastic:<uint32>` and joins it only to a record with that
exact on-air ID. Multiple matching reactions are grouped by Unicode scalar and
sender; an unresolved reaction remains visible as an orphan. BLE and native
serial/TCP sends retain the radio-returned packet ID. MQTT submission does not
return an on-air ID, so MeshNet does not guess a match for that local outbound
record.

The conversation selector and message draft survive the five-second cached
snapshot refresh while the panel remains open. They are not written to browser
storage. The message timeline also preserves its exact scroll position during
refresh unless it was already near the bottom, in which case it follows the
newest message. Navigating away discards unsent text.

### Delivery wording

`Submitted`, `queued`, `sent to radio`, and `delivered` are different states:

- **Queued** means MeshNet durably accepted the message for a compatible
  gateway that was unavailable or failed during submission.
- **Sent** means the connected radio accepted the local request.
- A broadcast acknowledgement can be implicit evidence that one relay handled
  the packet; it is not proof that every node received it.
- A direct-message acknowledgement is stronger transport evidence, but the
  first version does not claim human delivery or that the recipient read it.

Inbound direct/broadcast classification is made only when an exact destination
or MeshCore's explicit `contact_message` event proves it. If the provider omits
enough data, the UI says `Unknown delivery` rather than guessing.

## Manual Traceroute

Traceroute is intentionally separate from graph refresh.

### Operator flow

1. Open a Meshtastic node's details in the Mesh or Messages view.
2. Select an active Meshtastic Bluetooth gateway.
3. Press **Traceroute**, review the RF-traffic notice, and confirm.
4. MeshNet reserves the integration-wide cooldown in SQLite.
5. One RouteDiscovery application packet is submitted to that destination;
   it inherits the connected radio's configured LoRa hop limit. MeshNet
   performs no application retry. Firmware may still relay or retransmit
   reliable traffic.
6. A correlated response, routing rejection, or timeout completes the action.

The button displays the next permitted time. The backend permits at most one
manual traceroute across the entire MeshNet integration every 60 seconds,
regardless of gateway or destination. The reservation is shared by every
browser session and survives Home Assistant restarts. The panel reloads the
persisted cooldown and last privacy-bounded result, including completion time
and available per-hop SNR, instead of treating a browser reload as a fresh
airtime budget. A rejected local validation does not reserve airtime; once the
backend reaches the reservation step, all outcomes consume the minute because RF
transmission may have occurred.

The panel distinguishes stable preflight failures from an unknown
post-reservation outcome. Self, stale-session, gateway, and validation failures
send no RF and do not fabricate a new browser cooldown. If a request may have
been submitted, the panel remains locked and the persisted status endpoint is
authoritative before another attempt.

There is no traceroute Home Assistant service and no automation action in the
initial implementation. It cannot be invoked by snapshot polling, coordinator
refresh, reconnection, diagnostics, graph animation, map opening, or startup.
There is no broadcast, batch, scheduled, automatic, or retry mode.

### Manual NeighborInfo request

An administrator may request one exact known Meshtastic node's cached
NeighborInfo report through a connected Bluetooth gateway. MeshNet atomically
reserves 180-second integration-wide and same-target cooldowns in SQLite
before submitting one unicast application packet without an application-level
retry. The request inherits the connected controller radio's configured LoRa
hop limit. Those floors
are shared by every gateway and browser and survive Home Assistant restarts.

The manual request is never sent by graph rendering, refresh, startup,
reconnection, diagnostics, services, automations, or polling. It has no
broadcast, batch, schedule, or retry form. MeshNet accepts only a non-MQTT
response whose request ID, source, destination, and channel match the submitted
packet, caps the report at ten neighbors, and stores only the sanitized result.
This experimental request is verified against firmware 2.7.26 and requires
Neighbor Info to be enabled on the target. Older firmware may reject it or time
out. The successful result is cached zero-hop evidence, not a live scan; an
empty successful report means no cached neighbors, while a timeout is unknown.
Every post-reservation outcome consumes both cooldowns.

Privacy-safe Bluetooth diagnostics count response, routing rejection, timeout,
cancellation, send failure, and disconnect separately. A correlated routing
NAK retains only its bounded protocol enum (for example `BAD_REQUEST`), never a
node ID or raw packet. `BAD_REQUEST` can mean that the target firmware
does not support the request or Neighbor Info is disabled; MeshNet still never
retries it.

### Opt-in maintenance NeighborInfo

Automatic network maintenance is a separate, disabled-by-default option. When
an administrator enables it, MeshNet can start a bounded NeighborInfo cycle no
more often than once per hour through one exact Meshtastic Bluetooth gateway.
The scheduler waits for the configured quiet period, defers immediately when
legitimate radio or foreground work appears, and spaces requests by at least
one minute. It never catches up with a burst after downtime, never broadcasts,
never requests a traceroute, and never retries a failed or unknown request.
Manual and maintenance requests share the same durable global and per-target
NeighborInfo cooldowns.

A maintenance response uses the same exact identity, correlation, size,
non-MQTT, and one-hour graph-freshness checks as a manual response, but it keeps
the distinct `maintenance_scan` provenance. The graph can display that cached
friend-of-friend evidence; opening or manipulating the graph never starts or
wakes the scheduler.

### Route interpretation

The result may contain a forward route, reverse route, and per-hop quarter-dB
SNR observations. MeshNet validates size, source, destination, request ID,
channel, and hop ordering before caching it. It labels the result with source
and age. Route evidence can expire or become stale; it is not a guarantee that
the same route will be used for the next packet.

## Moving, Distance-Aware Graph

The **Layout** selector offers two local views over the same cached evidence.
Neither layout is a live route query, tile map, RF coverage model, or reason to
transmit a packet.

### Geographic scale

**Geographic scale** is the default. Every sufficiently precise located node
and rolling-day trail point is projected into one local coordinate plane with
the same meters-per-pixel value on the horizontal and vertical axes. Longitude
is unwrapped across the date line, and the projection uses the displayed
latitude range so geometry remains finite. A 1/2/5 scale bar reports a readable
distance in Home Assistant's metric or imperial unit system. Located direct
connections therefore have an actual map scale, but the plot has no basemap and
must not be interpreted as radio range.

A located node's previous positions remain as a 24-hour trail. MeshNet records
at most one validated observation per node per UTC hour, returns at most 25
samples per node and 100 nodes, and prunes older observations regardless of the
normal message-history setting. The full rolling-day trail participates in the
extent, so a node that moves during the day does not make the scale refit only
around its newest point. Nodes without valid, sufficiently precise coordinates
remain visible in an **Unlocated endpoints — not to scale** rail outside the
scaled plotting area.

### Topology

**Topology** is the force-directed view inspired by Home Assistant's Zigbee
graph:

- Nodes repel each other and evidence-backed edges act as springs.
- The layout animates with `requestAnimationFrame` and can be dragged.
- Force positions live only in the current panel element; they are not written
  to SQLite, Home Assistant configuration, or radio firmware.
- Reduced-motion preference disables continuous motion and uses a stable
  bounded layout.
- Animation is stopped when the panel is detached or another view is selected.
- Coordinates, velocities, iterations, and node counts are bounded so corrupt
  input cannot create an infinite loop or non-finite SVG values.

### Evidence-aware node bounds

The selector shows the 20, 50, or 100 most recently heard nodes, defaulting to
50, but it does not simply truncate a flat node list. MeshNet considers complete
direct, fresh NeighborInfo, and cached route endpoint pairs before filling
remaining capacity by recency. Direct gateway observations have
the highest edge-selection priority. The displayed count never exceeds the
selected limit, an edge is never rendered with only one endpoint, and a bounded
gateway cap prevents a large gateway set from consuming the whole view. When
the cap cannot retain every direct observation, the panel reports the omitted
count so the operator can select 50 or 100 rather than infer that no connection
exists. Changing layout or limit is local browser work and generates no radio
traffic.

### Edge sources

Edges are visually distinguished and exist only because of received evidence:

- **Cached direct/neighbor evidence** means the selected gateway has an exact
  cached direct observation for that node. It does not mean the node is still
  one-hop reachable now.
- **Friend-of-friend NeighborInfo** means one exact node reported another. It
  is never promoted to a direct gateway link. The report must be non-MQTT, no
  more than one hour old, timestamp-bounded, and exact and unambiguous at both
  endpoints. Five minutes of future clock skew is tolerated; older,
  farther-future, malformed, ambiguous, or MQTT-sourced evidence is ignored.
  Unsolicited or legacy reports are labeled **Passive NeighborInfo**, explicit
  operator results are **Requested NeighborInfo**, and opt-in quiet-time
  results are **Maintenance NeighborInfo** with separate line styling.
- **Cached route/path evidence** uses exact endpoint pairs from a previously
  received route record. Its label says cached evidence rather than a live
  route.
- **Manual traceroute results** are displayed separately with their age and
  durable cooldown. MeshNet does not silently add those routes to the passive
  layout, avoiding a stale active measurement being mistaken for a current
  neighbor observation.
- Missing, ambiguous, or invalid route identifiers do not create edges.
- Physical proximity alone never creates an edge.

### Physical-distance spring length

When both endpoints have valid locations, MeshNet computes great-circle
distance with the Haversine formula. Geographic scale uses equal
meters-per-pixel geometry. Topology maps the distance monotonically to a
bounded, deliberately compressed spring length so the force graph stays
readable; topology pixels are not meters. Each located edge displays its
great-circle distance in miles at the line midpoint. That label is not an
estimate of RF range or route length.

Fallback order for a gateway endpoint is:

1. the exact local radio node's valid cached GPS position;
2. Home Assistant's configured home latitude/longitude, used only inside the
   browser as a visibly labeled `Home Assistant location fallback`;
3. a neutral default spring length.

The fallback is never copied into a radio node, packet, diagnostic file, event,
or MeshNet database. Invalid, zero-pair, deliberately imprecise, or missing
locations use the neutral spring in Topology and the not-to-scale rail in
Geographic scale. A cached position can be older than the node's last-heard
time and is labeled as cached rather than presented as live. SNR/RSSI never
substitutes for GPS.

### Local retention and browser privacy

Validated trail samples live only in the isolated
`graph_position_observations` table in local `meshnet.sqlite3`. The table is
hard-pruned to the rolling 24-hour window; it does not follow a longer
`history_days` option. The frontend receives only the bounded history needed to
draw the selected local panel. It does not write node IDs, names, coordinates,
trails, or graph positions to `localStorage`. The only persistent browser data
used by MeshNet is the separately validated main-card layout described above.

## Home Assistant Automations and Network Failures

### Sending

Use the existing actions:

- `meshnet.send_message`
- `meshnet.broadcast_message`
- `meshnet.schedule_message`
- `meshnet.refresh_gateway`

`send_message` accepts an exact target, gateway, channel 0–7, priority, and
message type. The common backend enforces UTF-8 and the radio payload bound.
Queued messages are retained by the outbox. The action response returns a
schema version, correlation ID, and current status so an automation can record
the accepted operation without parsing logs.

Example direct send:

```yaml
action:
  - action: meshnet.send_message
    data:
      target_node: "meshtastic:!1234abcd"
      gateway_id: "garage_radio"
      channel: 0
      message_type: direct
      message: "Generator alarm acknowledged"
    response_variable: mesh_send
```

### Stable events

MeshNet exposes bounded, versioned events. Event payloads never contain raw
provider objects, credentials, admin packets, channel PSKs, private keys, or
exception strings.

| Event | Purpose |
| --- | --- |
| `meshnet_message_received` | Valid decoded inbound text |
| `meshnet_message_status` | `queued`, `sent`, `blocked`, or `failed` transition |
| `meshnet_gateway_status` | Connected/disconnected/start/reconnect transition |

`meshnet_packet` is retained only as a deprecated compatibility event with a
strict metadata projection. Automations should use entities and the events
above; arbitrary raw packet and payload dictionaries are not a stable API.

Example notification on a direct or channel message:

```yaml
trigger:
  - platform: event
    event_type: meshnet_message_received
condition:
  - condition: template
    value_template: >-
      {{ trigger.event.data.schema_version == 1
         and trigger.event.data.delivery in ['direct', 'channel'] }}
action:
  - action: persistent_notification.create
    data:
      title: "Mesh message"
      message: "{{ trigger.event.data.text }}"
```

### Failure detection

Use each gateway's connectivity binary sensor as the simplest reliable
trigger. It is `off` when that adapter is disconnected. Gateway last-packet,
last-connected, packets, and monotonic failure-count sensors provide context.

For richer automations, `meshnet_gateway_status` includes only:

- `schema_version`
- exact configured `gateway_id` within the local Home Assistant instance
- protocol and transport
- previous and current connectivity
- stable transition and reason category
- monotonic failure count
- whether reconnect is scheduled
- occurrence time

The event is emitted on a real transition, not every coordinator refresh.
Stale callbacks from a prior gateway generation are ignored.

## Passive Weather and Sensor Data

MeshNet passively decodes documented telemetry that a configured radio already
receives and is authorized to decrypt. Supported data includes device metrics,
temperature, humidity, pressure, air quality, power channels, radio statistics,
and other documented finite scalar metrics. These become normal Home Assistant
sensor or binary-sensor entities and can be used with state triggers.

This is not promiscuous capture:

- MeshNet cannot decode other channels without their PSKs.
- It cannot read PKI direct messages exchanged between other nodes.
- Unknown ports and unknown telemetry variants are ignored, not guessed.
- ADMIN_APP/config/session payloads are consumed internally or dropped and are
  never published as telemetry.
- Metric keys, values, text lengths, and per-node metric counts are bounded.
- NaN, infinity, invalid coordinates, and malformed messages are rejected.
- Valid numeric zero values are preserved.
- Packet deduplication and LoRa/MQTT provenance remain visible where known.

For automations, prefer a node's Home Assistant sensor state over a generic
packet event. A cached sensor remains available after its node goes offline, so
check that node's **Online** and **Last heard** entities for freshness. MeshNet
does not poll remote sensors merely because a dashboard is open.

## Privacy, Recovery, and Uninstall

Advanced features keep MeshNet compartmentalized as a custom integration:

- RF actions go only through a configured adapter and bounded coordinator path.
- The browser cannot call provider objects directly.
- Cooldowns and cached routes live in `meshnet.sqlite3` with the existing
  integration history; they do not alter Home Assistant core databases.
- Browser drafts and force-layout coordinates remain current-panel-instance
  memory only. The sole persistent browser value is the versioned Mesh-card
  layout: an exact allowlist of card IDs and bounded integer sizes. **Reset**
  deletes it; no content, identity, setting, credential, or key is eligible.
- Diagnostics remain cached and redacted and never run remote admin or
  traceroute.
- Unloading cancels in-flight waiters and animation owners. Late callbacks are
  fenced by gateway generation.
- Removing the integration stops adapters and entities. HACS removal deletes
  component code; deleting `meshnet.sqlite3` after the integration is unloaded
  removes the optional history/cooldown cache. HACS cannot execute removed
  panel code, so press **Reset** before uninstall to remove the browser-only
  card-layout record too.

If a remote setting has an unknown outcome, stop. Do not repeatedly press
Apply. Reconnect locally with the official app/CLI, inspect the target, restore
known-good settings, and only then retry remote administration.

## Acceptance and Regression Contract

The implementation is complete only when automated tests prove all of these:

### Keys and remote administration

- No private key, session passkey, channel PSK, or sentinel credential reaches
  frontend JSON, storage, diagnostics, logs, events, action responses, or
  public exceptions.
- A generic AdminMessage and every SecurityConfig mutation are rejected by the
  backend even when WebSocket validation is bypassed.
- A remote read requires exact target identity, public-key presence, Bluetooth,
  explicit admin action, and a correlated response.
- A write obtains/uses the eight-byte session key in memory, sends once,
  serializes per target, and performs exact readback verification.
- Unauthorized, missing-key, bad-session, no-route, duty-cycle, timeout, and
  unknown-outcome results remain distinct and actionable.

### Messages and automation

- Broadcast, channel, direct, and unknown-delivery records group correctly.
- Exact identity is retained; duplicate names never merge conversations.
- Draft, focus, and selected conversation survive snapshot refresh; navigation
  clears the draft.
- Timeline refresh preserves reading position or follows the bottom, and a
  reaction joins only through an exact validated on-air packet ID; unresolved
  reactions stay visible.
- Malformed/oversized text and records are rejected and every rendered value is
  escaped.
- Immediate send, offline queue, provider failure, replay, and permanent block
  each emit exactly one safe status transition with a correlation ID.
- Deprecated packet events cannot expose raw payloads or admin/config data.

### Traceroute

- Broadcast, self, unknown, ambiguous, wrong-protocol, and wrong-transport
  destinations fail before reservation or transmission.
- The selected gateway and cached-only nodes are removed from the browser
  target list; the backend independently requires active-session observation.
- The packet copies the controller radio's bounded configured LoRa hop limit.
- Reservation is atomic, stored before send, survives database reopen, and
  permits one manual traceroute across the entire MeshNet integration every
  60 seconds.
- Simultaneous callers cannot both transmit. Timeout/failure consumes the
  reservation and is never retried.
- Only the admin WebSocket/manual panel path reaches the provider; polling,
  refresh, startup, diagnostics, messages, maps, graph, services, and
  automations have no traceroute call path.
- Response request/source/destination/channel and bounded hop order are
  validated before caching.

### Graph

- Limits accept only 20, 50, or 100 and evidence-aware selection preserves
  complete endpoint pairs while prioritizing direct gateway observations.
- Geographic scale uses equal finite meters-per-pixel axes, a bounded 1/2/5
  scale bar, date-line-safe coordinates, and a separate not-to-scale rail.
- Position history accepts only validated coordinates, retains no more than
  one observation per UTC hour and 25 per node, and is hard-pruned after 24
  hours in its isolated SQLite table.
- Haversine distance is finite, monotonic, and clamped; invalid or missing GPS
  uses the neutral spring or the unlocated rail.
- Radio GPS wins over the labeled, browser-only Home Assistant fallback.
- Proximity never creates an edge and SNR/RSSI is never converted to distance.
- NeighborInfo creates only one-hour-fresh friend-of-friend edges with exact
  identities and distinct passive, manual, or maintenance provenance.
- The force step remains finite and bounded; animation and drag listeners stop
  on detach/view change; reduced motion has no continuous animation.
- Changing layout or limit, drawing trails, or moving a node performs zero
  transport calls and never invokes traceroute or maintenance scheduling.
- Browser storage never receives node identity, coordinates, trails, or graph
  positions.

### Telemetry and gateway health

- Only documented decoded variants produce typed, bounded entities.
- Metric count/key/value limits, finite-number checks, zero preservation,
  deduplication, and provenance are tested for Meshtastic and MeshCore.
- ADMIN_APP and unknown/config ports never become events or entities.
- Gateway transitions fire once with monotonic failure counts and stale
  lifecycle callbacks cannot emit failures.

## Official Protocol References

- [Meshtastic remote node administration](https://meshtastic.org/docs/configuration/remote-admin/)
- [Meshtastic security configuration](https://meshtastic.org/docs/configuration/radio/security/)
- [Meshtastic CLI usage](https://meshtastic.org/docs/software/python/cli/usage/)
- [Meshtastic Python API](https://python.meshtastic.org/)
- [Admin protocol definition](https://github.com/meshtastic/protobufs/blob/master/meshtastic/admin.proto)
- [Mesh packet, route, neighbor, and error definitions](https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto)
- [Port number definitions](https://github.com/meshtastic/protobufs/blob/master/meshtastic/portnums.proto)
- [Telemetry protocol definition](https://github.com/meshtastic/protobufs/blob/master/meshtastic/telemetry.proto)
