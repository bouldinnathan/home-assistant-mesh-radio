# Diagnostics Checklist

Print this or keep it open while validating.

## Files

- [ ] `custom_components/meshnet/manifest.json` exists in the Home Assistant config directory.
- [ ] `ha-mesh-setup-output/generated_config.yaml` exists.
- [ ] `ha-mesh-setup-output/install_log.txt` exists.
- [ ] `ha-mesh-setup-output/rollback_info.json` exists.

Commands:

```bash
ls -l /config/custom_components/meshnet/manifest.json
ls -l ha-mesh-setup-output/
```

Expected output:

```text
manifest.json
generated_config.yaml
install_log.txt
rollback_info.json
```

## Home Assistant

- [ ] Home Assistant starts cleanly.
- [ ] Config validation passes.
- [ ] MeshNet integration appears under Devices & Services.
- [ ] MeshNet sidebar panel opens for an admin user.
- [ ] Diagnostics download works.

Commands:

```bash
ha core check
ha core logs | grep -i meshnet
```

Expected output:

```text
Processing... Done.
```

Logs should not show import errors or repeated reconnect failures.

## USB

- [ ] Meshtastic USB device exists.
- [ ] MeshCore USB device exists.
- [ ] Stable `/dev/serial/by-id/...` paths are used.
- [ ] Current user or container can read/write serial devices.
- [ ] USB devices still exist after reboot.

Commands:

```bash
ls -l /dev/serial/by-id/
id -nG
```

Expected output:

```text
usb-... -> ../../ttyUSB0
youruser ... dialout ...
```

## TCP

- [ ] Meshtastic IP is reserved in DHCP.
- [ ] MeshCore IP is reserved in DHCP.
- [ ] Meshtastic TCP port is reachable.
- [ ] MeshCore TCP port is known and reachable.

Commands:

```bash
nc -z -w 3 192.0.2.50 4403 && echo "meshtastic tcp ok"
nc -z -w 3 192.0.2.51 12345 && echo "meshcore tcp ok"
```

Expected output:

```text
meshtastic tcp ok
meshcore tcp ok
```

## Meshtastic

- [ ] `meshtastic --info` works over TCP or serial.
- [ ] Nodes are visible from the gateway.
- [ ] Text messages arrive in Home Assistant.
- [ ] Battery/telemetry appears after packets arrive.

Commands:

```bash
meshtastic --host 192.0.2.50 --info
meshtastic --port /dev/serial/by-id/YOUR_MESHTASTIC_DEVICE --info
```

Expected output contains:

```text
Nodes in mesh
```

## MeshCore

- [ ] The exact API/transport is known.
- [ ] TCP port or serial path is correct.
- [ ] PIN/baudrate/debug options are set if firmware requires them.
- [ ] Contacts/nodes appear after packets arrive.
- [ ] Messages can be sent through the chosen gateway.

Command:

```bash
nc -z -w 3 MESHCORE_IP MESHCORE_PORT && echo "meshcore open"
```

Expected output:

```text
meshcore open
```

## Entities

- [ ] `sensor.meshnet_total_nodes`
- [ ] `sensor.meshnet_active_nodes`
- [ ] `sensor.meshnet_offline_nodes`
- [ ] `sensor.meshnet_average_battery`
- [ ] `sensor.meshnet_mesh_health_score`
- [ ] node battery sensors
- [ ] node RSSI/SNR sensors
- [ ] node device trackers for GPS nodes
- [ ] online binary sensors

Home Assistant path:

```text
Settings -> Devices & Services -> Entities -> search "meshnet"
```

## Messaging

- [ ] Received message event fires.
- [ ] Send service accepts a message.
- [ ] Broadcast service accepts a message.
- [ ] Scheduled message queues and sends.
- [ ] Emergency automation triggers.

Developer Tools service call:

```yaml
action: meshnet.broadcast_message
data:
  message: "MeshNet test from Home Assistant"
  priority: normal
```

Expected result:

```text
Service call succeeds. Message appears on mesh if RF path and channel are correct.
```

## Final Reboot Test

- [ ] Restart Home Assistant.
- [ ] Reboot host.
- [ ] Confirm USB paths are unchanged.
- [ ] Confirm gateways reconnect.
- [ ] Confirm nodes repopulate after packets arrive.
- [ ] Send one test message.

Command:

```bash
./verify_setup.sh --config-dir /config
```

Expected output:

```text
OK: ...
```
