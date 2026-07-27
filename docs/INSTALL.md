# Installation

This guide installs the MeshNet Home Assistant custom integration and verifies that Home Assistant can see your mesh gateways.

## Supported Systems

Supported test deployments for the current in-process integration (Home
Assistant 2025.1 or newer):

- Home Assistant OS
- Home Assistant Container with Docker or Docker Compose

Legacy, best-effort test deployments:

- Home Assistant Supervised
- Home Assistant Core in a Python virtual environment

Home Assistant ended production support for Core and Supervised with 2025.12. New installations should use OS or Container.

For a production deployment that requires fault isolation, use the standalone
App/MQTT architecture in [Distribution and isolation](DISTRIBUTION.md). It has
not yet been implemented in this repository.

Supported host operating systems for the helper scripts:

- Debian 12
- Ubuntu 22.04 LTS or 24.04 LTS
- Raspberry Pi OS based on Debian 12
- Home Assistant OS Terminal & SSH add-on shell

The scripts use Bash and Linux device paths. They are not designed for Windows PowerShell or macOS as the Home Assistant host.

## Hardware Requirements

Minimum:

- A machine running Home Assistant
- One configured Meshtastic or MeshCore gateway
- Network access from Home Assistant to TCP, REST, or MQTT gateways
- USB access from Home Assistant to serial gateways

Recommended:

- Stable DHCP reservations for every WiFi gateway
- Stable USB paths from `/dev/serial/by-id/`
- A powered USB hub for multiple radios
- At least 1 GB free disk space for Home Assistant logs, dependencies, and `meshnet.sqlite3`

## Software Dependencies

On the host running the helper scripts:

```bash
sudo apt-get update
sudo apt-get install -y bash coreutils findutils grep gawk sed python3 python3-venv netcat-openbsd usbutils
```

Optional but useful:

```bash
sudo apt-get install -y git curl docker.io docker-compose-plugin
```

Home Assistant installs Python integration requirements from `custom_components/meshnet/manifest.json` when the integration loads:

```text
meshtastic==2.7.11
meshcore==2.3.7
```

For a legacy Home Assistant Core venv, install those packages inside the Home Assistant venv if automatic installation fails.

## Fresh VM Path

This path is for a new Ubuntu or Debian VM running Home Assistant Container.

1. Install OS packages:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv netcat-openbsd usbutils docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Expected result:

```text
Adding user ... to group docker
```

Log out and back in so the Docker group applies.

2. Clone or copy this repository:

```bash
git clone https://github.com/OWNER/home-assistant-mesh-radio.git home-assistant-mesh-radio
cd home-assistant-mesh-radio
```

If you already have the repo directory, use:

```bash
cd /path/to/home-assistant-mesh-radio
```

3. Create local settings:

```bash
cp .env.example .env
nano .env
```

Set these values:

```dotenv
HA_CONFIG_DOCKER=./ha-config
HA_HTTP_PORT=8123
TZ=Etc/UTC
```

4. Start Home Assistant:

```bash
docker compose up -d
docker compose ps
```

Expected result:

```text
meshnet-homeassistant   ...   Up
```

5. Open Home Assistant:

```text
http://<VM_IP_ADDRESS>:8123
```

Complete the Home Assistant onboarding flow.

6. Install or stage MeshNet:

For Docker Compose with the default `docker-compose.yml`, the integration is live-mounted at:

```text
/config/custom_components/meshnet
```

You can still run the setup helper to detect devices and generate config:

```bash
HA_CONFIG_DIR="$(pwd)/ha-config" DRY_RUN=1 ./install.sh
```

Review:

```bash
sed -n '1,220p' ha-mesh-setup-output/NEXT_STEPS.txt
```

7. Restart Home Assistant:

```bash
docker compose restart homeassistant
```

8. Add MeshNet:

```text
Settings -> Devices & Services -> Add Integration -> MeshNet
```

## Home Assistant OS Path

Run these commands from the Terminal & SSH add-on.

1. Go to the repository:

```bash
cd /config/home-assistant-mesh-radio
```

2. Dry run:

```bash
./setup.sh --config-dir /config --dry-run
```

Expected result:

```text
Dry run mode: enabled
Wrote environment detection ...
Wrote serial detection ...
Done.
```

3. Install the custom component:

```bash
./setup.sh --config-dir /config --install-custom-component
```

4. Restart Home Assistant:

```bash
ha core restart
```

If `ha` is not available, use:

```text
Settings -> System -> Restart Home Assistant
```

5. Add the integration from the UI.

## Docker Setup Path

1. Copy env file:

```bash
cp .env.example .env
nano .env
```

2. Start Home Assistant:

```bash
docker compose up -d
docker compose logs -f homeassistant
```

Expected useful log line:

```text
Starting Home Assistant
```

3. If using USB radios, find stable device paths:

```bash
ls -l /dev/serial/by-id/
```

Edit `docker-compose.yml` and uncomment device mappings:

```yaml
devices:
  - /dev/serial/by-id/usb-YOUR_MESHTASTIC_DEVICE:/dev/meshtastic0
```

Restart:

```bash
docker compose up -d
docker compose exec homeassistant ls -l /dev/meshtastic0
```

Use `/dev/meshtastic0` as the MeshNet `serial_path`.

4. Add MeshNet in Home Assistant.

## Manual Setup Path

Use this when you already have Home Assistant running and want to copy the integration yourself.

1. Find your Home Assistant config directory:

```bash
for p in /config /homeassistant /usr/share/hassio/homeassistant /var/lib/homeassistant "$HOME/.homeassistant"; do
  [ -d "$p" ] && echo "$p"
done
```

2. Copy the custom component:

```bash
HA_CONFIG_DIR=/config
mkdir -p "$HA_CONFIG_DIR/custom_components"
cp -a custom_components/meshnet "$HA_CONFIG_DIR/custom_components/meshnet"
```

3. Restart Home Assistant.

4. Add MeshNet from:

```text
Settings -> Devices & Services -> Add Integration -> MeshNet
```

## Environment Variables

`install.sh` reads `.env`. Important variables:

| Variable | Purpose |
| --- | --- |
| `HA_CONFIG_DIR` | Home Assistant config directory for setup/install scripts |
| `MESHNET_OUTPUT_DIR` | Output directory for generated files and logs |
| `DRY_RUN` | `1` means detect and generate only |
| `INSTALL_CUSTOM_COMPONENT` | `1` copies `custom_components/meshnet` into Home Assistant |
| `YES` | `1` skips confirmation prompts |
| `VERBOSE` | `1` prints extra command output |
| `WIFI_MESHTASTIC` | One or more `HOST:PORT` Meshtastic TCP endpoints |
| `USB_MESHTASTIC` | One or more Meshtastic serial device paths |
| `WIFI_MESHCORE` | One or more `HOST:PORT` MeshCore TCP endpoints |
| `USB_MESHCORE` | One or more MeshCore serial device paths |
| `HA_VERSION` | Home Assistant container image tag; set an exact version for reproducible testing |
| `HA_CONFIG_DOCKER` | Docker Compose config directory bind mount |
| `HA_HTTP_PORT` | Host port mapped to Home Assistant port `8123` |
| `TZ` | Container timezone |

## First Run

After installing and restarting:

1. Open Home Assistant.
2. Go to `Settings -> Devices & Services`.
3. Click `Add Integration`.
4. Search for `MeshNet`.
5. Choose the radio platform.
6. Choose a filtered connection method.
7. Enter only the fields shown for that method.
8. Keep **Test before saving** enabled to catch network, device, MQTT, and REST problems from inside Home Assistant.
9. Add another gateway if needed, then finish setup.

Use the integration's **Configure** button later to add, edit, or remove gateways without editing JSON.

Required transport fields:

| Transport | Required field |
| --- | --- |
| TCP | `host` and `port` |
| USB serial | `serial_path` |
| Bluetooth | `ble_address` |
| MQTT | Home Assistant MQTT configured and a decoded JSON subscribe topic |
| REST | `api_url` |

## Verify Everything Works

Run the repository verifier:

```bash
./verify_setup.sh --config-dir /config
```

For Docker:

```bash
./verify_setup.sh --config-dir "$(pwd)/ha-config"
docker compose ps
docker compose logs --tail=200 homeassistant
```

In Home Assistant verify:

```text
Settings -> Devices & Services -> MeshNet -> Devices
```

Expected:

- One MeshNet hub device
- One device per configured gateway
- Node devices appear after packets arrive
- Summary sensors exist immediately
- Gateway online sensors exist immediately

## Known Good Test

Without radios:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pytest
python -m compileall -q custom_components tests
bash -n setup.sh install.sh verify_setup.sh uninstall.sh
```

Expected: all tests pass. The exact count grows as transport and compatibility coverage is added.

With a TCP gateway:

```bash
nc -z -w 3 192.0.2.50 4403
echo $?
```

Expected:

```text
0
```

With a USB gateway:

```bash
ls -l /dev/serial/by-id/
```

Expected:

```text
usb-... -> ../../ttyUSB0
```

## Logs

Repository setup logs:

```text
ha-mesh-setup-output/install_log.txt
ha-mesh-setup-output/detected_environment.txt
ha-mesh-setup-output/detected_serial_devices.txt
```

Home Assistant logs:

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

## Restart

Docker Compose:

```bash
docker compose restart homeassistant
```

Home Assistant OS:

```bash
ha core restart
```

UI:

```text
Settings -> System -> Restart Home Assistant
```

## Update

1. Back up first:

```bash
HA_CONFIG_DIR=/config
mkdir -p backups
cp -a "$HA_CONFIG_DIR/custom_components/meshnet" "backups/meshnet-component-$(date +%Y%m%d-%H%M%S)"
cp -a "$HA_CONFIG_DIR/meshnet.sqlite3" "backups/meshnet.sqlite3-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
```

2. Update the repo:

```bash
git pull
```

3. Reinstall:

```bash
./setup.sh --config-dir /config --install-custom-component
```

4. Restart Home Assistant.

## Backup And Restore

Back up:

```bash
HA_CONFIG_DIR=/config
mkdir -p backups
cp -a "$HA_CONFIG_DIR/custom_components/meshnet" backups/meshnet-component
cp -a "$HA_CONFIG_DIR/meshnet.sqlite3" backups/meshnet.sqlite3 2>/dev/null || true
cp -a "$HA_CONFIG_DIR/.storage" backups/storage
```

Restore:

```bash
HA_CONFIG_DIR=/config
cp -a backups/meshnet-component "$HA_CONFIG_DIR/custom_components/meshnet"
cp -a backups/meshnet.sqlite3 "$HA_CONFIG_DIR/meshnet.sqlite3"
```

Restart Home Assistant after restoring.

## Uninstall

First delete the MeshNet integration entry while its code is still loaded:

```text
Settings -> Devices & Services -> MeshNet -> three-dot menu -> Delete
```

If HACS installed it, remove MeshNet from HACS and restart Home Assistant.

If `setup.sh` installed it, use the recorded, validated rollback path:

```bash
./uninstall.sh --metadata ha-mesh-setup-output/rollback_info.json
```

Dry run:

```bash
./uninstall.sh --metadata ha-mesh-setup-output/rollback_info.json --dry-run
```

For a recoverable manual removal, move only the component directory aside:

```bash
mv /config/custom_components/meshnet /config/meshnet-component-removed
```

Restart Home Assistant and verify normal operation before deleting the moved
copy. `meshnet.sqlite3`, recorder history, logs, and cached Python dependencies
are separate and are not removed automatically. A disposable test instance or
full backup restore is the cleanest complete rollback.
