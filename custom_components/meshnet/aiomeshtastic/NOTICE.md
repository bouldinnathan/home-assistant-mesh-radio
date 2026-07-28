# aiomeshtastic adaptation notice

The Python files in this directory are a Bluetooth-only adaptation of the
`aiomeshtastic` implementation in the Meshtastic Home Assistant integration:

- Source: <https://github.com/meshtastic/home-assistant>
- Audited upstream commit: `3594f3525f4451880d33e988dd0e4956dab75f53`
- Upstream authors retained in SPDX headers: Pascal Brogle (`@broglep`) and
  Hendrik (`@novag`)
- License: MIT (the full license text is in the repository-level `LICENSE`)

MeshNet's adaptation removes MQTT, serial, TCP, Home Assistant, pubsub, and
generated-protobuf code. It imports protobuf messages from the separately
installed `meshtastic` package. It also adds bounded startup/shutdown,
cancellation-safe task ownership, retry-time device resolution, plain-dict
callbacks, privacy-safe diagnostics, and the post-`want_config` forced read
needed by affected Meshtastic Bluetooth firmware.
