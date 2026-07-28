"""Behavior and safety checks for the dependency-free MeshNet panel."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

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
globalThis.customElements = {{
  define(_name, constructor) {{ PanelClass = constructor; }},
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
    assert '<a class="map-link" href="/map">Map · ${locatedNodeCount} located</a>' in source
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
    { node_key: "meshtastic:!12345678", short_name: "Alpha" },
  ];
  panel._snapshot = { nodes: { first: nodes[0], second: nodes[1] } };
  const choices = panel._recipientChoices(nodes);
  assert.deepEqual(choices.map((choice) => choice.value), [
    "meshtastic:!12345678",
    "meshcore:key&one",
  ]);
  assert.match(panel._recipientOptions(nodes), /B &lt;unsafe&gt;/);
  assert.match(panel._recipientOptions(nodes), /meshcore:key&amp;one/);
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
    assert 'button.getAttribute("data-message-node")' in source


def test_node_sorting_is_deterministic_and_handles_bad_timestamps() -> None:
    """Favorites and valid recent timestamps sort before stable name/key ties."""
    _run_panel_script(
        r"""
  const nodes = [
    { node_key: "k-old-favorite", long_name: "Zulu", favorite: true, last_heard: "2026-01-01T00:00:00Z" },
    { node_key: "k-new-favorite", long_name: "Beta", favorite: true, last_heard: "2026-01-03T00:00:00Z" },
    { node_key: "k-new", long_name: "Gamma", favorite: false, last_heard: "2026-01-04T00:00:00Z" },
    { node_key: "k-invalid", long_name: "Alpha", favorite: "true", last_heard: "not-a-date" },
  ];
  assert.deepEqual(
    panel._sortNodes(nodes, "favorites_recent").map((node) => node.node_key),
    ["k-new-favorite", "k-old-favorite", "k-new", "k-invalid"],
  );
  assert.deepEqual(
    panel._sortNodes(nodes, "last_seen").map((node) => node.node_key),
    ["k-new", "k-new-favorite", "k-old-favorite", "k-invalid"],
  );
  assert.deepEqual(
    panel._sortNodes(nodes, "name").map((node) => node.node_key),
    ["k-invalid", "k-new-favorite", "k-new", "k-old-favorite"],
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
    assert "this.ownerDocument.activeElement" in source
    assert "this.contains(active)" in source
    assert 'typeof active.selectionStart === "number"' in source
    assert "field.focus()" in source
    assert "field.setSelectionRange(state.start, state.end)" in source
    assert "if (this._sending) return" in source
    assert 'this._sending || (directDelivery && !recipientCount) ? " disabled" : ""' in source
    assert 'if (this._draft.message === draft.message) this._draft.message = ""' in source


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

    assert "snapshot = await this._refreshSnapshot()" in send_method
    assert 'status === "sent"' in send_method
    assert 'text: "Message sent."' in send_method
    assert 'text: "Message queued for delivery."' in send_method
    assert 'text: "Message could not be submitted."' in send_method
    assert "_err.message" not in send_method
    assert "String(_err)" not in send_method
    assert '${this._escape(this._sendStatus ? this._sendStatus.text : "")}' in source
