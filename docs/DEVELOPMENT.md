# Development

This guide is for working on the integration code, docs, and setup scripts.

## Repository Layout

```text
custom_components/meshnet/      Home Assistant custom integration
custom_components/meshnet/entities/
custom_components/meshnet/frontend/
tests/                          Unit tests for normalization, store, dedupe, clients
examples/                       Example HA YAML
docs/                           Operator and developer docs
setup.sh                        Safe setup and staging helper
install.sh                      .env wrapper around setup.sh
verify_setup.sh                 Non-mutating verification helper
uninstall.sh                    Rollback/removal helper
docker-compose.yml              Local HA container for testing
```

## Local Test Environment

Create a Python venv:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pytest ruff
```

Run tests:

```bash
python -m pytest
python -m compileall -q custom_components tests
python -m ruff check .
```

Expected: all tests and checks pass. The exact test count grows with coverage.

If `ruff` is not installed, skip that check or install it as shown above.

## Script Validation

```bash
bash -n setup.sh install.sh verify_setup.sh uninstall.sh
./install.sh --help
./setup.sh --help
./verify_setup.sh --help
./uninstall.sh --help
```

No command above should modify Home Assistant.

## Local Home Assistant With Docker

```bash
cp .env.example .env
docker compose up -d
docker compose logs -f homeassistant
```

Open:

```text
http://localhost:8123
```

The Compose file live-mounts:

```text
./custom_components/meshnet -> /config/custom_components/meshnet
```

Restart after code changes:

```bash
docker compose restart homeassistant
```

Check container health:

```bash
docker compose ps
```

## Manual Integration Install For Testing

```bash
HA_CONFIG_DIR=/config
mkdir -p "$HA_CONFIG_DIR/custom_components"
cp -a custom_components/meshnet "$HA_CONFIG_DIR/custom_components/meshnet"
ha core restart
```

## Data Model

Important model classes:

- `GatewayConfig`
- `GatewayStatus`
- `MeshPacket`
- `NodeState`
- `MessageRecord`
- `MeshSnapshot`

Provider-specific clients normalize raw data into these models:

- `meshtastic_client.py`
- `meshcore_client.py`

Entities read only the normalized coordinator snapshot.

## Persistence

SQLite file:

```text
<HA_CONFIG_DIR>/meshnet.sqlite3
```

Tables:

- `nodes`
- `messages`
- `packets`
- `routes`

Tests use temporary SQLite files and do not touch Home Assistant data.

## Adding A Gateway Transport

1. Add constants only if the transport name is new.
2. Add validation in `config_flow.py`.
3. Implement start/stop/send/refresh behavior in the provider client.
4. Normalize packets into `MeshPacket`.
5. Normalize node data into `NodeState`.
6. Add unit tests for normalization and connection parameters.
7. Update `docs/CONFIGURATION.md` and `docs/TROUBLESHOOTING.md`.

## Adding Entities

Entity rules:

- Keep stable unique IDs.
- Use existing `MeshNetGatewayEntity` and `MeshNetNodeEntity` bases.
- Add entities only when values are known or when the entity is a core status entity.
- Avoid changing existing entity names unless a migration is planned.

## Documentation Rules

When changing behavior:

1. Update README if the quickstart changes.
2. Update `docs/INSTALL.md` for install or operational changes.
3. Update `docs/CONFIGURATION.md` for config schema changes.
4. Update `docs/USAGE.md` for services, entities, events, or UI behavior.
5. Update `docs/TROUBLESHOOTING.md` for new failure modes.
6. Update `.env.example` for new setup environment variables.

Every command in docs should be copy-paste-ready or clearly marked as a placeholder.

## Release Checklist

```bash
python -m pytest
python -m compileall -q custom_components tests
python -m ruff check .
bash -n setup.sh install.sh verify_setup.sh uninstall.sh
python -m json.tool custom_components/meshnet/manifest.json >/dev/null
python -m json.tool custom_components/meshnet/strings.json >/dev/null
python -m json.tool custom_components/meshnet/translations/en.json >/dev/null
```

Then test install flow:

```bash
DRY_RUN=1 ./install.sh
./verify_setup.sh --output-dir ./ha-mesh-setup-output
```

For Docker:

```bash
docker compose config
docker compose up -d
docker compose ps
```
