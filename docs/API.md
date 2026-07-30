# MeshNet Websocket API

All MeshNet websocket commands require an authenticated Home Assistant admin user.

## Snapshot

```json
{
  "type": "meshnet/snapshot"
}
```

Returns the normalized mesh snapshot:

- `nodes`
- `gateways`
- `recent_messages`
- `mesh_health_score`
- `messages_today`

## Messages

```json
{
  "type": "meshnet/messages",
  "limit": 100
}
```

Returns recent message history oldest-first.

## Send Message

```json
{
  "type": "meshnet/send_message",
  "target_node": "node-id",
  "message": "Generator battery low",
  "priority": "high",
  "channel": "0",
  "message_type": "direct"
}
```

Returns:

```json
{
  "schema_version": 1,
  "message_id": "...",
  "status": "sent"
}
```

The status is the durable submission state (`sent` or `queued`), not a claim
that a person read the message. Provider submission failures retained in the
outbox return `queued` and emit a correlated, privacy-safe
`meshnet_message_status` failure event.

## Manual Traceroute

```json
{
  "type": "meshnet/traceroute",
  "gateway_id": "garage_radio",
  "target_node": "meshtastic:!1234abcd"
}
```

This admin-only command accepts one exact cached Meshtastic identity and one
connected Bluetooth gateway. The server atomically reserves the one-hour
integration-wide cooldown before submitting exactly one RF request. It rejects broadcast,
name-based, self, unknown, non-Bluetooth, and incompatible targets. There is no
service, scheduled form, automatic caller, or retry path.

Successful results are versioned, bounded, correlated, and contain only the
validated source, destination, channel, route identifiers, status, and time.
Raw packets and provider exception text are never returned.

Read the current global cooldown and last bounded result without creating RF
traffic:

```json
{
  "type": "meshnet/traceroute/status"
}
```

The result reports `available` or `cooldown`, the persisted next-allowed time,
remaining seconds, the exact gateway/target used by the last reservation, and
the sanitized completed route/SNR evidence when present. The status call takes
no target and cannot reserve airtime or reach a radio.

## Remote Node Settings

The three commands below are administrator-only and supported only through a
connected Meshtastic Bluetooth gateway. `target_node` must be one exact
lowercase `!xxxxxxxx` ID already known to the controller radio.

Load and test access:

```json
{
  "type": "meshnet/remote_settings/get",
  "gateway_id": "garage_radio",
  "target_node": "!1234abcd"
}
```

The result contains a copy-only controller public key, target identity,
reviewed owner/display fields, and a server revision. It contains no private
key, session passkey, channel PSK, SecurityConfig, or raw protobuf.

Preview one typed draft:

```json
{
  "type": "meshnet/remote_settings/preview",
  "gateway_id": "garage_radio",
  "target_node": "!1234abcd",
  "revision": "<64 lowercase hex characters>",
  "changes": {"owner.short_name": "NODE"}
}
```

Apply the exact single-use preview:

```json
{
  "type": "meshnet/remote_settings/apply",
  "gateway_id": "garage_radio",
  "target_node": "!1234abcd",
  "revision": "<same revision>",
  "preview_id": "<preview token>",
  "confirm_remote": true
}
```

The preview expires after 90 seconds and is consumed before any possible RF
write. A failed/timeout write cannot be blindly replayed with the same token.
There is no Home Assistant service or generic admin command for this feature.
