# Distribution and isolation

MeshNet currently ships as a Home Assistant custom integration. That is the
quickest way to test the UI and radio support, but custom integrations execute
inside Home Assistant Core and therefore do not provide a hard failure boundary.

## Quick disposable test: GitHub and HACS

A public GitHub repository plus a HACS custom repository is the shortest test
path. MeshNet already has a guided config flow and optional YAML import.

Before publishing a fork, complete these repository-specific items:

1. In `custom_components/meshnet/manifest.json`, replace `documentation` with
   the real repository URL, add its `issue_tracker` URL, and put the maintainer's
   GitHub handle in `codeowners`.
2. Keep the included `custom_components/meshnet/brand/icon.png` and optional
   high-density `icon@2x.png` with the integration.
3. Keep the included MIT `LICENSE` and its notices with redistributed copies.
4. Run the tests and publish the repository publicly. HACS cannot install a
   private GitHub repository.

Install on the test Home Assistant instance:

1. Open HACS, select the three-dot menu, then **Custom repositories**.
2. Paste `https://github.com/bouldinnathan/home-assistant-mesh-radio`, choose **Integration**, and add it.
3. Open MeshNet in HACS, choose **Download**, and restart Home Assistant.
4. Open **Settings -> Devices & services -> Add integration -> MeshNet**.

Once the repository exists, this My Home Assistant link can be placed in the
README to open it directly in HACS:

```text
https://my.home-assistant.io/redirect/hacs_repository/?owner=bouldinnathan&repository=home-assistant-mesh-radio&category=integration
```

Releases are recommended for stable versions but are not required for testing;
HACS can install the default branch.

This route is not suitable for the strict "cannot break Home Assistant"
requirement. It installs under `/config/custom_components`, loads the radio SDKs
inside Core, and can leave `meshnet.sqlite3`, recorder history, logs, and cached
Python dependencies after removal. For the cleanest test rollback, use a full
Home Assistant backup or a disposable test instance.

## Recommended final package: Home Assistant App plus MQTT

For Home Assistant OS, extract the radio logic into a standalone Home Assistant
App (formerly called an add-on). The app should be its own protected container:

```text
Meshtastic/MeshCore radio
        | USB, TCP, BLE, or decoded MQTT
        v
MeshNet App container
        | MQTT Discovery, state, command, availability
        v
MQTT broker -> Home Assistant's built-in MQTT integration
```

The app should:

- have no Home Assistant `/config` mount and no Home Assistant or Supervisor API
  permission;
- run without host networking, privileged mode, or root where possible;
- receive only the specific serial devices it needs;
- use protection mode and an AppArmor profile;
- keep its database and configuration only under its private `/data` directory;
- obtain broker credentials through the Home Assistant MQTT service;
- publish retained MQTT Discovery documents and delete those retained documents
  during shutdown/uninstall;
- publish availability with a last-will message and use bounded queues/storage;
- ship signed `amd64` and `aarch64` images from a public App repository.

Its `config.yaml` `options` and `schema` provide the normal Home Assistant GUI.
Advanced users can edit the same configuration as YAML. Removing the app then
removes its private container and data without deleting Home Assistant files.

An app materially reduces risk but cannot make a mathematical guarantee: a
process on the same machine can still consume CPU, memory, disk, USB, or network
resources. Bounded resource use, least privilege, backups, and fail-closed input
handling are still required.

## Home Assistant Container alternative

Home Assistant Apps are available only on Home Assistant OS. For Home Assistant
Container, run the same MeshNet daemon as a separate Docker Compose service with
a read-only root filesystem, a non-root user, explicit device mappings, no
`/config` mount, and CPU/memory/PID limits. Connect it to Home Assistant only
through MQTT. This gives the same architectural boundary and is stronger than a
HACS custom integration.

## What MQTT means in the current integration

The present custom integration uses Home Assistant's existing MQTT connection;
it does not include a broker and it does not use MQTT Discovery.

- Meshtastic can publish decoded JSON directly. Enable MQTT, JSON uplink, and
  channel uplink on a radio connected to the same broker. MeshNet subscribes to
  `msh/+/2/json/#`. Sending additionally needs downlink enabled, the exact
  `msh/<region>/2/json/mqtt/` topic, and the gateway radio's numeric node ID.
- MeshCore MQTT in this repository is not a standard radio feature. It expects
  an external bridge that publishes the documented decoded JSON and consumes
  `<publish_topic>/send`. Direct serial/TCP/BLE is simpler until that bridge or
  the standalone MeshNet App exists.

The future app can own either radio over USB/TCP/BLE and translate it to MQTT,
so the radios themselves do not have to support MQTT.
