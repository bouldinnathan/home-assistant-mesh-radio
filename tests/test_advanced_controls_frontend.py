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
    assert "sessionStorage" not in source
    persistence = source[
        source.index("  _persistMeshCardLayout()") : source.index("  _orderedMeshCards(")
    ]
    assert "_remoteSettingsDraft" not in persistence
    assert "_settingsDraft" not in persistence
    assert "public_key" not in persistence


def test_neighbor_info_is_explicit_experimental_and_status_gated() -> None:
    source = PANEL.read_text(encoding="utf-8")
    assert 'type: "meshnet/neighbor_info/status"' in source
    assert 'type: "meshnet/neighbor_info"' in source
    assert "Experimental / newer firmware only" in source
    assert "official android" in source.casefold()
    assert "_maybeLoadNeighborInfo" not in source


def test_neighbor_info_loads_target_status_then_requires_two_clicks_for_one_rf_request() -> None:
    _run_panel_script(
        r"""
  panel._safeRender = () => true;
  const requests = [];
  panel._hass = {
    async callWS(payload) {
      requests.push(structuredClone(payload));
      if (payload.type === "meshnet/neighbor_info/status") {
        return {
          schema_version: 1,
          scope: "integration_and_target",
          target_node: "meshtastic:!1234abcd",
          status: "available",
          global_remaining_seconds: 0,
          target_remaining_seconds: 0,
          remaining_seconds: 0,
        };
      }
      if (payload.type === "meshnet/neighbor_info") {
        return {
          schema_version: 1,
          gateway_id: "ble-gateway",
          source: "meshtastic:!1234abcd",
          destination: "meshtastic:!01020304",
          channel: 0,
          node_broadcast_interval_secs: 3600,
          neighbors: [
            { node_id: "meshtastic:!11121314", snr: -2.25 },
            { node_id: "meshtastic:!21222324", snr: 4.5 },
          ],
          completed_at: "2026-07-31T12:00:05+00:00",
          next_allowed_at: "2026-07-31T12:03:05+00:00",
        };
      }
      throw new Error("unexpected request");
    },
  };

  assert.equal(panel._neighborInfoStatusReady, false);
  await panel._requestNeighborInfo("ble-gateway", "meshtastic:!1234abcd");
  assert.equal(requests.length, 0, "RF stays locked until persisted status is read");

  await panel._loadNeighborInfoStatus("meshtastic:!1234abcd");
  assert.deepEqual(requests, [{
    type: "meshnet/neighbor_info/status",
    target_node: "meshtastic:!1234abcd",
  }]);
  assert.equal(panel._neighborInfoStatusReady, true);
  assert.equal(panel._neighborInfoStatusTarget, "meshtastic:!1234abcd");

  await panel._requestNeighborInfo("ble-gateway", "meshtastic:!1234abcd");
  assert.equal(requests.length, 1, "first click only creates the RF confirmation");
  assert.deepEqual(panel._neighborInfoConfirmation, {
    gateway_id: "ble-gateway",
    target_node: "meshtastic:!1234abcd",
  });
  await panel._requestNeighborInfo("ble-gateway", "meshtastic:!1234abcd");
  assert.deepEqual(requests[1], {
    type: "meshnet/neighbor_info",
    gateway_id: "ble-gateway",
    target_node: "meshtastic:!1234abcd",
  });
  assert.equal(panel._neighborInfoResult.neighbors.length, 2);
  assert.equal(panel._neighborInfoResult.neighbors[0].snr, -2.25);
  assert.equal(panel._neighborInfoStatusData.global_remaining_seconds, 60);
  assert.equal(panel._neighborInfoStatusData.target_remaining_seconds, 180);
  assert.equal(panel._neighborInfoCooldownActive(), true);

  const html = panel._neighborInfoResultPanel(panel._neighborInfoResult);
  assert.match(html, /!11121314/);
  assert.match(html, /-2\.25 dB/);
  assert.match(html, /3600 seconds/);
"""
    )


def test_neighbor_info_validation_is_bounded_and_failures_never_claim_zero_neighbors() -> None:
    _run_panel_script(
        r"""
  const base = {
    schema_version: 1,
    gateway_id: "ble-gateway",
    source: "meshtastic:!1234abcd",
    destination: "meshtastic:!01020304",
    channel: 0,
    node_broadcast_interval_secs: 3600,
    neighbors: [],
    completed_at: "2026-07-31T12:00:05+00:00",
    next_allowed_at: "2026-07-31T12:03:05+00:00",
  };
  assert.equal(
    panel._sanitizeNeighborInfoResult(base, "ble-gateway", "meshtastic:!1234abcd").neighbors.length,
    0,
  );
  assert.throws(() => panel._sanitizeNeighborInfoResult({
    ...base,
    neighbors: Array.from({ length: 11 }, (_item, index) => ({
      node_id: `meshtastic:!${(index + 1).toString(16).padStart(8, "0")}`,
      snr: 1,
    })),
  }, "ble-gateway", "meshtastic:!1234abcd"));
  assert.throws(() => panel._sanitizeNeighborInfoResult({
    ...base,
    neighbors: [{ node_id: "meshtastic:!11121314", snr: Number.NaN }],
  }, "ble-gateway", "meshtastic:!1234abcd"));

  const persisted = panel._sanitizeNeighborInfoStatus({
    schema_version: 1,
    scope: "integration_and_target",
    target_node: "meshtastic:!1234abcd",
    status: "cooldown",
    global_remaining_seconds: 180,
    target_remaining_seconds: 60,
    remaining_seconds: 180,
    next_allowed_at: "2026-07-31T12:03:05+00:00",
    gateway_id: "newer-global-reservation-gateway",
    reserved_at: "2026-07-31T12:00:05+00:00",
    result_updated_at: "2026-07-31T11:59:05+00:00",
    result: {
      ...base,
      gateway_id: "persisted-result-gateway",
      next_allowed_at: undefined,
    },
  }, "meshtastic:!1234abcd");
  assert.equal(persisted.gateway_id, "newer-global-reservation-gateway");
  assert.equal(persisted.result.gateway_id, "persisted-result-gateway");
  assert.equal(persisted.result.next_allowed_at, "2026-07-31T12:03:05+00:00");

  const sentinel = "provider detail node !deadbeef key=secret";
  const error = new Error(sentinel);
  error.code = "neighbor_info_failed";
  panel._safeRender = () => true;
  panel._neighborInfoStatusReady = true;
  panel._neighborInfoStatusTarget = "meshtastic:!1234abcd";
  panel._neighborInfoStatusData = {
    status: "available",
    global_remaining_seconds: 0,
    target_remaining_seconds: 0,
    remaining_seconds: 0,
    loaded_at_ms: Date.now(),
  };
  panel._neighborInfoConfirmation = {
    gateway_id: "ble-gateway",
    target_node: "meshtastic:!1234abcd",
  };
  panel._hass = { async callWS() { throw error; } };
  await panel._requestNeighborInfo("ble-gateway", "meshtastic:!1234abcd");
  assert.equal(panel._neighborInfoResult, null);
  assert.equal(panel._neighborInfoStatusData, null);
  assert.equal(panel._neighborInfoStatusReady, false);
  assert.equal(panel._neighborInfoStatus.text.includes(sentinel), false);
  assert.doesNotMatch(panel._neighborInfoStatus.text, /zero|0 neighbors/i);
  assert.match(panel._neighborInfoStatus.text, /may have been sent|do not retry/i);
"""
    )


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


def test_traceroute_filters_self_and_cached_only_nodes_for_selected_gateway() -> None:
    """The UI must not offer targets the active BLE provider will reject."""
    _run_panel_script(
        r"""
  const gateway = {
    gateway_id: "ble-gateway",
    name: "BLE gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
    local_node_id: "!aaaaaaaa",
  };
  const node = (id, observed) => ({
    node_key: `meshtastic:${id}`,
    node_id: id,
    protocol: "meshtastic",
    identity_valid: true,
    observed_this_session: observed,
    long_name: id,
    last_heard: null,
  });
  const candidates = panel._tracerouteNodeCandidates([
    node("!aaaaaaaa", true),
    node("!1234abcd", false),
    node("!87654321", true),
  ], gateway);

  assert.deepEqual(candidates.map((item) => item.node_id), ["!87654321"]);
  const html = panel._traceroutePanel([
    node("!aaaaaaaa", true),
    node("!1234abcd", false),
    node("!87654321", true),
  ], [gateway]);
  assert.doesNotMatch(html, /meshtastic:!aaaaaaaa/);
  assert.doesNotMatch(html, /meshtastic:!1234abcd/);
  assert.match(html, /meshtastic:!87654321/);
"""
    )


def test_neighbor_info_filters_self_and_cached_only_nodes_for_selected_gateway() -> None:
    """The panel and node-row shortcut expose only live remote targets."""
    _run_panel_script(
        r"""
  const gateway = {
    gateway_id: "ble-gateway",
    name: "BLE gateway",
    protocol: "meshtastic",
    transport: "bluetooth",
    connected: true,
    local_node_id: "!aaaaaaaa",
  };
  const node = (id, observed) => ({
    node_key: `meshtastic:${id}`,
    node_id: id,
    protocol: "meshtastic",
    identity_valid: true,
    observed_this_session: observed,
    long_name: id,
    last_heard: null,
  });
  const local = node("!aaaaaaaa", true);
  const cached = node("!1234abcd", false);
  const remote = node("!87654321", true);
  const candidates = panel._neighborInfoNodeCandidates(
    [local, cached, remote],
    gateway,
  );

  assert.deepEqual(candidates.map((item) => item.node_id), ["!87654321"]);
  const html = panel._neighborInfoPanel([local, cached, remote], [gateway]);
  assert.doesNotMatch(html, /meshtastic:!aaaaaaaa/);
  assert.doesNotMatch(html, /meshtastic:!1234abcd/);
  assert.match(html, /meshtastic:!87654321/);

  panel._safeRender = () => true;
  panel._snapshot = {
    nodes: { local, cached, remote },
    gateways: { "ble-gateway": gateway },
  };
  panel._neighborInfoGatewayId = "ble-gateway";
  assert.equal(panel._openNeighborInfoForNode(local.node_key), false);
  assert.equal(panel._openNeighborInfoForNode(cached.node_key), false);
  assert.equal(panel._openNeighborInfoForNode(remote.node_key), true);
  assert.equal(panel._neighborInfoTargetNode, remote.node_key);
"""
    )


def test_neighbor_info_failure_phase_preserves_only_authoritative_status() -> None:
    """No-RF failures never invent cooldowns; unknown RF outcomes re-lock."""
    _run_panel_script(
        r"""
  panel._safeRender = () => true;
  panel._queuePanelReport = () => {};
  const target = "meshtastic:!1234abcd";
  const available = {
    schema_version: 1,
    scope: "integration_and_target",
    target_node: target,
    status: "available",
    global_remaining_seconds: 0,
    target_remaining_seconds: 0,
    remaining_seconds: 0,
    next_allowed_at: null,
    gateway_id: null,
    reserved_at: null,
    result_updated_at: null,
    result: null,
    loaded_at_ms: Date.now(),
  };
  const dispatch = async (code) => {
    panel._neighborInfoStatusReady = true;
    panel._neighborInfoStatusTarget = target;
    panel._neighborInfoStatusData = available;
    panel._neighborInfoConfirmation = {
      gateway_id: "ble-gateway",
      target_node: target,
    };
    const error = new Error("private node !deadbeef /dev/secret");
    error.code = code;
    panel._hass = { async callWS() { throw error; } };
    await panel._requestNeighborInfo("ble-gateway", target);
    return panel._neighborInfoStatus.text;
  };

  let text = await dispatch("neighbor_info_target_self");
  assert.equal(panel._neighborInfoStatusData, available);
  assert.equal(panel._neighborInfoStatusReady, true);
  assert.match(text, /No RF request was sent/i);
  assert.doesNotMatch(text, /deadbeef|\/dev\/secret/i);

  text = await dispatch("neighbor_info_timeout");
  assert.equal(panel._neighborInfoStatusData, null);
  assert.equal(panel._neighborInfoStatusReady, false);
  assert.match(text, /not retried|reload persisted status/i);

  text = await dispatch("neighbor_info_cooldown");
  assert.equal(panel._neighborInfoStatusData, null);
  assert.equal(panel._neighborInfoStatusReady, false);
  assert.match(text, /persisted.*cooldown|reload persisted status/i);

  const requestReport = panel._backendPanelReport({
    operation: "neighbor_info_request",
    category: "timeout",
    error_type: "Error",
    error_code: "neighbor_info_timeout",
    occurrence: 1,
    consecutive: 1,
  });
  const statusReport = panel._backendPanelReport({
    operation: "neighbor_info_status",
    category: "websocket",
    error_type: "Error",
    error_code: "neighbor_info_status_failed",
    occurrence: 1,
    consecutive: 1,
  });
  assert.equal(requestReport.operation, "neighbor_info_request");
  assert.equal(requestReport.error_code, "neighbor_info_timeout");
  assert.equal(statusReport.operation, "neighbor_info_status");
  assert.equal(statusReport.error_code, "neighbor_info_status_failed");
"""
    )


def test_traceroute_status_then_confirmation_dispatches_provider_once() -> None:
    """One successful persisted preflight gates exactly one confirmed request."""
    _run_panel_script(
        r"""
  const requests = [];
  panel._safeRender = () => true;
  panel._hass = {
    async callWS(payload) {
      requests.push(structuredClone(payload));
      if (payload.type === "meshnet/traceroute/status") {
        return {
          schema_version: 1,
          scope: "integration",
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
      }
      assert.equal(payload.type, "meshnet/traceroute");
      return {
        schema_version: 1,
        status: "complete",
        gateway_id: "ble-gateway",
        destination: "meshtastic:!1234abcd",
        correlation_id: "trace-once",
        source: "meshtastic:!aaaaaaaa",
        channel: 0,
        forward_route: [
          "meshtastic:!aaaaaaaa",
          "meshtastic:!1234abcd",
        ],
      };
    },
  };

  await panel._loadTracerouteStatus();
  await panel._requestTraceroute("ble-gateway", "meshtastic:!1234abcd");
  assert.equal(
    requests.filter((item) => item.type === "meshnet/traceroute").length,
    0,
    "the first click only prepares confirmation",
  );
  await panel._requestTraceroute("ble-gateway", "meshtastic:!1234abcd");

  assert.equal(
    requests.filter((item) => item.type === "meshnet/traceroute/status").length,
    1,
  );
  assert.equal(
    requests.filter((item) => item.type === "meshnet/traceroute").length,
    1,
  );
  assert.equal(
    panel._tracerouteResults["meshtastic:!1234abcd"].correlation_id,
    "trace-once",
  );
"""
    )


def test_traceroute_preflight_failure_does_not_invent_an_rf_cooldown() -> None:
    """Stable server preflight errors state that no radio packet was sent."""
    _run_panel_script(
        r"""
  const error = new Error("private provider detail");
  error.code = "traceroute_target_self";
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
  panel._tracerouteConfirmation = {
    gateway_id: "ble-gateway",
    target_node: "meshtastic:!aaaaaaaa",
  };
  let sends = 0;
  panel._hass = { async callWS(payload) {
    assert.equal(payload.type, "meshnet/traceroute");
    sends += 1;
    throw error;
  } };

  await panel._requestTraceroute("ble-gateway", "meshtastic:!aaaaaaaa");

  assert.equal(sends, 1);
  assert.equal(panel._tracerouteGlobalStatus.status, "available");
  assert.equal(panel._tracerouteGlobalStatus.reserved, false);
  assert.equal(panel._tracerouteStatusReady, true);
  assert.match(panel._tracerouteStatus.text, /cannot traceroute itself|remote node/i);
  assert.doesNotMatch(panel._tracerouteStatus.text, /may have been sent/i);
"""
    )


def test_shared_neighbor_info_airtime_blocks_traceroute_without_fake_identity() -> None:
    """The traceroute panel accepts a shared cooldown with no invented target."""
    _run_panel_script(
        r"""
  const status = panel._sanitizeTracerouteStatus({
    schema_version: 1,
    scope: "integration",
    status: "cooldown",
    reserved: true,
    gateway_id: null,
    target_node: null,
    reserved_at: "2026-08-01T12:00:00+00:00",
    next_allowed_at: "2026-08-01T12:01:00+00:00",
    remaining_seconds: 59,
    result_updated_at: null,
    result: null,
    airtime_operation: "neighbor_info",
  });
  assert.equal(status.airtime_operation, "neighbor_info");
  assert.equal(status.gateway_id, null);
  panel._tracerouteGlobalStatus = status;
  assert.match(panel._tracerouteGlobalStatusPanel(), /Shared metadata cooldown active/);
  assert.match(panel._tracerouteGlobalStatusPanel(), /NeighborInfo request/);

  panel._snapshot = { panel_metadata: { maintenance: {
    enabled: false,
  } } };
  assert.match(panel._maintenanceStatusPanel(), /Automatic maintenance is off/);
  assert.match(panel._maintenanceStatusPanel(), /automatic retries remain disabled/i);
"""
    )
