"""Frontend contracts for the rearrangeable Mesh workspace and node actions."""

from __future__ import annotations

from pathlib import Path

from test_advanced_frontend import _run_panel_script

PANEL = Path("custom_components/meshnet/frontend/meshnet-panel.js")


def test_mesh_card_layout_is_instance_local_bounded_and_storage_free() -> None:
    """Moving/resizing cards survives rerenders without persistent storage or I/O."""
    source = PANEL.read_text(encoding="utf-8")
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "data-mesh-card" in source
    assert "data-mesh-drag-handle" in source
    assert "data-mesh-resize-handle" in source
    assert "max-width: min(${MESH_CARD_MAX_WIDTH}px, 48vw" in source
    assert ".mesh-card-resize-handle { display: none; }" in source
    assert "Layout resets when you leave MeshNet." in source

    _run_panel_script(
        r"""
  assert.equal(typeof panel._moveMeshCard, "function");
  assert.equal(typeof panel._setMeshCardSize, "function");
  assert.ok(Array.isArray(panel._meshCardOrder));
  assert.ok(panel._meshCardSizes instanceof Map);

  assert.equal(typeof panel._orderedMeshCards, "function");
  const requiredCards = [
    "send-message",
    "gateways",
    "remote-admin",
    "traceroute",
    "neighbor-info",
    "panel-diagnostics",
    "nodes",
    "recent-messages",
    "rf-heat",
  ];
  assert.deepEqual(panel._meshCardOrder, requiredCards);
  const initialOrder = [...panel._meshCardOrder];
  let calls = 0;
  panel._hass = {
    async callWS() { calls += 1; throw new Error("layout must be local"); },
    async callService() { calls += 1; throw new Error("layout must be local"); },
  };

  const nodeIndex = panel._meshCardOrder.indexOf("nodes");
  panel._moveMeshCard("nodes", "earlier");
  assert.equal(panel._meshCardOrder.indexOf("nodes"), nodeIndex - 1);
  assert.deepEqual(
    panel._orderedMeshCards(["send-message", "nodes", "gateways"]),
    ["send-message", "gateways", "nodes"],
    "the render helper applies instance order to any available subset",
  );
  assert.equal(new Set(panel._meshCardOrder).size, requiredCards.length);
  assert.deepEqual(
    [...panel._meshCardOrder].sort(),
    [...requiredCards].sort(),
    "moving cannot lose, duplicate, or invent a card",
  );

  panel._setMeshCardSize("nodes", 640, 480);
  assert.deepEqual(panel._meshCardSizes.get("nodes"), {
    width: 640,
    height: 480,
  });
  const acceptedSize = { ...panel._meshCardSizes.get("nodes") };
  for (const invalid of [
    [Number.NaN, 480],
    [640, Number.POSITIVE_INFINITY],
    [-1, 480],
    [640, 0],
    ["640", 480],
  ]) {
    panel._setMeshCardSize("nodes", invalid[0], invalid[1]);
    assert.deepEqual(panel._meshCardSizes.get("nodes"), acceptedSize);
  }
  panel._setMeshCardSize("nodes", 1_000_000, 1_000_000);
  const clamped = panel._meshCardSizes.get("nodes");
  assert.ok(Number.isSafeInteger(clamped.width) && clamped.width <= 720);
  assert.ok(Number.isSafeInteger(clamped.height) && clamped.height <= 1200);

  const beforeInvalidMove = [...panel._meshCardOrder];
  panel._moveMeshCard("not-a-card", "earlier");
  panel._moveMeshCard("nodes", "sideways");
  assert.deepEqual(panel._meshCardOrder, beforeInvalidMove);
  panel._setMeshCardSize("not-a-card", 500, 500);
  assert.equal(panel._meshCardSizes.has("not-a-card"), false);
  assert.equal(calls, 0);

  panel._meshLayoutInteraction = { kind: "drag", card_id: "nodes" };
  assert.equal(panel._panelInteractionActive(), true);
  let renders = 0;
  panel._safeRender = () => { renders += 1; return true; };
  assert.equal(panel._renderPollSnapshot(), false);
  assert.equal(panel._pollRenderPending, true);
  assert.equal(renders, 0, "polling cannot replace a card during layout interaction");
  panel._meshLayoutInteraction = null;

  const secondPanel = new PanelClass();
  assert.deepEqual(secondPanel._meshCardOrder, initialOrder);
  assert.equal(secondPanel._meshCardSizes.size, 0);
  assert.notEqual(secondPanel._meshCardOrder, panel._meshCardOrder);
  assert.notEqual(secondPanel._meshCardSizes, panel._meshCardSizes);

  panel._detachWindowFailureHandlers = () => {};
  panel._stopGraphAnimation = () => {};
  panel._clearSecretSettingsDrafts = () => {};
  panel.disconnectedCallback();
  assert.deepEqual(panel._meshCardOrder, initialOrder);
  assert.equal(panel._meshCardSizes.size, 0);
  assert.equal(panel._meshLayoutInteraction, null);
"""
    )


def test_every_node_target_dropdown_is_favorites_then_last_seen() -> None:
    """Recipient and operator selectors share one deterministic node ordering."""
    _run_panel_script(
        r"""
  const node = (suffix, favorite, lastHeard, name) => ({
    node_key: `meshtastic:!${suffix}`,
    node_id: `!${suffix}`,
    protocol: "meshtastic",
    identity_valid: true,
    favorite,
    last_heard: lastHeard,
    long_name: name,
    short_name: name.slice(0, 4).toUpperCase(),
  });
  const nodes = [
    node("00000001", false, "2026-07-31T12:04:00Z", "Newest nonfavorite"),
    node("00000002", true, "2026-07-31T12:01:00Z", "Older favorite"),
    node("00000003", false, null, "Unknown nonfavorite"),
    node("00000004", true, "2026-07-31T12:03:00Z", "Newest favorite"),
    node("00000005", false, "2026-07-31T12:02:00Z", "Older nonfavorite"),
  ];
  const expectedKeys = [
    "meshtastic:!00000004",
    "meshtastic:!00000002",
    "meshtastic:!00000001",
    "meshtastic:!00000005",
    "meshtastic:!00000003",
  ];

  assert.deepEqual(
    panel._recipientChoices(nodes).map((choice) => choice.value),
    expectedKeys,
    "both Mesh and Messages direct-recipient dropdowns use this helper",
  );
  const operatorNodes = panel._remoteNodeCandidates(nodes);
  assert.deepEqual(
    operatorNodes.map((candidate) => candidate.node_key),
    expectedKeys,
    "remote settings, traceroute, and NeighborInfo use this helper",
  );

  const recipientHtml = panel._recipientOptions(nodes);
  const operatorHtml = panel._operatorTargetOptions(operatorNodes, "", { traceroute: true });
  for (const html of [recipientHtml, operatorHtml]) {
    let previous = -1;
    expectedKeys.forEach((key) => {
      const index = html.indexOf(key);
      assert.ok(index > previous, `${key} must follow the shared favorites/recent order`);
      previous = index;
    });
    assert.match(html, /★/u, "favorites are visibly identified in every node selector");
    assert.match(html, /Last seen/u, "recency is visibly identified in every node selector");
  }

  panel._draft.recipient = "meshtastic:!deadbeef";
  const unavailableHtml = panel._recipientOptions(nodes);
  assert.ok(
    unavailableHtml.indexOf("meshtastic:!deadbeef")
      > unavailableHtml.indexOf("meshtastic:!00000003"),
    "a selected but unavailable sentinel remains last",
  );
  assert.deepEqual(nodes.map((candidate) => candidate.node_id), [
    "!00000001",
    "!00000002",
    "!00000003",
    "!00000004",
    "!00000005",
  ], "sorting never mutates the snapshot array");
"""
    )


def test_neighbor_info_row_shortcut_only_opens_existing_safe_flow() -> None:
    """The shortcut selects a canonical target but cannot send or load status."""
    source = PANEL.read_text(encoding="utf-8")
    assert "data-neighbor-info-node" in source

    _run_panel_script(
        r"""
  assert.equal(typeof panel._openNeighborInfoForNode, "function");
  const canonical = {
    node_key: "meshtastic:!1234abcd",
    node_id: "!1234abcd",
    protocol: "meshtastic",
    identity_valid: true,
    long_name: "Safe target",
    short_name: "SAFE",
    favorite: true,
    last_heard: "2026-07-31T12:00:00Z",
  };
  panel._snapshot = {
    nodes: { canonical },
    gateways: {
      ble: {
        gateway_id: "ble-gateway",
        name: "Local BLE",
        protocol: "meshtastic",
        transport: "bluetooth",
        connected: true,
      },
    },
    recent_messages: [],
  };
  let calls = 0;
  panel._hass = {
    async callWS() { calls += 1; throw new Error("shortcut must not call WS"); },
    async callService() { calls += 1; throw new Error("shortcut must not call a service"); },
  };
  let renders = 0;
  let scrolled = 0;
  let focused = 0;
  const neighborPanel = { scrollIntoView() { scrolled += 1; } };
  const statusLoad = { focus() { focused += 1; } };
  panel.querySelector = (selector) => selector === "#meshnet-neighbor-info-panel"
    ? neighborPanel
    : selector === "#meshnet-neighbor-info-status-load" ? statusLoad : null;
  window.requestAnimationFrame = (callback) => { callback(); return 1; };
  panel._safeRender = () => { renders += 1; return true; };

  assert.equal(panel._openNeighborInfoForNode(canonical.node_key), true);
  assert.equal(panel._neighborInfoGatewayId, "ble-gateway");
  assert.equal(panel._neighborInfoTargetNode, "meshtastic:!1234abcd");
  assert.equal(panel._neighborInfoStatusReady, false);
  assert.equal(panel._neighborInfoStatusData, null);
  assert.equal(panel._neighborInfoConfirmation, null);
  assert.equal(panel._neighborInfoResult, null);
  assert.equal(calls, 0);
  assert.equal(renders, 1);
  assert.equal(scrolled, 1);
  assert.equal(focused, 1);

  const unsafeCases = [
    {
      key: "meshcore:contact-one",
      node: {
        node_key: "meshcore:contact-one",
        node_id: "contact-one",
        protocol: "meshcore",
        identity_valid: true,
      },
    },
    {
      key: "mac:aabbccddeeff",
      node: {
        node_key: "mac:aabbccddeeff",
        node_id: "!2234abcd",
        protocol: "meshtastic",
        identity_valid: true,
      },
    },
    {
      key: "meshtastic:!3234abcd",
      node: {
        node_key: "meshtastic:!3234abcd",
        node_id: "!3234abcd",
        protocol: "meshtastic",
        identity_valid: false,
      },
    },
  ];
  for (const candidate of unsafeCases) {
    panel._snapshot.nodes = { unsafe: candidate.node };
    assert.equal(panel._openNeighborInfoForNode(candidate.key), false);
  }
  panel._snapshot.nodes = { canonical };
  panel._snapshot.gateways = {};
  assert.equal(panel._openNeighborInfoForNode(canonical.node_key), false);
  assert.equal(calls, 0);
  assert.equal(renders, 1, "rejected shortcuts do not replace the safe selection");
"""
    )


def test_pointer_resize_clamps_owns_pointer_and_removes_every_listener() -> None:
    """One resize gesture is bounded and leaves no listener or pointer ownership."""
    _run_panel_script(
        r"""
  const handlers = new Map();
  const removed = [];
  let captured = null;
  let released = null;
  let prevented = 0;
  let stopped = 0;
  const handle = {
    addEventListener(type, callback) { handlers.set(type, callback); },
    removeEventListener(type, callback) {
      assert.equal(handlers.get(type), callback);
      handlers.delete(type);
      removed.push(type);
    },
    setPointerCapture(pointerId) { captured = pointerId; },
    releasePointerCapture(pointerId) { released = pointerId; },
  };
  const card = {
    style: {},
    getBoundingClientRect() { return { width: 400, height: 300 }; },
  };
  panel.querySelector = (selector) => selector === '[data-mesh-card="nodes"]' ? card : null;
  panel.querySelectorAll = () => [];
  panel._startMeshCardResize({
    button: 0,
    pointerId: 7,
    clientX: 100,
    clientY: 200,
    preventDefault() { prevented += 1; },
    stopPropagation() { stopped += 1; },
  }, card, handle, "nodes");

  assert.deepEqual(panel._meshLayoutInteraction, { kind: "resize", card_id: "nodes" });
  assert.equal(captured, 7);
  assert.equal(prevented, 1);
  assert.equal(stopped, 1);
  assert.deepEqual(
    [...handlers.keys()].sort(),
    ["lostpointercapture", "pointercancel", "pointermove", "pointerup"],
  );

  handlers.get("pointermove")({ pointerId: 8, clientX: 1000, clientY: 1000 });
  assert.equal(panel._meshCardSizes.has("nodes"), false, "a different pointer cannot resize");
  handlers.get("pointermove")({ pointerId: 7, clientX: 1_000_100, clientY: 1_000_200 });
  assert.deepEqual(panel._meshCardSizes.get("nodes"), { width: 720, height: 1200 });
  assert.deepEqual(card.style, { width: "720px", height: "1200px" });

  handlers.get("pointerup")({ pointerId: 8 });
  assert.ok(panel._meshLayoutInteraction, "a different pointer cannot finish the gesture");
  handlers.get("pointerup")({ pointerId: 7 });
  assert.equal(panel._meshLayoutInteraction, null);
  assert.equal(panel._meshLayoutCleanup, null);
  assert.equal(released, 7);
  assert.deepEqual(
    removed.sort(),
    ["lostpointercapture", "pointercancel", "pointermove", "pointerup"],
  );
  assert.equal(handlers.size, 0);

  panel._startMeshCardResize({
    button: 1,
    pointerId: 9,
    clientX: 0,
    clientY: 0,
  }, card, handle, "nodes");
  assert.equal(panel._meshLayoutInteraction, null, "non-primary pointer input is ignored");

  const windowHandlers = new Map();
  const originalWindowAdd = window.addEventListener;
  const originalWindowRemove = window.removeEventListener;
  window.addEventListener = (type, callback) => windowHandlers.set(type, callback);
  window.removeEventListener = (type, callback) => {
    assert.equal(windowHandlers.get(type), callback);
    windowHandlers.delete(type);
  };
  const failedCaptureHandlers = new Map();
  const failedCaptureHandle = {
    addEventListener(type, callback) { failedCaptureHandlers.set(type, callback); },
    removeEventListener(type, callback) {
      assert.equal(failedCaptureHandlers.get(type), callback);
      failedCaptureHandlers.delete(type);
    },
    setPointerCapture() { throw new Error("capture unavailable"); },
    releasePointerCapture() {},
  };
  panel._startMeshCardResize({
    button: 0,
    pointerId: 11,
    clientX: 0,
    clientY: 0,
    preventDefault() {},
    stopPropagation() {},
  }, card, failedCaptureHandle, "nodes");
  assert.ok(panel._meshLayoutInteraction, "capture failure still starts a bounded gesture");
  assert.equal(typeof windowHandlers.get("pointermove"), "function");
  assert.equal(typeof windowHandlers.get("pointerup"), "function");
  windowHandlers.get("pointermove")({ pointerId: 11, clientX: 55, clientY: 65 });
  assert.deepEqual(panel._meshCardSizes.get("nodes"), { width: 455, height: 365 });
  windowHandlers.get("pointerup")({ pointerId: 11 });
  assert.equal(panel._meshLayoutInteraction, null, "window release clears failed capture");
  assert.equal(windowHandlers.size, 0);
  assert.equal(failedCaptureHandlers.size, 0);
  window.addEventListener = originalWindowAdd;
  window.removeEventListener = originalWindowRemove;
"""
    )


def test_drag_order_keyboard_order_and_reset_remain_allowlisted() -> None:
    """Pointer and keyboard ordering cannot duplicate, lose, or invent cards."""
    _run_panel_script(
        r"""
  const defaults = [...panel._meshCardOrder];
  assert.equal(panel._moveMeshCard("send-message", "earlier"), false);
  assert.equal(panel._moveMeshCard("send-message", "later"), true);
  assert.deepEqual(panel._meshCardOrder.slice(0, 2), ["gateways", "send-message"]);
  assert.equal(panel._moveMeshCardTo("rf-heat", "gateways", false), true);
  assert.deepEqual(panel._meshCardOrder.slice(0, 2), ["rf-heat", "gateways"]);
  assert.equal(panel._moveMeshCardTo("send-message", "neighbor-info", true), true);
  assert.equal(
    panel._meshCardOrder.indexOf("send-message"),
    panel._meshCardOrder.indexOf("neighbor-info") + 1,
  );
  assert.equal(new Set(panel._meshCardOrder).size, defaults.length);
  assert.deepEqual([...panel._meshCardOrder].sort(), [...defaults].sort());

  const stable = [...panel._meshCardOrder];
  assert.equal(panel._moveMeshCardTo("nodes", "nodes", false), false);
  assert.equal(panel._moveMeshCardTo("unknown", "nodes", false), false);
  assert.equal(panel._moveMeshCardTo("nodes", "unknown", false), false);
  assert.equal(panel._moveMeshCardTo("nodes", "gateways", "after"), false);
  assert.deepEqual(panel._meshCardOrder, stable);

  panel._setMeshCardSize("nodes", 700, 600);
  panel._resetMeshCardLayout();
  assert.deepEqual(panel._meshCardOrder, defaults);
  assert.equal(panel._meshCardSizes.size, 0);

  const controls = panel._meshCardControls("nodes");
  assert.match(controls, /data-mesh-card-move="earlier"/);
  assert.match(controls, /data-mesh-card-move="later"/);
  assert.match(controls, /data-mesh-drag-handle="nodes"/);
  assert.match(controls, /data-mesh-resize-handle="nodes"/);
  assert.match(controls, /aria-label="Move nodes card earlier"/);
  assert.match(controls, /aria-label="Resize nodes card"/);
"""
    )


def test_card_scroll_is_restored_and_poll_flushes_only_after_interaction() -> None:
    """A full panel refresh preserves each resized card's independent viewport."""
    _run_panel_script(
        r"""
  const cards = {
    nodes: {
      scrollTop: 420,
      scrollLeft: 12,
      getAttribute(name) { return name === "data-mesh-card" ? "nodes" : null; },
    },
    remote: {
      scrollTop: 85,
      scrollLeft: 0,
      getAttribute(name) { return name === "data-mesh-card" ? "remote-admin" : null; },
    },
    unknown: {
      scrollTop: 999,
      scrollLeft: 999,
      getAttribute(name) { return name === "data-mesh-card" ? "not-allowlisted" : null; },
    },
  };
  panel.querySelectorAll = (selector) => selector === "[data-mesh-card]"
    ? Object.values(cards) : [];
  let currentCards = cards;
  panel.querySelector = (selector) => selector === '[data-mesh-card="nodes"]'
    ? currentCards.nodes
    : selector === '[data-mesh-card="remote-admin"]' ? currentCards.remote : null;

  const state = panel._captureMeshCardScrollState();
  assert.deepEqual([...state.entries()], [
    ["nodes", { top: 420, left: 12 }],
    ["remote-admin", { top: 85, left: 0 }],
  ]);
  currentCards = {
    nodes: { scrollTop: 0, scrollLeft: 0 },
    remote: { scrollTop: 0, scrollLeft: 0 },
  };
  panel._restoreMeshCardScrollState(state);
  assert.deepEqual(
    { top: currentCards.nodes.scrollTop, left: currentCards.nodes.scrollLeft },
    { top: 420, left: 12 },
  );
  assert.deepEqual(
    { top: currentCards.remote.scrollTop, left: currentCards.remote.scrollLeft },
    { top: 85, left: 0 },
  );

  let cleanupCalls = 0;
  let renders = 0;
  panel._connected = true;
  panel._meshLayoutInteraction = { kind: "resize", card_id: "nodes" };
  panel._meshLayoutCleanup = () => { cleanupCalls += 1; };
  panel._safeRender = () => {
    renders += 1;
    panel._pollRenderPending = false;
    return true;
  };
  assert.equal(panel._renderPollSnapshot(), false);
  assert.equal(panel._pollRenderPending, true);
  assert.equal(renders, 0);
  panel._finishMeshLayoutInteraction();
  await Promise.resolve();
  assert.equal(cleanupCalls, 1);
  assert.equal(panel._meshLayoutInteraction, null);
  assert.equal(panel._meshLayoutCleanup, null);
  assert.equal(renders, 1, "one deferred poll flushes after the gesture releases ownership");
"""
    )


def test_operator_candidates_reject_duplicate_alias_and_invalid_identity() -> None:
    """RF target dropdowns contain only one exact validated canonical identity."""
    _run_panel_script(
        r"""
  const exact = (suffix, overrides = {}) => ({
    node_key: `meshtastic:!${suffix}`,
    node_id: `!${suffix}`,
    protocol: "meshtastic",
    identity_valid: true,
    long_name: `Node ${suffix}`,
    last_heard: "2026-07-31T12:00:00Z",
    ...overrides,
  });
  const candidates = panel._remoteNodeCandidates([
    exact("00000001"),
    exact("00000002"),
    exact("00000002", { long_name: "Duplicate observation" }),
    exact("00000003", { identity_valid: false }),
    exact("00000004", { identity_valid: undefined }),
    exact("00000005", { node_key: "mac:aabbccddeeff" }),
    exact("00000006", { node_key: "meshtastic:!00000007" }),
    exact("00000008", { protocol: "meshcore" }),
    exact("00000000"),
    exact("ffffffff"),
    null,
    [],
  ]);
  assert.deepEqual(candidates.map((node) => node.node_key), [
    "meshtastic:!00000001",
  ]);
  const html = panel._operatorTargetOptions(candidates, "", { traceroute: true });
  assert.match(html, /meshtastic:!00000001/);
  for (const rejected of [
    "meshtastic:!00000002",
    "meshtastic:!00000003",
    "meshtastic:!00000004",
    "mac:aabbccddeeff",
    "meshtastic:!00000007",
    "meshtastic:!00000008",
    "meshtastic:!00000000",
    "meshtastic:!ffffffff",
  ]) assert.doesNotMatch(html, new RegExp(rejected.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&")));
"""
    )
