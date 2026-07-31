# Troubleshooting

Start here when something breaks. Change one thing at a time and test again.

## Do This First

Run these commands from the repository directory:

```bash
./verify_setup.sh --config-dir /config
sed -n '1,220p' ha-mesh-setup-output/install_log.txt
sed -n '1,220p' ha-mesh-setup-output/detected_serial_devices.txt
```

Docker:

```bash
docker compose ps
docker compose logs --tail=200 homeassistant
./verify_setup.sh --config-dir "$(pwd)/ha-config"
```

Home Assistant OS:

```bash
ha core logs
ha core check
```

## Decision Tree

1. Home Assistant does not start

Check Home Assistant logs first. The problem is before MeshNet can run.

```bash
ha core logs
```

Docker:

```bash
docker compose logs --tail=300 homeassistant
```

2. Home Assistant starts but MeshNet is missing

Check the custom component path:

```bash
ls -l /config/custom_components/meshnet/manifest.json
```

Docker:

```bash
docker compose exec homeassistant ls -l /config/custom_components/meshnet/manifest.json
```

If missing, reinstall:

```bash
./setup.sh --config-dir /config --install-custom-component
```

3. MeshNet loads but gateway is offline

Check the transport:

- TCP: test `nc -z -w 3 HOST PORT`
- USB: test `ls -l /dev/serial/by-id/`
- Docker USB: test inside the container
- MQTT: confirm Home Assistant MQTT integration is configured
- REST: test `curl API_URL`

4. Gateway online but no nodes

Wait for packets or force refresh:

```yaml
action: meshnet.refresh_gateway
data: {}
```

Then download diagnostics and check `runtime.store.packet_count`. Raw packets,
messages, node identifiers, and locations are intentionally excluded.

5. Nodes appear but telemetry/location is missing

The node has not sent that field, or your bridge is not publishing decoded JSON.

6. Sending fails

Confirm at least one gateway is online and pass `gateway_id` explicitly.

## Log Locations

Setup helper logs:

```text
ha-mesh-setup-output/install_log.txt
ha-mesh-setup-output/detected_environment.txt
ha-mesh-setup-output/detected_serial_devices.txt
```

Home Assistant log:

```text
<HA_CONFIG_DIR>/home-assistant.log
```

Docker logs:

```bash
docker compose logs -f homeassistant
```

Home Assistant OS logs:

```bash
ha core logs
```

## Enable Debug Logging

Add this to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.meshnet: debug
```

Restart Home Assistant.

Keep `meshtastic`, `meshcore`, and `pubsub` at their normal levels unless a
maintainer specifically asks for a short private capture. Those third-party
debug streams are outside MeshNet's normal log projection and may contain
radio identities, endpoints, topics, packet content, or credentials. Never
post an unreviewed provider log publicly.

## Common Failure Modes

### Integration Not Found

Symptom:

```text
MeshNet does not appear in Add Integration
```

Check:

```bash
ls -l /config/custom_components/meshnet/manifest.json
```

Expected:

```text
manifest.json
```

Fix:

```bash
./setup.sh --config-dir /config --install-custom-component
ha core restart
```

Docker fix:

```bash
docker compose restart homeassistant
```

### Python Requirement Install Fails

Symptom:

```text
Unable to install package meshtastic==2.7.11
```

Checks:

```bash
ha core logs | grep -i meshnet
ha core logs | grep -i pip
```

Docker:

```bash
docker compose logs homeassistant | grep -i "meshtastic\\|meshcore\\|pip"
```

Fixes:

- Confirm Home Assistant has internet access.
- Restart Home Assistant after network is available.
- For Home Assistant Core venv, install manually:

```bash
. /srv/homeassistant/bin/activate
pip install meshtastic==2.7.11 meshcore==2.3.7
```

### Serial Device Not Found

Check:

```bash
ls -l /dev/serial/by-id/
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

Expected:

```text
usb-... -> ../../ttyUSB0
```

If empty:

- Replug USB.
- Try another cable.
- Avoid charge-only cables.
- Try another USB port.
- Check kernel messages:

```bash
dmesg | tail -80
```

### Serial Permission Denied

Check:

```bash
ls -l /dev/ttyUSB0
id -nG
```

Expected group:

```text
dialout
```

Fix:

```bash
sudo usermod -aG dialout "$USER"
```

Log out and back in. Replug the USB device.

### Docker Cannot See USB

Host check:

```bash
ls -l /dev/serial/by-id/
```

Container check:

```bash
docker compose exec homeassistant ls -l /dev/meshtastic0
```

If missing, edit `docker-compose.yml`:

```yaml
devices:
  - /dev/serial/by-id/usb-YOUR_DEVICE:/dev/meshtastic0
```

Restart:

```bash
docker compose up -d
```

Configure MeshNet with:

```yaml
serial_path: /dev/meshtastic0
```

### Meshtastic Bluetooth Pairing or Startup Fails (version 0.5)

The pairing wizard works only through a local BlueZ adapter on the
Home Assistant host. Bluetooth proxies cannot pair a Meshtastic radio or carry
this direct connection.

MeshNet selects the controller by its stored stable Bluetooth address, so other
valid local adapters may remain powered when the radio has one unambiguous
local controller path. It refuses startup if the verified controller is absent,
powered off, ambiguous, or has malformed BlueZ metadata; it never silently
falls back to another controller or a Bluetooth proxy.

Before retrying:

1. Close the Meshtastic phone app and disconnect other Bluetooth clients. A
   radio normally accepts only one Bluetooth client at a time.
2. Confirm Bluetooth is enabled on the radio. Some Meshtastic hardware disables
   Bluetooth while Wi-Fi is enabled.
3. Move the radio close to the Home Assistant host's local adapter.
4. Select the radio from the dropdown. If entering it manually, use the exact
   canonical MAC form `AA:BB:CC:DD:EE:FF`.
5. Select **Start pairing** again and answer within about 50 seconds.

For a screened radio using `RANDOM_PIN`, the display should show a six-digit
code only after pairing starts. Enter that code in the password-masked field.
For a screenless radio, use its configured fixed PIN. The factory value may be
`123456`, but change that default in Meshtastic before regular use.

If no PIN appears:

- Verify that the radio is configured for `RANDOM_PIN`, not fixed-PIN or
  no-PIN mode.
- Wake the screen and restart the pairing request.
- Disconnect the phone app completely; merely putting it in the background may
  leave its Bluetooth connection open.
- Restart Bluetooth on the radio, then retry from MeshNet.

If the UI says **Loading next step for MeshNet** after accepting the PIN, first
check **Settings → Devices & services** before starting another pairing request.
Home Assistant may already have created the entry. Version 0.4.1 and newer start
the radio SDK in an entry-owned background task, so the Meshtastic SDK's long
BLE discovery and configuration waits cannot hold the config-flow response
open. After updating, restart Home Assistant and reload the existing entry; the
verified BlueZ bond is preserved.

Version 0.5 replaces the indefinitely blocking synchronous BLE constructor
with a bounded async protocol session. It resolves a fresh local `BLEDevice`,
connects GATT, validates the Meshtastic characteristics, enables FromNum
notifications, requests configuration, and actively reads FromRadio. Every
stage has a deadline. A failure cleans up the partial client instead of leaving
Home Assistant startup stuck.

If the radio pairs but no data arrives, another client may have reclaimed its
single Bluetooth connection. Close that client and reload the MeshNet config
entry. A second failure mode is a stale pre-existing BlueZ bond: the host still
reports the radio as paired, so no PIN appears, but the radio no longer accepts
the stored security keys. Diagnostics commonly show
`bluetooth_bond_managed: false`, successful GATT/notification setup, and a
timeout at `bluetooth_requesting_configuration` or `writing_to_radio` with no
received packets.

Version 0.5.5 provides a GUI-only repair for that state. Open **Configure →
Remove gateway**, select the affected radio, confirm removal, and enable
**Remove this radio's current Bluetooth bond so it can be paired again (may
disconnect other apps)**. MeshNet uses the exact stable adapter identity and
radio address saved by guided setup, resolves the same configured local device,
removes only that adapter-scoped BlueZ bond, and verifies removal before it
removes the gateway. Add the gateway again and enter the newly displayed PIN.
If exact identity or cleanup verification fails, MeshNet keeps the gateway for
a safe retry.

During that fresh pairing, MeshNet's temporary agent authorizes only the exact
selected device and Meshtastic service. It sets BlueZ trust only after `Pair()`
has succeeded, verifies the resulting paired/trusted state, and uses its
existing transaction-owned rollback if that verification fails. A failed or
raced `Pair()` call therefore cannot alter the trust state of an external bond.

This option is off by default because BlueZ cannot distinguish a bond another
app recreated at the same address. MeshNet never removes a bond automatically
during reload, config-entry deletion, or HACS uninstall.

Download diagnostics before reloading. The identity-free fields show the exact
bounded stage, for example:

```text
runtime.gateways[0].client.last_start_failed_phase: bluetooth_synchronizing_configuration
runtime.gateways[0].client.last_start_error_subtype: TimeoutError
runtime.gateways[0].client.bluetooth_adapter_validation.status: passed
runtime.gateways[0].client.last_bluetooth_failure.cleanup_outcome: confirmed
runtime.gateways[0].client.last_bluetooth_failure.transport.last_resolution_result: matched_verified_local_adapter
runtime.gateways[0].client.last_bluetooth_failure.transport.last_transport_before_cleanup.last_failure_phase: reading_from_radio
```

This example means adapter validation, GATT connection, and notification setup
completed, but a FromRadio read timed out before the radio finished its
configuration exchange; teardown was confirmed. Version 0.5.11 no longer lets
the 15-second general I/O limit preempt the full configuration budget. It also
waits for the ToRadio write to propagate, drains FromRadio only after an actual
write/notification/idle trigger, and schedules a fresh-link retry through the
single-flight coordinator after confirmed teardown. A recovered gateway clears
this warning automatically.

If the warning returns after that retry, check for another connected phone or
computer and wake or restart the radio's Bluetooth. Reloading the MeshNet entry
requests an immediate fresh attempt but should not normally be necessary.
Other phase values distinguish local-device resolution, GATT connection,
profile validation, notification setup, config request, active operation,
teardown, and reconnect backoff. Diagnostic collection itself does not probe
or reconnect the radio. Failure snapshots are strictly allowlisted primitive
states and counters; they never retain a BLE client, device object, endpoint,
address, exception message, or packet content.

Direct Bluetooth needs no MQTT, broker, Internet, Wi-Fi, or LAN. If Home
Assistant shows a dependency-install error immediately after a new HACS
install, Internet access may be needed once to download the declared Python
packages; that is separate from normal radio operation.

On Home Assistant OS, do not use root-shell or `bluetoothctl` steps for the
normal path. The wizard creates a temporary agent scoped to the
selected radio and cleans it up after success, error, cancellation, or timeout.
If the wizard reports that the local BlueZ service is unavailable, restart the
host Bluetooth service or Home Assistant and retry; a proxy cannot substitute
for the missing local adapter.

MeshNet never stores or logs the PIN or raw BlueZ daemon error text. The UI shows
a safe failure category; it cannot recover a lost code, so start a new request.

### TCP Connection Refused

Check:

```bash
nc -z -w 3 192.0.2.50 4403
echo $?
```

Expected success:

```text
0
```

If `1`:

- Wrong IP.
- Wrong port.
- TCP API disabled.
- Device asleep or offline.
- Firewall or VLAN block.
- MeshCore firmware uses a different port.

### TCP Timeout

Check reachability:

```bash
ping -c 3 192.0.2.50
```

If ping fails:

- Wrong IP.
- Device is offline.
- WiFi disconnected.
- Different subnet or VLAN.

If ping works but TCP times out:

- Port is blocked.
- Service is not listening.
- You configured the wrong protocol.

### DHCP Changed Gateway IP

Find the gateway in your router DHCP leases. Create a DHCP reservation by MAC address.

Then update MeshNet:

```text
Settings -> Devices & Services -> MeshNet -> Configure
```

### MQTT Gateway Online But No Packets

Confirm MQTT is loaded:

```text
Settings -> Devices & Services -> MQTT
```

Use MQTT Explorer or the Home Assistant MQTT tools to inspect the topic.

Expected:

- Topic matches `mqtt_topic`.
- Payload is JSON.
- Payload contains text, telemetry, node, or packet fields.

Not supported directly:

- Raw binary protobuf MQTT payloads without a decoding bridge.

### REST Gateway Fails

From the Home Assistant host:

```bash
curl -fsS http://192.0.2.51:8080/meshcore/state | head
```

Expected:

```text
{
```

If unauthorized, configure `api_key`.

### No Nodes Appear

Checks:

```yaml
action: meshnet.refresh_gateway
data: {}
```

Then download diagnostics:

```text
Settings -> Devices & Services -> MeshNet -> three-dot menu -> Download diagnostics
```

Look for:

- `gateways[].connected`
- `runtime.store.packet_count`

If `packet_count` is zero, MeshNet is not receiving packets.

### No Telemetry

Possible causes:

- Node telemetry module is disabled.
- Node has not transmitted telemetry yet.
- MQTT bridge publishes raw packets instead of decoded JSON.
- Gateway packet payload lacks telemetry fields.

Check whether `runtime.store.packet_count` increases when telemetry is sent.

### No Location

Possible causes:

- Node has no GPS.
- GPS has no fix.
- Position sharing is disabled.
- Packet lacks latitude/longitude.

Expected behavior:

- `device_tracker` appears only after a packet includes latitude and longitude.

### Duplicate Nodes

Likely causes:

- Same physical node appears with different IDs.
- Packets lack MAC or public key.
- Bridge rewrites node IDs.
- Gateway IDs were renamed.

Fix:

- Prefer data sources that include MAC or public key.
- Keep gateway IDs stable.
- Avoid duplicate gateway definitions for the same physical gateway.

### Send Message Does Nothing

If Home Assistant says a **device ID could not be found** after you enter a
Meshtastic short name or node number, it is interpreting that value as a Home
Assistant device-registry target. MeshNet versions before 0.5.7 incorrectly
advertised that unsupported target type, so the request was rejected before
MeshNet or the radio could see it. Upgrade, restart Home Assistant, and use the
MeshNet sidebar recipient dropdown or the action's `target_node` data field.

Checks:

1. Install MeshNet 0.5.7 or newer and restart Home Assistant.
2. In the MeshNet sidebar, leave **Delivery** on **Broadcast** and try the
   **Automatic** gateway first.
3. Is the gateway online?
4. If you supplied `gateway_id`, is it the exact configured gateway ID?
5. For direct messages, choose **Delivery → Direct**, then choose the node from
   the sidebar dropdown or press **Message** on its node row. In YAML, use a
   full Meshtastic `!` node ID, integer node number, or exact unique cached
   short/long name. Quote numeric short names.
   Meshtastic choices show their normalized `!ID`; any remaining duplicate
   human labels receive an exact disambiguator so distinct targets are visible.
6. For MQTT, does `options.publish_topic` match the command topic consumed by your bridge?
7. For MeshCore direct messages, is the destination contact known to the MeshCore SDK?

Try broadcast first:

```yaml
action: meshnet.broadcast_message
data:
  message: "MeshNet test"
  channel: "0"
```

Then download diagnostics. If the MeshNet store and coordinator still report
zero sent, queued, and received message records, Home Assistant did not submit
the action to MeshNet. Check the action YAML for an unsupported `target:` block
and use `action: meshnet.broadcast_message` with only the documented `data`
fields. If a queued record appears, the action reached MeshNet and the gateway
or destination error is the next item to inspect.

### Sidebar Panel Forbidden

The MeshNet sidebar panel is admin-only by design. Use an admin Home Assistant account.

### Updated Sidebar Still Looks Old

MeshNet 0.5.10 version-stamps the sidebar JavaScript URL. Restart Home Assistant
after updating, then hard-refresh the browser once. The current panel has a
separate **Delivery** selector, node sort selector, **Message** buttons, a Map
link, and the heading **Cached evidence topology — no traceroutes sent automatically**.

### Sidebar Control Loses Focus During Refresh

MeshNet 0.5.10 continues collecting its five-second snapshots while a message
field, recipient, gateway, channel, priority, delivery, or node-sort control is
active, but postpones replacing the visible panel until focus leaves those
controls. This preserves text selection, the insertion cursor, input-method
composition, and native select menus across Home Assistant's shadow roots.

If a control still loses focus on 0.5.10 or newer, hard-refresh the browser once
and reproduce it with debug logging enabled. The diagnostics download includes
bounded, privacy-safe panel failure counters; it does not include typed message
text, selected recipients, node names, or browser identity.

### A Node Is Shown as `Unnamed node · !xxxxxxxx`

`!xxxxxxxx` is the Meshtastic node ID used for routing, not a connection error.
MeshNet shows this explicit unnamed label when it has received a packet or
cached record for that ID but has not received usable NodeInfo containing a
long, user, or short name. When both are available, the sidebar displays
`Long Name · SHORT`; compact graph and RF labels prefer `SHORT`.

On 0.6.0 and newer, retained records with the exact same valid Meshtastic node
ID are shown as one effective node only when their MAC and public-key evidence
forms one consistently observed proof bundle. A separate MAC-only record and
public-key-only record are not combined unless an observation binds them
together. MeshNet never groups by name, location, or signal and never
constructs a long/short pair from separate donors. Conflicting or malformed
evidence fails closed and remains separate. Original SQLite records are kept;
the sidebar reports how many aliases were collapsed and how many records remain
retained for rollback. Old alias entities become unavailable rather than being
deleted, and an existing favorite label or direct-message alias still resolves
to the effective node.

If conflicting records share the same routing ID, MeshNet disables direct
messaging to every record in that unresolved group. Meshtastic ultimately
routes by the shared ID, so choosing one apparent record could otherwise send
to the wrong physical node. Resolve the device-ID conflict before sending.
The same fail-closed rule applies when one MAC or public key is shared by
different routing IDs. Those records stay visible and cannot be direct-message
targets until their strong proof agrees.

The phone app can look different because it has its own node database and may
hide or already have NodeInfo for records that this radio or MeshNet cache does
not. MeshNet does not request NodeInfo, merge nodes by name or signal, or send
extra radio traffic to fill these labels. Exact destination IDs remain the
authoritative direct-message targets except while an ID has unresolved,
conflicting identity evidence.

### Sidebar Data Looks Incomplete or Contains Distant Nodes

The sidebar intentionally limits the visible node list and RF heat cells, caps
its recurring projection at 1,000 retained nodes, and only draws passive graph
links backed by received evidence. Nodes beyond the safety cap remain in Home
Assistant and are counted as omitted in Panel diagnostics. A Bluetooth
connection is local, but the radio's stored node database can still contain
multi-hop, previously received, or MQTT-marked nodes. That mark does not mean
the MeshNet integration itself uses MQTT. MeshNet also loads its durable node
cache at startup; a location for a node that has not been seen recently can
therefore remain on Home Assistant's native Map.

Open **Panel diagnostics** in the sidebar and compare **Distinct nodes**,
**Identity aliases**, **Gateway-reported**, **Retained cache only**, **Recent**,
**Located**, **Nodes marked MQTT**, and **Unknown**. A gateway report may come
from the radio's stored node database; it is not proof of a fresh RF packet.
These counts do not trigger radio traffic.
Download integration diagnostics from the three-dot menu for the corresponding
privacy-safe aggregates and bounded panel failure history.

To capture every sanitized panel failure in the Home Assistant log, enable
debug logging for `custom_components.meshnet` from the integration's debug
logging action, reproduce the problem briefly, then disable debug logging and
download the resulting log and diagnostics. MeshNet never intentionally logs
message text, recipients, node or gateway IDs, names, coordinates, Bluetooth
addresses, serial paths, URLs, credentials, or browser identity.

### Gateway Settings Is Missing or Read-Only

The dedicated **Gateway settings** tab requires MeshNet 0.6.0 and a Home
Assistant administrator account. Restart Home Assistant after updating and
hard-refresh the browser once if the older Mesh-only panel is still cached.

MeshNet 0.6.0 could leave this page at **Loading gateway settings…** when a
Meshtastic power-monitor field advertised JavaScript's maximum safe integer as
its upper bound. Upgrade to 0.6.1, restart Home Assistant, and hard-refresh the
panel. Beginning with 0.6.1, an incompatible schema stops after one automatic
attempt; press **Reload live values** after updating instead of relying on an
automatic retry.

Choose an online, physically connected gateway and press **Reload live
values**. MeshNet 0.6.2 shows an exact `N editable · M read-only` summary and an
**Editable** badge on every control that can enter the preview flow. Read-only
behavior is expected for:

- Meshtastic MQTT, serial, and TCP settings in the current release;
- MeshCore MQTT and REST bridges, which have no standardized verified settings
  contract;
- a Meshtastic radio in managed mode;
- firmware/SDK fields for which MeshNet has no typed writer and readback; and
- identity, repeated-value, key-management, reset, or other intentionally
  excluded fields.

MeshCore BLE/serial/TCP companion paths and Meshtastic direct Bluetooth expose
only the writable fields reported by their current live schema. A field-level
reason explains a capability gap. Do not use a raw console or remote RF admin
command to bypass that guard; doing so bypasses MeshNet's validation and
verification boundaries.

If the page reports zero editable fields unexpectedly, download diagnostics
without reloading the integration and inspect
`runtime.gateway_settings.capability_observations`. `disconnected`,
`unavailable`, `incomplete`, and `managed_mode` identify gateway-wide blocks.
`no_writable_fields` means the provider did not expose a reviewed writable
field. `all_claimed_writable_fields_rejected` means MeshNet's central contract
validator downgraded every claimed field. The counts are cached from the last
page read; collecting diagnostics performs no radio operation and exports no
field paths, values, IDs, names, provider text, or secrets.

### A Gateway Settings Preview Expired or Became Stale

A settings preview is single-use, tied to the live radio revision, and retained
in server memory for at most five minutes. A newer preview, integration reload,
gateway replacement, or changed radio value invalidates it. Press **Reload live
values**, recreate the draft, inspect the new diff, and confirm critical fields
again. This is a stale-write protection, not a connection failure.

### Apply Timed Out or a Setting Was Not Verified

Do not blindly press Apply again. A timeout means the device state is unknown:
the radio may have accepted the only transmitted command even if its response
or reconnect never reached Home Assistant. Let the gateway reconnect, then use
**Reload live values** and compare the live state with the radio display or an
official local client. MeshNet deliberately does not retry settings writes.

If a MeshCore Bluetooth PIN was verified, MeshNet updates that gateway's saved
connection PIN so the entry can reconnect after reload. If the link was lost
before verification, no new PIN is saved. Determine the PIN currently active
on the physical radio first, then use **Configure** on the MeshNet integration
to repair the gateway connection; do not alternate guessed PIN writes.

Secret inputs cannot display the old value. **Configured** means only that the
radio reported a value. Previews and diagnostics intentionally show no secret.

### Uninstall Did Not Restore a Radio Setting

This is expected. Apply changes radio firmware state, while uninstall removes
integration code. Restore known-good settings and verify them while local
access still works, before removing MeshNet. HACS uninstall cannot safely infer
which prior radio value was intended and does not attempt a rollback.

## Health Checks

Repository:

```bash
python3 -m pytest
python3 -m compileall -q custom_components tests
bash -n setup.sh install.sh verify_setup.sh uninstall.sh
```

Docker:

```bash
docker compose ps
docker inspect --format '{{json .State.Health}}' meshnet-homeassistant
```

Home Assistant config:

```bash
ha core check
```

Core venv:

```bash
hass --script check_config -c /config
```
