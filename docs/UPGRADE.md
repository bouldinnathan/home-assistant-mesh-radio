# Upgrade Guide

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
