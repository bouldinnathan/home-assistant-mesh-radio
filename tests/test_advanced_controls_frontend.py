"""Panel contracts for explicit remote administration and traceroute controls."""

from __future__ import annotations

import json
from pathlib import Path

from test_advanced_frontend import _run_panel_script

PANEL = Path("custom_components/meshnet/frontend/meshnet-panel.js")


def test_remote_admin_and_traceroute_are_visible_explicit_controls_only() -> None:
    source = PANEL.read_text(encoding="utf-8")
    for command in (
        'type: "meshnet/remote_settings/get"',
        'type: "meshnet/remote_settings/preview"',
        'type: "meshnet/remote_settings/apply"',
        'type: "meshnet/traceroute"',
        'type: "meshnet/traceroute/status"',
    ):
        assert command in source
    assert "Controller public key" in source
    assert "copy-only" in source.casefold()
    assert "Private key" not in source
    assert "admin key slot" not in source.casefold()
    assert "localStorage" not in source
    assert "sessionStorage" not in source


def test_remote_admin_requires_load_preview_and_confirmation_before_one_apply() -> None:
    _run_panel_script(
        r"""
  panel._safeRender = () => true;
  panel.querySelectorAll = () => [];
  const requests = [];
  const snapshot = {
    schema_version: 1,
    gateway_id: "ble-gateway",
    target_node: "!1234abcd",
    revision: "a".repeat(64),
    controller: {
      node_id: "!01020304",
      short_name: "CTRL",
      public_key: "base64:AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA=",
      public_key_copy_only: true,
    },
    target: { node_id: "!1234abcd", short_name: "RMTE", long_name: "Remote node" },
    categories: [{
      key: "owner",
      label: "Owner",
      fields: [{
        path: "owner.short_name",
        label: "Short name",
        type: "string",
        value: "RMTE",
        writable: true,
      }],
    }],
  };
  panel._hass = {
    async callWS(payload) {
      requests.push(structuredClone(payload));
      if (payload.type === "meshnet/remote_settings/get") return snapshot;
      if (payload.type === "meshnet/remote_settings/preview") {
        return {
          schema_version: 1,
          preview_id: "p".repeat(43),
          gateway_id: "ble-gateway",
          target_node: "!1234abcd",
          revision: "a".repeat(64),
          changes: [{ path: "owner.short_name", label: "Short name" }],
          requires_confirmation: true,
          expires_at: "2026-07-30T12:05:00Z",
        };
      }
      if (payload.type === "meshnet/remote_settings/apply") {
        return {
          schema_version: 1,
          status: "verified",
          gateway_id: "ble-gateway",
          target_node: "!1234abcd",
          verified: ["owner.short_name"],
          unverified: [],
        };
      }
      throw new Error("unexpected request");
    },
  };

  assert.equal(typeof panel._loadRemoteSettings, "function");
  assert.equal(typeof panel._previewRemoteSettings, "function");
  assert.equal(typeof panel._applyRemoteSettings, "function");
  await panel._loadRemoteSettings("ble-gateway", "!1234abcd");
  assert.deepEqual(requests[0], {
    type: "meshnet/remote_settings/get",
    gateway_id: "ble-gateway",
    target_node: "!1234abcd",
  });
  assert.equal(panel._remoteSettingsSnapshot.controller.public_key_copy_only, true);

  panel._remoteSettingsDraft["owner.short_name"] = "NEW";
  await panel._previewRemoteSettings({ preventDefault() {} });
  assert.deepEqual(requests[1], {
    type: "meshnet/remote_settings/preview",
    gateway_id: "ble-gateway",
    target_node: "!1234abcd",
    revision: "a".repeat(64),
    changes: { "owner.short_name": "NEW" },
  });
  assert.equal(panel._remoteSettingsPreview.requires_confirmation, true);

  await panel._applyRemoteSettings();
  assert.equal(requests.length, 2, "an unconfirmed remote write must emit no RF request");
  assert.match(panel._remoteSettingsStatus.text, /confirm/i);

  panel._remoteSettingsConfirmed = true;
  await panel._applyRemoteSettings();
  assert.deepEqual(requests[2], {
    type: "meshnet/remote_settings/apply",
    gateway_id: "ble-gateway",
    target_node: "!1234abcd",
    revision: "a".repeat(64),
    preview_id: "p".repeat(43),
    confirm_remote: true,
  });
  assert.equal(panel._remoteSettingsPreview, null);
  assert.deepEqual(panel._remoteSettingsDraft, {});
  assert.equal(panel._remoteSettingsConfirmed, false);
  assert.match(panel._remoteSettingsStatus.text, /verified/i);
"""
    )


def test_manual_traceroute_is_two_step_and_surfaces_persistent_cooldown() -> None:
    _run_panel_script(
        r"""
  const requests = [];
  panel._safeRender = () => true;
  panel._tracerouteStatusReady = true;
  panel._tracerouteGlobalStatus = {
    schema_version: 1,
    status: "available",
    reserved: false,
    gateway_id: null,
    target_node: null,
    reserved_at: null,
    next_allowed_at: null,
    remaining_seconds: 0,
    result_updated_at: null,
    result: null,
  };
  panel._hass = {
    async callWS(payload) {
      requests.push(structuredClone(payload));
      return {
        schema_version: 1,
        status: "complete",
        gateway_id: "ble-gateway",
        destination: "meshtastic:!1234abcd",
        correlation_id: "trace-one",
        forward_route: ["meshtastic:!01020304", "meshtastic:!1234abcd"],
        reverse_route: [],
        next_allowed_at: "2026-07-30T13:00:00+00:00",
      };
    },
  };

  assert.equal(typeof panel._requestTraceroute, "function");
  await panel._requestTraceroute("ble-gateway", "meshtastic:!1234abcd");
  assert.equal(requests.length, 0);
  assert.deepEqual(panel._tracerouteConfirmation, {
    gateway_id: "ble-gateway",
    target_node: "meshtastic:!1234abcd",
  });

  await panel._requestTraceroute("ble-gateway", "meshtastic:!1234abcd");
  assert.deepEqual(requests, [{
    type: "meshnet/traceroute",
    gateway_id: "ble-gateway",
    target_node: "meshtastic:!1234abcd",
  }]);
  assert.equal(panel._tracerouteConfirmation, null);
  assert.equal(panel._tracerouteResults["meshtastic:!1234abcd"].correlation_id, "trace-one");
  assert.match(JSON.stringify(panel._tracerouteResults), /next_allowed_at/);

  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-07-30T12:30:00+00:00");
  await panel._requestTraceroute("other-gateway", "meshtastic:!87654321");
  assert.equal(requests.length, 1, "one result must activate the global cooldown");
  assert.equal(panel._tracerouteConfirmation, null);
  Date.now = originalNow;
"""
    )


def test_persisted_traceroute_status_is_loaded_once_and_rendered_safely() -> None:
    """One local status read gates every target and retains bounded route metadata."""
    _run_panel_script(
        r"""
  const requests = [];
  panel._safeRender = () => true;
  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-07-30T12:30:00+00:00");
  panel._hass = {
    async callWS(payload) {
      requests.push(structuredClone(payload));
      return {
        schema_version: 1,
        scope: "integration",
        status: "cooldown",
        reserved: true,
        gateway_id: "ble-gateway",
        target_node: "meshtastic:!1234abcd",
        reserved_at: "2026-07-30T12:00:00+00:00",
        next_allowed_at: "2026-07-30T13:00:00+00:00",
        remaining_seconds: 1800,
        result_updated_at: "2026-07-30T12:00:08+00:00",
        result: {
          schema_version: 1,
          gateway_id: "ble-gateway",
          source: "meshtastic:!01020304",
          destination: "meshtastic:!1234abcd",
          channel: 0,
          completed_at: "2026-07-30T12:00:07+00:00",
          forward_route: ["meshtastic:!01020304", "meshtastic:!1234abcd"],
          reverse_route: ["meshtastic:!1234abcd", "meshtastic:!01020304"],
          snr_towards: [1, -2.25],
          snr_back: [3.5],
        },
      };
    },
  };

  assert.equal(typeof panel._loadTracerouteStatus, "function");
  await panel._loadTracerouteStatus();
  assert.deepEqual(requests, [{ type: "meshnet/traceroute/status" }]);
  assert.equal(panel._tracerouteStatusReady, true);
  assert.equal(panel._tracerouteGlobalStatus.status, "cooldown");
  assert.equal(
    panel._tracerouteResults["meshtastic:!1234abcd"].completed_at,
    "2026-07-30T12:00:07+00:00",
  );
  assert.deepEqual(
    panel._tracerouteResults["meshtastic:!1234abcd"].snr_towards,
    [1, -2.25],
  );
  assert.equal(
    panel._tracerouteResults["meshtastic:!1234abcd"].correlation_id,
    null,
  );
  assert.equal(
    panel._tracerouteCooldownActive("another-gateway", "meshtastic:!87654321"),
    true,
  );

  await panel._requestTraceroute("another-gateway", "meshtastic:!87654321");
  assert.equal(requests.length, 1, "global cooldown must block every other pair");
  assert.equal(panel._tracerouteConfirmation, null);

  const html = panel._tracerouteResultPanel(
    panel._tracerouteResults["meshtastic:!1234abcd"],
  );
  assert.match(html, /ble-gateway/);
  assert.match(html, /7\/30\/2026/);
  assert.match(html, /1(?:\.0)? dB/);
  assert.match(html, /-2\.25 dB/);
  assert.match(html, /3\.5 dB/);
  assert.doesNotMatch(html, /Correlation/);
  Date.now = originalNow;
"""
    )


def test_operator_errors_use_fixed_local_actions_and_never_server_text() -> None:
    """Known server codes select local copy; arbitrary messages never render."""
    _run_panel_script(
        r"""
  const sentinel = "provider detail node !deadbeef key=secret";
  const remoteError = new Error(sentinel);
  remoteError.code = "remote_admin_controller_unauthorized";
  panel._safeRender = () => true;
  panel._hass = { async callWS() { throw remoteError; } };
  await panel._loadRemoteSettings("ble-gateway", "!1234abcd");
  assert.match(panel._remoteSettingsStatus.text, /authorize|controller public key/i);
  assert.equal(panel._remoteSettingsStatus.text.includes(sentinel), false);
  assert.equal(JSON.stringify(panel._remoteSettingsStatus).includes("!deadbeef"), false);

  const traceError = new Error(sentinel);
  traceError.code = "traceroute_cooldown";
  panel._tracerouteStatusReady = true;
  panel._tracerouteGlobalStatus = {
    schema_version: 1,
    status: "available",
    reserved: false,
    gateway_id: null,
    target_node: null,
    reserved_at: null,
    next_allowed_at: null,
    remaining_seconds: 0,
    result_updated_at: null,
    result: null,
  };
  panel._tracerouteConfirmation = {
    gateway_id: "ble-gateway",
    target_node: "meshtastic:!1234abcd",
  };
  panel._hass = { async callWS() { throw traceError; } };
  await panel._requestTraceroute("ble-gateway", "meshtastic:!1234abcd");
  assert.match(panel._tracerouteStatus.text, /cooldown|wait/i);
  assert.equal(panel._tracerouteStatus.text.includes(sentinel), false);
  assert.equal(JSON.stringify(panel._tracerouteStatus).includes("!deadbeef"), false);

  for (const code of [
    "remote_admin_target_public_key_unavailable",
    "remote_admin_session_rejected",
    "remote_admin_no_route",
    "remote_admin_rate_limited",
    "remote_admin_revision_conflict",
    "remote_admin_unknown_outcome",
  ]) {
    const error = new Error(sentinel);
    error.code = code;
    const text = panel._remoteAdminErrorText(error, "apply");
    assert.equal(typeof text, "string");
    assert.ok(text.length > 10);
    assert.equal(text.includes(sentinel), false);
  }
"""
    )


def test_remote_ui_rejects_malformed_success_without_retaining_draft_values() -> None:
    """A malformed server response cannot legitimize an RF write preview."""
    private_value = "must-not-survive-failed-validation"
    _run_panel_script(
        rf"""
  panel._safeRender = () => true;
  panel._remoteGatewayId = "ble-gateway";
  panel._remoteTargetNode = "!1234abcd";
  panel._remoteSettingsSnapshot = {{
    schema_version: 1,
    gateway_id: "ble-gateway",
    target_node: "!1234abcd",
    revision: "a".repeat(64),
    controller: {{ public_key: "base64:AAAA", public_key_copy_only: true }},
    target: {{ node_id: "!1234abcd" }},
    categories: [],
  }};
  panel._remoteSettingsDraft = {{ "owner.short_name": {json.dumps(private_value)} }};
  panel._hass = {{ async callWS() {{ return {{ preview_id: "short" }}; }} }};
  await panel._previewRemoteSettings({{ preventDefault() {{}} }});
  assert.equal(panel._remoteSettingsPreview, null);
  assert.deepEqual(panel._remoteSettingsDraft, {{}});
  assert.equal(JSON.stringify(panel).includes({json.dumps(private_value)}), false);
"""
    )
