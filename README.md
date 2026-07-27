# MeshNet for Home Assistant

MeshNet turns Home Assistant into one operating surface for Meshtastic and MeshCore radios. It creates gateway and node entities, records telemetry and messages, tracks GPS positions, exposes actions and events, and provides an admin-only mesh panel.

> [!IMPORTANT]
> The current package is an in-process Home Assistant custom integration. Use it
> on a disposable test Home Assistant instance, not on a primary installation
> that requires fault isolation. The repository `Dockerfile` also builds a full
> Home Assistant test image; it is not an isolated MeshNet sidecar. See
> [Distribution and isolation](docs/DISTRIBUTION.md) for the HACS test path and
> the recommended Home Assistant App/MQTT architecture.

## Fast setup

MeshNet targets Home Assistant 2025.1 and newer. Evaluate the current custom
integration only on a test Home Assistant OS or Container instance; the isolated
App/sidecar described below is the intended production package. Existing Core
and Supervised installations can run the integration only on a best-effort basis.

From a copy of this repository, run a safe preview:

```bash
./setup.sh --dry-run
```

Review `ha-mesh-setup-output/NEXT_STEPS.txt`, then install the component into the detected Home Assistant configuration directory:

```bash
./setup.sh --install-custom-component
```

If detection finds more than one configuration directory, give it explicitly:

```bash
# Home Assistant OS Terminal & SSH app
./setup.sh --config-dir /config --install-custom-component

# Home Assistant Container host
./setup.sh --config-dir /path/to/home-assistant/config --install-custom-component
```

Restart Home Assistant, then open:

```text
Settings -> Devices & services -> Add integration -> MeshNet
```

The UI walks through:

```text
radio platform -> connection method -> relevant fields -> connection test -> add another?
```

No YAML or gateway JSON is needed. Later, choose **Configure** on the MeshNet integration to add, edit, or remove gateways.

## Pick the easiest connection

| Radio | Connection | When to use it |
| --- | --- | --- |
| Meshtastic | Wi-Fi/Ethernet TCP | Recommended when the radio is on the LAN; default port `4403` |
| Meshtastic | USB serial | Reliable local connection; use a stable `/dev/serial/by-id/` path |
| Meshtastic | Bluetooth | Local adapter only; useful when TCP/USB is unavailable |
| Meshtastic | MQTT JSON | Advanced; requires Meshtastic JSON uplink and an exact downlink topic for sending |
| MeshCore | USB serial | Recommended direct MeshCore connection |
| MeshCore | Wi-Fi/Ethernet TCP | Use the TCP port configured by the device or bridge |
| MeshCore | Bluetooth | Local adapter only; may require a PIN |
| MeshCore | MQTT/REST JSON bridge | Advanced; requires a bridge implementing the documented JSON contract |

MQTT is not a magic replacement for a broker or bridge. Meshtastic MQTT consumes only the decoded `/json/` branch; MeshCore MQTT and REST require a compatible external JSON bridge.

## Container hardware access

Network gateways need to be reachable from inside the Home Assistant container, not just from the Docker host.

For USB, map the stable host path into the container:

```yaml
devices:
  - /dev/serial/by-id/usb-YOUR_RADIO:/dev/mesh-radio
```

Then enter `/dev/mesh-radio` in MeshNet.

For local Bluetooth, Home Assistant Container needs BlueZ on the host plus D-Bus and Bluetooth capabilities:

```yaml
cap_add:
  - NET_ADMIN
  - NET_RAW
volumes:
  - /run/dbus:/run/dbus:ro
```

The radio SDKs currently require a local Bluetooth adapter; Home Assistant Bluetooth proxies are not supported for these direct connections.

## Verify and troubleshoot

Run the verifier after restart:

```bash
./verify_setup.sh --config-dir /config
```

For Container, pass the host-mounted configuration directory. The verifier now prints a pass/warn/fail summary and exits nonzero for required failures.

Useful documentation:

1. [Installation](docs/INSTALL.md)
2. [Configuration](docs/CONFIGURATION.md)
3. [Usage](docs/USAGE.md)
4. [Troubleshooting](docs/TROUBLESHOOTING.md)
5. [Security](docs/SECURITY.md)
6. [Architecture](docs/ARCHITECTURE.md)

## Development check

The radio-independent suite does not need physical hardware:

Use Python 3.13 for the minimum-Home-Assistant development environment in
`requirements-dev.txt`. CI also tests the current Home Assistant release on
Python 3.14.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
python -m compileall -q custom_components tests
python -m ruff check .
bash -n setup.sh install.sh verify_setup.sh uninstall.sh
```

The integration source is `custom_components/meshnet`. Runtime history is stored as `meshnet.sqlite3` in the Home Assistant configuration directory. Diagnostics expose aggregate health/count data only—not configuration, message content, node identifiers, or locations—and the panel and WebSocket API require an administrator account.

## License

MeshNet is available under the [MIT License](LICENSE). Meshtastic, MeshCore,
Home Assistant, and their respective names and logos belong to their respective
owners; this project is not affiliated with or endorsed by them.
