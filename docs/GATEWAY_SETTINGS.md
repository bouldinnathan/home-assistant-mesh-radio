# Gateway Settings

MeshNet 0.6.0 adds a dedicated **Gateway settings** tab to the admin-only
MeshNet sidebar panel. It reads the settings from the physically connected
gateway and exposes only the fields that MeshNet can represent safely. It is
not a raw radio console.

> [!IMPORTANT]
> Applying a setting intentionally changes the radio itself. Those changes can
> survive a Home Assistant restart, removal of the MeshNet config entry, and a
> HACS uninstall. MeshNet cannot automatically restore a radio after its code
> has been removed. Record known-good settings before changing radio, channel,
> location, or connection parameters.

## Safe Apply Flow

Open **MeshNet → Gateway settings**, select an online gateway, and choose
**Reload live values**. The complete write flow is:

1. MeshNet reads the current supported settings from that local gateway.
2. Edits remain only in the current browser tab; they are not placed in browser
   storage or written to the radio.
3. **Preview changes** sends a bounded draft to Home Assistant. The server
   validates every field, rechecks the live revision, and creates a redacted
   diff. The single-use preview is held in memory for at most five minutes.
4. Connection-critical changes require a separate confirmation. MeshNet orders
   those operations last so an earlier safe change cannot accidentally be sent
   after access to the radio has been disrupted.
5. **Apply preview** submits only that exact preview. Writes for one gateway are
   serialized, each operation is sent once, and the preview cannot be reused.
6. MeshNet reads the radio again and reports which fields were verified. A
   reconnect may be required before verification can finish.

Changing the draft or selecting another gateway discards the browser's copy of
the preview. The server-held copy is single-use and is invalidated by a new
preview, integration reload/shutdown, or its five-minute expiry. A changed live
revision is also rejected instead of applying a stale edit.

## Current Support Matrix

The live page is authoritative because firmware and installed provider-library
capabilities can vary. A field shown as read-only includes the reason.
Beginning with 0.6.2, the page also reports the exact number of controls that
are currently editable and read-only. An **Editable** badge means both the live
gateway capability and MeshNet's path-specific safety contract permit a
preview; an enabled-looking value without that badge is not writable.

| Radio | Transport | Live settings | Apply | Verification |
| --- | --- | --- | --- | --- |
| Meshtastic | Direct Bluetooth | Yes | Supported fields only | Correlated local admin response followed by a fresh full configuration read |
| Meshtastic | USB serial | Yes | Read-only in the current release | Not applicable |
| Meshtastic | TCP | Yes | Read-only in the current release | Not applicable |
| Meshtastic | MQTT JSON | No standardized local settings contract | Read-only | Not applicable |
| MeshCore | Direct Bluetooth | Yes | Supported companion-radio fields | Command response and live field readback |
| MeshCore | USB serial | Yes | Supported companion-radio fields | Command response and live field readback |
| MeshCore | TCP/native companion | Yes | Supported companion-radio fields | Command response and live field readback |
| MeshCore | MQTT or REST bridge | No standardized, verifiable settings contract | Read-only | Not applicable |

Meshtastic Bluetooth projects the scalar owner, channel, device configuration,
and module configuration fields delivered by the connected radio. The exact
set follows the installed Meshtastic protobuf schema. Repeated/list fields,
hardware identity and metadata, managed-mode configuration, and security key
administration remain read-only or are omitted.

The reviewed Meshtastic Bluetooth write allowlist currently contains the owner
long and short names, the fixed Bluetooth PIN, and selected display preferences.
The Bluetooth enable switch cannot disable the connection currently carrying
the settings transaction. Other displayed fields remain read-only until they
have path-specific validation, bounded command behavior, recovery analysis,
and verified live readback.

MeshCore local companion connections can expose validated changes such as the
device name, coordinates and position advertisement policy, bounded transmit
power, telemetry modes, manual contact approval, supported channel fields, and
the device Bluetooth PIN. Custom RF frequency, bandwidth, spreading-factor,
and coding-rate fields stay read-only until firmware and hardware report
region-specific allowed combinations that MeshNet can validate. Firmware or
SDK gaps remain visible as read-only rather than being approximated. Repeater
tuning, unsupported flood/path options, custom variables, private keys, and
reset remain read-only or are omitted.

## Secrets and Connection PINs

Secret fields are write-only. MeshNet may show only a **Configured** boolean;
it does not return the existing value to the browser, include the submitted
value in a preview diff, or export it through diagnostics. A newly typed secret
exists in the page's memory and the short-lived server preview only until it is
applied, replaced, expired, or discarded. Provider logging is suppressed
during sensitive settings reads and writes.

The browser deliberately erases a newly typed secret after every preview
attempt, including a rejected or timed-out attempt. Re-enter it only after the
gateway state has been checked; MeshNet does not silently retain or resend it.

There are two different kinds of Bluetooth PIN in this project:

- The Meshtastic BlueZ pairing PIN is transient and is never stored by MeshNet.
- A MeshCore radio's configured Bluetooth PIN is a device setting and is also
  needed for that gateway to reconnect. Only after the radio confirms the new
  PIN by live readback does MeshNet update that gateway's Home Assistant config
  entry. The value remains masked and is excluded from the settings response,
  preview, diagnostics, and normal logs.

Other secrets, such as a channel secret, persist on the radio when deliberately
applied. MeshNet does not turn a write-only field into a secret-export tool.

## Timeout or Lost Connection

A timeout has an **unknown outcome**: the radio may have accepted a command
even though Home Assistant did not receive its response. MeshNet does not retry
the write, because doing so could repeat an operation against an already
changed device.

For a multi-field MeshCore plan, the first acknowledged value that cannot be
verified stops the remainder of the plan. MeshNet does not attempt later
previewed writes after that uncertainty, and it reports which earlier fields
were verified.

If Apply times out or reports an unverified field:

1. Do not press Apply again with the same values.
2. Let the gateway reconnect, then use **Reload live values**.
3. Compare the new live values with the redacted preview and with the radio's
   own display or official local client.
4. If a connection setting changed, recover physical/local access first and
   update the MeshNet gateway connection only after the radio state is known.

MeshCore's companion protocol does not provide a transaction or guaranteed
rollback. Meshtastic's edit transaction still cannot make a lost link or radio
reboot reversible. Post-write readback is evidence of the result, not a backup.

## Deliberately Excluded Operations

Gateway Settings does not provide:

- raw provider commands or arbitrary protobuf/JSON submission;
- remote RF administration of another node;
- automatic discovery-and-write behavior;
- factory reset or destructive erase;
- reboot/shutdown controls;
- firmware flashing;
- private-key, admin-key, or public-key import/export;
- a way to display or download existing secret values; or
- automatic rollback of intentional radio changes during uninstall.

These boundaries are intentional. A newly supported setting must have a typed
schema, strict validation, a bounded local command, and a credible live
verification path before it becomes writable.

## Capability Diagnostics

Downloading diagnostics does not read the radio. MeshNet reports only the last
capability decision already cached by a settings page read under
`runtime.gateway_settings.capability_observations`. Each configured gateway is
represented by a generated `gateway_NNN` diagnostic index rather than its real
ID. Useful fields include:

- `capability_state`: a fixed state such as `writable`, `disconnected`,
  `unavailable`, `incomplete`, `managed_mode`,
  `all_claimed_writable_fields_rejected`, or `no_writable_fields`;
- `source_available`, `source_complete`, and `source_writable`;
- aggregate source/sanitized category and field counts;
- claimed, schema-writable, currently editable, read-only, and
  contract-downgraded field counts; and
- a fixed `last_read_outcome` plus aggregate timeout, provider, and invalid
  snapshot failure counters.

No field path, category name, label, current value, option, raw provider reason,
revision, configured-secret state, or radio identity is retained in this
diagnostic cache.

## Uninstall and Recovery

Removing MeshNet stops future access but does not undo a setting already saved
by radio firmware. If you want to restore values, do so while a known-good
local connection still works, verify the readback, and only then remove the
gateway or integration. HACS uninstall also does not remove MeshNet's SQLite
history, Home Assistant registry records, config entries, or external BlueZ
bonds; see [Security](SECURITY.md#uninstall-safety) for the full removal scope.
