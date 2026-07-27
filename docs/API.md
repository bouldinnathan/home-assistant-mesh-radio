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
  "message_id": "..."
}
```
