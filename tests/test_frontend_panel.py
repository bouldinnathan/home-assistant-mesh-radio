"""Behavior and safety checks for the dependency-free MeshNet panel."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

PANEL = Path("custom_components/meshnet/frontend/meshnet-panel.js")


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def _run_panel_script(body: str) -> None:
    """Load the custom element under Node and run a behavior assertion script."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = f"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
globalThis.HTMLElement = class {{}};
globalThis.window = {{ setTimeout() {{}}, clearTimeout() {{}} }};
let PanelClass = null;
let registeredPanel = null;
let panelDefineCount = 0;
globalThis.customElements = {{
  get(name) {{ return name === "meshnet-panel" ? registeredPanel : null; }},
  define(_name, constructor) {{
    panelDefineCount += 1;
    registeredPanel = constructor;
    PanelClass = constructor;
  }},
}};
const panelPath = {json.dumps(str(PANEL))};
vm.runInThisContext(fs.readFileSync(panelPath, "utf8"), {{ filename: panelPath }});
assert.ok(PanelClass, "panel custom element was not registered");
(async () => {{
  const panel = new PanelClass();
  {body}
}})().catch((error) => {{
  console.error(error);
  process.exitCode = 1;
}});
"""
    subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )


def test_panel_javascript_parses() -> None:
    """Keep malformed JavaScript out of a HACS release."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    subprocess.run(
        [node, "--check", str(PANEL)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_gateway_settings_view_is_explicit_and_keeps_drafts_in_memory() -> None:
    """Expose an admin editor without browser persistence or implicit writes."""
    source = _source()

    assert 'data-meshnet-view="settings"' in source
    assert "Gateway settings" in source
    assert "Administrator gateway controls" in source
    assert 'type: "meshnet/settings/get"' in source
    assert 'type: "meshnet/settings/preview"' in source
    assert 'type: "meshnet/settings/apply"' in source
    assert "Preview is required before Apply" in source
    assert 'id="meshnet-settings-critical"' in source
    assert "confirm_critical" in source
    assert 'type="password" value=""' in source
    assert 'autocomplete="new-password"' in source
    assert "this._settingsDraft = Object.create(null)" in source
    assert "Treat navigation away from the panel as abandoning the draft" in source
    assert 'id="meshnet-settings-gateway"${busy ? " disabled" : ""}' in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source


def test_settings_schema_is_bounded_typed_and_scrubs_secret_values() -> None:
    """Only the fixed settings contract reaches rendering or in-memory state."""
    _run_panel_script(
        r"""
  const response = panel._validateSettingsResponse({
    gateways: [{
      gateway_id: "gateway-1",
      name: "Living Room <radio>",
      protocol: "meshtastic",
      transport: "bluetooth",
      connected: true,
      writable: true,
    }],
    selected: {
      schema_version: 1,
      gateway_id: "gateway-1",
      name: "Living Room <radio>",
      protocol: "meshtastic",
      transport: "bluetooth",
      connected: true,
      writable: true,
      revision: "a".repeat(64),
      fetched_at: "2026-07-29T12:00:00Z",
      categories: [{
        key: "radio",
        label: "Radio & LoRa",
        description: "Device-provided fields",
        fields: [
          {
            path: "radio.enabled",
            label: "Enabled",
            type: "boolean",
            value: true,
            writable: true,
            critical: false,
            requires_reconnect: false,
          },
          {
            path: "radio.power",
            label: "Transmit power",
            type: "integer",
            value: 17,
            min: 0,
            max: 30,
            step: 1,
            unit: "dBm",
            writable: true,
            critical: true,
            requires_reconnect: true,
          },
          {
            path: "radio.region",
            label: "Region",
            type: "select",
            value: "US",
            options: [
              { value: "US", label: "United States" },
              { value: 7, label: "Typed numeric option" },
            ],
            writable: true,
            critical: true,
            requires_reconnect: true,
          },
          {
            path: "security.key",
            label: "Private key <never render>",
            type: "secret",
            value: "backend-must-not-expose-this",
            configured: true,
            allow_clear: true,
            max_length: 64,
            writable: true,
            critical: true,
            requires_reconnect: true,
          },
          {
            path: "config.power.powermon_enables",
            label: "Power Monitor Enables",
            type: "integer",
            value: 0,
            min: 0,
            max: Number.MAX_SAFE_INTEGER,
            writable: false,
            read_only_reason: "setting_requires_dedicated_semantic_validation",
            critical: false,
            requires_reconnect: false,
          },
        ],
      }],
      warnings: ["Changing region can disconnect the radio."],
    },
  });
  assert.equal(response.gateways[0].name, "Living Room <radio>");
  assert.equal(response.selected.categories[0].fields[0].value, true);
  assert.equal(response.selected.categories[0].fields[1].value, 17);
  assert.equal(response.selected.categories[0].fields[2].options[1].value, 7);
  const secret = response.selected.categories[0].fields[3];
  assert.equal(secret.value, "");
  assert.equal(secret.allow_clear, true);
  assert.equal(secret.max_length, 64);
  const powerMask = response.selected.categories[0].fields[4];
  assert.equal(powerMask.path, "config.power.powermon_enables");
  assert.equal(powerMask.max, Number.MAX_SAFE_INTEGER);
  const html = panel._settingsField(secret, 3, response.selected);
  assert.match(html, /type="password" value=""/);
  assert.match(html, /Configured/);
  assert.match(html, /Clear the configured secret/);
  assert.deepEqual(panel._coerceSettingValue(secret, { operation: "clear" }), {
    operation: "clear",
  });
  assert.doesNotMatch(html, /backend-must-not-expose-this/);
  assert.doesNotMatch(html, /<never render>/);
  assert.match(html, /&lt;never render&gt;/);

  const duplicatePath = structuredClone({
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "b".repeat(64),
    fetched_at: "2026-07-29T12:00:00Z",
    categories: [{
      key: "bad",
      label: "Bad",
      fields: [
        { path: "same", label: "One", type: "string", value: "", writable: true },
        { path: "same", label: "Two", type: "string", value: "", writable: true },
      ],
    }],
  });
  assert.throws(
    () => panel._sanitizeSettingsSnapshot(duplicatePath),
    (error) => error.name === "PanelSchemaError" && error.code === "invalid_format",
  );
"""
    )


def test_real_meshtastic_power_mask_obeys_server_frontend_integer_contract() -> None:
    """Keep the pinned uint64 protobuf projection loadable by the real panel.

    ``powermon_enables`` is a uint64 in Meshtastic's pinned protobuf. The
    server deliberately caps its UI metadata at JavaScript's exact-integer
    boundary. Exercise the actual protobuf descriptor, the server sanitizer,
    JSON serialization, and the panel validator together so those two bounds
    cannot silently diverge again.
    """
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import mesh_pb2

    from custom_components.meshnet.gateway_settings import (
        GatewaySettingsManager,
    )
    from custom_components.meshnet.meshtastic_settings import (
        MeshtasticSettingsState,
    )

    state = MeshtasticSettingsState()
    state.begin_refresh()
    owner_record = mesh_pb2.FromRadio()
    owner_record.node_info.num = 123
    owner_record.node_info.user.long_name = "Test gateway"
    owner_record.node_info.user.short_name = "TEST"
    state.capture_from_radio(owner_record, my_node_num=123)
    bluetooth_record = mesh_pb2.FromRadio()
    bluetooth_record.config.bluetooth.enabled = True
    bluetooth_record.config.bluetooth.fixed_pin = 123456
    bluetooth_record.config.bluetooth.SetInParent()
    state.capture_from_radio(bluetooth_record, my_node_num=123)
    record = mesh_pb2.FromRadio()
    record.config.power.powermon_enables = 0
    record.config.power.SetInParent()
    state.capture_from_radio(record, my_node_num=None)
    state.mark_complete()

    gateway = SimpleNamespace(
        config=SimpleNamespace(
            gateway_id="gateway-1",
            name="Test gateway",
            protocol="meshtastic",
            transport="bluetooth",
        ),
        status=SimpleNamespace(connected=True),
    )
    manager = GatewaySettingsManager(SimpleNamespace())
    snapshot = manager._sanitize_snapshot(
        gateway,
        state.public_snapshot(
            transport="bluetooth",
            write_supported=True,
        ),
    )
    power_mask = next(
        field
        for category in snapshot["categories"]
        for field in category["fields"]
        if field["path"] == "config.power.powermon_enables"
    )
    assert power_mask["value"] == 0
    assert power_mask["max"] == 2**53 - 1
    assert power_mask["read_only_reason"] == (
        "This setting needs dedicated validation before MeshNet can edit it."
    )
    assert snapshot["writable"] is True

    response = {
        "gateways": [
            {
                "gateway_id": "gateway-1",
                "name": "Test gateway",
                "protocol": "meshtastic",
                "transport": "bluetooth",
                "connected": True,
                "locally_managed": True,
            }
        ],
        "selected": snapshot,
    }
    encoded_response = json.dumps(response, separators=(",", ":"))
    _run_panel_script(
        f"""
  const response = panel._validateSettingsResponse({encoded_response});
  const powerMask = response.selected.categories
    .flatMap((category) => category.fields)
    .find((field) => field.path === "config.power.powermon_enables");
  assert.ok(powerMask);
  assert.equal(powerMask.value, 0);
  assert.equal(powerMask.max, Number.MAX_SAFE_INTEGER);
  assert.equal(powerMask.writable, false);
  assert.match(panel._settingsField(powerMask, 2, response.selected), /dedicated validation/);
  const ownerShortName = response.selected.categories
    .flatMap((category) => category.fields)
    .find((field) => field.path === "owner.short_name");
  assert.ok(ownerShortName);
  assert.equal(ownerShortName.writable, true);
  const ownerHtml = panel._settingsField(ownerShortName, 0, response.selected);
  assert.match(ownerHtml, />Editable</);
  assert.doesNotMatch(ownerHtml, / disabled/);
  const fixedPin = response.selected.categories
    .flatMap((category) => category.fields)
    .find((field) => field.path === "config.bluetooth.fixed_pin");
  assert.ok(fixedPin);
  assert.equal(fixedPin.writable, true);
  assert.match(panel._settingsField(fixedPin, 1, response.selected), /type="password"/);
"""
    )


def test_settings_render_distinguishes_editable_and_read_only_controls() -> None:
    """Render mixed and gateway-wide read-only schemas without false affordances."""
    _run_panel_script(
        r"""
  panel._bindViewControls = () => {};
  panel._bindSettingsControls = () => {};
  panel._restoreComposerFocus = () => {};
  panel.querySelectorAll = () => [];
  panel.querySelector = () => null;
  const snapshot = panel._sanitizeSettingsSnapshot({
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Test gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "c".repeat(64),
    fetched_at: "2026-07-30T12:00:00Z",
    categories: [{
      key: "device",
      label: "Device",
      fields: [
        {
          path: "owner.short_name",
          label: "Short name",
          type: "string",
          value: "TEST",
          max_length: 4,
          writable: true,
        },
        {
          path: "config.display.flip_screen",
          label: "Flip screen",
          type: "boolean",
          value: false,
          writable: true,
        },
        {
          path: "config.future.mode",
          label: "Future mode",
          type: "select",
          value: 99,
          options: [{ value: 0, label: "Known mode" }],
          writable: false,
          read_only_reason: "This future value is displayed safely but cannot be edited.",
        },
      ],
    }],
    warnings: [],
  });
  panel._settingsSnapshot = snapshot;
  panel._settingsGateways = [{
    gateway_id: "gateway-1",
    name: "Test gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
  }];
  panel._settingsGatewayId = "gateway-1";
  panel._renderSettings();
  assert.match(panel.innerHTML, /2 editable · 1 read-only/);
  assert.equal((panel.innerHTML.match(/>Editable<\/span>/g) || []).length, 2);
  assert.match(panel.innerHTML, /id="meshnet-setting-2"[^>]* disabled/);
  assert.match(panel.innerHTML, /Current: 99/);
  assert.match(panel.innerHTML, /future value is displayed safely/);
  assert.equal(panel.innerHTML.includes("No settings can be edited"), false);

  panel._settingsSnapshot = {
    ...snapshot,
    connected: false,
    writable: false,
    read_only_reason: "Connect this gateway before editing its settings.",
    revision: "d".repeat(64),
  };
  panel._renderSettings();
  assert.match(panel.innerHTML, /0 editable · 3 read-only/);
  assert.match(panel.innerHTML, /No settings can be edited safely right now/);
  assert.match(panel.innerHTML, /Connect this gateway before editing/);
  assert.equal((panel.innerHTML.match(/>Editable<\/span>/g) || []).length, 0);
  assert.equal((panel.innerHTML.match(/data-setting-index="[012]" disabled/g) || []).length, 3);
"""
    )


def test_read_only_settings_load_status_does_not_invite_editing() -> None:
    """A successful read-only load must explain the state instead of saying edit."""
    _run_panel_script(
        r"""
  panel._safeRender = () => true;
  panel._hass = {
    async callWS() {
      return {
        gateways: [{
          gateway_id: "gateway-1",
          name: "Test gateway",
          protocol: "meshtastic",
          transport: "bluetooth",
          connected: true,
        }],
        selected: {
          schema_version: 1,
          gateway_id: "gateway-1",
          name: "Test gateway",
          protocol: "meshtastic",
          transport: "bluetooth",
          connected: true,
          writable: false,
          read_only_reason: "No reviewed settings were received.",
          revision: "e".repeat(64),
          fetched_at: "2026-07-30T12:00:00Z",
          categories: [{
            key: "radio",
            label: "Radio",
            fields: [{
              path: "radio.region",
              label: "Region",
              type: "string",
              value: "US",
              writable: false,
            }],
          }],
          warnings: [],
        },
      };
    },
  };
  await panel._loadGatewaySettings("gateway-1");
  assert.deepEqual(panel._settingsStatus, {
    kind: "warn",
    text: "Gateway settings loaded read-only. Review the explanation above.",
  });
  assert.equal(panel._settingsStatus.text.includes("Edit values"), false);
"""
    )


def test_settings_schema_failure_does_not_retry_on_every_hass_assignment() -> None:
    """A bad response waits for explicit reload instead of hammering HA."""
    _run_panel_script(
        r"""
  panel._safeRender = () => true;
  panel._connected = true;
  panel._activeView = "settings";
  panel._loaded = true;
  let settingsRequests = 0;
  const validResponse = {
    gateways: [{
      gateway_id: "gateway-1",
      name: "Test gateway",
      protocol: "meshtastic",
      transport: "bluetooth",
      connected: true,
      writable: false,
    }],
    selected: {
      schema_version: 1,
      gateway_id: "gateway-1",
      name: "Test gateway",
      protocol: "meshtastic",
      transport: "bluetooth",
      connected: true,
      writable: false,
      read_only_reason: "No writable settings",
      revision: "a".repeat(64),
      fetched_at: "2026-07-30T12:00:00Z",
      categories: [],
      warnings: [],
    },
  };
  const hass = {
    async callWS(payload) {
      if (payload.type === "meshnet/panel_log") return { accepted: true };
      if (payload.type !== "meshnet/settings/get") throw new Error("unexpected request");
      settingsRequests += 1;
      return settingsRequests === 1
        ? { gateways: "invalid", selected: null }
        : validResponse;
    },
  };
  panel._hass = hass;

  await panel._loadGatewaySettings();
  assert.equal(settingsRequests, 1);
  assert.equal(panel._settingsBusy, null);
  assert.equal(panel._settingsSnapshot, null);
  assert.equal(panel._settingsStatus.kind, "bad");

  panel.hass = hass;
  panel.hass = hass;
  panel.hass = hass;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settingsRequests, 1);

  await panel._loadGatewaySettings();
  assert.equal(settingsRequests, 2);
  assert.equal(panel._settingsSnapshot.gateway_id, "gateway-1");
  assert.equal(panel._settingsStatus.kind, "warn");
  assert.match(panel._settingsStatus.text, /loaded read-only/);
"""
    )


def test_settings_render_has_one_loading_message_then_terminal_error() -> None:
    """The visible page must settle instead of appearing permanently busy."""
    _run_panel_script(
        r"""
  panel._connected = true;
  panel._activeView = "settings";
  panel.querySelectorAll = () => [];
  panel.querySelector = () => null;
  let resolveSettings;
  panel._hass = {
    callWS(payload) {
      if (payload.type === "meshnet/panel_log") return Promise.resolve({ accepted: true });
      return new Promise((resolve) => { resolveSettings = resolve; });
    },
  };

  const request = panel._loadGatewaySettings();
  await Promise.resolve();
  assert.equal((panel.innerHTML.match(/Loading gateway settings…/g) || []).length, 1);

  resolveSettings({ gateways: "invalid", selected: null });
  await request;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(panel.innerHTML.includes("Loading gateway settings…"), false);
  assert.equal(
    (panel.innerHTML.match(/Gateway settings could not be loaded\./g) || []).length,
    1,
  );
  assert.equal(panel._settingsBusy, null);
"""
    )


def test_hung_settings_get_times_out_once_and_waits_for_manual_retry() -> None:
    """A lost WebSocket response clears busy state without a retry storm."""
    _run_panel_script(
        r"""
  let nextTimer = 0;
  const timers = new Map();
  window.setTimeout = (callback, delay) => {
    nextTimer += 1;
    timers.set(nextTimer, { callback, delay });
    return nextTimer;
  };
  window.clearTimeout = (timer) => timers.delete(timer);
  panel._safeRender = () => true;
  panel._connected = true;
  panel._activeView = "settings";
  panel._loaded = true;
  let settingsRequests = 0;
  const hass = {
    callWS(payload) {
      if (payload.type === "meshnet/panel_log") return Promise.resolve({ accepted: true });
      settingsRequests += 1;
      return new Promise(() => {});
    },
  };
  panel._hass = hass;

  const request = panel._loadGatewaySettings();
  await Promise.resolve();
  const timeout = [...timers.values()].find((timer) => timer.delay === 35000);
  assert.ok(timeout);
  timeout.callback();
  await request;
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(settingsRequests, 1);
  assert.equal(panel._settingsBusy, null);
  assert.equal(panel._settingsStatus.kind, "bad");
  assert.equal(
    panel._failureTelemetry.some(
      (event) => event.operation === "settings_get" && event.error_code === "timeout",
    ),
    true,
  );

  panel.hass = hass;
  panel.hass = hass;
  await Promise.resolve();
  assert.equal(settingsRequests, 1);
"""
    )


def test_settings_get_preview_and_confirmed_apply_use_exact_ws_contract() -> None:
    """A typed draft must be previewed and critical changes confirmed once."""
    _run_panel_script(
        r"""
  panel._safeRender = () => true;
  panel.querySelectorAll = () => [];
  const requests = [];
  const snapshot = {
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Test gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    fetched_at: "2026-07-29T12:00:00Z",
    categories: [{
      key: "radio",
      label: "Radio",
      fields: [
        {
          path: "radio.power",
          label: "Transmit power",
          type: "integer",
          value: 17,
          min: 0,
          max: 30,
          writable: true,
          critical: false,
          requires_reconnect: false,
        },
        {
          path: "radio.enabled",
          label: "Enabled",
          type: "boolean",
          value: true,
          writable: true,
          critical: false,
          requires_reconnect: false,
        },
        {
          path: "security.key",
          label: "Channel key",
          type: "secret",
          value: "",
          configured: true,
          writable: true,
          critical: true,
          requires_reconnect: true,
        },
      ],
    }],
    warnings: [],
  };
  panel._hass = {
    async callWS(payload) {
      requests.push(structuredClone(payload));
      if (payload.type === "meshnet/settings/get") {
        return {
          gateways: [{
            gateway_id: "gateway-1",
            name: "Test gateway",
            protocol: "meshtastic",
            transport: "bluetooth",
            connected: true,
          }],
          selected: snapshot,
        };
      }
      if (payload.type === "meshnet/settings/preview") {
        return {
          preview_id: "ppppppppppppppppppppppppppppppppppppppppppp",
          gateway_id: "gateway-1",
          revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          expires_at: "2026-07-29T12:05:00Z",
          changes: [
            {
              path: "radio.enabled",
              label: "Enabled",
              before: true,
              after: false,
              secret: false,
              critical: false,
              requires_reconnect: false,
            },
            {
              path: "radio.power",
              label: "Transmit power",
              before: 17,
              after: 20,
              secret: false,
              critical: false,
              requires_reconnect: false,
            },
            {
              path: "security.key",
              label: "Channel key",
              before: "must-be-scrubbed",
              after: "must-also-be-scrubbed",
              secret: true,
              operation: "replace",
              critical: true,
              requires_reconnect: true,
            },
          ],
          requires_critical_confirmation: true,
          warnings: ["The Bluetooth session will reconnect."],
        };
      }
      if (payload.type === "meshnet/settings/apply") {
        return {
          status: "verified",
          gateway_id: "gateway-1",
          verified: ["radio.power", "radio.enabled", "security.key"],
          unverified: [],
          reconnect_required: true,
          connection_recovery_required: false,
          warnings: [],
          snapshot: {
            ...snapshot,
            revision: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          },
        };
      }
      throw new Error("unexpected request");
    },
  };

  await panel._loadGatewaySettings("gateway-1");
  assert.deepEqual(requests[0], {
    type: "meshnet/settings/get",
    gateway_id: "gateway-1",
  });
  assert.equal(panel._settingsSnapshot.revision, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
  panel._settingsDraft["radio.power"] = "20";
  panel._settingsDraft["radio.enabled"] = false;
  panel._settingsDraft["security.key"] = "temporary secret";

  await panel._previewGatewaySettings({ preventDefault() {} });
  assert.deepEqual(requests[1], {
    type: "meshnet/settings/preview",
    gateway_id: "gateway-1",
    revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    changes: {
      "radio.power": 20,
      "radio.enabled": false,
      "security.key": { operation: "replace", value: "temporary secret" },
    },
  });
  assert.equal(Object.hasOwn(panel._settingsDraft, "security.key"), false);
  assert.equal(panel._settingsPreview.changes[2].before, null);
  assert.equal(panel._settingsPreview.changes[2].after, null);
  assert.equal(JSON.stringify(panel._settingsPreview).includes("must-be-scrubbed"), false);
  assert.equal(panel._settingsPreview.requires_critical_confirmation, true);

  await panel._applyGatewaySettings();
  assert.equal(requests.length, 2);
  assert.match(panel._settingsStatus.text, /Confirm the critical-setting warning/);

  panel._settingsCriticalConfirmed = true;
  await panel._applyGatewaySettings();
  assert.deepEqual(requests[2], {
    type: "meshnet/settings/apply",
    gateway_id: "gateway-1",
    revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    preview_id: "ppppppppppppppppppppppppppppppppppppppppppp",
    confirm_critical: true,
  });
  assert.equal(panel._settingsSnapshot.revision, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
  assert.equal(panel._settingsPreview, null);
  assert.deepEqual(Object.keys(panel._settingsDraft), []);
  assert.equal(panel._settingsCriticalConfirmed, false);
  assert.deepEqual(panel._settingsStatus, {
    kind: "good",
    text: "Settings applied and verified. The gateway is restarting; wait for it to reconnect.",
  });
  const validationPreview = {
    changes: [{ path: "radio.power" }],
  };
  assert.deepEqual(panel._validateSettingsApply({
    status: "applied_unverified",
    gateway_id: "gateway-1",
    verified: [],
    unverified: ["radio.power"],
    reconnect_required: false,
    connection_recovery_required: true,
    warnings: ["Readback was unavailable."],
  }, validationPreview, snapshot), {
    verified_count: 0,
    unverified_count: 1,
    reconnect_required: false,
    connection_recovery_required: true,
    snapshot: null,
    warnings: ["Readback was unavailable."],
  });

  panel._settingsSnapshot = snapshot;
  panel._settingsPreview = {
    preview_id: "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr",
    expires_at: "2026-07-29T12:10:00Z",
    changes: [{ path: "security.key" }],
    requires_critical_confirmation: false,
    warnings: [],
  };
  panel._hass = {
    async callWS() {
      return {
        status: "applied_unverified",
        gateway_id: "gateway-1",
        verified: [],
        unverified: ["security.key"],
        reconnect_required: true,
        connection_recovery_required: true,
        warnings: [],
      };
    },
  };
  await panel._applyGatewaySettings();
  assert.deepEqual(panel._settingsStatus, {
    kind: "warn",
    text: "Settings applied; 1 value(s) could not be verified. Verify or recover the gateway connection before another settings change. Reload live values before making another change.",
  });

  panel._settingsPreview = {
    preview_id: "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
    expires_at: "2026-07-29T12:10:00Z",
    changes: [],
    requires_critical_confirmation: false,
    warnings: [],
  };
  panel._hass = { async callWS() { throw new Error("uncertain apply outcome"); } };
  await panel._applyGatewaySettings();
  assert.equal(panel._settingsPreview, null);
  assert.match(panel._settingsStatus.text, /outcome could not be confirmed/);
  assert.match(panel._settingsStatus.text, /Do not repeat the write/);
"""
    )


def test_settings_draft_invalidates_preview_and_polling_preserves_focus() -> None:
    """Editing settings must invalidate stale previews without poll rerenders."""
    _run_panel_script(
        r"""
  panel._settingsSnapshot = panel._sanitizeSettingsSnapshot({
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "c".repeat(64),
    fetched_at: "2026-07-29T12:00:00Z",
    categories: [{
      key: "radio",
      label: "Radio",
      fields: [{
        path: "radio.power",
        label: "Power",
        type: "integer",
        value: 17,
        min: 0,
        max: 30,
        writable: true,
      }],
    }],
  });
  panel._settingsPreview = {
    preview_id: "stale-preview",
    expires_at: "later",
    changes: [],
    requires_critical_confirmation: false,
    warnings: [],
  };
  panel.querySelector = () => null;
  const input = {
    id: "meshnet-setting-0",
    type: "number",
    value: "22",
    selectionStart: 1,
    selectionEnd: 2,
    getAttribute(name) { return name === "data-setting-index" ? "0" : null; },
    hasAttribute(name) { return name === "data-setting-index"; },
  };
  panel._updateSettingsDraft(input);
  assert.equal(panel._settingsDraft["radio.power"], "22");
  assert.equal(panel._settingsPreview, null);
  assert.throws(
    () => panel._coerceSettingValue(panel._settingsFields()[0], "31"),
    (error) => error.name === "ValidationError" && error.code === "invalid_format",
  );

  panel._activeView = "settings";
  panel.getRootNode = () => ({ activeElement: input });
  panel.contains = (element) => element === input;
  assert.deepEqual(panel._settingsFocusState(), {
    id: "meshnet-setting-0",
    start: 1,
    end: 2,
  });
  assert.equal(panel._panelInteractionActive(), true);
  let renders = 0;
  panel._render = () => { renders += 1; };
  assert.equal(panel._renderPollSnapshot(), false);
  assert.equal(panel._pollRenderPending, true);
  assert.equal(renders, 0);
"""
    )


def test_settings_preview_filters_coerced_numeric_no_op() -> None:
    """A restored numeric input must not make a valid preview look incomplete."""
    _run_panel_script(
        r"""
  const snapshot = panel._sanitizeSettingsSnapshot({
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "d".repeat(64),
    fetched_at: "2026-07-29T12:00:00Z",
    categories: [{
      key: "radio",
      label: "Radio",
      fields: [
        {
          path: "radio.power",
          label: "Power",
          type: "integer",
          value: 17,
          min: 0,
          max: 30,
          writable: true,
          critical: false,
          requires_reconnect: false,
        },
        {
          path: "radio.enabled",
          label: "Enabled",
          type: "boolean",
          value: true,
          writable: true,
          critical: false,
          requires_reconnect: false,
        },
      ],
    }],
    warnings: [],
  });
  panel._settingsSnapshot = snapshot;
  panel._settingsDraft["radio.power"] = "17";
  panel._settingsDraft["radio.enabled"] = false;

  const changes = panel._settingsChanges();
  assert.deepEqual(changes, { "radio.enabled": false });
  assert.equal(Object.hasOwn(panel._settingsDraft, "radio.power"), false);
  assert.deepEqual(panel._validateSettingsPreview({
    preview_id: "p".repeat(43),
    gateway_id: "gateway-1",
    revision: "d".repeat(64),
    expires_at: "2026-07-29T12:05:00Z",
    changes: [{
      path: "radio.enabled",
      label: "Enabled",
      before: true,
      after: false,
      secret: false,
      critical: false,
      requires_reconnect: false,
    }],
    requires_critical_confirmation: false,
    warnings: [],
  }, snapshot, changes).changes.map((change) => change.path), ["radio.enabled"]);
"""
    )


def test_failed_settings_preview_forgets_submitted_secret() -> None:
    """A failed preview must not keep credentials in the panel draft."""
    _run_panel_script(
        r"""
  panel._settingsSnapshot = panel._sanitizeSettingsSnapshot({
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Gateway",
    protocol: "meshcore",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "a".repeat(64),
    fetched_at: "2026-07-29T12:00:00Z",
    categories: [{
      key: "security",
      label: "Security",
      fields: [{
        path: "security.pin",
        label: "Bluetooth PIN",
        type: "secret",
        configured: true,
        allow_clear: true,
        max_length: 6,
        writable: true,
        critical: true,
        requires_reconnect: true,
      }],
    }],
    warnings: [],
  });
  panel._settingsDraft["security.pin"] = "654321";
  panel._hass = { async callWS() { throw new Error("preview failed"); } };
  panel._safeRender = () => {};

  await panel._previewGatewaySettings({ preventDefault() {} });

  assert.equal(Object.hasOwn(panel._settingsDraft, "security.pin"), false);
  assert.equal(panel._settingsPreview, null);
  assert.match(panel._settingsStatus.text, /could not be prepared/);
"""
    )


def test_settings_requests_cannot_cross_busy_gateway_change_or_disconnect() -> None:
    """Late responses must not repopulate an abandoned settings editor."""
    _run_panel_script(
        r"""
  const snapshot = panel._sanitizeSettingsSnapshot({
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Gateway",
    protocol: "meshcore",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "a".repeat(64),
    fetched_at: "2026-07-29T12:00:00Z",
    categories: [{
      key: "identity",
      label: "Identity",
      fields: [{
        path: "identity.name",
        label: "Name",
        type: "string",
        value: "Gateway",
        max_length: 32,
        writable: true,
      }],
    }],
    warnings: [],
  });
  panel._safeRender = () => {};
  panel._settingsSnapshot = snapshot;
  let calls = 0;
  panel._hass = { async callWS() { calls += 1; return {}; } };
  panel._settingsBusy = "apply";
  await panel._loadGatewaySettings("other-gateway");
  assert.equal(calls, 0);
  assert.equal(panel._settingsSnapshot, snapshot);
  assert.equal(panel._settingsBusy, "apply");

  let resolvePreview;
  panel._settingsBusy = null;
  panel._settingsDraft["identity.name"] = "Changed";
  panel._hass = {
    callWS() {
      return new Promise((resolve) => { resolvePreview = resolve; });
    },
  };
  const previewTask = panel._previewGatewaySettings({ preventDefault() {} });
  await Promise.resolve();
  assert.equal(panel._settingsBusy, "preview");
  panel.disconnectedCallback();
  resolvePreview({
    preview_id: "p".repeat(43),
    expires_at: "2026-07-29T12:05:00Z",
    changes: [{
      path: "identity.name",
      label: "Name",
      before: "Gateway",
      after: "Changed",
      secret: false,
      critical: false,
      requires_reconnect: false,
    }],
    requires_critical_confirmation: false,
    warnings: [],
  });
  await previewTask;
  assert.equal(panel._settingsSnapshot, null);
  assert.equal(panel._settingsPreview, null);
  assert.equal(panel._settingsStatus, null);
  assert.equal(panel._settingsBusy, null);

  let resolveApply;
  panel._settingsSnapshot = snapshot;
  panel._settingsPreview = {
    preview_id: "q".repeat(43),
    expires_at: "2026-07-29T12:05:00Z",
    changes: [],
    requires_critical_confirmation: false,
    warnings: [],
  };
  panel._hass = {
    callWS() {
      return new Promise((resolve) => { resolveApply = resolve; });
    },
  };
  const applyTask = panel._applyGatewaySettings();
  await Promise.resolve();
  assert.equal(panel._settingsBusy, "apply");
  panel.disconnectedCallback();
  resolveApply({
    status: "verified",
    verified: ["identity.name"],
    unverified: [],
    reconnect_required: false,
    warnings: [],
  });
  await applyTask;
  assert.equal(panel._settingsSnapshot, null);
  assert.equal(panel._settingsPreview, null);
  assert.equal(panel._settingsStatus, null);
  assert.equal(panel._settingsBusy, null);
"""
    )


def test_settings_success_responses_fail_closed_on_contract_mismatch() -> None:
    """Malformed success payloads must never clear or legitimize a preview."""
    _run_panel_script(
        r"""
  const rawSnapshot = {
    schema_version: 1,
    gateway_id: "gateway-1",
    name: "Gateway",
    protocol: "meshcore",
    transport: "bluetooth",
    connected: true,
    writable: true,
    revision: "a".repeat(64),
    fetched_at: "2026-07-29T12:00:00Z",
    categories: [{
      key: "identity",
      label: "Identity",
      fields: [{
        path: "identity.name",
        label: "Name",
        type: "string",
        value: "Gateway",
        max_length: 32,
        writable: true,
        critical: false,
        requires_reconnect: false,
      }],
    }],
    warnings: [],
  };
  const snapshot = panel._sanitizeSettingsSnapshot(rawSnapshot);
  const requested = { "identity.name": "Changed" };
  const validPreviewResponse = {
    preview_id: "p".repeat(43),
    gateway_id: "gateway-1",
    revision: "a".repeat(64),
    expires_at: "2026-07-29T12:05:00Z",
    changes: [{
      path: "identity.name",
      label: "Name",
      before: "Gateway",
      after: "Changed",
      secret: false,
      critical: false,
      requires_reconnect: false,
    }],
    requires_critical_confirmation: false,
    warnings: [],
  };
  const preview = panel._validateSettingsPreview(
    validPreviewResponse,
    snapshot,
    requested,
  );
  const validApply = {
    status: "verified",
    gateway_id: "gateway-1",
    verified: ["identity.name"],
    unverified: [],
    reconnect_required: false,
    connection_recovery_required: false,
    warnings: [],
  };
  assert.equal(panel._validateSettingsApply(validApply, preview, snapshot).verified_count, 1);

  for (const invalid of [
    { ...validApply, status: "anything" },
    { ...validApply, verified: true },
    { ...validApply, verified: ["identity.name", "identity.name"] },
    { ...validApply, verified: [], unverified: [] },
    { ...validApply, gateway_id: "other-gateway" },
  ]) {
    assert.throws(
      () => panel._validateSettingsApply(invalid, preview, snapshot),
      (error) => error.name === "PanelSchemaError" && error.code === "invalid_format",
    );
  }

  for (const invalid of [
    { ...validPreviewResponse, gateway_id: "other-gateway" },
    { ...validPreviewResponse, revision: "b".repeat(64) },
    { ...validPreviewResponse, changes: [] },
    {
      ...validPreviewResponse,
      changes: [
        ...validPreviewResponse.changes,
        ...validPreviewResponse.changes,
      ],
    },
  ]) {
    assert.throws(
      () => panel._validateSettingsPreview(invalid, snapshot, requested),
      (error) => error.name === "PanelSchemaError" && error.code === "invalid_format",
    );
  }

  assert.throws(
    () => panel._sanitizeSettingsSnapshot({ ...rawSnapshot, revision: 1 }),
    (error) => error.name === "PanelSchemaError" && error.code === "invalid_format",
  );
  assert.throws(
    () => panel._sanitizeSettingsSnapshot({
      ...rawSnapshot,
      categories: Array.from({ length: 49 }, (_value, index) => ({
        key: `category.${index}`,
        label: "Category",
        fields: [],
      })),
    }),
    (error) => error.name === "PanelSchemaError" && error.code === "invalid_format",
  );
"""
    )


def test_panel_polling_stops_when_element_disconnects() -> None:
    """Navigating away must not leave detached five-second pollers running."""
    _run_panel_script(
        r"""
  let nextTimer = 0;
  const timers = new Map();
  window.setTimeout = (callback) => {
    nextTimer += 1;
    timers.set(nextTimer, callback);
    return nextTimer;
  };
  window.clearTimeout = (timer) => timers.delete(timer);
  let refreshes = 0;
  panel._refreshSnapshot = async () => {
    refreshes += 1;
    panel._snapshot = { nodes: {}, gateways: {}, recent_messages: [] };
    return panel._snapshot;
  };
  panel._render = () => {};
  panel.hass = {};
  assert.equal(refreshes, 0);

  panel.connectedCallback();
  await Promise.resolve();
  assert.equal(refreshes, 1);
  assert.equal(timers.size, 1);

  panel.disconnectedCallback();
  assert.equal(timers.size, 0);
  assert.equal(panel._pollTimer, null);
  assert.equal(panel._loaded, false);

  panel.connectedCallback();
  await Promise.resolve();
  assert.equal(refreshes, 2);
  assert.equal(timers.size, 1);
  const callback = [...timers.values()][0];
  panel.disconnectedCallback();
  callback();
  await Promise.resolve();
  assert.equal(refreshes, 2);
  assert.equal(timers.size, 0);
"""
    )


def test_panel_exposes_explicit_delivery_sorting_and_native_map_ui() -> None:
    """Make the requested controls visible without creating HA dashboards."""
    source = _source()

    assert 'select id="meshnet-delivery"' in source
    assert '<option value="broadcast"' in source
    assert '<option value="direct"' in source
    assert "Direct recipient" in source
    assert 'id="meshnet-recipient"${directDelivery && recipientCount ? " required" : " disabled"}' in source
    assert "No cached nodes available yet" in source
    assert 'select id="meshnet-node-sort"' in source
    assert "Favorites + last seen" in source
    assert '<a class="map-link" href="/map">Map · ${locatedNodeCount} cached locations</a>' in source
    assert 'this._error || "Snapshot current"' in source
    assert 'this._stat("Recently seen"' in source
    assert 'this._stat("Cached health"' in source
    assert '${node.online ? "recent" : "stale"}' in source
    assert "sortedNodes.slice(0, 24)" in source
    assert "node.favorite === true" in source
    assert "snapshot.panel_metadata.favorite_label_configured === true" in source
    assert "MeshNet Favorite" in source
    assert "this._humanLastSeen(node.last_heard)" in source


def test_send_composer_builds_safe_direct_and_broadcast_requests() -> None:
    """Only exact snapshot node keys may become direct-message destinations."""
    _run_panel_script(
        r"""
  panel._render = () => {};
  panel._snapshot = {
    nodes: {
      known: { node_key: "meshtastic:!12345678", long_name: "Known node" },
    },
  };
  const payloads = [];
  panel._hass = {
    async callWS(payload) {
      payloads.push(payload);
      return { message_id: `message-${payloads.length}` };
    },
  };
  panel._refreshSnapshot = async () => ({ recent_messages: [] });
  const event = { preventDefault() {} };

  panel._draft.delivery = "broadcast";
  panel._draft.recipient = "meshtastic:!12345678";
  panel._draft.message = "broadcast test";
  await panel._sendMessage(event);
  assert.equal(payloads[0].message_type, "broadcast");
  assert.equal(Object.hasOwn(payloads[0], "target_node"), false);

  panel._draft.delivery = "direct";
  panel._draft.recipient = "meshtastic:!12345678";
  panel._draft.message = "direct test";
  await panel._sendMessage(event);
  assert.equal(payloads[1].message_type, "direct");
  assert.equal(payloads[1].target_node, "meshtastic:!12345678");

  panel._draft.delivery = "direct";
  panel._draft.recipient = "not-in-the-snapshot";
  panel._draft.message = "must not leave the browser";
  await panel._sendMessage(event);
  assert.equal(payloads.length, 2);
  assert.deepEqual(panel._sendStatus, {
    kind: "bad",
    text: "Choose an available direct recipient.",
  });
"""
    )


def test_recipient_choices_and_node_message_shortcut_use_canonical_keys() -> None:
    """Recipient choices and row shortcuts must retain exact canonical IDs."""
    _run_panel_script(
        r"""
  const nodes = [
    { node_key: "meshcore:key&one", long_name: "B <unsafe>" },
    { protocol: "meshtastic", node_key: "meshtastic:!12345678", short_name: "Alpha" },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!00000001",
      node_id: "!00000001",
    },
  ];
  panel._snapshot = { nodes: { first: nodes[0], second: nodes[1], third: nodes[2] } };
  const choices = panel._recipientChoices(nodes);
  assert.deepEqual(choices.map((choice) => choice.value), [
    "meshtastic:!12345678",
    "meshcore:key&one",
    "meshtastic:!00000001",
  ]);
  assert.match(panel._recipientOptions(nodes), /B &lt;unsafe&gt;/);
  assert.match(panel._recipientOptions(nodes), /meshcore:key&amp;one/);
  assert.equal((panel._recipientOptions(nodes).match(/meshtastic:!12345678/g) || []).length, 1);
  assert.match(panel._recipientOptions([]), /No cached nodes available yet/);
  assert.equal(panel._chooseDirectRecipient("meshtastic:!12345678"), true);
  assert.equal(panel._draft.delivery, "direct");
  assert.equal(panel._draft.recipient, "meshtastic:!12345678");
  assert.equal(panel._chooseDirectRecipient("!12345678"), false);
  assert.equal(panel._draft.recipient, "meshtastic:!12345678");
"""
    )
    source = _source()
    assert 'data-message-node="${this._escape(node.node_key)}"' in source
    assert "unsafeRecipientKeys.has(String(node.node_key))" in source
    assert "Unsafe node identity; direct messaging is disabled" in source
    assert 'button.getAttribute("data-message-node")' in source


def test_unresolved_meshtastic_id_collision_is_not_a_send_target() -> None:
    """The browser must agree with the server's fail-closed routing rule."""
    _run_panel_script(
        r"""
  const nodes = [
    {
      protocol: "meshtastic",
      node_key: "mac:111111111111",
      node_id: "!12345678",
      mac: "111111111111",
      long_name: "Collision one",
    },
    {
      protocol: "meshtastic",
      node_key: "mac:222222222222",
      node_id: "305419896",
      mac: "222222222222",
      long_name: "Collision two",
    },
  ];
  panel._snapshot = { nodes: { first: nodes[0], second: nodes[1] } };

  const choices = panel._recipientChoices(nodes);
  assert.equal(choices.length, 2);
  assert.equal(choices.every((choice) => choice.ambiguous === true), true);
  const options = panel._recipientOptions(nodes);
  assert.equal((options.match(/<option[^>]* disabled>/g) || []).length, 2);
  assert.match(options, /No unambiguous direct recipients/);
  assert.match(options, /ambiguous ID — sending disabled/);
  assert.equal(panel._isKnownRecipient("mac:111111111111"), false);
  assert.equal(panel._chooseDirectRecipient("mac:222222222222"), false);
"""
    )


def test_composite_proof_key_collision_is_not_a_send_target() -> None:
    """Opaque proof-aware keys still participate in exact-ID ambiguity checks."""
    _run_panel_script(
        r"""
  const nodes = [
    {
      protocol: "meshtastic",
      node_key: `meshtastic-proof:${"1".repeat(64)}`,
      node_id: "!12345678",
      mac: "111111111111",
      public_key: "1".repeat(64),
      identity_valid: true,
      long_name: "Collision one",
    },
    {
      protocol: "meshtastic",
      node_key: `meshtastic-proof:${"2".repeat(64)}`,
      node_id: "305419896",
      mac: "111111111111",
      public_key: "2".repeat(64),
      identity_valid: true,
      long_name: "Collision two",
    },
  ];
  panel._snapshot = { nodes: { first: nodes[0], second: nodes[1] } };

  assert.equal(panel._meshtasticNodeId(nodes[0]), "!12345678");
  assert.equal(panel._meshtasticNodeId({
    ...nodes[0],
    node_key: "meshtastic-proof:not-a-proof",
  }), "");
  const choices = panel._recipientChoices(nodes);
  assert.equal(choices.length, 2);
  assert.equal(choices.every((choice) => choice.ambiguous === true), true);
  assert.equal((panel._recipientOptions(nodes).match(/<option[^>]* disabled>/g) || []).length, 2);
  assert.equal(panel._isKnownRecipient(nodes[0].node_key), false);
"""
    )


def test_server_rejected_identity_is_disabled_in_recipient_ui() -> None:
    """The panel must honor the server's proof validation result."""
    _run_panel_script(
        r"""
  const invalid = {
    protocol: "meshtastic",
    node_key: `meshtastic-proof:${"1".repeat(64)}`,
    node_id: "!12345678",
    mac: "111111111111",
    public_key: "2".repeat(64),
    identity_valid: false,
    long_name: "Invalid proof",
  };
  panel._snapshot = { nodes: { invalid } };

  const choices = panel._recipientChoices([invalid]);
  assert.equal(choices.length, 1);
  assert.equal(choices[0].invalidIdentity, true);
  const options = panel._recipientOptions([invalid]);
  assert.match(options, /invalid identity — sending disabled/);
  assert.match(options, /<option[^>]* disabled>/);
  assert.equal(panel._isKnownRecipient(invalid.node_key), false);
"""
    )


def test_recipient_name_collisions_receive_exact_disambiguators() -> None:
    """Same-named nodes must never become indistinguishable send targets."""
    _run_panel_script(
        r"""
  const nodes = [
    {
      protocol: "meshcore",
      node_key: "meshcore:contact-one",
      node_id: "contact-one",
      long_name: "Shared Relay",
      short_name: "SR",
    },
    {
      protocol: "meshcore",
      node_key: "meshcore:contact-two",
      node_id: "contact-two",
      long_name: " shared relay ",
      short_name: "sr",
    },
    {
      protocol: "meshcore",
      node_key: "meshcore:unique",
      node_id: "unique",
      long_name: "Unique Relay",
      short_name: "UR",
    },
    {
      protocol: "meshcore",
      node_key: "meshcore:alias-one",
      node_id: "same-contact",
      long_name: "Same Contact",
    },
    {
      protocol: "meshcore",
      node_key: "meshcore:alias-two",
      node_id: "same-contact",
      long_name: "same contact",
    },
  ];
  const byValue = Object.fromEntries(
    panel._recipientChoices(nodes).map((choice) => [choice.value, choice.label]),
  );
  assert.equal(byValue["meshcore:contact-one"], "Shared Relay · SR · contact-one");
  assert.equal(byValue["meshcore:contact-two"], "shared relay · sr · contact-two");
  assert.equal(byValue["meshcore:unique"], "Unique Relay · UR");
  assert.equal(byValue["meshcore:alias-one"], "Same Contact · meshcore:alias-one");
  assert.equal(byValue["meshcore:alias-two"], "same contact · meshcore:alias-two");
  const options = panel._recipientOptions(nodes);
  for (const node of nodes) {
    assert.match(options, new RegExp(`value="${node.node_key}"`));
  }
"""
    )


def test_node_labels_show_long_and_short_names_without_raw_key_duplication() -> None:
    """Use phone-like human labels while retaining exact hidden send targets."""
    _run_panel_script(
        r"""
  const named = {
    protocol: "meshtastic",
    node_key: "meshtastic:!12345678",
    node_id: "!12345678",
    long_name: " Backyard   Repeater ",
    short_name: " BY ",
  };
  assert.equal(panel._nodeName(named), "Backyard Repeater · BY");
  assert.equal(panel._nodeCompactName(named), "BY");
  assert.equal(
    panel._nodeName({ ...named, short_name: "backyard repeater" }),
    "Backyard Repeater",
  );
  assert.equal(
    panel._nodeName({ ...named, long_name: null, user_name: null, short_name: "BY" }),
    "BY",
  );
  assert.equal(
    panel._nodeName({
      protocol: "meshtastic",
      node_key: "meshtastic:!12345678",
      node_id: "!12345678",
    }),
    "Unnamed node · !12345678",
  );
  assert.equal(
    panel._nodeName({
      protocol: "meshtastic",
      node_key: "meshtastic:305419896",
      node_id: "305419896",
    }),
    "Unnamed node · !12345678",
  );
  assert.equal(
    panel._nodeCompactName({
      protocol: "meshtastic",
      node_key: "meshtastic:!12345678",
      node_id: "!12345678",
    }),
    "!12345678",
  );
  const options = panel._recipientOptions([named]);
  assert.match(options, />Backyard Repeater · BY · !12345678<\/option>/);
  assert.equal((options.match(/meshtastic:!12345678/g) || []).length, 1);
"""
    )


def test_exact_meshtastic_id_name_hints_are_display_only_and_fail_closed() -> None:
    """Share unambiguous labels across exact IDs without merging node state."""
    _run_panel_script(
        r"""
  const named = {
    protocol: "meshtastic",
    node_key: "mac:aabbccddeeff",
    node_id: "!12345678",
    long_name: "Hill Repeater",
    short_name: "HR",
    favorite: true,
  };
  const decimal = {
    protocol: "meshtastic",
    node_key: "meshtastic:305419896",
    node_id: "305419896",
    last_heard: "2026-01-02T00:00:00Z",
  };
  const hexadecimal = {
    protocol: "meshtastic",
    node_key: "meshtastic:0x12345678",
    node_id: "0x12345678",
  };
  const source = [named, decimal, hexadecimal];
  const before = JSON.stringify(source);
  const display = panel._nodesWithExactMeshtasticNameHints(source);

  assert.equal(JSON.stringify(source), before);
  assert.equal(display[0], named);
  assert.notEqual(display[1], decimal);
  assert.notEqual(display[2], hexadecimal);
  assert.equal(panel._nodeName(display[1]), "Hill Repeater · HR");
  assert.equal(display[1]._name_hint_exact_node_id, true);
  assert.equal(display[1].node_key, decimal.node_key);
  assert.equal(display[1].favorite, decimal.favorite);
  assert.deepEqual(
    panel._recipientChoices(display).map((choice) => choice.value).sort(),
    source.map((node) => node.node_key).sort(),
  );
  assert.match(
    panel._recipientNodeName(display[1]),
    /^Hill Repeater · HR · !12345678 · cached-name match$/,
  );

  const conflict = panel._nodesWithExactMeshtasticNameHints([
    {
      protocol: "meshtastic",
      node_key: "mac:aaaaaaaaaaaa",
      node_id: "!22222222",
      long_name: "Alpha",
      short_name: "SAME",
    },
    {
      protocol: "meshtastic",
      node_key: "mac:bbbbbbbbbbbb",
      node_id: "0x22222222",
      long_name: "Beta",
      short_name: " same ",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:572662306",
      node_id: "572662306",
    },
  ]);
  assert.equal(conflict[2].long_name, undefined);
  assert.equal(conflict[2].short_name, undefined);
  assert.equal(panel._nodeName(conflict[2]), "Unnamed node · !22222222");

  const splitTuple = [
    {
      protocol: "meshtastic",
      node_key: "mac:111111111111",
      node_id: "!55555555",
      long_name: "Never Combined",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!55555555",
      node_id: "!55555555",
      short_name: "NC",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:1431655765",
      node_id: "1431655765",
    },
  ];
  assert.deepEqual(panel._nodesWithExactMeshtasticNameHints(splitTuple), splitTuple);

  const conflictingProofs = [
    {
      protocol: "meshtastic",
      node_key: "mac:111111111111",
      node_id: "!66666666",
      long_name: "Proof One",
      short_name: "P1",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!66666666",
      node_id: "!66666666",
      mac: "22:22:22:22:22:22",
    },
  ];
  assert.deepEqual(
    panel._nodesWithExactMeshtasticNameHints(conflictingProofs),
    conflictingProofs,
  );

  const conflictingPublicKeys = [
    {
      protocol: "meshtastic",
      node_key: "pub:public-key-one",
      node_id: "!77777777",
      public_key: "public-key-one",
      long_name: "Public Proof",
      short_name: "PP",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!77777777",
      node_id: "!77777777",
      public_key: "public-key-two",
    },
  ];
  assert.deepEqual(
    panel._nodesWithExactMeshtasticNameHints(conflictingPublicKeys),
    conflictingPublicKeys,
  );

  const selfConflictingMac = [
    {
      protocol: "meshtastic",
      node_key: "mac:111111111111",
      node_id: "!88888888",
      mac: "22:22:22:22:22:22",
      long_name: "Bad MAC donor",
      short_name: "BM",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!88888888",
      node_id: "!88888888",
    },
  ];
  assert.deepEqual(
    panel._nodesWithExactMeshtasticNameHints(selfConflictingMac),
    selfConflictingMac,
  );

  const selfConflictingPublic = [
    {
      protocol: "meshtastic",
      node_key: "pub:public-key-one",
      node_id: "!99999999",
      public_key: "public-key-two",
      long_name: "Bad public donor",
      short_name: "BP",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!99999999",
      node_id: "!99999999",
    },
  ];
  assert.deepEqual(
    panel._nodesWithExactMeshtasticNameHints(selfConflictingPublic),
    selfConflictingPublic,
  );

  const malformedMac = [
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!aaaaaaaa",
      node_id: "!aaaaaaaa",
      mac: "aa:bb-cc:dd-ee:ff",
      long_name: "Malformed MAC donor",
      short_name: "MM",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:2863311530",
      node_id: "2863311530",
    },
  ];
  assert.deepEqual(panel._nodesWithExactMeshtasticNameHints(malformedMac), malformedMac);

  const unsafe = [
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!33333333",
      node_id: "!44444444",
      long_name: "Conflicting ID",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!33333333",
      node_id: "!33333333",
    },
    {
      protocol: "meshcore",
      node_key: "meshcore:!33333333",
      node_id: "!33333333",
      long_name: "Wrong protocol",
    },
    {
      protocol: "meshtastic",
      node_key: "mac:cccccccccccc",
      mac: "cccccccccccc",
      long_name: "MAC alone",
    },
    {
      protocol: "meshtastic",
      node_key: "mac:cccccccccccc-copy",
      mac: "cccccccccccc",
    },
  ];
  assert.deepEqual(panel._nodesWithExactMeshtasticNameHints(unsafe), unsafe);
  assert.equal(panel._meshtasticNodeId(unsafe[0]), "");
  assert.equal(panel._meshtasticNodeId({
    protocol: "meshcore",
    node_key: "meshtastic:!12345678",
    node_id: "!12345678",
  }), "");
  assert.equal(panel._meshtasticNodeId({
    protocol: "meshtastic",
    node_key: "meshcore:!12345678",
    node_id: "!12345678",
  }), "");
  for (const invalid of ["0", "!00000000", "4294967295", "!ffffffff", "4294967296", "!100000000", "bad"] ) {
    assert.equal(panel._parseMeshtasticNodeId(invalid), "");
  }
"""
    )


def test_inherited_meshtastic_names_remain_html_escaped() -> None:
    """A cached NodeInfo hint must never introduce sidebar HTML."""
    _run_panel_script(
        r"""
  const nodes = panel._nodesWithExactMeshtasticNameHints([
    {
      protocol: "meshtastic",
      node_key: "mac:aabbccddeeff",
      node_id: "!12345678",
      long_name: "<img src=x onerror=alert(1)>",
      short_name: "A&B",
    },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!12345678",
      node_id: "!12345678",
    },
  ]);
  const options = panel._recipientOptions(nodes);
  assert.doesNotMatch(options, /<img/);
  assert.match(options, /&lt;img src=x onerror=alert\(1\)&gt; · A&amp;B/);
"""
    )


def test_render_reports_original_unnamed_records_and_display_hints_separately() -> None:
    """The sidebar must not imply that a hinted cache record received NodeInfo."""
    _run_panel_script(
        r"""
  panel._snapshot = {
    nodes: {
      named: {
        protocol: "meshtastic",
        node_key: "mac:aabbccddeeff",
        node_id: "!12345678",
        long_name: "Named Node",
        short_name: "NN",
        gateway_ids: [],
        connectivity: {},
        location: {},
        routing: {},
      },
      unnamed: {
        protocol: "meshtastic",
        node_key: "meshtastic:!12345678",
        node_id: "!12345678",
        gateway_ids: [],
        connectivity: {},
        location: {},
        routing: {},
      },
    },
    gateways: {},
    recent_messages: [],
    panel_metadata: { favorite_label_configured: true },
  };
  panel._graph = () => "";
  panel._panelDiagnostics = () => "";
  panel._bindComposer = () => {};
  panel._bindNodeControls = () => {};
  panel._restoreComposerFocus = () => {};
  panel._render();
  assert.match(panel.innerHTML, /Named Node · NN/);
  assert.match(panel.innerHTML, /Name matched from the same exact !ID/);
  assert.match(panel.innerHTML, /1 Meshtastic packet\/cache record arrived without a NodeInfo name/);
  assert.match(panel.innerHTML, /1 display label uses an unambiguous cached name/);
"""
    )


def test_node_sorting_is_deterministic_and_handles_bad_timestamps() -> None:
    """Favorites and valid recent timestamps sort before stable name/key ties."""
    _run_panel_script(
        r"""
  const nodes = [
    { node_key: "k-old-favorite", long_name: "Zulu", favorite: true, last_heard: "2026-01-01T00:00:00Z" },
    { node_key: "k-new-favorite", long_name: "Beta", favorite: true, last_heard: "2026-01-03T00:00:00Z" },
    { node_key: "k-new", long_name: "Gamma", favorite: false, last_heard: "2026-01-04T00:00:00Z" },
    { node_key: "k-invalid", long_name: "Alpha", favorite: "true", last_heard: "not-a-date" },
    {
      protocol: "meshtastic",
      node_key: "meshtastic:!00000001",
      node_id: "!00000001",
      favorite: false,
      last_heard: "2026-01-05T00:00:00Z",
    },
  ];
  assert.deepEqual(
    panel._sortNodes(nodes, "favorites_recent").map((node) => node.node_key),
    ["k-new-favorite", "k-old-favorite", "meshtastic:!00000001", "k-new", "k-invalid"],
  );
  assert.deepEqual(
    panel._sortNodes(nodes, "last_seen").map((node) => node.node_key),
    ["meshtastic:!00000001", "k-new", "k-new-favorite", "k-old-favorite", "k-invalid"],
  );
  assert.deepEqual(
    panel._sortNodes(nodes, "name").map((node) => node.node_key),
    ["k-invalid", "k-new-favorite", "k-new", "k-old-favorite", "meshtastic:!00000001"],
  );
  assert.equal(panel._timestampMs("not-a-date"), null);
  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-01-04T00:30:00Z");
  assert.equal(panel._humanLastSeen("2026-01-04T00:00:00Z"), "Last seen 30m ago");
  assert.equal(panel._humanLastSeen("bad"), "Last seen unknown");
  Date.now = originalNow;
"""
    )


def test_passive_topology_uses_only_direct_hops_and_exact_meshcore_routes() -> None:
    """Never invent links merely because nodes share a receiving gateway."""
    _run_panel_script(
        r"""
  const gateways = [
    { gateway_id: "gateway-one", name: "One", connected: true },
    { gateway_id: "gateway-two", name: "Two", connected: true },
  ];
  const nodes = [
    {
      node_key: "meshtastic:direct",
      node_id: "direct",
      protocol: "meshtastic",
      last_gateway_id: "gateway-one",
      connectivity: { hops: 0, hops_gateway_id: "gateway-one" },
    },
    {
      node_key: "meshtastic:string-zero",
      node_id: "string-zero",
      protocol: "meshtastic",
      last_gateway_id: "gateway-one",
      connectivity: { hops: "0" },
    },
    {
      node_key: "meshtastic:remote",
      node_id: "remote",
      protocol: "meshtastic",
      last_gateway_id: "gateway-one",
      connectivity: { hops: 2 },
    },
    {
      node_key: "meshtastic:stale-multigateway-hop",
      node_id: "stale-multigateway-hop",
      protocol: "meshtastic",
      last_gateway_id: "gateway-two",
      gateway_ids: ["gateway-one", "gateway-two"],
      connectivity: { hops: 0 },
    },
    {
      node_key: "meshtastic:mqtt-zero-hop",
      node_id: "mqtt-zero-hop",
      protocol: "meshtastic",
      last_gateway_id: "gateway-one",
      gateway_ids: ["gateway-one"],
      connectivity: {
        hops: 0,
        hops_gateway_id: "gateway-one",
        via_mqtt: true,
      },
    },
    {
      node_key: "meshcore:a",
      node_id: "a",
      protocol: "meshcore",
      routing: { path: ["a", "b", "missing", "remote"] },
    },
    { node_key: "meshcore:b", node_id: "b", protocol: "meshcore" },
    {
      node_key: "meshtastic:fake-route",
      node_id: "fake-route",
      protocol: "meshtastic",
      routing: { path: ["direct", "remote"] },
    },
  ];
  const topology = panel._passiveTopology(nodes, gateways);
  assert.deepEqual(
    topology.edges.map((edge) => [edge.from, edge.to, edge.type]).sort(),
    [
      ["gateway:gateway-one", "node:meshtastic:direct", "direct"],
      ["node:meshcore:a", "node:meshcore:b", "route"],
    ].sort(),
  );
  assert.match(panel._graph(topology), /Cached passive topology — no traceroutes sent/);
  assert.match(panel._graph(topology), /last received evidence, not a live route/);

  const empty = panel._passiveTopology(
    [{
      node_key: "meshtastic:no-evidence",
      protocol: "meshtastic",
      last_gateway_id: "gateway-one",
      connectivity: { hops: "0" },
    }],
    gateways,
  );
  assert.equal(empty.edges.length, 0);
  assert.match(panel._graph(empty), /No passive connection evidence yet/);

  const many = Array.from({ length: 60 }, (_value, index) => ({
    node_key: `meshcore:${index}`,
    protocol: "meshcore",
  }));
  assert.equal(panel._passiveTopology(many, gateways).nodes.length, 36);
"""
    )
    source = _source()
    assert 'type: "meshnet/traceroute"' not in source
    assert "sendTraceroute" not in source
    assert "byGateway" not in source


def test_map_count_requires_valid_numeric_coordinates() -> None:
    """Only plausible numeric coordinates contribute to the native map count."""
    _run_panel_script(
        r"""
  const nodes = [
    { protocol: "meshcore", location: { latitude: 0, longitude: 0 } },
    { protocol: "meshtastic", location: { latitude: 0, longitude: 0 } },
    { location: { latitude: 45.5, longitude: -93.2 } },
    { location: { latitude: "45.5", longitude: -93.2 } },
    { location: { latitude: true, longitude: 0 } },
    { location: { latitude: 91, longitude: 0 } },
    { location: { latitude: 45, longitude: Number.NaN } },
    {},
  ];
  assert.equal(panel._validLocationCount(nodes), 3);
"""
    )


def test_send_composer_preserves_drafts_and_prevents_duplicate_submits() -> None:
    """Periodic full renders must not erase an in-progress message."""
    source = _source()

    assert "this._draft = {" in source
    assert 'const eventName = key === "message" ? "input" : "change"' in source
    assert "this._draft[key] = field.value" in source
    assert "this._renderPollSnapshot()" in source
    assert "this._pollRenderPending = true" in source
    assert 'field.addEventListener("focusout"' in source
    assert 'id="meshnet-send-button"' in source
    assert "this._panelInteractionActive()" in source
    assert "this._handlePollFocusOut(event)" in source
    assert "const composerFocus = this._composerFocusState()" in source
    assert "this._restoreComposerFocus(composerFocus)" in source
    for field_id in (
        "meshnet-delivery",
        "meshnet-recipient",
        "meshnet-gateway",
        "meshnet-message",
        "meshnet-channel",
        "meshnet-priority",
        "meshnet-node-sort",
    ):
        assert f'"{field_id}"' in source
    assert "this.getRootNode()" in source
    assert "root.activeElement" in source
    assert "this.ownerDocument.activeElement" in source
    assert "this.contains(active)" in source
    assert 'typeof active.selectionStart === "number"' in source
    assert "field.focus()" in source
    assert "field.setSelectionRange(state.start, state.end)" in source
    assert "if (this._sending) return" in source
    assert 'this._sending || (directDelivery && !recipientCount) ? " disabled" : ""' in source
    assert 'if (this._draft.message === draft.message) this._draft.message = ""' in source
    assert "const unnamedNodeCount = sourceNodes.filter" in source
    assert "const nodes = this._nodesWithExactMeshtasticNameHints(sourceNodes)" in source


def test_periodic_snapshot_waits_for_editor_focus_to_leave() -> None:
    """Polling must not replace a textarea, native select, or IME mid-use."""
    _run_panel_script(
        r"""
  let renders = 0;
  let schedules = 0;
  const textarea = {
    id: "meshnet-message",
    selectionStart: 4,
    selectionEnd: 7,
  };
  const recipient = { id: "meshnet-recipient" };
  const outside = { id: "outside" };
  let rootActive = textarea;
  panel.getRootNode = () => ({ activeElement: rootActive });
  panel.ownerDocument = {
    activeElement: { id: "home-assistant-shadow-host" },
  };
  panel.contains = (element) => element === textarea || element === recipient;
  panel._connected = true;
  panel._loaded = true;
  panel._pollEpoch = 4;
  panel._render = () => { renders += 1; };
  panel._scheduleNextPoll = () => { schedules += 1; };
  panel._refreshSnapshot = async () => {
    panel._snapshot = { nodes: {}, gateways: {}, recent_messages: [], marker: "new" };
    return panel._snapshot;
  };

  assert.deepEqual(panel._composerFocusState(), {
    id: "meshnet-message",
    start: 4,
    end: 7,
  });
  await panel._load(4);
  assert.equal(panel._snapshot.marker, "new");
  assert.equal(renders, 0);
  assert.equal(schedules, 1);
  assert.equal(panel._pollRenderPending, true);

  rootActive = recipient;
  panel._queuePendingPollRender();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(renders, 0);
  assert.equal(panel._pollRenderPending, true);

  rootActive = outside;
  panel._queuePendingPollRender();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(renders, 1);
  assert.equal(panel._pollRenderPending, false);

  panel._pollRenderPending = true;
  rootActive = textarea;
  panel._queuePendingPollRender();
  panel.disconnectedCallback();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(renders, 1);
  assert.equal(panel._pollRenderPending, false);
"""
    )


def test_focus_resolution_descends_home_assistant_shadow_roots() -> None:
    """Fallback focus discovery must cross open HA shadow-root hosts."""
    _run_panel_script(
        r"""
  const select = { id: "meshnet-recipient" };
  panel.getRootNode = () => ({ activeElement: null });
  panel.ownerDocument = {
    activeElement: {
      shadowRoot: {
        activeElement: {
          shadowRoot: { activeElement: select },
        },
      },
    },
  };
  panel.contains = (element) => element === select;
  assert.deepEqual(panel._composerFocusState(), {
    id: "meshnet-recipient",
    start: null,
    end: null,
  });
"""
    )


def test_pending_poll_does_not_replace_an_action_before_its_click() -> None:
    """Moving from an editor to Send/Message must not swallow activation."""
    _run_panel_script(
        r"""
  const sendButton = {
    id: "meshnet-send-button",
    hasAttribute() { return false; },
  };
  const messageButton = {
    id: "",
    hasAttribute(name) { return name === "data-message-node"; },
  };
  let active = sendButton;
  panel.getRootNode = () => ({ activeElement: active });
  panel.contains = (element) => element === sendButton || element === messageButton;
  panel._connected = true;
  panel._pollRenderPending = true;
  let renders = 0;
  panel._render = () => { renders += 1; };

  assert.equal(panel._panelInteractionActive(), true);
  assert.equal(panel._renderPollSnapshot(), false);
  assert.equal(renders, 0);

  active = messageButton;
  assert.equal(panel._panelInteractionActive(), true);
  assert.equal(panel._renderPollSnapshot(), false);
  assert.equal(renders, 0);

  let queued = 0;
  panel._queuePendingPollRender = () => { queued += 1; };
  panel._handlePollFocusOut({ relatedTarget: sendButton });
  panel._handlePollFocusOut({ relatedTarget: messageButton });
  panel._handlePollFocusOut({ relatedTarget: null });
  assert.equal(queued, 0);

  const outside = { id: "outside" };
  panel._handlePollFocusOut({ relatedTarget: outside });
  assert.equal(queued, 1);
"""
    )


def test_send_composer_enforces_utf8_byte_limit_client_side() -> None:
    """Meshtastic's limit is bytes, not JavaScript character count."""
    source = _source()

    assert "new TextEncoder().encode(String(message)).length" in source
    assert "this._messageByteLength(draft.message) > 237" in source
    assert 'text: "Message must be 237 UTF-8 bytes or fewer."' in source


def test_send_status_is_safe_and_refreshes_after_acceptance() -> None:
    """Show only fixed status text, never backend exception contents."""
    source = _source()
    send_method = source.split("  async _sendMessage(event) {", 1)[1].split(
        "  _messageByteLength(message) {", 1
    )[0]

    assert 'snapshot = await this._refreshSnapshot(this._pollEpoch, "post_send_refresh")' in send_method
    assert 'status === "sent"' in send_method
    assert 'text: "Message sent."' in send_method
    assert 'text: "Message queued for delivery."' in send_method
    assert 'text: "Message could not be submitted."' in send_method
    assert "_err.message" not in send_method
    assert "String(_err)" not in send_method
    assert '${this._escape(this._sendStatus ? this._sendStatus.text : "")}' in source


def test_failed_snapshot_retries_and_reports_only_backend_safe_vocabulary() -> None:
    """A rejected snapshot remains retryable without exposing exception content."""
    _run_panel_script(
        r"""
  let nextTimer = 0;
  const timers = new Map();
  window.setTimeout = (callback, delay) => {
    nextTimer += 1;
    timers.set(nextTimer, { callback, delay });
    return nextTimer;
  };
  window.clearTimeout = (timer) => timers.delete(timer);
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...parts) => warnings.push(parts);
  const reports = [];
  let snapshotCalls = 0;
  const privateError = new Error("private message /dev/ttyUSB0 44.1,-93.2");
  privateError.name = "PrivateNodeIdentifier";
  privateError.code = "meshtastic:!12345678";
  panel._render = () => {};
  panel.hass = {
    async callWS(payload) {
      if (payload.type === "meshnet/panel_log") {
        reports.push(payload);
        return { accepted: true };
      }
      snapshotCalls += 1;
      throw privateError;
    },
  };

  panel.connectedCallback();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(snapshotCalls, 1);
  assert.equal(panel._error, "Snapshot unavailable");
  assert.equal(panel._snapshotConsecutiveFailures, 1);
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].delay, 5000);
  assert.equal(reports.length, 1);
  assert.deepEqual(Object.keys(reports[0]).sort(), [
    "category",
    "consecutive",
    "error_code",
    "error_type",
    "occurrence",
    "operation",
    "type",
  ]);
  assert.deepEqual(reports[0], {
    type: "meshnet/panel_log",
    operation: "snapshot",
    category: "connection",
    error_type: "other_error",
    error_code: "snapshot_failed",
    occurrence: 1,
    consecutive: 1,
  });
  const operations = new Set([
    "snapshot", "messages", "send_message", "render", "poll",
    "snapshot_schema", "snapshot_timeout", "post_send_refresh",
    "event_handler", "global_error", "unhandled_rejection",
    "invalid_recipient", "reporting",
  ]);
  const categories = new Set([
    "authentication", "availability", "connection", "data", "internal",
    "lifecycle", "network", "permission", "timeout", "unknown", "validation",
  ]);
  const errorTypes = new Set([
    "AbortError", "CancelledError", "ConnectionError", "DOMException", "Error",
    "HomeAssistantError", "InvalidAuth", "NetworkError", "NotFoundError",
    "OSError", "PermissionError", "RuntimeError", "SchemaError",
    "ServiceValidationError", "SyntaxError", "TimeoutError", "TypeError",
    "Unauthorized", "ValueError", "WebSocketError", "other_error", "unknown_error",
  ]);
  const errorCodes = new Set([
    "callback_failed", "connection_failed", "favorite_device_lookup_failed",
    "favorite_registry_failed", "handler_failed", "invalid_recipient",
    "invalid_response", "invalid_schema", "message_load_failed",
    "operation_cancelled", "operation_failed", "poll_failed",
    "post_send_refresh_failed", "provenance_failed", "render_failed",
    "report_failed", "send_failed", "snapshot_failed", "timeout", "unavailable",
    "unexpected_error", "websocket_failed",
  ]);
  assert.equal(operations.has(reports[0].operation), true);
  assert.equal(categories.has(reports[0].category), true);
  assert.equal(errorTypes.has(reports[0].error_type), true);
  assert.equal(errorCodes.has(reports[0].error_code), true);
  const retained = JSON.stringify({ warnings, telemetry: panel._failureTelemetry, reports });
  assert.equal(retained.includes("private message"), false);
  assert.equal(retained.includes("ttyUSB0"), false);
  assert.equal(retained.includes("44.1"), false);
  assert.equal(retained.includes("12345678"), false);
  console.warn = originalWarn;
"""
    )


def test_hung_snapshot_times_out_and_render_failure_cannot_stop_polling() -> None:
    """Timeout and render failures are independently captured before retry."""
    _run_panel_script(
        r"""
  let nextTimer = 0;
  const timers = new Map();
  window.setTimeout = (callback, delay) => {
    nextTimer += 1;
    timers.set(nextTimer, { callback, delay });
    return nextTimer;
  };
  window.clearTimeout = (timer) => timers.delete(timer);
  const reports = [];
  panel._render = () => {
    const error = new TypeError("private render detail");
    error.code = "private-render-code";
    throw error;
  };
  panel.hass = {
    callWS(payload) {
      if (payload.type === "meshnet/panel_log") {
        reports.push(payload);
        return Promise.resolve({ accepted: true });
      }
      return new Promise(() => {});
    },
  };

  panel.connectedCallback();
  await Promise.resolve();
  const timeoutEntry = [...timers.values()].find((timer) => timer.delay === 15000);
  assert.ok(timeoutEntry);
  timeoutEntry.callback();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  const operations = panel._failureTelemetry.map((event) => event.operation);
  assert.equal(operations.includes("snapshot_request"), true);
  assert.equal(operations.includes("render"), true);
  assert.equal(panel._snapshotConsecutiveFailures, 1);
  assert.ok(panel._pollTimer != null);
  assert.equal([...timers.values()].some((timer) => timer.delay === 5000), true);
  assert.equal(reports.some((report) => report.operation === "snapshot_timeout"), true);
  assert.equal(reports.some((report) => report.operation === "render"), true);
"""
    )


def test_malformed_and_late_snapshots_never_replace_last_good_snapshot() -> None:
    """Schema rejection is observable, while disconnected late data is ignored."""
    _run_panel_script(
        r"""
  const oldSnapshot = { nodes: {}, gateways: {}, recent_messages: [], marker: "old" };
  panel._snapshot = oldSnapshot;
  panel._connected = true;
  panel._pollEpoch = 7;
  const reports = [];
  panel._hass = {
    async callWS(payload) {
      if (payload.type === "meshnet/panel_log") {
        reports.push(payload);
        return { accepted: true };
      }
      return { nodes: [], gateways: {}, recent_messages: [] };
    },
  };
  await assert.rejects(
    panel._refreshSnapshot(7, "snapshot_request"),
    (error) => error.code === "nodes_not_object",
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(panel._snapshot, oldSnapshot);
  assert.equal(reports[0].operation, "snapshot_schema");
  assert.equal(reports[0].category, "data");
  assert.equal(reports[0].error_type, "SchemaError");
  assert.equal(reports[0].error_code, "invalid_schema");

  let resolveLate;
  panel._hass = {
    callWS() {
      return new Promise((resolve) => { resolveLate = resolve; });
    },
  };
  const lateRequest = panel._refreshSnapshot(7, "snapshot_request");
  panel.disconnectedCallback();
  resolveLate({ nodes: {}, gateways: {}, recent_messages: [], marker: "late" });
  assert.equal(await lateRequest, null);
  assert.equal(panel._snapshot, oldSnapshot);
  assert.equal(panel._failureTelemetry.length, 1);
"""
    )


def test_unexpected_poll_failure_is_recorded_and_retry_is_scheduled() -> None:
    """Even an uninstrumented internal rejection cannot silently kill polling."""
    _run_panel_script(
        r"""
  let nextTimer = 0;
  const timers = new Map();
  window.setTimeout = (callback, delay) => {
    nextTimer += 1;
    timers.set(nextTimer, { callback, delay });
    return nextTimer;
  };
  window.clearTimeout = (timer) => timers.delete(timer);
  panel._connected = true;
  panel._loaded = true;
  panel._pollEpoch = 4;
  panel._render = () => {};
  panel._refreshSnapshot = async () => {
    throw new RangeError("private unexpected poll detail");
  };

  await panel._load(4);

  assert.equal(panel._failureTelemetry[0].operation, "poll_unexpected");
  assert.equal(panel._failureTelemetry[0].error_type, "RangeError");
  assert.equal([...timers.values()].some((timer) => timer.delay === 5000), true);
"""
    )


def test_failure_buffers_are_bounded_and_reporting_failure_does_not_recurse() -> None:
    """Local queues cap at 100 and a failed report produces no report loop."""
    _run_panel_script(
        r"""
  const originalWarn = console.warn;
  console.warn = () => {};
  const privateError = new Error("private message and node identifier");
  privateError.name = "PrivateErrorClass";
  privateError.code = "private-node-code";
  for (let index = 0; index < 130; index += 1) {
    panel._recordFailure("private-operation", "private-category", privateError);
  }
  assert.equal(panel._failureCount, 130);
  assert.equal(panel._failureTelemetry.length, 100);
  assert.equal(panel._panelReportQueue.length, 100);
  assert.equal(panel._failureTelemetry[0].occurrence, 31);
  assert.deepEqual(Object.keys(panel._panelReportQueue[0]).sort(), [
    "category", "consecutive", "error_code", "error_type", "occurrence", "operation",
  ]);
  const retained = JSON.stringify({
    telemetry: panel._failureTelemetry,
    queue: panel._panelReportQueue,
  });
  assert.equal(retained.includes("private message"), false);
  assert.equal(retained.includes("identifier"), false);
  assert.equal(retained.includes("private-operation"), false);
  assert.equal(retained.includes("private-node-code"), false);

  const reportPanel = new PanelClass();
  reportPanel._connected = true;
  let reportCalls = 0;
  reportPanel._hass = {
    async callWS(payload) {
      assert.equal(payload.type, "meshnet/panel_log");
      reportCalls += 1;
      const error = new Error("private reporting failure");
      error.code = "private-report-code";
      throw error;
    },
  };
  reportPanel._recordFailure("render", "render", new TypeError("private render"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reportCalls, 1);
  assert.equal(reportPanel._panelReportFailureCount, 1);
  assert.equal(reportPanel._failureTelemetry.length, 2);
  assert.equal(reportPanel._failureTelemetry[1].operation, "reporting");
  assert.equal(reportPanel._panelReportQueue.length, 1);
  assert.equal(reportPanel._panelReportQueue[0].operation, "render");
  console.warn = originalWarn;
"""
    )


def test_post_send_refresh_failure_is_logged_without_changing_send_success() -> None:
    """An accepted send remains queued/sent when only its status refresh fails."""
    _run_panel_script(
        r"""
  const reports = [];
  const requests = [];
  panel._connected = true;
  panel._render = () => {};
  panel._snapshot = { nodes: {}, gateways: {}, recent_messages: [] };
  panel._hass = {
    async callWS(payload) {
      if (payload.type === "meshnet/panel_log") {
        reports.push(payload);
        return { accepted: true };
      }
      requests.push(payload);
      return { message_id: "accepted-message" };
    },
  };
  panel._refreshSnapshot = async () => {
    const error = new TypeError("private refresh /dev/serial coordinates 1,2");
    error.code = "private-code";
    throw error;
  };
  panel._draft.message = "private outgoing text";

  await panel._sendMessage({ preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(requests.length, 1);
  assert.deepEqual(panel._sendStatus, {
    kind: "warn",
    text: "Message queued for delivery.",
  });
  assert.equal(panel._draft.message, "");
  const failure = panel._failureTelemetry.find(
    (event) => event.operation === "post_send_refresh",
  );
  assert.ok(failure);
  assert.equal(reports.some((report) => report.operation === "post_send_refresh"), true);
  assert.equal(JSON.stringify(reports).includes("private outgoing text"), false);
  assert.equal(JSON.stringify(reports).includes("serial"), false);
"""
    )


def test_global_failure_handlers_are_safe_and_removed_on_disconnect() -> None:
    """Window failures are classified without retaining event text or leaking listeners."""
    _run_panel_script(
        r"""
  const listeners = new Map();
  window.addEventListener = (name, callback) => listeners.set(name, callback);
  window.removeEventListener = (name, callback) => {
    if (listeners.get(name) === callback) listeners.delete(name);
  };
  const reports = [];
  panel._loaded = true;
  panel.hass = {
    async callWS(payload) {
      reports.push(payload);
      return { accepted: true };
    },
  };
  panel.connectedCallback();
  assert.equal(listeners.size, 2);

  const privateError = new Error("private URL and coordinates");
  privateError.name = "PrivateNodeClass";
  privateError.code = "private-node-code";
  listeners.get("error")({
    message: "unrelated Home Assistant error",
    filename: "/frontend_latest/core.js",
    error: new Error("unrelated"),
  });
  listeners.get("unhandledrejection")({ reason: new Error("unrelated rejection") });
  assert.equal(panel._failureTelemetry.length, 0);

  privateError.stack = "PrivateNodeClass at /meshnet_static/meshnet-panel.js:1:1";
  listeners.get("error")({
    message: "private browser message",
    filename: "/meshnet_static/meshnet-panel.js",
    error: privateError,
  });
  const privateRejection = new Error("private rejection text");
  privateRejection.stack = "Error at /meshnet_static/meshnet-panel.js:2:2";
  listeners.get("unhandledrejection")({ reason: privateRejection });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(reports.length, 2);
  assert.deepEqual(reports.map((report) => report.operation), [
    "global_error",
    "unhandled_rejection",
  ]);
  const retained = JSON.stringify({ reports, telemetry: panel._failureTelemetry });
  assert.equal(retained.includes("private"), false);
  panel.disconnectedCallback();
  assert.equal(listeners.size, 0);
"""
    )


def test_panel_diagnostics_uses_only_fixed_counts_and_registration_is_idempotent() -> None:
    """The compact status explains provenance without rendering arbitrary metadata."""
    _run_panel_script(
        r"""
  const html = panel._panelDiagnostics({
    total_node_count: 286,
    retained_node_record_count: 473,
    collapsed_alias_record_count: 187,
    resolved_identity_group_count: 172,
    unresolved_identity_group_count: 2,
    unresolved_identity_node_count: 4,
    invalid_identity_record_count: 3,
    analyzed_node_count: 286,
    omitted_node_count: 0,
    current_session_node_count: 191,
    cached_only_node_count: 114,
    online_node_count: 24,
    located_node_count: 175,
    located_offline_node_count: 163,
    mqtt_node_count: 42,
    mqtt_unknown_node_count: 114,
    identity_collision_group_count: 2,
    identity_collision_node_count: 4,
    last_snapshot_generated_at: "2026-07-28T12:00:00Z",
    private_metadata: "must-not-render",
  }, Array.from({ length: 286 }, () => ({})));
  assert.match(html, /286 distinct · 24 recent · 175 located/);
  assert.match(html, /187 collapsed · 172 groups · 473 retained records/);
  assert.match(html, /Original cache records remain stored/);
  assert.match(html, /191 gateway-reported · 114 retained cache only/);
  assert.match(html, /stored node database/);
  assert.match(html, /does not mean they were directly heard this session/);
  assert.match(html, /42 yes · 114 unknown/);
  assert.match(html, /does not mean this MeshNet gateway currently uses MQTT/);
  assert.match(html, /163 not recently seen/);
  assert.match(html, /Unresolved identity evidence/);
  assert.match(html, /2 conflicting groups · 4 records · 3 malformed/);
  assert.equal(html.includes("must-not-render"), false);

  const cappedHtml = panel._panelDiagnostics({
    total_node_count: 1007,
    analyzed_node_count: 1000,
    omitted_node_count: 7,
  }, []);
  assert.match(cappedHtml, /Panel safety limit/);
  assert.match(cappedHtml, /1000 analyzed · 7 omitted/);
  assert.match(cappedHtml, /remain in Home Assistant/);
  assert.match(cappedHtml, /protect the event loop/);

  let duplicateDefineCount = 0;
  vm.runInNewContext(fs.readFileSync(panelPath, "utf8"), {
    HTMLElement,
    TextEncoder,
    console,
    window,
    customElements: {
      get(name) { return name === "meshnet-panel" ? PanelClass : null; },
      define() { duplicateDefineCount += 1; },
    },
  }, { filename: panelPath });
  assert.equal(duplicateDefineCount, 0);
"""
    )
