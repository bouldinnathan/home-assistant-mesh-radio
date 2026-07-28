# Upgrade Guide

## From 0.4.1

0.4.2 restores Home Assistant's native **Download diagnostics** action in the
MeshNet three-dot menu and greatly expands its cached health report. The
diagnostics platform now uses the supported Home Assistant import path and
reports integration/library versions, coordinator and transport task state,
gateway counters, safe node radio and power telemetry, deduplication/rate-limit
state, and detailed SQLite/outbox aggregates. Meshtastic diagnostics also expose
an identity-free startup phase, elapsed time, tracked native constructor, and
cached local-adapter validation summary so a blocked Bluetooth SDK constructor
is distinguishable from pairing or adapter validation.

Diagnostics never connect, pair, scan, refresh, or transmit. Identifiers,
addresses, names, credentials, message content, raw provider data, precise
locations, and occupancy-related values remain omitted or redacted from
MeshNet's `data` section. Setup removes legacy pre-0.4.2 repair IDs that could
contain a gateway slug. Restart Home Assistant after updating so it reloads the
diagnostics platform and performs that cleanup. Home Assistant's outer wrapper
and filename still include the config-entry ID and standard system metadata;
inspect and rename the complete download before sharing it.

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
check once after upgrading. Only one local Bluetooth adapter may be powered
during pairing and runtime; its stable controller address is then recorded.

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
