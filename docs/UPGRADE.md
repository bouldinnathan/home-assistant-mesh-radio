# Upgrade Guide

## From 0.7.0

0.8.0 attaches Meshtastic reactions to the exact referenced on-air packet,
preserves the Messages timeline position during refresh, displays located graph
edge distances in miles, changes the persisted integration-wide manual
traceroute cooldown to 60 seconds, and adds an experimental manual NeighborInfo
request. Restart Home Assistant and hard-refresh the browser so the versioned
panel JavaScript is replaced.

NeighborInfo requests are administrator-only, Meshtastic-Bluetooth-only,
unicast, and never automatic or retried by MeshNet. Global and same-target
180-second reservations are persisted before submission. The feature is
verified with firmware 2.7.26 and requires the target's Neighbor Info module;
older firmware may reject or time out. A response is a cached zero-hop report
of at most ten nodes, not a live scan, and a timeout does not mean zero
neighbors.

The existing SQLite file is extended in place with isolated NeighborInfo
reservation/result state. No Home Assistant core configuration or radio
setting is changed by this migration. The graph can use Home Assistant's Home
location as a browser-only fallback for a gateway without cached GPS; MeshNet
does not write a fixed position to the radio.

## From 0.6.2

0.7.0 adds an app-like **Messages** view, 20/50/100-node moving passive graph,
distance-aware evidence-backed springs, manual Meshtastic Bluetooth traceroute,
versioned automation outcomes, bounded passive telemetry, and a guarded remote
node editor. Restart Home Assistant and hard-refresh the browser so the new
panel JavaScript replaces its cached 0.6.x copy.

Traceroute is manual and administrator-only. The backend reserves a SQLite
cooldown before the single application request and permits at most one manual traceroute
across the entire MeshNet integration every 60 seconds. Failure and timeout
consume it, and there is no service, polling, startup, graph-fill, scheduled,
broadcast, or retry path.

Remote administration initially supports only a connected Meshtastic
Bluetooth controller, one exact known target with a valid public key, owner
long/short names, and reviewed display options. Copy the controller radio's
displayed public key into an Admin Key slot on the target using the official
app or CLI first. MeshNet has no private-key, SecurityConfig, admin-key-slot,
channel-PSK, raw AdminMessage, reset, firmware, or remote-admin service API.
Every write requires Load, Preview, explicit confirmation, a short-lived
single-use server token, one transmission, and readback verification. Treat an
unknown outcome as potentially applied and do not retry blindly.

The existing `meshnet.sqlite3` is extended in place with an isolated
`traceroutes` table. Removing the integration leaves no Home Assistant core
configuration changes; deleting that file after unload removes MeshNet history
and cooldowns. Intentional settings written to radio firmware survive uninstall
and must be reverted on the radio. See
[Advanced Mesh Operations](ADVANCED_MESH_OPERATIONS.md) before enabling remote
administration on anything important.

## From 0.6.1

0.6.2 makes the **Gateway settings** page distinguish working read-only fields
from editable fields. It reports exact editable/read-only counts, marks each
editable control, and gives a clear explanation when no setting can be changed
safely. Known Meshtastic capability codes are shown as readable explanations.
Unknown future enum values and out-of-range values that the server has already
made read-only remain visible as disabled controls instead of rejecting the
entire page.

Meshtastic owner records are now retained when firmware sends the local
`node_info` record before `my_info`, so the reviewed long-name and short-name
controls do not disappear because of stream ordering. This does not make
additional radio, security, channel, network, module, reset, or remote-admin
fields writable.

Privacy-safe diagnostics now cache the last settings capability decision for
each configured gateway. They include fixed connection/capability states and
aggregate source, accepted, editable, read-only, and contract-downgrade counts.
They never trigger a settings read and contain no gateway IDs, names, field
paths, labels, values, options, revision material, provider reasons, or secret
state.

No setting is changed during the upgrade or by opening/reloading the page.
Only **Apply preview** can send a validated write. Restart Home Assistant and
hard-refresh the browser after installing 0.6.2.

## From 0.6.0

0.6.1 fixes the Gateway settings page rejecting a valid Meshtastic unsigned
64-bit bitmask bound that had already been clamped to JavaScript's maximum safe
integer by the server. The page now accepts the same integer range as the
server. A failed or future incompatible settings schema also stops after one
automatic attempt instead of being retried on every Home Assistant state
update; use **Reload live values** to make an explicit new attempt.

No radio setting is changed during this upgrade. Restart Home Assistant and
hard-refresh the browser after installing so the corrected panel module loads.

## From 0.5.11

0.6.0 adds a dedicated **Gateway settings** tab to the admin-only MeshNet
sidebar. It reads supported values from the physically connected radio, keeps
drafts in the current browser tab, requires a server-generated redacted
preview, applies each exact preview once, and rereads the radio to distinguish
verified from unverified fields. Connection-critical operations require an
additional confirmation and run last.

MeshCore native companion connections over Bluetooth, serial, and TCP support
validated writes for the fields made writable by their live schema.
Meshtastic writes are limited to MeshNet's bounded direct Bluetooth transport;
Meshtastic serial/TCP and MQTT/REST bridge settings remain read-only in 0.6.0.
Managed Meshtastic radios and unsupported firmware/SDK fields also fail closed
to read-only. There is no raw-command, remote RF admin, reset, firmware-flash,
or private-key interface.

Existing secret values are never returned. New values are write-only and held
only in the browser draft and short-lived in-memory preview while applying. A
verified MeshCore Bluetooth PIN change is persisted only to that gateway's
connection option so it can reconnect; the Meshtastic BlueZ pairing PIN remains
transient and is never stored. See [Gateway Settings](GATEWAY_SETTINGS.md) for
the support and recovery matrix.

Treat a settings timeout as an unknown radio state and never repeat the write
blindly. Reload live values after reconnect. Intentional settings persist in
radio firmware, so removing the config entry or uninstalling through HACS does
not restore prior radio values. There is no config-entry or SQLite schema
migration for this feature.

0.6.0 also replaces the sidebar's raw Meshtastic record count with a conservative
distinct-node projection. A NodeInfo/configuration record historically used a
MAC-based key while a later packet from the same radio used its `!xxxxxxxx`
routing ID, so one physical node could become two or three cached records,
devices, map trackers, and graph entries. New NodeDB and packet observations
now use deterministic proof-aware keys and one shared effective-node
projection. When MAC and/or public-key proof is present, a one-way composite key
binds every available proof to the routing ID. A conflicting observation can no
longer overwrite the earlier proof before the projection sees it.

At startup MeshNet groups existing records only by an exact, valid, nonzero,
non-broadcast 32-bit Meshtastic ID. A group collapses only when every record is
internally consistent and all strong proof belongs to one observed MAC/public-
key bundle. Separate MAC-only and public-key-only records do not establish a
relationship between those proofs and therefore remain unresolved unless one
record actually carries both.
Conflicting or malformed evidence fails closed and remains separate. Names are
taken from one coherent donor, the freshest connectivity observation remains
atomic, and no grouping is performed by name, location, signal, or protocol
guessing.

If one MAC or public key appears under different routing IDs, MeshNet also keeps
those records separate and disables direct messaging through either identity.
That evidence may represent an old ID, cloned configuration, or corruption;
MeshNet does not guess which record should receive a message.

The projection feeds the sidebar, passive graph, Map trackers, sensors, binary
sensors, health calculations, and direct-recipient list. Original SQLite rows
are retained unchanged and are reported separately from the distinct count;
there is no database or Home Assistant registry deletion. Old alias entities
therefore become unavailable after reload instead of being destructively
removed. An existing `MeshNet Favorite` label on one of those retained alias
devices still marks the effective node as favorite, and an old alias remains a
valid direct-message target.

This migration sends no NodeInfo request, traceroute, or other radio packet.
Restart Home Assistant and hard-refresh the browser after installing so the
versioned panel module loads.

## From 0.5.10

0.5.11 hardens direct Meshtastic Bluetooth startup for radios with a large or
temporarily slow node database. `FromRadio` reads now follow Meshtastic's
write/notification-triggered drain sequence, include the official post-write
settling delay and bounded empty-response retries, and use a separate read
deadline that cannot preempt the 60-second configuration budget.

A timed-out ATT operation is still treated conservatively: MeshNet never
retries that read or write on the same uncertain connection. It cancels and
joins the session owner, confirms GATT teardown, and only then uses the existing
single-flight, jittered reconnect loop to open a fresh link. Initial startup
failures now enter that loop automatically instead of requiring a manual
reload. A successful recovery clears its `gateway_start` repair. Reads and
writes also share one bounded ATT-operation lock so a message or heartbeat
cannot overlap an active `FromRadio` transaction.

No gateway, pairing, node, entity, message, or history migration is required.
Install the update and restart Home Assistant. Keep the phone app disconnected
because Meshtastic radios still permit only one client connection.

## From 0.5.9

0.5.10 keeps periodic sidebar snapshots from replacing an active message,
recipient, gateway, channel, priority, delivery, or sort control. Polling and
data collection continue while the control is active; one pending visual update
is applied after focus leaves the editor. Focus detection follows Home
Assistant's shadow roots, so native selects, text selection, cursor position,
and input-method composition are not interrupted by the five-second refresh.

Meshtastic node labels now show the long or user name together with a distinct
short name. Provider variants such as `short_name`, `shortname`, `long_name`,
and `longname` are normalized. Records that have not received NodeInfo are
labeled as unnamed with their normalized `!xxxxxxxx` identifier instead of
presenting the identifier as if it were a name. Exact canonical destination
keys remain unchanged in selector values. Recipient labels include the
normalized Meshtastic `!ID`; when two other nodes still have the same visible
name, only those colliding choices gain an exact node-ID or canonical-key
suffix so they cannot be mistaken for one another.
When a separate cached record with the exact same normalized Meshtastic node ID
has one coherent, unambiguous NodeInfo name tuple, the sidebar may use that
tuple as a clearly marked, display-only hint. Conflicting cached names or
identity proofs are never inherited.

This is a display and normalization update only. It does not merge or delete
cached nodes, change entity unique IDs, transmit NodeInfo requests, or add any
radio traffic.

## From 0.5.8

0.5.9 adds bounded, privacy-safe observability for the admin-only MeshNet
sidebar. Snapshot, render, polling, schema, message submission, and post-send
refresh failures are counted and classified without recording message text,
node or gateway identifiers, names, coordinates, endpoints, URLs, or browser
identity. Repeated failures are retained in a bounded diagnostic ring and
sampled in the Home Assistant log so a persistent five-second polling failure
cannot flood the log. The recurring projection is capped at 1,000 nodes and 64
gateways so an unexpectedly large historical radio database cannot monopolize
Home Assistant's event loop; omitted nodes are reported without being deleted.

The panel now distinguishes nodes reported by a gateway or its stored radio
database from older nodes loaded only from MeshNet's durable cache. It also
reports recently seen, located, explicitly MQTT-marked, and unknown-provenance
counts. A gateway report is not proof of a fresh RF packet, and an MQTT mark
does not mean this integration uses MQTT. These are observability labels only:
this release does not delete cached nodes, remove Home Assistant devices, merge
node identities, transmit traceroutes, or make any additional radio request.

No configuration or database migration is required. Restart Home Assistant and
hard-refresh the browser after installing so the versioned panel module loads.

## From 0.5.7

0.5.8 makes direct messaging explicit in the MeshNet panel. Choose
**Delivery → Direct**, then select a cached node, or use the **Message** button
beside that node. Broadcast remains the default and cannot accidentally inherit
an old direct recipient. The node list can be sorted by favorites and last
seen, last seen alone, or name.

Favorites are read from an optional Home Assistant device label named exactly
`MeshNet Favorite`. MeshNet does not create, edit, or delete the label, and it
does not add new favorite state to its database. The panel also links to Home
Assistant's native Map; only nodes with finite, geographically valid cached
coordinates receive location trackers. Meshtastic's unset `(0, 0)` protobuf
position is ignored, and its coordinate precision bits are not mislabeled as
meters of GPS accuracy.

The topology is now passive and evidence-only. It no longer draws inferred
node-to-node links merely because nodes were heard by the same gateway. It may
show a gateway-to-node edge for cached, gateway-provenanced, non-MQTT zero-hop
evidence and exact received route/path edges when their identifiers resolve
unambiguously. If no such
evidence exists, it says so. MeshNet has no traceroute action and the panel does
not transmit to populate the graph.

No configuration or database migration is required. Restart Home Assistant and
hard-refresh the browser after installing so the versioned panel module loads.

## From 0.5.6

0.5.7 repairs Home Assistant's message-action boundary and adds a working
message composer to the admin-only MeshNet sidebar panel. The panel provides
cached node and gateway dropdowns, supports broadcast and direct sends, retains
an in-progress draft across refreshes, and enforces Meshtastic's 237-byte UTF-8
text limit before submitting.

Action metadata no longer advertises unsupported Home Assistant entity/device/
area targets. YAML node numbers are normalized safely instead of being rejected
when unquoted, and the local Meshtastic Bluetooth backend accepts an exact,
unique cached short or long node name. Full node IDs remain the safest portable
choice; ambiguous, partial, and fuzzy name matches are rejected.

Pending in-memory scheduled sends are now owned by the config entry and canceled
on unload or restart. No configuration or database migration is required.
Restart Home Assistant after installing the update so the backend and sidebar
JavaScript are both reloaded.

## From 0.5.5

0.5.6 clears nonpersistent `no_gateways` and per-gateway startup repairs after
the corresponding condition is demonstrably resolved. A healthy gateway no
longer leaves an older startup warning active, while a currently empty setup or
failed/cancelled gateway start retains its repair. Repair-registry failures are
diagnostic-only and cannot block gateway startup. No configuration or database
migration is required.

## From 0.5.4

0.5.5 fixes the explicitly confirmed stale-bond recovery path for direct
Meshtastic Bluetooth. A radio paired before MeshNet has verified adapter
metadata but is intentionally not marked as MeshNet-owned. Earlier releases
showed the current-bond removal checkbox for that radio but skipped removal,
which could leave BlueZ reusing invalid security keys without a new PIN prompt.

The warned **Configure → Remove gateway** action can now remove either a
MeshNet-created or pre-existing current bond only when the user checks the
cleanup option and the exact saved controller/radio identity validates. It
keeps the gateway if identification or verified removal fails. Reload, entry
deletion, and HACS uninstall remain non-destructive and never remove a bond.

Fresh MeshNet-created pairings also use a service-scoped temporary agent and
set `Device1.Trusted` only after `Device1.Pair` succeeds. Paired and trusted
state are then re-read before the bond is accepted; a failure remains inside
the existing transaction-owned rollback boundary. Pre-existing bonds are not
silently trusted or modified.

## From 0.5.3

0.5.4 fixes the direct-Bluetooth configuration handshake by sending the
initial Meshtastic `want_config` request before starting the blocking
`FromRadio` reader. This follows the firmware's required write-then-read GATT
sequence and prevents a successful connection from stalling on its first
write. The write remains the firmware-declared write-with-response path; there
is no ambiguous retry, pairing bypass, or configuration migration. Restart
Home Assistant after updating so the Bluetooth backend is reloaded.

## From 0.5.2

0.5.3 retains a detached, strictly allowlisted snapshot of the most recent
Bluetooth startup failure after successful cleanup. It preserves the original
protocol/GATT phase, error class, counters, local-resolution result, and cleanup
outcome without retaining device objects, endpoints, addresses, messages, or
exception text. No configuration or database migration is required.

## From 0.4.2

0.4.3 hardens the first live diagnostics release. Meshtastic diagnostics now
show an identity-free startup phase, elapsed time, tracked native constructor,
cached local-adapter validation summary, and last-start outcome. If Home
Assistant cancels an internally owned startup while the synchronous SDK
constructor is still running, MeshNet retains the late result, closes it, and
keeps replacement clients serialized behind the endpoint lock until cleanup is
confirmed.

Coordinator diagnostics now use explicitly tracked update timestamps and add
safe repair and entity-state aggregates. Setup removes legacy pre-0.4.2 repair
IDs that could contain a configured gateway slug. No config-entry or database
migration is required. Restart Home Assistant after updating so the component
reloads and performs the legacy repair cleanup.

Home Assistant's outer diagnostics wrapper and filename still include the
config-entry ID and standard system metadata; inspect and rename the complete
download before sharing it.

## From 0.4.1

0.4.2 restores Home Assistant's native **Download diagnostics** action in the
MeshNet three-dot menu and greatly expands its cached health report. The
diagnostics platform now uses the supported Home Assistant import path and
reports integration/library versions, coordinator and transport task state,
gateway counters, safe node radio and power telemetry, deduplication/rate-limit
state, and detailed SQLite/outbox aggregates.

Diagnostics never connect, pair, scan, refresh, or transmit. Identifiers,
addresses, names, credentials, message content, raw provider data, precise
locations, and occupancy-related values remain omitted or redacted from
MeshNet's `data` section. Restart Home Assistant after updating so it reloads
the diagnostics platform. Home Assistant's outer wrapper and filename still
include the config-entry ID and standard system metadata; inspect and rename
the complete download before sharing it.

## From 0.4.x

0.5.0 replaces Meshtastic's blocking synchronous `BLEInterface` with a bounded,
Home Assistant-native async Bluetooth session. The existing verified radio bond
and gateway configuration are preserved. Restart Home Assistant after updating,
close any Meshtastic phone/web client, then reload the MeshNet entry.

Direct Bluetooth remains local and does not require MQTT, Internet, Wi-Fi, or
LAN connectivity. The selected adapter is now resolved by its stored stable
controller address on every attempt, so other valid local adapters may remain
powered when the radio resolves through one unambiguous local controller. No
config-entry or database migration is required.

## From 0.4.0

0.4.1 moves radio SDK connection work out of Home Assistant's config-entry
setup request. This prevents Meshtastic Bluetooth discovery and configuration
waits from leaving the pairing dialog at **Loading next step for MeshNet**.
Restart Home Assistant after updating. If the dialog previously stalled, check
**Settings → Devices & services** before pairing again: the entry or verified
BlueZ bond may already exist.

## From 0.2.x

0.4.0 adds the bounded Meshtastic Bluetooth pairing wizard. Config entry major
version 1 and SQLite schema version 1 remain unchanged; minor version 2 removes
v0.4-only pairing-authority/adapter metadata from older entries without changing
gateway or history data. Existing Bluetooth gateways are treated as pre-paired,
and MeshNet never marks their BlueZ bonds as originally paired by MeshNet.

Existing Meshtastic Bluetooth gateways do not yet contain verified adapter
metadata. Open **Configure → Edit gateway** and complete the guided pairing
check once after upgrading. Its stable controller address is then recorded.

0.3.0 added the USB-device picker.

## From 0.1.x

0.2.0 keeps config entry version 1 and SQLite schema version 1, so existing gateway and history data do not need migration. Back up `meshnet.sqlite3` before upgrading.

After restart, **Configure** opens the new form-based gateway manager. Existing gateway JSON remains valid.

Meshtastic MQTT users should change broad subscriptions such as `msh/#` to the decoded JSON branch, for example `msh/+/2/json/#`. Sending now follows the official Meshtastic downlink envelope and requires both an exact `options.publish_topic` and `options.mqtt_node_id`. This prevents accidental publishing to guessed topics. MeshCore JSON bridge sending likewise requires an explicit `options.publish_topic`.

Future releases should keep these compatibility rules:

- Do not rename normalized node keys without a migration.
- Add new sensor keys without removing old keys.
- Preserve queued outbound message shape until an outbox table migration exists.
- Keep gateway IDs stable.

## Backup

Stop Home Assistant and back up:

```text
custom_components/meshnet
meshnet.sqlite3
```

## Rollback

Restore the previous `custom_components/meshnet` directory and the matching `meshnet.sqlite3` backup, then restart Home Assistant.
