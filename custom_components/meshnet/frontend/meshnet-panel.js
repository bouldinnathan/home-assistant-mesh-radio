class MeshNetPanel extends HTMLElement {
  constructor() {
    super();
    this._draft = {
      recipient: "",
      gateway: "",
      message: "",
      channel: "0",
      priority: "normal",
    };
    this._sending = false;
    this._sendStatus = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
  }

  async _load() {
    try {
      await this._refreshSnapshot();
    } catch (_err) {
      this._error = "Snapshot unavailable";
    }
    this._render();
    window.setTimeout(() => {
      this._loaded = false;
      if (this._hass) this.hass = this._hass;
    }, 5000);
  }

  _render() {
    const composerFocus = this._composerFocusState();
    const snapshot = this._snapshot || { nodes: {}, gateways: {}, recent_messages: [] };
    const nodes = Object.values(snapshot.nodes || {});
    const gateways = Object.values(snapshot.gateways || {});
    const links = this._links(nodes);
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
        svg {
          width: 100%;
          height: min(68vh, 720px);
          min-height: 420px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
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
        circle.offline { fill: var(--disabled-text-color); }
        line.link { stroke: var(--divider-color); stroke-width: 1.4; }
        text { fill: var(--primary-text-color); font-size: 12px; }
        @media (max-width: 900px) {
          .wrap { grid-template-columns: 1fr; padding: 10px; }
          .stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
          svg { min-height: 360px; height: 56vh; }
        }
      </style>
      <div class="wrap">
        <main>
          <div class="toolbar">
            <h1>MeshNet</h1>
            <span class="${this._error ? "bad" : "good"}">${this._escape(this._error || "Live")}</span>
          </div>
          <div class="stats">
            ${this._stat("Nodes", nodes.length)}
            ${this._stat("Online", nodes.filter((node) => node.online).length)}
            ${this._stat("Gateways", gateways.filter((gateway) => gateway.connected).length + "/" + gateways.length)}
            ${this._stat("Health", snapshot.mesh_health_score == null ? "n/a" : snapshot.mesh_health_score + "%")}
          </div>
          ${this._graph(nodes, links)}
        </main>
        <aside class="side">
          <section class="panel">
            <h2>Send message</h2>
            <form class="composer" id="meshnet-send-form">
              <label>
                Recipient
                <select id="meshnet-recipient">
                  ${this._recipientOptions(nodes)}
                </select>
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
              <button type="submit"${this._sending ? " disabled" : ""}>${this._sending ? "Sending…" : "Send"}</button>
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
            <h2>Nodes</h2>
            ${nodes.slice(0, 24).map((node) => `
              <div class="row">
                <span>${this._escape(node.long_name || node.user_name || node.node_id || node.node_key)}</span>
                <span class="${node.online ? "good" : "bad"}">${node.online ? "online" : "offline"}</span>
              </div>
            `).join("") || `<div class="label">Waiting for node data</div>`}
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
    this._restoreComposerFocus(composerFocus);
  }

  _composerFocusState() {
    const active = this.ownerDocument && this.ownerDocument.activeElement;
    const fieldIds = [
      "meshnet-recipient",
      "meshnet-gateway",
      "meshnet-message",
      "meshnet-channel",
      "meshnet-priority",
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
      });
    });
    form.addEventListener("submit", (event) => this._sendMessage(event));
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

    const recipient = draft.recipient.trim();
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
      message_type: recipient ? "direct" : "broadcast",
    };
    if (recipient) payload.target_node = recipient;
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

  _recipientOptions(nodes) {
    const selected = String(this._draft.recipient || "");
    const choices = nodes
      .filter((node) => node && node.node_key != null && String(node.node_key))
      .map((node) => ({
        value: String(node.node_key),
        label: String(node.long_name || node.user_name || node.short_name || node.node_id || node.node_key),
      }))
      .filter((choice, index, all) => all.findIndex((item) => item.value === choice.value) === index)
      .sort((left, right) => left.label.localeCompare(right.label));
    if (selected && !choices.some((choice) => choice.value === selected)) {
      choices.push({ value: selected, label: `${selected} (currently unavailable)` });
    }
    return [
      `<option value=""${this._selected(selected, "")}>Broadcast</option>`,
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

  _graph(nodes, links) {
    const width = 1000;
    const height = 640;
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.36;
    const points = new Map();
    nodes.forEach((node, index) => {
      const angle = (Math.PI * 2 * index) / Math.max(nodes.length, 1) - Math.PI / 2;
      points.set(node.node_key, {
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius,
        node,
      });
    });
    return `
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Mesh topology">
        ${links.map((link) => {
          const a = points.get(link[0]);
          const b = points.get(link[1]);
          if (!a || !b) return "";
          return `<line class="link" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"></line>`;
        }).join("")}
        ${Array.from(points.values()).map(({ x, y, node }) => `
          <g>
            <circle class="node ${node.online ? "" : "offline"}" cx="${x}" cy="${y}" r="16"></circle>
            <text x="${x + 22}" y="${y + 4}">${this._escape((node.short_name || node.long_name || node.node_id || node.node_key).slice(0, 22))}</text>
          </g>
        `).join("")}
      </svg>
    `;
  }

  _links(nodes) {
    const links = [];
    const byGateway = new Map();
    nodes.forEach((node) => {
      const gateway = node.last_gateway_id || "unknown";
      if (!byGateway.has(gateway)) byGateway.set(gateway, []);
      byGateway.get(gateway).push(node.node_key);
    });
    byGateway.forEach((items) => {
      for (let i = 1; i < items.length; i += 1) links.push([items[0], items[i]]);
    });
    nodes.forEach((node) => {
      const route = node.routing && (node.routing.route || node.routing.path);
      if (Array.isArray(route)) {
        for (let i = 1; i < route.length; i += 1) links.push([route[i - 1], route[i]]);
      }
    });
    return links;
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
