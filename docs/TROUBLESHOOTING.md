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
