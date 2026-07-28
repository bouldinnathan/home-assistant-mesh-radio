# Security

MeshNet can transmit radio messages and may expose location, names, telemetry, and operational status. Treat it as an operational control surface, not just a dashboard.

## Safe Defaults

- The MeshNet sidebar panel is admin-only.
- MeshNet websocket commands require a Home Assistant admin user.
- Diagnostics redact common secret fields.
- `setup.sh` does not edit `configuration.yaml`.
- `install.sh` defaults to dry-run when `.env` is missing.
- Existing component files are backed up before replacement.

## Secrets

Do not commit real secrets to Git.

Use Home Assistant `secrets.yaml`:

```yaml
meshcore_api_key: "REPLACE_WITH_REAL_KEY"
```

Reference it from YAML:

```yaml
api_key: !secret meshcore_api_key
```

Keep `.env` local. `.env.example` is safe to commit; `.env` should not contain shareable values.

## Exposed Ports

Default Home Assistant port:

```text
8123/tcp
```

Docker Compose maps:

```yaml
ports:
  - "${HA_HTTP_PORT:-8123}:8123"
```

Recommendations:

- Do not expose Home Assistant directly to the public internet.
- Use a trusted reverse proxy with TLS if remote access is required.
- Prefer Home Assistant Cloud, VPN, or private network access.
- Firewall Home Assistant so only trusted clients can reach it.

## Authentication And Authorization

MeshNet send actions can transmit over radio. Use Home Assistant admin accounts carefully.

Recommended:

- Use unique Home Assistant accounts per person.
- Do not share admin credentials.
- Disable unused accounts.
- Use long random passwords.
- Enable multi-factor authentication where possible.

The panel and websocket commands are admin-only, but Home Assistant service calls may still be available to automations and scripts. Review who can edit automations.

## USB Permissions

Serial devices usually belong to groups such as `dialout`, `tty`, or `uucp`.

Check:

```bash
ls -l /dev/ttyUSB0
id -nG
```

Grant only the group needed:

```bash
sudo usermod -aG dialout "$USER"
```

Avoid running broad host scripts as root unless needed. The setup helper does not require root for normal dry runs.

## Docker Permissions

Avoid `privileged: true` unless you understand the risk and need it for a specific hardware path.

Prefer explicit device mappings:

```yaml
devices:
  - /dev/serial/by-id/usb-YOUR_DEVICE:/dev/meshtastic0
```

If using `network_mode: host`, remember:

- The container shares the host network namespace.
- Port mappings are ignored.
- More services may be reachable from the container.

## MQTT Security

If using MQTT:

- Require MQTT username/password.
- Do not allow anonymous publish unless the broker is isolated.
- Limit topics if your broker supports ACLs.
- Use a dedicated publish topic for MeshNet commands.

Example topic separation:

```yaml
mqtt_topic: msh/+/2/json/#
options:
  publish_topic: msh/US/2/json/mqtt/
```

## REST Security

If using REST gateways:

- Use `api_key` when supported.
- Prefer HTTPS on untrusted networks.
- Keep REST bridges on a private network.
- Do not expose REST bridges to the public internet.

## Radio Privacy

Mesh messages and metadata may be observable by other mesh participants depending on protocol and channel settings.

Avoid sending:

- Passwords
- Door codes
- Private keys
- Sensitive medical or financial data
- Precise home location unless intentionally shared

## Bluetooth Pairing and Runtime Security (version 0.5)

Meshtastic pairing is limited to a local BlueZ adapter and the exact canonical
MAC address selected in the Home Assistant form. Bluetooth proxies are not
accepted for this direct connection. MeshNet registers a temporary,
application-scoped BlueZ agent; it does not request the system-default agent
role and rejects pairing callbacks for any other device.

Ownership is bound to the controller's stable Adapter1 address as well as the
radio address. Runtime resolves a fresh Home Assistant `BLEDevice` through that
exact current controller and rejects proxy or wrong-controller candidates, so
other valid local adapters may remain powered when ownership is unambiguous and
an `hciN` rename cannot redirect the connection or deletion. New Meshtastic Bluetooth gateways cannot
bypass pairing through YAML or Advanced JSON.

Bluetooth protocol work is asynchronous and stage-bounded. A single supervisor
owns GATT, configuration, reader, and reconnect tasks; it cancels and awaits
them before releasing MeshNet's endpoint lease. Configuration subscribes to
notifications before sending `want_config` and performs an active FromRadio
read, including firmware that does not emit a fresh notification for that
request. Runtime does not use MQTT, Internet, Wi-Fi, or the LAN.

The six-digit PIN field is password-masked. The value exists only for the
active pairing request and is not written to config entries, options,
diagnostics, or logs. The PIN prompt expires after about 50 seconds, the entire
pairing transaction has a 75-second limit, and the temporary agent is removed
after success, failure, cancellation, or timeout.

Screened radios should use Meshtastic `RANDOM_PIN`. A screenless device may need
a fixed PIN; its factory value may be `123456`, but that shared default should
be changed before regular use. Close the Meshtastic phone app before pairing so
it cannot compete for the radio's single Bluetooth client connection.

MeshNet records that it originally paired an adapter-scoped radio, but it does
not treat that marker as proof about the current bond generation. BlueZ exposes
no generation identifier: another client can remove and recreate a bond at the
same address and Device1 path. Pre-0.4 entries are migrated by stripping any
untrusted originally-paired marker.

Pairing submissions are serialized, rate-limited, and idempotent. If Pair
succeeds but verification fails, MeshNet attempts bounded rollback immediately
inside the active D-Bus transaction. If rollback cannot be verified, the exact
Device1/controller evidence remains in memory only to block overlapping MeshNet
pairing flows. Ending the flow releases that evidence without a delayed
`RemoveDevice` call, because delayed deletion could target a bond recreated by
another client.

Config-entry deletion and HACS uninstall never remove BlueZ bonds. Abandoning a
flow after its pairing transaction has ended never starts delayed deletion.
Canceling while a pairing transaction is still active may immediately roll back
that transaction's uncommitted bond, with the same Device1 and stable-controller
identity guards. The guided Remove-gateway form offers an explicitly confirmed,
address-scoped current-bond deletion; it is off by default and warns that other
apps may be disconnected. This consent is not presented as proof that the
current bond is the historical one MeshNet created.

## Diagnostics

MeshNet's `data` section contains detailed cached health, lifecycle, version, telemetry, and
aggregate database information. Collection performs no radio connection,
pairing, scan, refresh, or transmit operation. Reports exclude raw packets,
message text, gateway and node identities/names, network addresses, serial
paths, URLs, MQTT topics, precise locations, occupancy-related state, and raw
provider/configuration values. A broad defensive redaction layer covers keys
named like:

- `api_key`
- `authorization`
- `client_secret`
- `password`
- `pin`
- `token`
- `private_key`
- `secret`

It also removes common credentials, URLs, IP/MAC addresses, serial paths, and
long identifiers embedded inside diagnostic strings. New repair issue IDs use
non-identifying gateway ordinals because Home Assistant appends repair metadata
outside the integration's own diagnostic payload. During setup MeshNet also
deletes pre-0.4.2 repair IDs that embedded a configured gateway slug; only
aggregate issue categories and counts are exported by MeshNet.

Home Assistant wraps MeshNet's payload with system information, installed
custom-component metadata, setup timing, manifest data, and repair issues. It
also builds the downloaded filename from registry metadata: config-entry files
contain the entry ID, while device files can contain the device name and
registry ID even though MeshNet's JSON section is redacted. Inspect the entire
Home Assistant wrapper and rename the file before sharing it publicly.

Browser downloads commonly inherit a permissive host umask. Restrict a saved
report before retaining it on a multi-user system:

```bash
chmod 600 config_entry-meshnet-*.json
```

Before sharing diagnostics publicly, still inspect the file manually.

The setup helper stores absolute paths and local hardware identifiers under
`ha-mesh-setup-output/`. That directory is Git-ignored and restricted to the
current user (`0700`), but it should still never be attached to a public issue
without careful review.

## Backups

Backups may contain message history and node locations.

Protect:

```text
<HA_CONFIG_DIR>/meshnet.sqlite3
<HA_CONFIG_DIR>/.storage
<HA_CONFIG_DIR>/secrets.yaml
```

Use file permissions appropriate for your host:

```bash
chmod 600 /config/secrets.yaml
```

## Uninstall Safety

Deleting the MeshNet integration entry and uninstalling through HACS preserve
all external BlueZ bonds. If address-scoped cleanup is desired, first use
**Configure → Remove gateway**, enable **Remove this radio's current Bluetooth
bond (may disconnect other apps)**, and confirm. The checkbox is off by default,
and a failed cleanup keeps the gateway. Otherwise, remove the gateway/entry and
HACS package without changing Bluetooth state.

`uninstall.sh` removes only an existing component whose path is exactly the
recorded `<config_dir>/custom_components/meshnet` and whose manifest declares
the `meshnet` domain. It rejects symlinked and mismatched targets. It does not
remove:

- `meshnet.sqlite3`
- Home Assistant config entries
- Home Assistant entity registry data
- Automations
- Dashboards

This is intentional to prevent data loss. Remove those manually only after backing up.

HACS custom integrations execute inside the Home Assistant Core process. These
pairing restrictions reduce privilege and scope, but they do not provide crash
or process isolation. True isolation requires a separate Home Assistant App or
sidecar that communicates with Home Assistant over a bounded interface such as
MQTT.

## Reporting Security Issues

Report ordinary bugs through the repository's
[issue tracker](https://github.com/bouldinnathan/home-assistant-mesh-radio/issues).
Do not post credentials, Bluetooth PINs, private addresses, diagnostics, or
other sensitive data in a public issue. For a vulnerability that needs a private
report, use GitHub's private vulnerability-reporting option on the repository's
**Security** tab when it is available.
