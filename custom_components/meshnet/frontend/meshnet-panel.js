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
    this._snapshotGeneration = 0;
    this._snapshotLastAttemptAt = null;
    this._snapshotLastSuccessAt = null;
    this._snapshotConsecutiveFailures = 0;
    this._failureTelemetry = [];
    this._failureOccurrences = new Map();
    this._failureConsecutive = new Map();
    this._recordedFailures = new WeakSet();
    this._failureCount = 0;
    this._panelReportQueue = [];
    this._panelReportActive = false;
    this._panelReportFailureCount = 0;
    this._windowErrorHandler = null;
    this._windowRejectionHandler = null;
  }

  set hass(hass) {
    this._hass = hass;
    this._startPolling();
    this._drainPanelReports();
  }

  connectedCallback() {
    this._connected = true;
    this._attachWindowFailureHandlers();
    this._startPolling();
    this._drainPanelReports();
  }

  disconnectedCallback() {
    this._connected = false;
    this._detachWindowFailureHandlers();
    this._loaded = false;
    this._pollEpoch += 1;
    this._snapshotGeneration += 1;
    if (this._pollTimer != null) window.clearTimeout(this._pollTimer);
    this._pollTimer = null;
  }

  _startPolling() {
    if (!this._connected || !this._hass || this._loaded) return;
    this._loaded = true;
    const epoch = ++this._pollEpoch;
    void this._load(epoch).catch((error) => {
      if (!this._pollIsCurrent(epoch)) return;
      this._recordFailure("poll_unexpected", "lifecycle", error);
      this._safeRender("render");
      this._scheduleNextPoll(epoch);
    });
  }

  _pollIsCurrent(epoch) {
    return this._connected && this._pollEpoch === epoch;
  }

  async _load(epoch) {
    try {
      await this._refreshSnapshot(epoch, "snapshot_request");
    } catch (error) {
      if (!this._failureWasRecorded(error)) {
        this._recordFailure("poll_unexpected", "lifecycle", error);
      }
      this._error = "Snapshot unavailable";
    } finally {
      if (!this._pollIsCurrent(epoch)) return;
      this._safeRender("render");
      this._scheduleNextPoll(epoch);
    }
  }

  _scheduleNextPoll(epoch) {
    if (!this._pollIsCurrent(epoch) || this._pollTimer != null) return;
    try {
      this._pollTimer = window.setTimeout(() => {
        if (!this._pollIsCurrent(epoch)) return;
        this._pollTimer = null;
        this._loaded = false;
        this._startPolling();
      }, 5000);
    } catch (error) {
      this._loaded = false;
      this._recordFailure("poll_schedule", "lifecycle", error);
    }
  }

  _safeRender(operation = "render") {
    try {
      this._render();
      this._markOperationSuccess(operation);
      return true;
    } catch (error) {
      this._recordFailure(operation, "render", error);
      return false;
    }
  }

  _safeStep(operation, category, callback) {
    try {
      const result = callback();
      if (result && typeof result.then === "function") {
        void result.then(
          () => this._markOperationSuccess(operation),
          (error) => this._recordFailure(operation, category, error),
        );
      } else {
        this._markOperationSuccess(operation);
      }
      return result;
    } catch (error) {
      this._recordFailure(operation, category, error);
      return null;
    }
  }

  _safeOperation(value, allowed, fallback) {
    return allowed.includes(value) ? value : fallback;
  }

  _safeErrorType(error) {
    let candidate = "";
    try {
      candidate = error && typeof error.name === "string"
        ? error.name
        : error && error.constructor && typeof error.constructor.name === "string"
          ? error.constructor.name
          : "";
    } catch (_ignored) {
      candidate = "";
    }
    const allowed = [
      "Error",
      "TypeError",
      "RangeError",
      "ReferenceError",
      "SyntaxError",
      "DOMException",
      "HomeAssistantError",
      "WebSocketError",
      "PanelTimeoutError",
      "PanelSchemaError",
      "ValidationError",
    ];
    return allowed.includes(candidate) ? candidate : "OtherError";
  }

  _safeErrorCode(error) {
    let candidate = "";
    try {
      candidate = error && typeof error.code === "string" ? error.code : "";
    } catch (_ignored) {
      candidate = "";
    }
    const allowed = [
      "timeout",
      "recipient_unavailable",
      "missing_message_id",
      "snapshot_not_object",
      "nodes_not_object",
      "gateways_not_object",
      "messages_not_array",
      "panel_metadata_not_object",
      "node_record_invalid",
      "gateway_record_invalid",
      "message_record_invalid",
      "unauthorized",
      "not_found",
      "not_configured",
      "connection_lost",
      "unknown_error",
      "invalid_format",
    ];
    return allowed.includes(candidate) ? candidate : "other";
  }

  _failureWasRecorded(error) {
    return Boolean(
      error
      && (typeof error === "object" || typeof error === "function")
      && this._recordedFailures.has(error),
    );
  }

  _recordFailure(operation, category, error, { report = true } = {}) {
    const safeOperation = this._safeOperation(operation, [
      "snapshot_request",
      "post_send_refresh",
      "poll_unexpected",
      "poll_schedule",
      "render",
      "bind_composer",
      "bind_nodes",
      "restore_focus",
      "composer_event",
      "sort_event",
      "node_message_event",
      "event_handler",
      "global_error",
      "unhandled_rejection",
      "invalid_recipient",
      "send_submission",
      "send_finalize",
      "reporting",
    ], "poll_unexpected");
    const safeCategory = this._safeOperation(category, [
      "websocket",
      "timeout",
      "schema",
      "render",
      "binding",
      "focus",
      "validation",
      "lifecycle",
    ], "lifecycle");
    const errorType = this._safeErrorType(error);
    const errorCode = this._safeErrorCode(error);
    const signature = `${safeOperation}:${safeCategory}:${errorType}:${errorCode}`;
    const occurrence = (this._failureOccurrences.get(signature) || 0) + 1;
    const consecutive = (this._failureConsecutive.get(safeOperation) || 0) + 1;
    this._failureOccurrences.set(signature, occurrence);
    this._failureConsecutive.set(safeOperation, consecutive);
    this._failureCount += 1;
    if (safeOperation === "snapshot_request" || safeOperation === "post_send_refresh") {
      this._snapshotConsecutiveFailures += 1;
    }
    if (error && (typeof error === "object" || typeof error === "function")) {
      this._recordedFailures.add(error);
    }
    const event = {
      operation: safeOperation,
      category: safeCategory,
      error_type: errorType,
      error_code: errorCode,
      occurrence,
      consecutive,
      at_ms: Date.now(),
    };
    this._failureTelemetry.push(event);
    if (this._failureTelemetry.length > 100) {
      this._failureTelemetry.splice(0, this._failureTelemetry.length - 100);
    }
    if (
      this._shouldWarnOccurrence(occurrence)
      && typeof console !== "undefined"
      && typeof console.warn === "function"
    ) {
      try {
        console.warn(
          "MeshNet panel failure operation=%s category=%s type=%s code=%s occurrence=%d consecutive=%d",
          safeOperation,
          safeCategory,
          errorType,
          errorCode,
          occurrence,
          consecutive,
        );
      } catch (_ignored) {
        // Browser logging must never become a panel failure itself.
      }
    }
    if (report && safeOperation !== "reporting") this._queuePanelReport(event);
    return event;
  }

  _markOperationSuccess(operation) {
    this._failureConsecutive.set(operation, 0);
  }

  _shouldWarnOccurrence(occurrence) {
    return occurrence <= 3
      || (occurrence & (occurrence - 1)) === 0
      || occurrence % 100 === 0;
  }

  _queuePanelReport(event) {
    const report = this._backendPanelReport(event);
    this._panelReportQueue.push(report);
    if (this._panelReportQueue.length > 100) {
      this._panelReportQueue.splice(0, this._panelReportQueue.length - 100);
    }
    this._drainPanelReports();
  }

  _backendPanelReport(event) {
    const operationMap = {
      snapshot_request: "snapshot",
      post_send_refresh: "post_send_refresh",
      poll_unexpected: "poll",
      poll_schedule: "poll",
      render: "render",
      bind_composer: "event_handler",
      bind_nodes: "event_handler",
      restore_focus: "event_handler",
      composer_event: "event_handler",
      sort_event: "event_handler",
      node_message_event: "event_handler",
      event_handler: "event_handler",
      global_error: "global_error",
      unhandled_rejection: "unhandled_rejection",
      invalid_recipient: "invalid_recipient",
      send_submission: "send_message",
      send_finalize: "render",
      reporting: "reporting",
    };
    let operation = operationMap[event.operation] || "reporting";
    if (
      (event.operation === "snapshot_request" || event.operation === "post_send_refresh")
      && event.category === "schema"
    ) operation = "snapshot_schema";
    if (
      (event.operation === "snapshot_request" || event.operation === "post_send_refresh")
      && event.category === "timeout"
    ) operation = "snapshot_timeout";

    const categoryMap = {
      websocket: "connection",
      timeout: "timeout",
      schema: "data",
      render: "internal",
      binding: "internal",
      focus: "internal",
      validation: "validation",
      lifecycle: "lifecycle",
    };
    let category = categoryMap[event.category] || "unknown";
    if (event.error_code === "unauthorized") category = "authentication";
    if (event.error_code === "not_found" || event.error_code === "not_configured") {
      category = "availability";
    }

    const errorTypeMap = {
      Error: "Error",
      TypeError: "TypeError",
      SyntaxError: "SyntaxError",
      DOMException: "DOMException",
      HomeAssistantError: "HomeAssistantError",
      WebSocketError: "WebSocketError",
      PanelTimeoutError: "TimeoutError",
      PanelSchemaError: "SchemaError",
      ValidationError: "ServiceValidationError",
      OtherError: "other_error",
    };
    const errorCodeMap = {
      timeout: "timeout",
      recipient_unavailable: "invalid_recipient",
      missing_message_id: "invalid_response",
      snapshot_not_object: "invalid_schema",
      nodes_not_object: "invalid_schema",
      gateways_not_object: "invalid_schema",
      messages_not_array: "invalid_schema",
      panel_metadata_not_object: "invalid_schema",
      node_record_invalid: "invalid_schema",
      gateway_record_invalid: "invalid_schema",
      message_record_invalid: "invalid_schema",
      not_found: "unavailable",
      not_configured: "unavailable",
      connection_lost: "connection_failed",
      invalid_format: "invalid_schema",
      unauthorized: "operation_failed",
      unknown_error: "unexpected_error",
    };
    const defaultCodes = {
      snapshot: "snapshot_failed",
      snapshot_schema: "invalid_schema",
      snapshot_timeout: "timeout",
      post_send_refresh: "post_send_refresh_failed",
      send_message: "send_failed",
      render: "render_failed",
      poll: "poll_failed",
      event_handler: "handler_failed",
      invalid_recipient: "invalid_recipient",
      reporting: "report_failed",
      global_error: "unexpected_error",
      unhandled_rejection: "unexpected_error",
    };
    const occurrence = Number.isSafeInteger(event.occurrence) && event.occurrence > 0
      ? Math.min(1000000, event.occurrence)
      : 1;
    const consecutive = Number.isSafeInteger(event.consecutive) && event.consecutive > 0
      ? Math.min(1000000, event.consecutive)
      : 1;
    return {
      operation,
      category,
      error_type: errorTypeMap[event.error_type] || "other_error",
      error_code: errorCodeMap[event.error_code] || defaultCodes[operation] || "unexpected_error",
      occurrence,
      consecutive,
    };
  }

  _drainPanelReports() {
    if (
      this._panelReportActive
      || !this._connected
      || !this._hass
      || typeof this._hass.callWS !== "function"
      || !this._panelReportQueue.length
    ) return;
    this._panelReportActive = true;
    let blockedByFailure = false;
    void (async () => {
      while (this._connected && this._panelReportQueue.length) {
        const report = this._panelReportQueue.shift();
        try {
          await this._hass.callWS({ type: "meshnet/panel_log", ...report });
          this._markOperationSuccess("reporting");
        } catch (error) {
          blockedByFailure = true;
          this._panelReportFailureCount += 1;
          this._panelReportQueue.unshift(report);
          this._recordFailure("reporting", "websocket", error, { report: false });
          break;
        }
      }
    })()
      .catch((_ignored) => {
        blockedByFailure = true;
        this._panelReportFailureCount += 1;
      })
      .finally(() => {
        this._panelReportActive = false;
        if (!blockedByFailure && this._panelReportQueue.length) {
          this._drainPanelReports();
        }
      });
  }

  _attachWindowFailureHandlers() {
    if (
      this._windowErrorHandler
      || typeof window === "undefined"
      || typeof window.addEventListener !== "function"
    ) return;
    const errorHandler = (event) => {
      let error = null;
      try {
        error = event && event.error;
      } catch (_ignored) {
        error = null;
      }
      if (!this._windowFailureBelongsToPanel(event, error)) return;
      this._recordFailure(
        "global_error",
        "lifecycle",
        error || { name: "Error", code: "other" },
      );
    };
    const rejectionHandler = (event) => {
      let reason = null;
      try {
        reason = event && event.reason;
      } catch (_ignored) {
        reason = null;
      }
      if (!this._windowFailureBelongsToPanel(event, reason)) return;
      this._recordFailure(
        "unhandled_rejection",
        "lifecycle",
        reason || { name: "Error", code: "other" },
      );
    };
    try {
      window.addEventListener("error", errorHandler);
      window.addEventListener("unhandledrejection", rejectionHandler);
      this._windowErrorHandler = errorHandler;
      this._windowRejectionHandler = rejectionHandler;
    } catch (error) {
      try {
        if (typeof window.removeEventListener === "function") {
          window.removeEventListener("error", errorHandler);
          window.removeEventListener("unhandledrejection", rejectionHandler);
        }
      } catch (_ignored) {
        // Cleanup remains best effort; neither browser error carries identifiers.
      }
      this._recordFailure("event_handler", "binding", error);
    }
  }

  _windowFailureBelongsToPanel(event, error) {
    let filename = "";
    let stack = "";
    try {
      filename = event && typeof event.filename === "string" ? event.filename : "";
    } catch (_ignored) {
      filename = "";
    }
    try {
      stack = error && typeof error.stack === "string" ? error.stack : "";
    } catch (_ignored) {
      stack = "";
    }
    return filename.includes("meshnet-panel.js")
      || filename.includes("/meshnet_static/")
      || stack.includes("meshnet-panel.js")
      || stack.includes("/meshnet_static/");
  }

  _detachWindowFailureHandlers() {
    const errorHandler = this._windowErrorHandler;
    const rejectionHandler = this._windowRejectionHandler;
    this._windowErrorHandler = null;
    this._windowRejectionHandler = null;
    if (typeof window === "undefined" || typeof window.removeEventListener !== "function") return;
    try {
      if (errorHandler) window.removeEventListener("error", errorHandler);
      if (rejectionHandler) window.removeEventListener("unhandledrejection", rejectionHandler);
    } catch (error) {
      this._recordFailure("event_handler", "lifecycle", error);
    }
  }

  _render() {
    const composerFocus = this._composerFocusState();
    const snapshot = this._snapshot || { nodes: {}, gateways: {}, recent_messages: [] };
    const nodes = Object.values(snapshot.nodes || {}).filter(
      (node) => node && typeof node === "object" && !Array.isArray(node),
    );
    const gateways = Object.values(snapshot.gateways || {}).filter(
      (gateway) => gateway && typeof gateway === "object" && !Array.isArray(gateway),
    );
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
        .diagnostic-detail {
          color: var(--secondary-text-color);
          font-size: 11px;
          overflow-wrap: anywhere;
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
              <a class="map-link" href="/map">Map · ${locatedNodeCount} cached locations</a>
              <span class="${this._error ? "bad" : "good"}">${this._escape(this._error || "Snapshot current")}</span>
            </div>
          </div>
          <div class="stats">
            ${this._stat("Nodes", nodes.length)}
            ${this._stat("Recently seen", nodes.filter((node) => node.online).length)}
            ${this._stat("Gateways", gateways.filter((gateway) => gateway.connected).length + "/" + gateways.length)}
            ${this._stat("Cached health", snapshot.mesh_health_score == null ? "n/a" : snapshot.mesh_health_score + "%")}
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
          ${this._panelDiagnostics(snapshot.panel_metadata, nodes)}
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
                  <span class="${node.online ? "good" : "bad"}">${node.online ? "recent" : "stale"}</span>
                  <button class="node-message" type="button" data-message-node="${this._escape(node.node_key)}">Message</button>
                </span>
              </div>
            `).join("") || `<div class="label">Waiting for node data</div>`}
            ${sortedNodes.length > 24 ? `<div class="label">Showing 24 of ${sortedNodes.length} nodes</div>` : ""}
            ${favoriteLabelConfigured ? "" : '<div class="label">To pin favorites, add the Home Assistant device label “MeshNet Favorite”.</div>'}
          </section>
          <section class="panel">
            <h2>Messages</h2>
            ${(Array.isArray(snapshot.recent_messages) ? snapshot.recent_messages : [])
              .filter((message) => message && typeof message === "object" && !Array.isArray(message))
              .slice(-8).reverse().map((message) => `
              <div class="msg">
                <div>${this._escape(message.text || "")}</div>
                <div class="meta">${this._escape(message.sender || "unknown")} → ${this._escape(message.receiver || message.channel || "broadcast")}</div>
              </div>
            `).join("") || `<div class="label">No messages recorded</div>`}
          </section>
          <section class="panel">
            <h2>RF Heat</h2>
            <div class="label">Cached SNR/RSSI may be stale or multi-hop and is not a distance estimate.</div>
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
    this._safeStep("bind_composer", "binding", () => this._bindComposer());
    this._safeStep("bind_nodes", "binding", () => this._bindNodeControls());
    this._safeStep("restore_focus", "focus", () => this._restoreComposerFocus(composerFocus));
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
        this._safeStep("composer_event", "binding", () => {
          this._draft[key] = field.value;
          if (key === "delivery") {
            this._sendStatus = null;
            this._safeRender("render");
          }
        });
      });
    });
    form.addEventListener("submit", (event) => {
      this._safeStep(
        "composer_event",
        "binding",
        () => this._sendMessage(event),
      );
    });
  }

  _bindNodeControls() {
    const sort = this.querySelector("#meshnet-node-sort");
    if (sort) {
      sort.addEventListener("change", () => {
        this._safeStep("sort_event", "binding", () => {
          this._nodeSort = ["favorites_recent", "last_seen", "name"].includes(sort.value)
            ? sort.value
            : "favorites_recent";
          this._safeRender("render");
        });
      });
    }
    this.querySelectorAll("[data-message-node]").forEach((button) => {
      button.addEventListener("click", () => {
        this._safeStep("node_message_event", "binding", () => {
          if (!this._chooseDirectRecipient(button.getAttribute("data-message-node"))) {
            this._recordFailure(
              "invalid_recipient",
              "validation",
              { name: "ValidationError", code: "recipient_unavailable" },
            );
            this._sendStatus = { kind: "bad", text: "Message target is no longer available." };
            this._safeRender("render");
            return;
          }
          this._sendStatus = null;
          this._safeRender("render");
          const message = this.querySelector("#meshnet-message");
          if (message) message.focus();
        });
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
      this._safeRender("render");
      return;
    }
    if (this._messageByteLength(draft.message) > 237) {
      this._sendStatus = { kind: "bad", text: "Message must be 237 UTF-8 bytes or fewer." };
      this._safeRender("render");
      return;
    }

    const delivery = draft.delivery === "direct" ? "direct" : "broadcast";
    const recipient = draft.recipient.trim();
    if (delivery === "direct" && (!recipient || !this._isKnownRecipient(recipient))) {
      this._recordFailure(
        "invalid_recipient",
        "validation",
        { name: "ValidationError", code: "recipient_unavailable" },
      );
      this._sendStatus = { kind: "bad", text: "Choose an available direct recipient." };
      this._safeRender("render");
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
    this._safeRender("render");

    try {
      const result = await this._hass.callWS(payload);
      if (!result || !result.message_id) {
        const error = { name: "PanelSchemaError", code: "missing_message_id" };
        this._recordFailure("send_submission", "schema", error);
        throw error;
      }
      this._markOperationSuccess("send_submission");

      if (this._draft.message === draft.message) this._draft.message = "";
      let snapshot = null;
      try {
        snapshot = await this._refreshSnapshot(this._pollEpoch, "post_send_refresh");
      } catch (error) {
        if (!this._failureWasRecorded(error)) {
          this._recordFailure("post_send_refresh", "websocket", error);
        }
        // The send was accepted; a failed status refresh must not report it as failed.
      }
      const sentMessage = snapshot && (snapshot.recent_messages || []).find(
        (message) => message.message_id === result.message_id,
      );
      const status = sentMessage && sentMessage.raw && sentMessage.raw.status;
      this._sendStatus = status === "sent"
        ? { kind: "good", text: "Message sent." }
        : { kind: "warn", text: "Message queued for delivery." };
    } catch (error) {
      if (!this._failureWasRecorded(error)) {
        this._recordFailure("send_submission", "websocket", error);
      }
      this._sendStatus = { kind: "bad", text: "Message could not be submitted." };
    } finally {
      this._sending = false;
      this._safeRender("send_finalize");
    }
  }

  _messageByteLength(message) {
    return new TextEncoder().encode(String(message)).length;
  }

  async _refreshSnapshot(epoch = this._pollEpoch, operation = "snapshot_request") {
    const generation = ++this._snapshotGeneration;
    this._snapshotLastAttemptAt = Date.now();
    let snapshot;
    try {
      snapshot = await this._withTimeout(
        this._hass.callWS({ type: "meshnet/snapshot" }),
        15000,
      );
    } catch (error) {
      if (!this._snapshotRequestIsCurrent(epoch, generation)) return null;
      this._recordFailure(
        operation,
        this._safeErrorCode(error) === "timeout" ? "timeout" : "websocket",
        error,
      );
      throw error;
    }
    if (!this._snapshotRequestIsCurrent(epoch, generation)) return null;
    try {
      this._validateSnapshot(snapshot);
    } catch (error) {
      this._recordFailure(operation, "schema", error);
      throw error;
    }
    this._snapshot = snapshot;
    this._error = null;
    this._snapshotLastSuccessAt = Date.now();
    this._snapshotConsecutiveFailures = 0;
    this._markOperationSuccess(operation);
    this._drainPanelReports();
    return snapshot;
  }

  _snapshotRequestIsCurrent(epoch, generation) {
    return this._connected
      && this._pollEpoch === epoch
      && this._snapshotGeneration === generation;
  }

  async _withTimeout(promise, timeoutMs) {
    let timer = null;
    const timeout = new Promise((_resolve, reject) => {
      timer = window.setTimeout(() => {
        reject({ name: "PanelTimeoutError", code: "timeout" });
      }, timeoutMs);
    });
    try {
      return await Promise.race([Promise.resolve(promise), timeout]);
    } finally {
      if (timer != null) {
        try {
          window.clearTimeout(timer);
        } catch (_ignored) {
          // A timeout cleanup failure cannot alter snapshot or polling state.
        }
      }
    }
  }

  _validateSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
      throw { name: "PanelSchemaError", code: "snapshot_not_object" };
    }
    if (!snapshot.nodes || typeof snapshot.nodes !== "object" || Array.isArray(snapshot.nodes)) {
      throw { name: "PanelSchemaError", code: "nodes_not_object" };
    }
    if (!snapshot.gateways || typeof snapshot.gateways !== "object" || Array.isArray(snapshot.gateways)) {
      throw { name: "PanelSchemaError", code: "gateways_not_object" };
    }
    if (!Array.isArray(snapshot.recent_messages)) {
      throw { name: "PanelSchemaError", code: "messages_not_array" };
    }
    if (
      snapshot.panel_metadata != null
      && (typeof snapshot.panel_metadata !== "object" || Array.isArray(snapshot.panel_metadata))
    ) {
      throw { name: "PanelSchemaError", code: "panel_metadata_not_object" };
    }
    if (Object.values(snapshot.nodes).some(
      (node) => !node || typeof node !== "object" || Array.isArray(node),
    )) {
      throw { name: "PanelSchemaError", code: "node_record_invalid" };
    }
    if (Object.values(snapshot.gateways).some(
      (gateway) => !gateway || typeof gateway !== "object" || Array.isArray(gateway),
    )) {
      throw { name: "PanelSchemaError", code: "gateway_record_invalid" };
    }
    if (snapshot.recent_messages.some(
      (message) => !message || typeof message !== "object" || Array.isArray(message),
    )) {
      throw { name: "PanelSchemaError", code: "message_record_invalid" };
    }
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

  _panelDiagnostics(metadata, nodes) {
    const safeMetadata = metadata && typeof metadata === "object" && !Array.isArray(metadata)
      ? metadata
      : {};
    const total = this._metadataCount(safeMetadata, "total_node_count", nodes.length);
    const current = this._metadataCount(safeMetadata, "current_session_node_count");
    const cached = this._metadataCount(safeMetadata, "cached_only_node_count");
    const analyzed = this._metadataCount(safeMetadata, "analyzed_node_count");
    const omitted = this._metadataCount(safeMetadata, "omitted_node_count", 0);
    const online = this._metadataCount(safeMetadata, "online_node_count");
    const located = this._metadataCount(safeMetadata, "located_node_count");
    const locatedOffline = this._metadataCount(safeMetadata, "located_offline_node_count");
    const mqtt = this._metadataCount(safeMetadata, "mqtt_node_count");
    const mqttUnknown = this._metadataCount(safeMetadata, "mqtt_unknown_node_count");
    const collisionGroups = this._metadataCount(safeMetadata, "identity_collision_group_count");
    const collisionNodes = this._metadataCount(safeMetadata, "identity_collision_node_count");
    const lastFailure = this._failureTelemetry.length
      ? this._failureTelemetry[this._failureTelemetry.length - 1]
      : null;
    const localFreshness = this._ageLabel(this._snapshotLastSuccessAt);
    const generatedTimestamp = this._timestampMs(safeMetadata.last_snapshot_generated_at);
    const serverFreshness = generatedTimestamp == null ? "not reported" : this._ageLabel(generatedTimestamp);
    const polling = this._connected
      ? this._loaded ? "active" : "starting"
      : "stopped";
    const provenanceAvailable = current !== "n/a" || cached !== "n/a";
    return `
      <section class="panel">
        <h2>Panel diagnostics</h2>
        <div class="row">
          <span>Snapshot</span>
          <span class="${this._snapshotLastSuccessAt == null ? "warn" : "good"}">${this._escape(localFreshness)}</span>
        </div>
        <div class="row">
          <span>Polling</span>
          <span>${this._escape(polling)}</span>
        </div>
        <div class="row">
          <span>Failures</span>
          <span>${this._failureCount} total · ${this._snapshotConsecutiveFailures} snapshot</span>
        </div>
        <div class="row">
          <span>Reports</span>
          <span>${this._panelReportQueue.length} queued · ${this._panelReportFailureCount} failed</span>
        </div>
        <div class="row">
          <span>Nodes</span>
          <span>${this._escape(String(total))} total · ${this._escape(String(online))} recent · ${this._escape(String(located))} located</span>
        </div>
        ${provenanceAvailable ? `
          <div class="row">
            <span>Node source</span>
            <span>${this._escape(String(current))} gateway-reported · ${this._escape(String(cached))} retained cache only</span>
          </div>
          <div class="diagnostic-detail">Gateway-reported nodes can include the radio’s stored node database; this does not mean they were directly heard this session.</div>
        ` : ""}
        ${omitted > 0 ? `
          <div class="row">
            <span>Panel safety limit</span>
            <span>${this._escape(String(analyzed))} analyzed · ${this._escape(String(omitted))} omitted</span>
          </div>
          <div class="diagnostic-detail">Omitted nodes remain in Home Assistant; the recurring sidebar projection is capped to protect the event loop.</div>
        ` : ""}
        ${mqtt !== "n/a" || mqttUnknown !== "n/a" ? `
          <div class="row">
            <span>Nodes marked MQTT</span>
            <span>${this._escape(String(mqtt))} yes · ${this._escape(String(mqttUnknown))} unknown</span>
          </div>
          <div class="diagnostic-detail">This can be historical node metadata and does not mean this MeshNet gateway currently uses MQTT.</div>
        ` : ""}
        ${locatedOffline !== "n/a" ? `
          <div class="row">
            <span>Cached map locations</span>
            <span>${this._escape(String(locatedOffline))} not recently seen</span>
          </div>
        ` : ""}
        ${collisionGroups !== "n/a" || collisionNodes !== "n/a" ? `
          <div class="row">
            <span>Possible identity overlap</span>
            <span>${this._escape(String(collisionGroups))} groups · ${this._escape(String(collisionNodes))} nodes</span>
          </div>
        ` : ""}
        <div class="row">
          <span>Server snapshot</span>
          <span>${this._escape(serverFreshness)}</span>
        </div>
        <div class="diagnostic-detail">${lastFailure
          ? this._escape(`${lastFailure.operation} · ${lastFailure.category} · ${lastFailure.error_type} · ${lastFailure.error_code}`)
          : "No panel failures recorded"}</div>
      </section>
    `;
  }

  _metadataCount(metadata, key, fallback = "n/a") {
    const value = metadata[key];
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
      ? value
      : fallback;
  }

  _ageLabel(timestamp) {
    if (typeof timestamp !== "number" || !Number.isFinite(timestamp)) return "never";
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 2) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
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

if (!customElements.get("meshnet-panel")) {
  customElements.define("meshnet-panel", MeshNetPanel);
}
