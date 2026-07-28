class MeshNetPanel extends HTMLElement {
  constructor() {
    super();
    this._draft = {
      delivery: "broadcast",
      recipient: "",
      gateway: "",
      message: "",
      channel: "0",
      priority: "normal",
    };
    this._nodeSort = "favorites_recent";
    this._sending = false;
    this._sendStatus = null;
    this._connected = false;
    this._loaded = false;
    this._pollEpoch = 0;
    this._pollTimer = null;
  }

  set hass(hass) {
    this._hass = hass;
    this._startPolling();
  }

  connectedCallback() {
    this._connected = true;
    this._startPolling();
  }

  disconnectedCallback() {
    this._connected = false;
    this._loaded = false;
    this._pollEpoch += 1;
    if (this._pollTimer != null) window.clearTimeout(this._pollTimer);
    this._pollTimer = null;
  }

  _startPolling() {
    if (!this._connected || !this._hass || this._loaded) return;
    this._loaded = true;
    const epoch = ++this._pollEpoch;
    void this._load(epoch);
  }

  _pollIsCurrent(epoch) {
    return this._connected && this._pollEpoch === epoch;
  }

  async _load(epoch) {
    try {
      await this._refreshSnapshot();
    } catch (_err) {
      this._error = "Snapshot unavailable";
    }
    if (!this._pollIsCurrent(epoch)) return;
    this._render();
    this._pollTimer = window.setTimeout(() => {
      if (!this._pollIsCurrent(epoch)) return;
      this._pollTimer = null;
      this._loaded = false;
      this._startPolling();
    }, 5000);
  }

  _render() {
    const composerFocus = this._composerFocusState();
    const snapshot = this._snapshot || { nodes: {}, gateways: {}, recent_messages: [] };
    const nodes = Object.values(snapshot.nodes || {});
    const gateways = Object.values(snapshot.gateways || {});
    const sortedNodes = this._sortNodes(nodes, this._nodeSort);
    const directDelivery = this._draft.delivery === "direct";
    const recipientCount = this._recipientChoices(nodes).length;
    const locatedNodeCount = this._validLocationCount(nodes);
    const favoriteLabelConfigured = snapshot.panel_metadata
      && snapshot.panel_metadata.favorite_label_configured === true;
    const topology = this._passiveTopology(nodes, gateways);
    this.innerHTML = `
      <style>
        :host {
          display: block;
          min-height: 100vh;
          color: var(--primary-text-color);
          background: var(--primary-background-color);
          font-family: var(--paper-font-body1_-_font-family);
        }
        .wrap {
          display: grid;
          grid-template-columns: minmax(0, 1fr) 360px;
          gap: 16px;
          padding: 16px;
          box-sizing: border-box;
        }
        .toolbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 12px;
        }
        .toolbar-actions {
          display: flex;
          align-items: center;
          gap: 12px;
        }
        .map-link {
          color: var(--primary-color);
          text-decoration: none;
          font-size: 13px;
        }
        .map-link:hover { text-decoration: underline; }
        h1 {
          font-size: 22px;
          margin: 0;
          font-weight: 500;
        }
        .stats {
          display: grid;
          grid-template-columns: repeat(4, minmax(120px, 1fr));
          gap: 8px;
          margin-bottom: 12px;
        }
        .stat, .panel {
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
        }
        .stat {
          padding: 10px 12px;
        }
        .label {
          color: var(--secondary-text-color);
          font-size: 12px;
        }
        .value {
          font-size: 22px;
          margin-top: 4px;
        }
        .topology {
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          overflow: hidden;
        }
        .topology-heading {
          display: flex;
          justify-content: space-between;
          gap: 8px;
          padding: 10px 12px;
          border-bottom: 1px solid var(--divider-color);
          font-size: 13px;
        }
        .topology-copy {
          display: grid;
          gap: 3px;
        }
        .topology-note {
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: normal;
        }
        .topology svg {
          width: 100%;
          height: min(68vh, 720px);
          min-height: 420px;
          display: block;
        }
        .side {
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .panel {
          padding: 12px;
          overflow: hidden;
        }
        .panel h2 {
          margin: 0 0 8px;
          font-size: 16px;
          font-weight: 500;
        }
        .row {
          display: grid;
          grid-template-columns: 1fr auto;
          gap: 8px;
          padding: 7px 0;
          border-top: 1px solid var(--divider-color);
          font-size: 13px;
        }
        .row:first-of-type { border-top: 0; }
        .good { color: var(--success-color, #168047); }
        .bad { color: var(--error-color, #d32f2f); }
        .warn { color: var(--warning-color, #b26a00); }
        .msg {
          font-size: 13px;
          padding: 8px 0;
          border-top: 1px solid var(--divider-color);
        }
        .msg:first-of-type { border-top: 0; }
        .msg .meta { color: var(--secondary-text-color); font-size: 12px; }
        .composer {
          display: grid;
          gap: 10px;
        }
        .composer label {
          display: grid;
          gap: 5px;
          color: var(--secondary-text-color);
          font-size: 12px;
        }
        .composer select, .composer textarea {
          width: 100%;
          box-sizing: border-box;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          padding: 8px;
          color: var(--primary-text-color);
          background: var(--card-background-color);
          font: inherit;
        }
        .composer textarea {
          min-height: 92px;
          resize: vertical;
        }
        .composer-controls {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 8px;
        }
        .composer button {
          justify-self: start;
          border: 0;
          border-radius: 6px;
          padding: 9px 16px;
          color: var(--text-primary-color, #fff);
          background: var(--primary-color);
          cursor: pointer;
          font: inherit;
        }
        .composer button:disabled {
          cursor: default;
          opacity: 0.55;
        }
        .send-status {
          min-height: 18px;
          font-size: 12px;
        }
        .field-help {
          color: var(--secondary-text-color);
          font-size: 11px;
        }
        .panel-heading {
          display: flex;
          align-items: end;
          justify-content: space-between;
          gap: 8px;
          margin-bottom: 6px;
        }
        .panel-heading h2 { margin: 0; }
        .sort-control {
          display: grid;
          gap: 3px;
          color: var(--secondary-text-color);
          font-size: 11px;
        }
        .sort-control select {
          max-width: 180px;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          padding: 5px;
          color: var(--primary-text-color);
          background: var(--card-background-color);
        }
        .node-row {
          grid-template-columns: minmax(0, 1fr) auto;
          align-items: center;
        }
        .node-summary {
          display: grid;
          min-width: 0;
          gap: 2px;
        }
        .node-title {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .favorite-star { color: var(--warning-color, #b26a00); }
        .node-actions {
          display: flex;
          align-items: center;
          gap: 7px;
        }
        .node-message {
          border: 1px solid var(--primary-color);
          border-radius: 6px;
          padding: 5px 8px;
          color: var(--primary-color);
          background: transparent;
          cursor: pointer;
          font: inherit;
        }
        .heat {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(74px, 1fr));
          gap: 6px;
        }
        .cell {
          min-height: 56px;
          border-radius: 6px;
          padding: 7px;
          box-sizing: border-box;
          color: #111;
          background: #d7d7d7;
          font-size: 12px;
          overflow: hidden;
        }
        .cell.good { background: #74c476; }
        .cell.warn { background: #fed976; }
        .cell.bad { background: #fb6a4a; color: #fff; }
        .cell .metric { font-size: 11px; margin-top: 4px; }
        circle.node { fill: var(--primary-color); }
        circle.gateway { fill: var(--success-color, #168047); }
        circle.offline { fill: var(--disabled-text-color); }
        line.link { stroke: var(--divider-color); stroke-width: 1.4; }
        line.direct-link { stroke: var(--success-color, #168047); stroke-width: 2; }
        line.route-link { stroke: var(--primary-color); stroke-dasharray: 6 4; }
        .topology-empty {
          fill: var(--secondary-text-color);
          font-size: 16px;
          text-anchor: middle;
        }
        text { fill: var(--primary-text-color); font-size: 12px; }
        @media (max-width: 900px) {
          .wrap { grid-template-columns: 1fr; padding: 10px; }
          .stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
          .topology svg { min-height: 360px; height: 56vh; }
        }
      </style>
      <div class="wrap">
        <main>
          <div class="toolbar">
            <h1>MeshNet</h1>
            <div class="toolbar-actions">
              <a class="map-link" href="/map">Map · ${locatedNodeCount} located</a>
              <span class="${this._error ? "bad" : "good"}">${this._escape(this._error || "Live")}</span>
            </div>
          </div>
          <div class="stats">
            ${this._stat("Nodes", nodes.length)}
            ${this._stat("Online", nodes.filter((node) => node.online).length)}
            ${this._stat("Gateways", gateways.filter((gateway) => gateway.connected).length + "/" + gateways.length)}
            ${this._stat("Health", snapshot.mesh_health_score == null ? "n/a" : snapshot.mesh_health_score + "%")}
          </div>
          ${this._graph(topology)}
        </main>
        <aside class="side">
          <section class="panel">
            <h2>Send message</h2>
            <form class="composer" id="meshnet-send-form">
              <label>
                Delivery
                <select id="meshnet-delivery">
                  <option value="broadcast"${this._selected(this._draft.delivery, "broadcast")}>Broadcast</option>
                  <option value="direct"${this._selected(this._draft.delivery, "direct")}>Direct</option>
                </select>
              </label>
              <label>
                Direct recipient
                <select id="meshnet-recipient"${directDelivery && recipientCount ? " required" : " disabled"}>
                  ${this._recipientOptions(nodes)}
                </select>
                <span class="field-help">${this._escape(
                  directDelivery
                    ? recipientCount
                      ? "Choose a cached mesh node."
                      : "No cached nodes available yet. Wait for node data before sending directly."
                    : "Select Direct delivery to choose a node.",
                )}</span>
              </label>
              <label>
                Gateway
                <select id="meshnet-gateway">
                  ${this._gatewayOptions(gateways)}
                </select>
              </label>
              <label>
                Message
                <textarea id="meshnet-message" required placeholder="Type a local mesh message">${this._escape(this._draft.message)}</textarea>
              </label>
              <div class="composer-controls">
                <label>
                  Channel
                  <select id="meshnet-channel">
                    ${Array.from({ length: 8 }, (_item, channel) => `<option value="${channel}"${this._selected(this._draft.channel, channel)}>${channel}</option>`).join("")}
                  </select>
                </label>
                <label>
                  Priority
                  <select id="meshnet-priority">
                    <option value="normal"${this._selected(this._draft.priority, "normal")}>Normal</option>
                    <option value="high"${this._selected(this._draft.priority, "high")}>High</option>
                    <option value="emergency"${this._selected(this._draft.priority, "emergency")}>Emergency</option>
                  </select>
                </label>
              </div>
              <button type="submit"${this._sending || (directDelivery && !recipientCount) ? " disabled" : ""}>${this._sending ? "Sending…" : "Send"}</button>
              <div class="send-status ${this._statusClass()}" role="status" aria-live="polite">${this._escape(this._sendStatus ? this._sendStatus.text : "")}</div>
            </form>
          </section>
          <section class="panel">
            <h2>Gateways</h2>
            ${gateways.map((gateway) => `
              <div class="row">
                <span>${this._escape(gateway.name || gateway.gateway_id)}</span>
                <span class="${gateway.connected ? "good" : "bad"}">${gateway.connected ? "online" : "offline"}</span>
              </div>
            `).join("") || `<div class="label">No gateways configured</div>`}
          </section>
          <section class="panel">
            <div class="panel-heading">
              <h2>Nodes</h2>
              <label class="sort-control">
                Sort
                <select id="meshnet-node-sort">
                  <option value="favorites_recent"${this._selected(this._nodeSort, "favorites_recent")}>Favorites + last seen</option>
                  <option value="last_seen"${this._selected(this._nodeSort, "last_seen")}>Last seen</option>
                  <option value="name"${this._selected(this._nodeSort, "name")}>Name</option>
                </select>
              </label>
            </div>
            ${sortedNodes.slice(0, 24).map((node) => `
              <div class="row node-row">
                <span class="node-summary">
                  <span class="node-title">${node.favorite === true ? '<span class="favorite-star" title="Favorite" aria-label="Favorite">★</span> ' : ""}${this._escape(this._nodeName(node))}</span>
                  <span class="label">${this._escape(this._humanLastSeen(node.last_heard))}</span>
                </span>
                <span class="node-actions">
                  <span class="${node.online ? "good" : "bad"}">${node.online ? "online" : "offline"}</span>
                  <button class="node-message" type="button" data-message-node="${this._escape(node.node_key)}">Message</button>
                </span>
              </div>
            `).join("") || `<div class="label">Waiting for node data</div>`}
            ${sortedNodes.length > 24 ? `<div class="label">Showing 24 of ${sortedNodes.length} nodes</div>` : ""}
            ${favoriteLabelConfigured ? "" : '<div class="label">To pin favorites, add the Home Assistant device label “MeshNet Favorite”.</div>'}
          </section>
          <section class="panel">
            <h2>Messages</h2>
            ${(snapshot.recent_messages || []).slice(-8).reverse().map((message) => `
              <div class="msg">
                <div>${this._escape(message.text || "")}</div>
                <div class="meta">${this._escape(message.sender || "unknown")} → ${this._escape(message.receiver || message.channel || "broadcast")}</div>
              </div>
            `).join("") || `<div class="label">No messages recorded</div>`}
          </section>
          <section class="panel">
            <h2>RF Heat</h2>
            <div class="heat">
              ${nodes.filter((node) => this._rfMetric(node) != null).slice(0, 36).map((node) => {
                const metric = this._rfMetric(node);
                const quality = this._quality(metric);
                return `
                  <div class="cell ${quality}">
                    <div>${this._escape(node.short_name || node.long_name || node.node_id || node.node_key)}</div>
                    <div class="metric">${metric.label}: ${metric.value}</div>
                  </div>
                `;
              }).join("") || `<div class="label">Waiting for RSSI/SNR packets</div>`}
            </div>
          </section>
        </aside>
      </div>
    `;
    this._bindComposer();
    this._bindNodeControls();
    this._restoreComposerFocus(composerFocus);
  }

  _composerFocusState() {
    const active = this.ownerDocument && this.ownerDocument.activeElement;
    const fieldIds = [
      "meshnet-delivery",
      "meshnet-recipient",
      "meshnet-gateway",
      "meshnet-message",
      "meshnet-channel",
      "meshnet-priority",
      "meshnet-node-sort",
    ];
    if (!active || !this.contains(active) || !fieldIds.includes(active.id)) return null;
    return {
      id: active.id,
      start: typeof active.selectionStart === "number" ? active.selectionStart : null,
      end: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
    };
  }

  _restoreComposerFocus(state) {
    if (!state) return;
    const field = this.querySelector(`#${state.id}`);
    if (!field) return;
    field.focus();
    if (state.start != null && typeof field.setSelectionRange === "function") {
      field.setSelectionRange(state.start, state.end);
    }
  }

  _bindComposer() {
    const form = this.querySelector("#meshnet-send-form");
    if (!form) return;

    const fields = {
      delivery: this.querySelector("#meshnet-delivery"),
      recipient: this.querySelector("#meshnet-recipient"),
      gateway: this.querySelector("#meshnet-gateway"),
      message: this.querySelector("#meshnet-message"),
      channel: this.querySelector("#meshnet-channel"),
      priority: this.querySelector("#meshnet-priority"),
    };
    Object.entries(fields).forEach(([key, field]) => {
      if (!field) return;
      const eventName = key === "message" ? "input" : "change";
      field.addEventListener(eventName, () => {
        this._draft[key] = field.value;
        if (key === "delivery") {
          this._sendStatus = null;
          this._render();
        }
      });
    });
    form.addEventListener("submit", (event) => this._sendMessage(event));
  }

  _bindNodeControls() {
    const sort = this.querySelector("#meshnet-node-sort");
    if (sort) {
      sort.addEventListener("change", () => {
        this._nodeSort = ["favorites_recent", "last_seen", "name"].includes(sort.value)
          ? sort.value
          : "favorites_recent";
        this._render();
      });
    }
    this.querySelectorAll("[data-message-node]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!this._chooseDirectRecipient(button.getAttribute("data-message-node"))) return;
        this._sendStatus = null;
        this._render();
        const message = this.querySelector("#meshnet-message");
        if (message) message.focus();
      });
    });
  }

  _chooseDirectRecipient(nodeKey) {
    const key = String(nodeKey || "");
    if (!this._isKnownRecipient(key)) return false;
    this._draft.delivery = "direct";
    this._draft.recipient = key;
    return true;
  }

  async _sendMessage(event) {
    event.preventDefault();
    if (this._sending) return;

    const draft = { ...this._draft };
    if (!draft.message.trim()) {
      this._sendStatus = { kind: "bad", text: "Enter a message first." };
      this._render();
      return;
    }
    if (this._messageByteLength(draft.message) > 237) {
      this._sendStatus = { kind: "bad", text: "Message must be 237 UTF-8 bytes or fewer." };
      this._render();
      return;
    }

    const delivery = draft.delivery === "direct" ? "direct" : "broadcast";
    const recipient = draft.recipient.trim();
    if (delivery === "direct" && (!recipient || !this._isKnownRecipient(recipient))) {
      this._sendStatus = { kind: "bad", text: "Choose an available direct recipient." };
      this._render();
      return;
    }
    const gateway = draft.gateway.trim();
    const priority = ["normal", "high", "emergency"].includes(draft.priority)
      ? draft.priority
      : "normal";
    const channel = /^[0-7]$/.test(draft.channel) ? draft.channel : "0";
    const payload = {
      type: "meshnet/send_message",
      message: draft.message,
      channel,
      priority,
      message_type: delivery,
    };
    if (delivery === "direct") payload.target_node = recipient;
    if (gateway) payload.gateway_id = gateway;

    this._sending = true;
    this._sendStatus = { kind: "warn", text: "Sending…" };
    this._render();

    try {
      const result = await this._hass.callWS(payload);
      if (!result || !result.message_id) throw new Error("Missing message identifier");

      if (this._draft.message === draft.message) this._draft.message = "";
      let snapshot = null;
      try {
        snapshot = await this._refreshSnapshot();
      } catch (_err) {
        // The send was accepted; a failed status refresh must not report it as failed.
      }
      const sentMessage = snapshot && (snapshot.recent_messages || []).find(
        (message) => message.message_id === result.message_id,
      );
      const status = sentMessage && sentMessage.raw && sentMessage.raw.status;
      this._sendStatus = status === "sent"
        ? { kind: "good", text: "Message sent." }
        : { kind: "warn", text: "Message queued for delivery." };
    } catch (_err) {
      this._sendStatus = { kind: "bad", text: "Message could not be submitted." };
    } finally {
      this._sending = false;
      this._render();
    }
  }

  _messageByteLength(message) {
    return new TextEncoder().encode(String(message)).length;
  }

  async _refreshSnapshot() {
    const snapshot = await this._hass.callWS({ type: "meshnet/snapshot" });
    this._snapshot = snapshot;
    this._error = null;
    return snapshot;
  }

  _isKnownRecipient(nodeKey) {
    const nodes = Object.values((this._snapshot && this._snapshot.nodes) || {});
    return nodes.some(
      (node) => node && node.node_key != null && String(node.node_key) === String(nodeKey),
    );
  }

  _recipientChoices(nodes) {
    return nodes
      .filter((node) => node && node.node_key != null && String(node.node_key))
      .map((node) => ({
        value: String(node.node_key),
        label: this._nodeName(node),
      }))
      .filter((choice, index, all) => all.findIndex((item) => item.value === choice.value) === index)
      .sort((left, right) => this._compareText(left.label, right.label) || this._compareText(left.value, right.value));
  }

  _recipientOptions(nodes) {
    const selected = String(this._draft.recipient || "");
    const choices = this._recipientChoices(nodes);
    if (selected && !choices.some((choice) => choice.value === selected)) {
      choices.push({ value: selected, label: `${selected} (currently unavailable)` });
    }
    const prompt = choices.length ? "Choose a node…" : "No cached nodes available yet";
    return [
      `<option value=""${this._selected(selected, "")}>${prompt}</option>`,
      ...choices.map((choice) => {
        const suffix = choice.label === choice.value ? "" : ` (${choice.value})`;
        return `<option value="${this._escape(choice.value)}"${this._selected(selected, choice.value)}>${this._escape(choice.label + suffix)}</option>`;
      }),
    ].join("");
  }

  _gatewayOptions(gateways) {
    const selected = String(this._draft.gateway || "");
    const choices = gateways
      .filter((gateway) => gateway && gateway.gateway_id != null && String(gateway.gateway_id))
      .map((gateway) => ({
        value: String(gateway.gateway_id),
        label: String(gateway.name || gateway.gateway_id),
        connected: Boolean(gateway.connected),
      }))
      .filter((choice, index, all) => all.findIndex((item) => item.value === choice.value) === index)
      .sort((left, right) => left.label.localeCompare(right.label));
    if (selected && !choices.some((choice) => choice.value === selected)) {
      choices.push({ value: selected, label: selected, connected: false, unavailable: true });
    }
    return [
      `<option value=""${this._selected(selected, "")}>Automatic</option>`,
      ...choices.map((choice) => {
        const identifier = choice.label === choice.value ? "" : ` (${choice.value})`;
        const state = choice.unavailable ? " — unavailable" : choice.connected ? " — online" : " — offline";
        return `<option value="${this._escape(choice.value)}"${this._selected(selected, choice.value)}>${this._escape(choice.label + identifier + state)}</option>`;
      }),
    ].join("");
  }

  _selected(value, expected) {
    return String(value) === String(expected) ? " selected" : "";
  }

  _statusClass() {
    const kind = this._sendStatus && this._sendStatus.kind;
    return ["good", "warn", "bad"].includes(kind) ? kind : "";
  }

  _nodeName(node) {
    return String(
      (node && (node.long_name || node.user_name || node.short_name || node.node_id || node.node_key))
      || "Unknown node",
    );
  }

  _textSortKey(value) {
    const text = String(value == null ? "" : value);
    try {
      return text.normalize("NFKC").toLocaleLowerCase("en-US");
    } catch (_err) {
      return text.toLowerCase();
    }
  }

  _compareText(left, right) {
    const a = this._textSortKey(left);
    const b = this._textSortKey(right);
    if (a < b) return -1;
    if (a > b) return 1;
    const originalA = String(left == null ? "" : left);
    const originalB = String(right == null ? "" : right);
    return originalA < originalB ? -1 : originalA > originalB ? 1 : 0;
  }

  _timestampMs(value) {
    let timestamp = null;
    if (typeof value === "number" && Number.isFinite(value)) {
      timestamp = Math.abs(value) < 1e12 ? value * 1000 : value;
    } else if (typeof value === "string" && value.trim()) {
      timestamp = Date.parse(value);
    }
    return typeof timestamp === "number" && Number.isFinite(timestamp) ? timestamp : null;
  }

  _compareLastSeen(left, right) {
    const a = this._timestampMs(left && left.last_heard);
    const b = this._timestampMs(right && right.last_heard);
    if (a == null && b != null) return 1;
    if (a != null && b == null) return -1;
    if (a != null && b != null && a !== b) return b - a;
    return 0;
  }

  _compareNodes(left, right, mode) {
    if (mode === "favorites_recent") {
      const favoriteDifference = Number(right.favorite === true) - Number(left.favorite === true);
      if (favoriteDifference) return favoriteDifference;
    }
    if (mode !== "name") {
      const recentDifference = this._compareLastSeen(left, right);
      if (recentDifference) return recentDifference;
    }
    return this._compareText(this._nodeName(left), this._nodeName(right))
      || this._compareText(left && left.node_key, right && right.node_key);
  }

  _sortNodes(nodes, mode = "favorites_recent") {
    const selectedMode = ["favorites_recent", "last_seen", "name"].includes(mode)
      ? mode
      : "favorites_recent";
    return nodes
      .filter((node) => node && node.node_key != null && String(node.node_key))
      .map((node, index) => ({ node, index }))
      .sort((left, right) => this._compareNodes(left.node, right.node, selectedMode) || left.index - right.index)
      .map((item) => item.node);
  }

  _humanLastSeen(value) {
    const timestamp = this._timestampMs(value);
    if (timestamp == null) return "Last seen unknown";
    const ageSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (ageSeconds < 60) return "Last seen just now";
    if (ageSeconds < 3600) return `Last seen ${Math.floor(ageSeconds / 60)}m ago`;
    if (ageSeconds < 86400) return `Last seen ${Math.floor(ageSeconds / 3600)}h ago`;
    if (ageSeconds < 604800) return `Last seen ${Math.floor(ageSeconds / 86400)}d ago`;
    return `Last seen ${new Date(timestamp).toLocaleString()}`;
  }

  _hasValidLocation(node) {
    const location = node && node.location;
    const latitude = this._validCoordinate(location && location.latitude, -90, 90);
    const longitude = this._validCoordinate(location && location.longitude, -180, 180);
    return latitude != null
      && longitude != null
      && !(String(node && node.protocol || "").toLowerCase() === "meshtastic"
        && latitude === 0
        && longitude === 0);
  }

  _validCoordinate(value, minimum, maximum) {
    const candidate = typeof value === "string" && value.trim()
      ? Number(value)
      : value;
    return typeof candidate === "number"
      && Number.isFinite(candidate)
      && candidate >= minimum
      && candidate <= maximum
      ? candidate
      : null;
  }

  _validLocationCount(nodes) {
    return nodes.filter((node) => this._hasValidLocation(node)).length;
  }

  _nodeAliases(node) {
    return [...new Set(
      [node.node_key, node.node_id, node.public_key, node.mac]
        .filter((value) => (typeof value === "string" && value.length) || (typeof value === "number" && Number.isFinite(value)))
        .map((value) => String(value)),
    )];
  }

  _aliasIndex(nodes) {
    const aliases = new Map();
    nodes.forEach((node) => {
      const key = String(node.node_key);
      this._nodeAliases(node).forEach((alias) => {
        if (!aliases.has(alias)) {
          aliases.set(alias, key);
        } else if (aliases.get(alias) !== key) {
          aliases.set(alias, null);
        }
      });
    });
    return aliases;
  }

  _routeIdentifiers(node) {
    if (!node || String(node.protocol || "").toLowerCase() !== "meshcore") return null;
    const routing = node.routing && typeof node.routing === "object" ? node.routing : {};
    const route = Array.isArray(routing.route) ? routing.route : routing.path;
    return Array.isArray(route) ? route : null;
  }

  _hopsGatewayId(node) {
    const connectivity = node && node.connectivity;
    if (String(node && node.protocol || "").toLowerCase() !== "meshtastic") return "";
    if (!connectivity || connectivity.via_mqtt === true) return "";
    const explicit = connectivity && connectivity.hops_gateway_id;
    if (explicit != null && String(explicit)) return String(explicit);
    const lastGateway = node && node.last_gateway_id != null
      ? String(node.last_gateway_id)
      : "";
    const gatewayIds = [...new Set(
      Array.isArray(node && node.gateway_ids)
        ? node.gateway_ids.filter((value) => value != null && String(value)).map(String)
        : [],
    )];
    return connectivity.via_mqtt === false
      && gatewayIds.length === 1
      && gatewayIds[0] === lastGateway
      ? lastGateway
      : "";
  }

  _resolveRouteIdentifier(aliasIndex, value) {
    if (!((typeof value === "string" && value.length) || (typeof value === "number" && Number.isFinite(value)))) {
      return null;
    }
    const alias = String(value);
    return aliasIndex.has(alias) ? aliasIndex.get(alias) : null;
  }

  _passiveTopology(nodes, gateways) {
    const allNodes = nodes.filter(
      (node) => node && node.node_key != null && String(node.node_key),
    );
    const allGateways = gateways.filter(
      (gateway) => gateway && gateway.gateway_id != null && String(gateway.gateway_id),
    );
    const allAliases = this._aliasIndex(allNodes);
    const evidenceNodes = new Set();
    const evidenceGateways = new Set();

    allNodes.forEach((node) => {
      const hops = node.connectivity && node.connectivity.hops;
      const gatewayId = this._hopsGatewayId(node);
      if (typeof hops === "number" && Number.isFinite(hops) && hops === 0 && gatewayId) {
        evidenceNodes.add(String(node.node_key));
        evidenceGateways.add(gatewayId);
      }
      const route = this._routeIdentifiers(node);
      if (!route) return;
      for (let index = 1; index < route.length; index += 1) {
        const left = this._resolveRouteIdentifier(allAliases, route[index - 1]);
        const right = this._resolveRouteIdentifier(allAliases, route[index]);
        if (left && right && left !== right) {
          evidenceNodes.add(left);
          evidenceNodes.add(right);
        }
      }
    });

    const orderedNodes = this._sortNodes(allNodes, "favorites_recent").sort((left, right) => {
      const evidenceDifference = Number(evidenceNodes.has(String(right.node_key)))
        - Number(evidenceNodes.has(String(left.node_key)));
      return evidenceDifference || this._compareNodes(left, right, "favorites_recent");
    });
    const visibleNodes = orderedNodes.slice(0, 36);
    const orderedGateways = [...allGateways].sort((left, right) => {
      const evidenceDifference = Number(evidenceGateways.has(String(right.gateway_id)))
        - Number(evidenceGateways.has(String(left.gateway_id)));
      return evidenceDifference
        || this._compareText(left.name || left.gateway_id, right.name || right.gateway_id)
        || this._compareText(left.gateway_id, right.gateway_id);
    });
    const visibleGateways = orderedGateways.slice(0, 8);
    const visibleAliases = this._aliasIndex(visibleNodes);
    const gatewayKeys = new Map(
      visibleGateways.map((gateway) => [String(gateway.gateway_id), `gateway:${gateway.gateway_id}`]),
    );
    const edges = [];
    const edgeKeys = new Set();
    const addEdge = (from, to, type) => {
      if (!from || !to || from === to) return;
      const endpoints = [from, to].sort();
      const key = `${type}:${endpoints[0]}:${endpoints[1]}`;
      if (edgeKeys.has(key)) return;
      edgeKeys.add(key);
      edges.push({ from, to, type });
    };

    visibleNodes.forEach((node) => {
      const hops = node.connectivity && node.connectivity.hops;
      const gatewayKey = gatewayKeys.get(this._hopsGatewayId(node));
      if (typeof hops === "number" && Number.isFinite(hops) && hops === 0 && gatewayKey) {
        addEdge(gatewayKey, `node:${node.node_key}`, "direct");
      }
      const route = this._routeIdentifiers(node);
      if (!route) return;
      for (let index = 1; index < route.length; index += 1) {
        const left = this._resolveRouteIdentifier(visibleAliases, route[index - 1]);
        const right = this._resolveRouteIdentifier(visibleAliases, route[index]);
        if (left && right) addEdge(`node:${left}`, `node:${right}`, "route");
      }
    });

    return {
      nodes: visibleNodes,
      gateways: visibleGateways,
      edges,
      totalNodes: allNodes.length,
      totalGateways: allGateways.length,
    };
  }

  _graph(topology) {
    const width = 1000;
    const height = 640;
    const centerX = 585;
    const centerY = height / 2;
    const radius = Math.min(360, 115 + topology.nodes.length * 4);
    const points = new Map();
    topology.gateways.forEach((gateway, index) => {
      const spacing = height / (topology.gateways.length + 1);
      points.set(`gateway:${gateway.gateway_id}`, {
        x: 105,
        y: spacing * (index + 1),
        kind: "gateway",
        item: gateway,
      });
    });
    topology.nodes.forEach((node, index) => {
      const angle = (Math.PI * 2 * index) / Math.max(topology.nodes.length, 1) - Math.PI / 2;
      points.set(`node:${node.node_key}`, {
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius,
        kind: "node",
        item: node,
      });
    });
    const shown = topology.totalNodes > topology.nodes.length
      ? `Showing ${topology.nodes.length} of ${topology.totalNodes} nodes`
      : `${topology.nodes.length} nodes`;
    return `
      <section class="topology">
        <div class="topology-heading">
          <strong class="topology-copy">
            <span>Cached passive topology — no traceroutes sent</span>
            <span class="topology-note">Edges are last received evidence, not a live route.</span>
          </strong>
          <span class="label">${shown} · ${topology.gateways.length} gateways</span>
        </div>
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Cached passive mesh topology; no traceroutes sent">
        ${topology.edges.map((edge) => {
          const a = points.get(edge.from);
          const b = points.get(edge.to);
          if (!a || !b) return "";
          return `<line class="link ${edge.type === "direct" ? "direct-link" : "route-link"}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"></line>`;
        }).join("")}
        ${topology.gateways.map((gateway) => {
          const point = points.get(`gateway:${gateway.gateway_id}`);
          return `
          <g>
            <title>${this._escape(gateway.name || gateway.gateway_id)}</title>
            <circle class="gateway ${gateway.connected ? "" : "offline"}" cx="${point.x}" cy="${point.y}" r="18"></circle>
            <text x="${point.x + 24}" y="${point.y + 4}">${this._escape(String(gateway.name || gateway.gateway_id).slice(0, 20))}</text>
          </g>`;
        }).join("")}
        ${topology.nodes.map((node) => {
          const point = points.get(`node:${node.node_key}`);
          return `
          <g>
            <title>${this._escape(this._nodeName(node))}</title>
            <circle class="node ${node.online ? "" : "offline"}" cx="${point.x}" cy="${point.y}" r="14"></circle>
            <text x="${point.x + 19}" y="${point.y + 4}">${this._escape(this._nodeName(node).slice(0, 18))}</text>
          </g>`;
        }).join("")}
        ${topology.edges.length ? "" : '<text class="topology-empty" x="500" y="610">No passive connection evidence yet</text>'}
        </svg>
      </section>
    `;
  }

  _stat(label, value) {
    return `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  }

  _rfMetric(node) {
    const snr = node.connectivity && node.connectivity.snr;
    if (typeof snr === "number") return { label: "SNR", value: `${snr} dB`, score: snr };
    const rssi = node.connectivity && node.connectivity.rssi;
    if (typeof rssi === "number") return { label: "RSSI", value: `${rssi} dBm`, score: (rssi + 120) / 4 };
    return null;
  }

  _quality(metric) {
    if (!metric) return "warn";
    if (metric.score >= 6) return "good";
    if (metric.score >= 0) return "warn";
    return "bad";
  }

  _escape(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }
}

customElements.define("meshnet-panel", MeshNetPanel);
