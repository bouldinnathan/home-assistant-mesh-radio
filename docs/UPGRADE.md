# Upgrade Guide

## From 0.2.x

0.3.0 adds the USB-device picker and keeps config entry version 1 and SQLite schema version 1, so existing gateway and history data do not need migration.

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
