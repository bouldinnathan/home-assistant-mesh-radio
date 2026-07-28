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

[Open MeshNet in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=bouldinnathan&repository=home-assistant-mesh-radio&category=integration)

MeshNet targets Home Assistant 2025.1.4 and newer. Evaluate the current custom
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

For Meshtastic Bluetooth, Home Assistant commits verified connection metadata
immediately with safe global defaults. MeshNet attempts rollback inside the
active pairing transaction if verification fails. After that transaction ends,
it preserves external BlueZ state instead of guessing that a same-address bond
still belongs to MeshNet. Radio SDK startup then runs as an entry-owned
background task, so a slow BLE discovery or configuration exchange cannot hold
the setup dialog or Home Assistant startup open.

For USB serial, the setup form lists local devices visible to Home Assistant. Choose one from the dropdown, or type an advanced path such as `/dev/serial/by-id/usb-YOUR_RADIO` or a custom container mapping.

For a fully local Meshtastic setup, choose **Bluetooth**. The radio talks
directly to the selected adapter on the Home Assistant host; no MQTT broker,
Internet access, Wi-Fi, or LAN connection is used after the integration and its
Python dependencies have been installed. Wi-Fi/TCP remains an optional
fallback, not a requirement.

## Pick the easiest connection

| Radio | Connection | When to use it |
| --- | --- | --- |
| Meshtastic | Wi-Fi/Ethernet TCP | Recommended when the radio is on the LAN; default port `4403` |
| Meshtastic | USB serial | Reliable local connection; use a stable `/dev/serial/by-id/` path |
| Meshtastic | Bluetooth | Direct, offline local connection; no MQTT, Wi-Fi, or Internet required |
| Meshtastic | MQTT JSON | Advanced; requires Meshtastic JSON uplink and an exact downlink topic for sending |
| MeshCore | USB serial | Recommended direct MeshCore connection |
| MeshCore | Wi-Fi/Ethernet TCP | Use the TCP port configured by the device or bridge |
| MeshCore | Bluetooth | Local adapter only; may require a PIN |
| MeshCore | MQTT/REST JSON bridge | Advanced; requires a bridge implementing the documented JSON contract |

MQTT is not a magic replacement for a broker or bridge. Meshtastic MQTT consumes only the decoded `/json/` branch; MeshCore MQTT and REST require a compatible external JSON bridge.

### Direct Meshtastic Bluetooth

> [!NOTE]
> This describes the version 0.5 behavior. Install a version 0.5 build before
> expecting these pairing controls in Home Assistant.

MeshNet provides a Home Assistant pairing wizard for Meshtastic radios on a
local BlueZ adapter. Choose a discovered radio from the dropdown, or use the
advanced field to enter its canonical MAC address (for example,
`AA:BB:CC:DD:EE:FF`). Bluetooth proxies cannot perform this direct pairing.
MeshNet records the controller's stable hardware address and resolves a fresh
Home Assistant `BLEDevice` through that exact local controller on every
connection attempt. Other valid local adapters may remain powered when the
selected radio is visible through one unambiguous local controller; Linux
`hciN` renumbering cannot redirect runtime validation or an explicitly
confirmed cleanup to another adapter.

Close the Meshtastic phone app before pairing; a radio normally accepts only
one Bluetooth client at a time. Select **Start pairing**, then enter the
six-digit code shown by a screened radio using `RANDOM_PIN`. A screenless radio
can instead use its configured fixed PIN. The factory default may be `123456`,
but change that default in Meshtastic before relying on Bluetooth.

The PIN field is password-masked, and MeshNet does not store or log the PIN.
The PIN prompt expires after about 50 seconds; the entire pairing transaction
has a 75-second limit. On Home Assistant OS, this flow is intended to replace
normal-use root-shell or `bluetoothctl` instructions.

BlueZ provides no identifier for a particular generation of a bond. MeshNet
therefore never removes a Bluetooth bond during config-entry deletion or HACS
uninstall, and abandoning a flow after its pairing transaction has ended never
starts delayed cleanup. Canceling an active pairing transaction may immediately
roll back the still-uncommitted bond using identity-guarded D-Bus state.
**Configure → Remove gateway** offers an optional current-bond removal checkbox
that is off by default and warns that other apps may be disconnected. Leaving it
off preserves the BlueZ bond.

After pairing, MeshNet keeps one persistent, local BLE connection. Its async
transport subscribes for radio notifications before requesting configuration,
actively reads the first response, bounds connection/configuration/teardown,
and reconnects with backoff after an established session is lost. This avoids
the indefinitely blocking synchronous BLE constructor used by older releases.
The radio normally permits only one Bluetooth client, so close the Meshtastic
phone or web client while Home Assistant owns the connection; use MeshNet's
local Home Assistant panel for normal operation.

## Container hardware access

Network gateways need to be reachable from inside the Home Assistant container, not just from the Docker host.

For USB, map the stable host path into the container:

```yaml
devices:
  - /dev/serial/by-id/usb-YOUR_RADIO:/dev/mesh-radio
```

Then type `/dev/mesh-radio` into MeshNet's USB picker.

For local Bluetooth, Home Assistant Container needs BlueZ on the host plus D-Bus and Bluetooth capabilities:

```yaml
cap_add:
  - NET_ADMIN
  - NET_RAW
volumes:
  - /run/dbus:/run/dbus:ro
```

The radio SDKs require a local Bluetooth adapter; Home Assistant Bluetooth
proxies are not supported for these direct connections or for the version 0.5
pairing wizard.

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

The integration source is `custom_components/meshnet`. Runtime history is stored as `meshnet.sqlite3` in the Home Assistant configuration directory. The admin-only three-dot menu provides a detailed, cached **Download diagnostics** report covering versions, lifecycle/task state, retained identity-free Bluetooth failure phases and counters, constructor ownership, gateway and node radio health, repairs, registries, and SQLite/outbox aggregates. It performs no radio operations and redacts or omits identities, endpoints, credentials, message/raw content, precise locations, and occupancy-related state from MeshNet's data section. Home Assistant's standard outer wrapper and filename can still contain the config-entry ID and system metadata, so inspect and rename a report before sharing it. The panel and WebSocket API also require an administrator account.

## License

MeshNet's source is available under the [MIT License](LICENSE). Installed
Meshtastic, MeshCore, Home Assistant, and other third-party runtime dependencies
remain under their own licenses. Their respective names and logos belong to
their owners; this project is not affiliated with or endorsed by them.
