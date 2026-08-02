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

Returns recent message history oldest-first. Meshtastic records may include a
validated `mesh_packet_id` (`meshtastic:<uint32>`), `reply_to_message_id`, and
one-Unicode-scalar `reaction`. Those public fields are projected separately
from retained provider metadata so the panel can attach a reaction only to the
exact on-air message it references. A reply whose target is outside the loaded
history remains a visible orphan instead of being attached by sender, time, or
text similarity.

BLE and native serial/TCP sends retain the packet ID returned by the radio.
The MQTT publish format does not return an on-air packet ID at submission time,
so an incoming reaction cannot be joined to that local outbound MQTT record
unless the original packet is later observed as a received record.

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
connected Bluetooth gateway. The server atomically reserves the one-minute
integration-wide cooldown before submitting one RouteDiscovery application
packet using the connected radio's configured LoRa hop limit, with no MeshNet
retry. Firmware routing, relaying, and reliable-packet behavior can still create
more than one physical RF transmission. It rejects broadcast, name-based, self,
unknown, cached-only, non-Bluetooth, and incompatible targets. There is no
service, scheduled form, automatic caller, or retry path.

Successful results are versioned, bounded, correlated, and contain only the
validated source, destination, channel, route identifiers, status, and time.
Raw packets and provider exception text are never returned.

Rejected commands use stable privacy-safe error codes, including
`traceroute_target_self`, `traceroute_target_unknown`,
`traceroute_gateway_disconnected`, `traceroute_cooldown`, and
`traceroute_preflight_failed`. Those failures occur before provider submission
and do not create a new cooldown. `traceroute_timeout`,
`traceroute_invalid_response`, and the fallback `traceroute_failed` mean RF may
have been submitted; the caller must reload persisted status and must not retry
blindly.

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

## Manual NeighborInfo Request

```json
{
  "type": "meshnet/neighbor_info",
  "gateway_id": "garage_radio",
  "target_node": "meshtastic:!1234abcd"
}
```

This administrator-only command submits one Meshtastic Bluetooth unicast
application packet using the protocol's NeighborInfo request marker and the
connected radio's configured LoRa hop limit, with no MeshNet retry. Before RF,
the server atomically reserves the persisted integration-wide 60-second metadata
airtime floor shared with traceroute and a 180-second floor for the same exact
NeighborInfo target. There is no Home Assistant service, broadcast, generic
batch, scheduled-message form, or manual retry path. No response is a bounded
unknown outcome and still consumes the applicable reservations. The selected
gateway itself and nodes not observed in the active radio session are rejected
before reservation. A separately configured opt-in maintenance scheduler is the
only non-manual caller and uses this same validated request boundary.

This request path is experimental and verified against Meshtastic firmware
2.7.26. The target must have the Neighbor Info module enabled; older firmware
may reject the request or time out. A successful response is the target's
cached zero-hop neighbor report, capped at ten entries—not a live RF scan. An
empty successful report means the target returned no cached neighbors; a
timeout does not mean zero neighbors.

Bluetooth diagnostics distinguish a correlated routing rejection, timeout,
cancellation, send failure, and disconnect. Only bounded protocol-enum reason
names and counters are projected; raw packets and node identities are omitted.

Zero-RF preflight failures use stable codes such as
`neighbor_info_target_self`, `neighbor_info_target_unknown`,
`neighbor_info_gateway_disconnected`, and `neighbor_info_preflight_failed`.
Post-reservation outcomes use `neighbor_info_unsupported`,
`neighbor_info_rejected`, `neighbor_info_timeout`,
`neighbor_info_disconnected`, `neighbor_info_send_failed`, or the bounded
fallback `neighbor_info_failed`. Callers must treat the latter group as a
possibly transmitted request, reload persisted status, and never retry it
blindly.

Read the selected target's persisted cooldown and last sanitized response
without sending RF:

```json
{
  "type": "meshnet/neighbor_info/status",
  "target_node": "meshtastic:!1234abcd"
}
```

The result includes global, target, and effective next-allowed timestamps and
remaining seconds, plus at most ten neighbors from the last bounded response.
Responses are tagged `manual_request` or `maintenance_scan` according to the
validated caller; unsolicited and legacy NeighborInfo evidence remains
`passive`.

## Automatic Idle NeighborInfo Maintenance

Automatic maintenance has no direct WebSocket command or Home Assistant
service. It is off by default and can be enabled only through **Settings →
Devices & services → MeshNet → Configure → Automatic network maintenance**.
The operator must select one exact configured Meshtastic Bluetooth gateway.

The accepted options are bounded as follows:

| Option | Default | Accepted values |
| --- | ---: | --- |
| Enabled | `false` | Boolean |
| Cycle interval | `3600` | `3600`–`86400` seconds |
| Quiet time | `120` | `60`–`3600` seconds |
| Maximum requests per cycle | `10` | `1`–`60` |

The first cycle and every post-resume cycle wait one full interval. A scheduler
tick invokes at most one exact-target request; requests remain at least 60
seconds apart and also pass the durable shared-metadata and 180-second
same-target reservations described above. Inbound traffic and foreground,
outbox, reconnect, gateway-settings, remote-admin, manual-tool, BLE-operation,
reload, and shutdown owners defer a cycle without a deadline. Missed intervals
do not accumulate, and a selected target is not retried in the same cycle after
failure, timeout, cooldown, or a late zero-RF idle rejection.

Targets must be exact, unambiguous, online Meshtastic identities observed
through the selected connected BLE gateway in the current radio session.
Cached-only, MQTT-only, self, other-gateway, and ambiguous records are excluded.
The scheduler can invoke NeighborInfo only; automatic traceroute and automatic
retry are fixed as unsupported.

The existing `meshnet/snapshot` response may contain this identity-free object
under `panel_metadata.maintenance`:

```json
{
  "enabled": true,
  "accepting": true,
  "task_state": "pending",
  "cycle_active": false,
  "last_outcome": "not_due",
  "interval_seconds": 3600,
  "quiet_seconds": 120,
  "request_spacing_seconds": 60,
  "max_requests_per_cycle": 10,
  "next_cycle_in_seconds": 2400,
  "last_activity_age_seconds": 30,
  "request_attempt_count": 0,
  "request_success_count": 0,
  "request_failure_count": 0,
  "traffic_deferral_count": 0,
  "busy_deferral_count": 0,
  "configuration_valid": true,
  "gateway_configured": true,
  "automatic_traceroute_supported": false,
  "automatic_retry_supported": false
}
```

This projection intentionally contains no selected gateway or target identity,
node/name/address, raw result, or exception string. Reading the snapshot or a
downloaded diagnostic report performs no RF operation. The per-target
`meshnet/neighbor_info/status` command remains administrator-only and can show
the exact target and its last sanitized result without transmitting.

See [Automatic Network Maintenance](NETWORK_MAINTENANCE.md) for target rotation,
traffic gates, lifecycle, privacy, and uninstall behavior.

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
