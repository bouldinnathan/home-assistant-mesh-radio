class MeshNetPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
  }

  async _load() {
    try {
      this._snapshot = await this._hass.callWS({ type: "meshnet/snapshot" });
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
    window.setTimeout(() => {
      this._loaded = false;
      if (this._hass) this.hass = this._hass;
    }, 5000);
  }

  _render() {
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
