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

## Diagnostics

Diagnostics contain aggregate health and count information only. They exclude
raw packets, message text, gateway and node identifiers, network addresses,
precise locations, and configuration values. A broad defensive redaction list
also covers keys named like:

- `api_key`
- `authorization`
- `client_secret`
- `password`
- `pin`
- `token`
- `private_key`
- `secret`

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

## Reporting Security Issues

This source snapshot does not declare a public issue tracker. If you publish this repository, add a real security contact and update:

```text
custom_components/meshnet/manifest.json
README.md
docs/SECURITY.md
```
