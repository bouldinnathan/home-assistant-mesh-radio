"""Acceptance tests for the advanced Messages and mesh graph panel views.

These tests intentionally describe the public, dependency-free frontend
contract before the corresponding runtime implementation is added.  They run
the real panel source under Node with only small browser doubles; no radio,
network, Home Assistant service, or provider object is available to them.
"""

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
    """Load the custom element under Node and execute an async assertion body."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = f"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

globalThis.HTMLElement = class {{}};
globalThis.window = {{
  setTimeout() {{ return 1; }},
  clearTimeout() {{}},
  requestAnimationFrame() {{ return 1; }},
  cancelAnimationFrame() {{}},
  addEventListener() {{}},
  removeEventListener() {{}},
  matchMedia() {{ return {{ matches: false }}; }},
}};
let PanelClass = null;
let registeredPanel = null;
globalThis.customElements = {{
  get(name) {{ return name === "meshnet-panel" ? registeredPanel : null; }},
  define(_name, constructor) {{
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


def test_messages_tab_is_a_first_class_view() -> None:
    """Expose Messages beside Mesh and settings without browser persistence."""
    source = _source()

    assert 'data-meshnet-view="messages"' in source
    assert 'type: "meshnet/messages"' in source
    assert "_renderMessages" in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source

    _run_panel_script(
        r"""
  panel._safeRender = () => true;
  panel._loadMessages = async () => [];
  panel._switchView("messages");
  assert.equal(panel._activeView, "messages");
  assert.match(panel._viewTabs(), /data-meshnet-view="messages"/);
  assert.match(panel._viewTabs(), />Messages</);
"""
    )


def test_message_history_load_is_bounded_and_skips_malformed_records() -> None:
    """Load full safe history and quarantine isolated malformed records."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._loadMessages, "function");
  assert.equal(typeof panel._validateMessagesResponse, "function");

  const base = {
    protocol: "meshtastic",
    gateway_id: "gateway-one",
    sender: "!00000001",
    receiver: "!ffffffff",
    channel: "0",
    text: "hello",
    message_type: "broadcast",
    priority: "normal",
    encrypted: false,
    hops: 0,
    timestamp: "2026-07-30T12:00:00+00:00",
    direction: "rx",
    delivery: "broadcast",
    peer_node_key: null,
    raw: {},
  };
  const records = [
    { ...base, message_id: "broadcast" },
    { ...base, message_id: "channel", channel: "3", delivery: "channel" },
    {
      ...base,
      message_id: "direct",
      receiver: "!00000002",
      delivery: "direct",
      peer_node_key: "meshtastic:!00000001",
    },
    { ...base, message_id: "unknown", delivery: "unknown", receiver: null },
    {
      ...base,
      message_id: "escaped-later",
      text: '<img src=x onerror="globalThis.pwned=true">',
    },
    null,
    [],
    { ...base, message_id: "" },
    { ...base, message_id: "bad-channel", channel: "8", delivery: "channel" },
    { ...base, message_id: "bad-direction", direction: "sideways" },
    { ...base, message_id: "bad-time", timestamp: "not-a-time" },
    { ...base, message_id: "bad-raw", raw: [] },
    { ...base, message_id: "oversized", text: "é".repeat(119) },
    { ...base, message_id: "bad-unicode", text: "\ud800" },
    {
      ...base,
      message_id: "unproven-direct",
      delivery: "direct",
      peer_node_key: null,
    },
  ];
  const calls = [];
  panel._hass = {
    callWS: async (payload) => {
      calls.push(payload);
      return records;
    },
  };
  panel._draft.message = "draft survives a history read";
  panel._messageConversation = "channel:3";

  const loaded = await panel._loadMessages(500);
  assert.deepEqual(calls, [{ type: "meshnet/messages", limit: 500 }]);
  assert.equal(Array.isArray(loaded), true);
  assert.deepEqual(
    loaded.map((message) => message.message_id),
    ["broadcast", "channel", "direct", "unknown", "escaped-later"],
  );
  assert.deepEqual(panel._messages, loaded);
  assert.equal(panel._draft.message, "draft survives a history read");
  assert.equal(panel._messageConversation, "channel:3");
  assert.equal(globalThis.pwned, undefined);
"""
    )


def test_message_history_ignores_a_stale_out_of_order_response() -> None:
    """A late history request cannot replace a newer conversation snapshot."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._loadMessages, "function");
  let resolveOld;
  let resolveNew;
  const oldResponse = new Promise((resolve) => { resolveOld = resolve; });
  const newResponse = new Promise((resolve) => { resolveNew = resolve; });
  const pending = [oldResponse, newResponse];
  panel._hass = { callWS: async () => pending.shift() };

  const record = (message_id, text) => ({
    message_id,
    protocol: "meshtastic",
    gateway_id: "gateway-one",
    sender: "!00000001",
    receiver: "!ffffffff",
    channel: "0",
    text,
    message_type: "broadcast",
    priority: "normal",
    encrypted: false,
    hops: 0,
    timestamp: "2026-07-30T12:00:00+00:00",
    direction: "rx",
    delivery: "broadcast",
    peer_node_key: null,
    raw: {},
  });

  const first = panel._loadMessages(100);
  const second = panel._loadMessages(100);
  resolveNew([record("new", "new")]);
  await second;
  resolveOld([record("old", "old")]);
  await first;

  assert.deepEqual(panel._messages.map((message) => message.message_id), ["new"]);
"""
    )


def test_message_conversations_group_delivery_without_merging_duplicate_names() -> None:
    """Conversation keys remain exact routing identities, never display names."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._messageConversations, "function");
  assert.equal(typeof panel._messageConversationKey, "function");

  const nodes = [
    {
      node_key: "meshtastic:!00000001",
      node_id: "!00000001",
      protocol: "meshtastic",
      long_name: "Same Name",
      short_name: "SAME",
    },
    {
      node_key: "meshtastic:!00000002",
      node_id: "!00000002",
      protocol: "meshtastic",
      long_name: "Same Name",
      short_name: "SAME",
    },
  ];
  const message = (id, delivery, channel, peer = null) => ({
    message_id: id,
    protocol: "meshtastic",
    gateway_id: "gateway-one",
    sender: peer || "!00000001",
    receiver: delivery === "broadcast" || delivery === "channel" ? "!ffffffff" : "!00000009",
    channel,
    text: id,
    message_type: delivery === "direct" ? "direct" : "broadcast",
    priority: "normal",
    encrypted: false,
    hops: 0,
    timestamp: "2026-07-30T12:00:00+00:00",
    direction: "rx",
    delivery,
    peer_node_key: peer,
    raw: {},
  });
  const messages = [
    message("primary", "broadcast", "0"),
    message("secondary", "channel", "3"),
    message("direct-one", "direct", "0", "meshtastic:!00000001"),
    message("direct-two", "direct", "0", "meshtastic:!00000002"),
    message("unknown", "unknown", null),
  ];

  assert.equal(panel._messageConversationKey(messages[0]), "broadcast:0");
  assert.equal(panel._messageConversationKey(messages[1]), "channel:3");
  assert.equal(
    panel._messageConversationKey(messages[2]),
    "direct:meshtastic:!00000001",
  );
  assert.equal(panel._messageConversationKey(messages[4]), "unknown");

  const conversations = panel._messageConversations(messages, nodes);
  const byKey = new Map(conversations.map((conversation) => [conversation.key, conversation]));
  assert.deepEqual(
    [...byKey.keys()].sort(),
    [
      "broadcast:0",
      "channel:3",
      "direct:meshtastic:!00000001",
      "direct:meshtastic:!00000002",
      "unknown",
    ].sort(),
  );
  assert.equal(byKey.get("broadcast:0").label, "Broadcast / Primary");
  assert.equal(byKey.get("channel:3").label, "Channel 3");
  assert.equal(byKey.get("unknown").label, "Unknown delivery");
  const first = byKey.get("direct:meshtastic:!00000001");
  const second = byKey.get("direct:meshtastic:!00000002");
  assert.notEqual(first.label, second.label);
  assert.match(first.label, /!00000001/);
  assert.match(second.label, /!00000002/);
  assert.deepEqual(first.messages.map((item) => item.message_id), ["direct-one"]);
  assert.deepEqual(second.messages.map((item) => item.message_id), ["direct-two"]);

  const empty = panel._messageConversations([], nodes);
  assert.deepEqual(empty.map((conversation) => conversation.key), ["broadcast:0"]);
  assert.deepEqual(empty[0].messages, []);
"""
    )


def test_messages_render_escapes_text_names_and_metadata() -> None:
    """No message-controlled value may become executable panel markup."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._renderMessages, "function");
  panel._activeView = "messages";
  panel._messageConversation = "direct:meshtastic:!00000001";
  panel._messages = [{
    message_id: "message-1",
    protocol: "meshtastic",
    gateway_id: '<svg onload="globalThis.pwned=true">',
    sender: "!00000001",
    receiver: "!00000009",
    channel: "0",
    text: '<img src=x onerror="globalThis.pwned=true">',
    message_type: "direct",
    priority: "normal",
    encrypted: false,
    hops: 0,
    timestamp: "2026-07-30T12:00:00+00:00",
    direction: "rx",
    delivery: "direct",
    peer_node_key: "meshtastic:!00000001",
    raw: {},
  }];
  panel._snapshot = {
    nodes: {
      peer: {
        node_key: "meshtastic:!00000001",
        node_id: "!00000001",
        protocol: "meshtastic",
        long_name: '<script>globalThis.pwned=true</script>',
        short_name: '<b onclick="globalThis.pwned=true">BAD</b>',
      },
    },
    gateways: {},
    recent_messages: [],
  };
  panel.querySelector = () => null;
  panel.querySelectorAll = () => [];
  panel.contains = () => false;
  panel._renderMessages();

  assert.equal(globalThis.pwned, undefined);
  assert.doesNotMatch(panel.innerHTML, /<img src=x/);
  assert.doesNotMatch(panel.innerHTML, /<script>/);
  assert.doesNotMatch(panel.innerHTML, /<b onclick=/);
  assert.doesNotMatch(panel.innerHTML, /<svg onload=/);
  assert.match(panel.innerHTML, /&lt;img/);
  assert.match(panel.innerHTML, /&lt;script&gt;/);
"""
    )


def test_message_draft_focus_and_thread_survive_poll_but_clear_on_detach() -> None:
    """Polling preserves active work; leaving the panel abandons memory state."""
    _run_panel_script(
        r"""
  panel._activeView = "messages";
  panel._connected = true;
  panel._draft.message = "unsent draft";
  panel._messageConversation = "direct:meshtastic:!00000001";
  panel._messages = [{ message_id: "kept" }];
  const active = {
    id: "meshnet-message-conversation",
    selectionStart: 0,
    selectionEnd: 0,
  };
  panel._activePanelElement = () => active;
  panel.contains = (element) => element === active;
  let renders = 0;
  panel._safeRender = () => { renders += 1; return true; };

  const focus = panel._composerFocusState();
  assert.equal(focus.id, "meshnet-message-conversation");
  assert.equal(panel._renderPollSnapshot(), false);
  assert.equal(panel._pollRenderPending, true);
  assert.equal(renders, 0);
  assert.equal(panel._draft.message, "unsent draft");
  assert.equal(panel._messageConversation, "direct:meshtastic:!00000001");

  panel._detachWindowFailureHandlers = () => {};
  panel._stopGraphAnimation = () => {};
  panel.disconnectedCallback();
  assert.equal(panel._draft.message, "");
  assert.equal(panel._messageConversation, "broadcast:0");
  assert.deepEqual(panel._messages, []);
"""
    )


def test_graph_limit_accepts_only_20_50_100_and_selects_recent_nodes() -> None:
    """Graph selection is bounded and deterministic by valid last-heard time."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._normalizeGraphLimit, "function");
  assert.equal(typeof panel._recentGraphNodes, "function");
  assert.equal(panel._graphLimit, 50);
  assert.equal(panel._normalizeGraphLimit(20), 20);
  assert.equal(panel._normalizeGraphLimit("20"), 20);
  assert.equal(panel._normalizeGraphLimit(50), 50);
  assert.equal(panel._normalizeGraphLimit("100"), 100);
  for (const invalid of [0, 19, 21, 36, 101, true, null, undefined, "all", "50.0"]) {
    assert.equal(panel._normalizeGraphLimit(invalid), 50);
  }

  const nodes = Array.from({ length: 110 }, (_value, index) => ({
    node_key: `meshtastic:!${index.toString(16).padStart(8, "0")}`,
    long_name: `Node ${index}`,
    last_heard: new Date(Date.UTC(2026, 6, 30, 0, index)).toISOString(),
  }));
  nodes.push({ node_key: "invalid-time", long_name: "A", last_heard: "bad" });
  nodes.push({ node_key: "missing-time", long_name: "B", last_heard: null });
  const original = nodes.map((node) => node.node_key);

  const recent20 = panel._recentGraphNodes(nodes, 20);
  assert.equal(recent20.length, 20);
  assert.equal(recent20[0].long_name, "Node 109");
  assert.equal(recent20[19].long_name, "Node 90");
  assert.deepEqual(nodes.map((node) => node.node_key), original);
  assert.equal(panel._recentGraphNodes(nodes, 50).length, 50);
  assert.equal(panel._recentGraphNodes(nodes, 100).length, 100);

  const tied = [
    { node_key: "z", long_name: "Same", last_heard: "2026-07-30T12:00:00Z" },
    { node_key: "a", long_name: "Same", last_heard: "2026-07-30T12:00:00Z" },
  ];
  assert.deepEqual(
    panel._recentGraphNodes(tied, 20).map((node) => node.node_key),
    ["a", "z"],
  );
"""
    )


def test_haversine_and_spring_mapping_are_finite_monotonic_and_clamped() -> None:
    """GPS distance affects bounded spring length; invalid data is neutral."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._haversineMeters, "function");
  assert.equal(typeof panel._edgeTargetLength, "function");
  const minneapolis = { latitude: 44.9778, longitude: -93.2650 };
  const nearby = { latitude: 44.9878, longitude: -93.2650 };
  const chicago = { latitude: 41.8781, longitude: -87.6298 };
  const london = { latitude: 51.5074, longitude: -0.1278 };

  assert.equal(panel._haversineMeters(minneapolis, minneapolis), 0);
  const nearbyMeters = panel._haversineMeters(minneapolis, nearby);
  const chicagoMeters = panel._haversineMeters(minneapolis, chicago);
  const londonMeters = panel._haversineMeters(minneapolis, london);
  assert.ok(Number.isFinite(nearbyMeters) && nearbyMeters > 500);
  assert.ok(chicagoMeters > nearbyMeters);
  assert.ok(londonMeters > chicagoMeters);
  for (const invalid of [
    [null, nearby],
    [{ latitude: true, longitude: 0 }, nearby],
    [{ latitude: 91, longitude: 0 }, nearby],
    [{ latitude: 0, longitude: Number.NaN }, nearby],
  ]) {
    assert.equal(panel._haversineMeters(invalid[0], invalid[1]), null);
  }

  const neutral = panel._edgeTargetLength(null);
  assert.ok(Number.isFinite(neutral));
  assert.equal(panel._edgeTargetLength(undefined), neutral);
  assert.equal(panel._edgeTargetLength(Number.NaN), neutral);
  assert.equal(panel._edgeTargetLength(-1), neutral);
  const lengths = [0, 100, 10_000, 1_000_000, 100_000_000]
    .map((distance) => panel._edgeTargetLength(distance));
  lengths.forEach((length) => {
    assert.ok(Number.isFinite(length));
    assert.ok(length >= 40 && length <= 400);
  });
  for (let index = 1; index < lengths.length; index += 1) {
    assert.ok(lengths[index] >= lengths[index - 1]);
  }
  assert.equal(
    panel._edgeTargetLength(1_000_000_000),
    panel._edgeTargetLength(1_000_000_000_000),
  );
"""
    )


def test_distance_annotation_never_invents_edges_or_uses_signal_as_gps() -> None:
    """Only route/neighbor evidence creates an edge, regardless of proximity."""
    _run_panel_script(
        r"""
  const gateways = [{
    gateway_id: "gateway-one",
    name: "Gateway",
    connected: true,
    location: { latitude: 44.9778, longitude: -93.2650 },
  }];
  const closeButUnlinked = [
    {
      node_key: "meshcore:a",
      node_id: "a",
      protocol: "meshcore",
      last_heard: "2026-07-30T12:00:00Z",
      location: { latitude: 44.9779, longitude: -93.2650 },
      connectivity: { snr: 12, rssi: -45 },
      routing: {},
    },
    {
      node_key: "meshcore:b",
      node_id: "b",
      protocol: "meshcore",
      last_heard: "2026-07-30T11:59:00Z",
      location: { latitude: 44.9780, longitude: -93.2650 },
      connectivity: { snr: -20, rssi: -130 },
      routing: {},
    },
  ];
  assert.equal(panel._passiveTopology(closeButUnlinked, gateways, 20).edges.length, 0);

  const withEvidence = [
    {
      node_key: "meshtastic:near",
      node_id: "near",
      protocol: "meshtastic",
      last_heard: "2026-07-30T12:00:00Z",
      last_gateway_id: "gateway-one",
      gateway_ids: ["gateway-one"],
      location: { latitude: 44.9878, longitude: -93.2650 },
      connectivity: {
        hops: 0,
        hops_gateway_id: "gateway-one",
        via_mqtt: false,
        snr: -20,
        rssi: -130,
      },
      routing: {},
    },
    {
      node_key: "meshtastic:far",
      node_id: "far",
      protocol: "meshtastic",
      last_heard: "2026-07-30T11:59:00Z",
      last_gateway_id: "gateway-one",
      gateway_ids: ["gateway-one"],
      location: { latitude: 41.8781, longitude: -87.6298 },
      connectivity: {
        hops: 0,
        hops_gateway_id: "gateway-one",
        via_mqtt: false,
        snr: 12,
        rssi: -45,
      },
      routing: {},
    },
  ];
  const topology = panel._passiveTopology(withEvidence, gateways, 20);
  assert.equal(topology.edges.length, 2);
  const near = topology.edges.find((edge) => edge.to === "node:meshtastic:near");
  const far = topology.edges.find((edge) => edge.to === "node:meshtastic:far");
  assert.ok(Number.isFinite(near.distance_meters));
  assert.ok(Number.isFinite(far.distance_meters));
  assert.ok(far.distance_meters > near.distance_meters);
  assert.ok(far.target_length > near.target_length);

  const withoutGps = withEvidence.map((node) => ({ ...node, location: {} }));
  const signalOnly = panel._passiveTopology(withoutGps, gateways.map((item) => ({ ...item, location: {} })), 20);
  assert.equal(signalOnly.edges[0].distance_meters, null);
  assert.equal(signalOnly.edges[1].distance_meters, null);
  assert.equal(signalOnly.edges[0].target_length, signalOnly.edges[1].target_length);
"""
    )


def test_fresh_non_mqtt_neighbor_evidence_adds_one_distance_sized_deduplicated_edge() -> None:
    """NeighborInfo is passive RF evidence, not distance or an RF trigger."""
    _run_panel_script(
        r"""
  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-07-30T12:00:00+00:00");
  let websocketCalls = 0;
  panel._hass = {
    callWS: async () => { websocketCalls += 1; throw new Error("unexpected websocket call"); },
  };
  const baseNodes = [
    {
      node_key: "meshtastic:!00000001",
      node_id: "!00000001",
      protocol: "meshtastic",
      last_heard: "2026-07-30T11:59:00+00:00",
      location: { latitude: 44.9778, longitude: -93.2650 },
      connectivity: {},
      routing: {
        neighbors: ["!00000002", "!00000002", "!00000009"],
        neighbor_count: 3,
        neighbors_updated_at: "2026-07-30T11:45:00+00:00",
        neighbors_via_mqtt: false,
      },
    },
    {
      node_key: "meshtastic:!00000002",
      node_id: "!00000002",
      protocol: "meshtastic",
      last_heard: "2026-07-30T11:58:00+00:00",
      location: { latitude: 41.8781, longitude: -87.6298 },
      connectivity: {},
      routing: {
        neighbors: ["!00000001"],
        neighbor_count: 1,
        neighbors_updated_at: "2026-07-30T11:50:00+00:00",
        neighbors_via_mqtt: false,
      },
    },
  ];

  const topology = panel._passiveTopology(baseNodes, [], 20);
  assert.equal(topology.edges.length, 1, "reciprocal and repeated evidence must deduplicate");
  assert.equal(topology.edges[0].type, "neighbor");
  assert.deepEqual(
    [topology.edges[0].from, topology.edges[0].to].sort(),
    ["node:meshtastic:!00000001", "node:meshtastic:!00000002"].sort(),
  );
  assert.ok(Number.isFinite(topology.edges[0].distance_meters));
  assert.ok(topology.edges[0].distance_meters > 0);
  assert.equal(
    topology.edges[0].target_length,
    panel._edgeTargetLength(topology.edges[0].distance_meters),
  );
  assert.equal(websocketCalls, 0);

  const withoutGps = baseNodes.map((node) => ({ ...node, location: {} }));
  const neutralTopology = panel._passiveTopology(withoutGps, [], 20);
  assert.equal(neutralTopology.edges.length, 1);
  assert.equal(neutralTopology.edges[0].distance_meters, null);
  assert.equal(neutralTopology.edges[0].target_length, panel._edgeTargetLength(null));

  const topologyFor = (routing) => panel._passiveTopology([
    { ...baseNodes[0], routing },
    { ...baseNodes[1], routing: {} },
  ], [], 20);
  const fresh = {
    neighbors: ["!00000002"],
    neighbor_count: 1,
    neighbors_updated_at: "2026-07-30T11:01:00+00:00",
    neighbors_via_mqtt: false,
  };
  assert.equal(topologyFor(fresh).edges.length, 1, "evidence up to one hour old is fresh");
  assert.equal(topologyFor({
    ...fresh,
    neighbors_updated_at: "2026-07-30T10:59:59+00:00",
  }).edges.length, 0, "older evidence is stale");
  assert.equal(topologyFor({
    ...fresh,
    neighbors_updated_at: "2026-07-30T12:05:00+00:00",
  }).edges.length, 1, "five minutes of browser/server clock skew is tolerated");
  assert.equal(topologyFor({
    ...fresh,
    neighbors_updated_at: "2026-07-30T12:05:01+00:00",
  }).edges.length, 0, "larger future timestamps are rejected");
  assert.equal(topologyFor({ ...fresh, neighbors_via_mqtt: true }).edges.length, 0);
  assert.equal(topologyFor({ ...fresh, neighbor_count: 2 }).edges.length, 0);
  const { neighbors_via_mqtt: _omitted, ...unknownProvenance } = fresh;
  assert.equal(topologyFor(unknownProvenance).edges.length, 0);
  assert.equal(topologyFor({ ...fresh, neighbors: ["!2"] }).edges.length, 0);
  assert.equal(topologyFor({ ...fresh, neighbors: ["!00000009"] }).edges.length, 0);
  assert.equal(topologyFor({ ...fresh, neighbors_updated_at: "not-a-time" }).edges.length, 0);

  const ambiguous = [
    ...baseNodes,
    {
      ...baseNodes[1],
      node_key: "meshtastic-proof:" + "a".repeat(64),
    },
  ];
  assert.equal(
    panel._passiveTopology(ambiguous, [], 20).edges.length,
    0,
    "an alias shared by different node records is not exact enough to link",
  );
  Date.now = originalNow;
"""
    )


def test_gateway_graph_location_prefers_exact_radio_gps_then_ha_fallback() -> None:
    """Home location is a labeled browser-only fallback and never mutates data."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._gatewayGraphLocation, "function");
  panel._hass = { config: { latitude: 45.1, longitude: -93.1 } };
  const gateway = {
    gateway_id: "gateway-one",
    local_node_id: "!00000001",
  };
  const nodes = [
    {
      node_key: "meshtastic:!00000002",
      node_id: "!00000002",
      protocol: "meshtastic",
      long_name: "Same gateway-like name",
      location: { latitude: 1, longitude: 2 },
    },
    {
      node_key: "meshtastic:!00000001",
      node_id: "!00000001",
      protocol: "meshtastic",
      location: { latitude: 44.9, longitude: -93.3 },
    },
  ];
  const before = JSON.stringify({ gateway, nodes, config: panel._hass.config });
  assert.deepEqual(panel._gatewayGraphLocation(gateway, nodes), {
    latitude: 44.9,
    longitude: -93.3,
    source: "radio_gps",
    label: "Radio GPS",
  });
  assert.equal(JSON.stringify({ gateway, nodes, config: panel._hass.config }), before);

  const fallback = panel._gatewayGraphLocation(
    { ...gateway, local_node_id: "!00000009" },
    nodes,
  );
  assert.deepEqual(fallback, {
    latitude: 45.1,
    longitude: -93.1,
    source: "home_assistant_fallback",
    label: "Home Assistant location fallback",
  });

  const zeroPair = panel._gatewayGraphLocation(gateway, [{
    node_key: "meshtastic:!00000001",
    node_id: "!00000001",
    protocol: "meshtastic",
    location: { latitude: 0, longitude: 0 },
  }]);
  assert.equal(zeroPair.source, "home_assistant_fallback");

  panel._hass = { config: { latitude: true, longitude: -93.1 } };
  assert.equal(
    panel._gatewayGraphLocation({ ...gateway, local_node_id: "!00000009" }, nodes),
    null,
  );
"""
    )


def test_graph_rejects_explicitly_coarsened_meshtastic_coordinates() -> None:
    """Low precision metadata must not become a misleading physical spring."""
    _run_panel_script(
        r"""
  const node = (precision) => ({
    node_key: "meshtastic:!00000001",
    node_id: "!00000001",
    protocol: "meshtastic",
    location: {
      latitude: 44.9778,
      longitude: -93.2650,
      ...(precision === undefined ? {} : { precision_bits: precision }),
    },
  });
  assert.deepEqual(panel._nodeGraphLocation(node(undefined)), {
    latitude: 44.9778,
    longitude: -93.265,
  });
  assert.equal(panel._nodeGraphLocation(node(0)), null);
  assert.equal(panel._nodeGraphLocation(node(10)), null);
  assert.equal(panel._nodeGraphLocation(node(18)), null);
  assert.deepEqual(panel._nodeGraphLocation(node(19)), {
    latitude: 44.9778,
    longitude: -93.265,
  });
  assert.deepEqual(panel._nodeGraphLocation(node(32)), {
    latitude: 44.9778,
    longitude: -93.265,
  });
  for (const invalid of [-1, 33, 19.5, true, "19", Number.NaN]) {
    assert.equal(panel._nodeGraphLocation(node(invalid)), null);
  }

  panel._hass = { config: { latitude: 45.1, longitude: -93.1 } };
  const gateway = {
    gateway_id: "gateway-one",
    location: {
      latitude: 44.9,
      longitude: -93.3,
      precision_bits: 12,
    },
    local_node_id: "!00000009",
  };
  assert.deepEqual(panel._gatewayGraphLocation(gateway, []), {
    latitude: 45.1,
    longitude: -93.1,
    source: "home_assistant_fallback",
    label: "Home Assistant location fallback",
  });

  const topology = panel._passiveTopology([{
    ...node(12),
    last_gateway_id: "gateway-one",
    gateway_ids: ["gateway-one"],
    connectivity: {
      hops: 0,
      hops_gateway_id: "gateway-one",
      via_mqtt: false,
    },
  }], [{
    gateway_id: "gateway-one",
    connected: true,
    location: { latitude: 44.8, longitude: -93.2 },
  }], 20);
  assert.equal(topology.edges.length, 1);
  assert.equal(topology.edges[0].distance_meters, null);
  assert.equal(topology.edges[0].target_length, panel._edgeTargetLength(null));
"""
    )


def test_force_step_repairs_nonfinite_state_and_keeps_layout_bounded() -> None:
    """One force iteration cannot produce non-finite or off-canvas SVG state."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._forceStep, "function");
  const positions = new Map([
    ["node:a", { x: Number.NaN, y: Number.POSITIVE_INFINITY, vx: 1e99, vy: -1e99 }],
    ["node:b", { x: -5000, y: 9000, vx: -5000, vy: 5000 }],
    ["gateway:g", { x: 50, y: 50, vx: 10, vy: 10, fixed: true }],
  ]);
  const edges = [
    { from: "node:a", to: "node:b", target_length: 120 },
    { from: "gateway:g", to: "node:a", target_length: 80 },
    { from: "missing", to: "node:a", target_length: Number.NaN },
  ];
  const stepped = panel._forceStep(positions, edges, {
    width: 300,
    height: 200,
    padding: 20,
  });
  assert.ok(stepped instanceof Map);
  assert.equal(stepped.size, 3);
  for (const value of stepped.values()) {
    for (const field of ["x", "y", "vx", "vy"]) {
      assert.ok(Number.isFinite(value[field]), `${field} must be finite`);
    }
    assert.ok(value.x >= 20 && value.x <= 280);
    assert.ok(value.y >= 20 && value.y <= 180);
    assert.ok(Math.abs(value.vx) <= 1000);
    assert.ok(Math.abs(value.vy) <= 1000);
  }
  assert.equal(stepped.get("gateway:g").x, 50);
  assert.equal(stepped.get("gateway:g").y, 50);
"""
    )


def test_graph_animation_honors_reduced_motion_and_stops_on_lifecycle_changes() -> None:
    """RAF has one owner and is cancelled on reduced motion, detach, or view change."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._startGraphAnimation, "function");
  assert.equal(typeof panel._stopGraphAnimation, "function");
  const callbacks = new Map();
  const cancelled = [];
  let nextFrame = 1;
  window.requestAnimationFrame = (callback) => {
    const id = nextFrame++;
    callbacks.set(id, callback);
    return id;
  };
  window.cancelAnimationFrame = (id) => {
    cancelled.push(id);
    callbacks.delete(id);
  };
  window.matchMedia = () => ({ matches: false });
  panel._connected = true;
  panel._activeView = "mesh";
  panel._graphPositions = new Map([
    ["node:a", { x: 50, y: 50, vx: 0, vy: 0 }],
  ]);
  panel._applyGraphPositions = () => {};
  const topology = { nodes: [{ node_key: "a" }], gateways: [], edges: [] };

  panel._startGraphAnimation(topology);
  assert.equal(callbacks.size, 1);
  const firstId = [...callbacks.keys()][0];
  const firstCallback = callbacks.get(firstId);
  callbacks.delete(firstId);
  firstCallback(16);
  assert.equal(callbacks.size, 1);
  panel._stopGraphAnimation();
  assert.equal(callbacks.size, 0);
  assert.ok(cancelled.length >= 1);
  assert.equal(panel._graphAnimationFrame, null);

  window.matchMedia = () => ({ matches: true });
  callbacks.clear();
  panel._startGraphAnimation(topology);
  assert.equal(callbacks.size, 0, "reduced motion must not continuously animate");

  let stops = 0;
  panel._stopGraphAnimation = () => { stops += 1; };
  panel._detachWindowFailureHandlers = () => {};
  panel.disconnectedCallback();
  assert.equal(stops, 1);

  panel._connected = true;
  panel._activeView = "mesh";
  panel._safeRender = () => true;
  panel._loadMessages = async () => [];
  panel._switchView("messages");
  assert.equal(stops, 2);
"""
    )


def test_graph_drag_is_pointer_bounded_and_has_removable_listeners() -> None:
    """Dragging changes only in-memory coordinates and provides exact cleanup."""
    _run_panel_script(
        r"""
  assert.equal(typeof panel._bindGraphDrag, "function");
  const listeners = new Map();
  const removed = [];
  const svg = {
    viewBox: { baseVal: { x: 0, y: 0, width: 1000, height: 640 } },
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name, callback) {
      removed.push([name, callback]);
      if (listeners.get(name) === callback) listeners.delete(name);
    },
    getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 640 }; },
    setPointerCapture() {},
    releasePointerCapture() {},
  };
  panel._graphPositions = new Map([
    ["node:a", { x: 100, y: 100, vx: 5, vy: -5 }],
  ]);
  const cleanup = panel._bindGraphDrag(svg);
  for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel"]) {
    assert.equal(typeof listeners.get(name), "function", `${name} listener missing`);
  }
  const graphNode = {
    dataset: { graphKey: "node:a" },
    getAttribute(name) { return name === "data-graph-key" ? "node:a" : null; },
  };
  const event = (x, y) => ({
    clientX: x,
    clientY: y,
    pointerId: 7,
    preventDefault() {},
    target: { closest: () => graphNode },
  });
  listeners.get("pointerdown")(event(100, 100));
  listeners.get("pointermove")(event(250, 300));
  const moved = panel._graphPositions.get("node:a");
  assert.ok(Number.isFinite(moved.x) && Number.isFinite(moved.y));
  assert.ok(moved.x >= 0 && moved.x <= 1000);
  assert.ok(moved.y >= 0 && moved.y <= 640);
  assert.notDeepEqual([moved.x, moved.y], [100, 100]);
  assert.equal(moved.vx, 0);
  assert.equal(moved.vy, 0);
  listeners.get("pointerup")(event(250, 300));
  assert.equal(panel._graphDrag, null);

  assert.equal(typeof cleanup, "function");
  cleanup();
  assert.equal(listeners.size, 0);
  assert.equal(removed.length, 4);
"""
    )


def test_graph_filter_animation_and_drag_make_zero_transport_calls() -> None:
    """Pure graph interaction cannot invoke WebSocket, service, or provider APIs."""
    _run_panel_script(
        r"""
  let websocketCalls = 0;
  let serviceCalls = 0;
  panel._hass = {
    config: { latitude: 45, longitude: -93 },
    callWS: async () => { websocketCalls += 1; throw new Error("unexpected websocket call"); },
    callService: async () => { serviceCalls += 1; throw new Error("unexpected service call"); },
  };
  const nodes = [{
    node_key: "meshtastic:a",
    node_id: "a",
    protocol: "meshtastic",
    last_heard: "2026-07-30T12:00:00Z",
    location: { latitude: 45.1, longitude: -93.1 },
    connectivity: {},
    routing: {},
  }];
  panel._normalizeGraphLimit("20");
  panel._recentGraphNodes(nodes, 20);
  const topology = panel._passiveTopology(nodes, [], 20);
  panel._graphPositions = new Map([
    ["node:meshtastic:a", { x: 100, y: 100, vx: 0, vy: 0 }],
  ]);
  panel._forceStep(panel._graphPositions, topology.edges, {
    width: 1000,
    height: 640,
    padding: 20,
  });

  const listeners = new Map();
  const svg = {
    viewBox: { baseVal: { x: 0, y: 0, width: 1000, height: 640 } },
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
    getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 640 }; },
    setPointerCapture() {},
    releasePointerCapture() {},
  };
  const cleanup = panel._bindGraphDrag(svg);
  cleanup();
  assert.equal(websocketCalls, 0);
  assert.equal(serviceCalls, 0);
"""
    )
