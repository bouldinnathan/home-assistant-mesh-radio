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
service: meshnet.refresh_gateway
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
    meshtastic: debug
    meshcore: debug
    pubsub: debug
```

Restart Home Assistant.

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

### Meshtastic Bluetooth Pairing Fails (version 0.4)

The version 0.4 pairing wizard works only through a local BlueZ adapter on the
Home Assistant host. Bluetooth proxies cannot pair a Meshtastic radio or carry
this direct SDK connection.

Only one local Bluetooth adapter may be powered. Meshtastic 2.7.11 cannot
select a controller, so MeshNet refuses pairing and runtime startup when more
than one is powered or when the verified adapter is off. Installed extra
adapters can remain powered off.

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

If the radio pairs but no data arrives, another client may have reclaimed its
single Bluetooth connection. Close that client and reload the MeshNet config
entry. MeshNet never removes a bond during entry/HACS teardown. If you
deliberately want to delete the current address-scoped bond, use **Configure →
Remove gateway** and enable **Remove this radio's current Bluetooth bond (may
disconnect other apps)**. The option is off by default because BlueZ cannot
distinguish a bond another app recreated at the same address.

On Home Assistant OS, do not use root-shell or `bluetoothctl` steps for the
normal version 0.4 path. The wizard creates a temporary agent scoped to the
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
service: meshnet.refresh_gateway
data: {}
```

Then download diagnostics:

```text
Settings -> Devices & Services -> MeshNet -> Download diagnostics
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

Checks:

1. Is the gateway online?
2. Did you pass `gateway_id`?
3. For direct messages, has the node been heard recently?
4. For MQTT, does `options.publish_topic` match the command topic consumed by your bridge?
5. For MeshCore direct messages, is the destination contact known to the MeshCore SDK?

Try broadcast first:

```yaml
service: meshnet.broadcast_message
data:
  gateway_id: meshtastic_wifi_1
  message: "MeshNet test"
  channel: "0"
```

### Sidebar Panel Forbidden

The MeshNet sidebar panel is admin-only by design. Use an admin Home Assistant account.

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
