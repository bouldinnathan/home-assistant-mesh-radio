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
    assert 'button.getAttribute("data-message-node")' in source


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
    total_node_count: 305,
    analyzed_node_count: 305,
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
  }, Array.from({ length: 305 }, () => ({})));
  assert.match(html, /305 total · 24 recent · 175 located/);
  assert.match(html, /191 gateway-reported · 114 retained cache only/);
  assert.match(html, /stored node database/);
  assert.match(html, /does not mean they were directly heard this session/);
  assert.match(html, /42 yes · 114 unknown/);
  assert.match(html, /does not mean this MeshNet gateway currently uses MQTT/);
  assert.match(html, /163 not recently seen/);
  assert.match(html, /2 groups · 4 nodes/);
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
