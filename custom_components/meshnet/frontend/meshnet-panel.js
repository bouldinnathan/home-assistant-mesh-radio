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
    this._pollRenderPending = false;
    this._focusFlushGeneration = 0;
    this._activeView = "mesh";
    this._settingsGateways = [];
    this._settingsSnapshot = null;
    this._settingsGatewayId = "";
    this._settingsDraft = Object.create(null);
    this._settingsPreview = null;
    this._settingsStatus = null;
    this._settingsResultWarnings = [];
    this._settingsBusy = null;
    this._settingsCriticalConfirmed = false;
    this._settingsRequestGeneration = 0;
  }

  set hass(hass) {
    this._hass = hass;
    this._startPolling();
    this._drainPanelReports();
    this._maybeLoadSettings();
  }

  connectedCallback() {
    this._connected = true;
    this._attachWindowFailureHandlers();
    this._startPolling();
    this._drainPanelReports();
    this._maybeLoadSettings();
  }

  disconnectedCallback() {
    this._connected = false;
    this._detachWindowFailureHandlers();
    this._loaded = false;
    this._pollRenderPending = false;
    this._focusFlushGeneration += 1;
    this._pollEpoch += 1;
    this._snapshotGeneration += 1;
    this._settingsRequestGeneration += 1;
    this._settingsBusy = null;
    // Treat navigation away from the panel as abandoning the draft. Secret
    // replacements must not linger on a detached custom-element instance.
    this._clearSecretSettingsDrafts();
    this._settingsDraft = Object.create(null);
    this._settingsPreview = null;
    this._settingsSnapshot = null;
    this._settingsGateways = [];
    this._settingsStatus = null;
    this._settingsCriticalConfirmed = false;
    this._settingsResultWarnings = [];
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
      this._renderPollSnapshot();
      this._scheduleNextPoll(epoch);
    });
  }

  _maybeLoadSettings() {
    if (
      this._connected
      && this._hass
      && this._activeView === "settings"
      && !this._settingsSnapshot
      && this._settingsBusy == null
    ) void this._loadGatewaySettings(this._settingsGatewayId);
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
      this._renderPollSnapshot();
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
    this._pollRenderPending = false;
    this._focusFlushGeneration += 1;
    try {
      this._render();
      this._markOperationSuccess(operation);
      return true;
    } catch (error) {
      this._recordFailure(operation, "render", error);
      return false;
    }
  }

  _renderPollSnapshot() {
    if (this._panelInteractionActive()) {
      this._pollRenderPending = true;
      return false;
    }
    return this._safeRender("render");
  }

  _queuePendingPollRender() {
    if (!this._pollRenderPending) return;
    const generation = ++this._focusFlushGeneration;
    const flush = () => {
      if (!this._connected || generation !== this._focusFlushGeneration) return;
      if (this._panelInteractionActive() || !this._pollRenderPending) return;
      this._safeRender("render");
    };
    try {
      if (typeof queueMicrotask === "function") {
        queueMicrotask(flush);
      } else {
        void Promise.resolve().then(flush);
      }
    } catch (error) {
      this._recordFailure("event_handler", "lifecycle", error);
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
      "settings_get",
      "settings_preview",
      "settings_apply",
      "settings_event",
      "bind_settings",
      "bind_views",
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
      settings_get: "settings_get",
      settings_preview: "settings_preview",
      settings_apply: "settings_apply",
      settings_event: "event_handler",
      bind_settings: "event_handler",
      bind_views: "event_handler",
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
      settings_get: "settings_load_failed",
      settings_preview: "settings_preview_failed",
      settings_apply: "settings_apply_failed",
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
    const composerFocus = this._composerFocusState() || this._settingsFocusState();
    if (this._activeView === "settings") {
      this._renderSettings(composerFocus);
      return;
    }
    const snapshot = this._snapshot || { nodes: {}, gateways: {}, recent_messages: [] };
    const sourceNodes = Object.values(snapshot.nodes || {}).filter(
      (node) => node && typeof node === "object" && !Array.isArray(node),
    );
    const nodes = this._nodesWithExactMeshtasticNameHints(sourceNodes);
    const gateways = Object.values(snapshot.gateways || {}).filter(
      (gateway) => gateway && typeof gateway === "object" && !Array.isArray(gateway),
    );
    const sortedNodes = this._sortNodes(nodes, this._nodeSort);
    const directDelivery = this._draft.delivery === "direct";
    const recipientChoices = this._recipientChoices(nodes);
    const unsafeRecipientKeys = new Set(
      recipientChoices
        .filter((choice) => choice.ambiguous || choice.invalidIdentity)
        .map((choice) => choice.value),
    );
    const recipientCount = recipientChoices
      .filter((choice) => !choice.ambiguous && !choice.invalidIdentity).length;
    const locatedNodeCount = this._validLocationCount(nodes);
    const unnamedNodeCount = sourceNodes.filter((node) => {
      if (!this._meshtasticNodeId(node)) return false;
      const names = this._nodeHumanNames(node);
      return !names.primary && !names.short;
    }).length;
    const hintedNodeCount = nodes.filter((node) => node._name_hint_exact_node_id === true).length;
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
        .view-tabs {
          display: flex;
          gap: 6px;
          margin-bottom: 12px;
        }
        .view-tab {
          border: 1px solid var(--divider-color);
          border-radius: 7px;
          padding: 7px 12px;
          color: var(--primary-text-color);
          background: var(--card-background-color);
          cursor: pointer;
          font: inherit;
        }
        .view-tab.active {
          border-color: var(--primary-color);
          color: var(--text-primary-color, #fff);
          background: var(--primary-color);
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
          ${this._viewTabs()}
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
              <button id="meshnet-send-button" type="submit"${this._sending || (directDelivery && !recipientCount) ? " disabled" : ""}>${this._sending ? "Sending…" : "Send"}</button>
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
                  <span class="label">${this._escape(this._humanLastSeen(node.last_heard))}${node._name_hint_exact_node_id === true ? " · Name matched from the same exact !ID" : ""}</span>
                </span>
                <span class="node-actions">
                  <span class="${node.online ? "good" : "bad"}">${node.online ? "recent" : "stale"}</span>
                  <button class="node-message" type="button" data-message-node="${this._escape(node.node_key)}"${unsafeRecipientKeys.has(String(node.node_key)) ? ' disabled aria-disabled="true" title="Unsafe node identity; direct messaging is disabled"' : ""}>${unsafeRecipientKeys.has(String(node.node_key)) ? "Identity blocked" : "Message"}</button>
                </span>
              </div>
            `).join("") || `<div class="label">Waiting for node data</div>`}
            ${sortedNodes.length > 24 ? `<div class="label">Showing 24 of ${sortedNodes.length} nodes</div>` : ""}
            ${unnamedNodeCount ? `<div class="label">${unnamedNodeCount} Meshtastic packet/cache record${unnamedNodeCount === 1 ? " arrived" : "s arrived"} without a NodeInfo name. MeshNet keeps uncertain identities separate.</div>` : ""}
            ${hintedNodeCount ? `<div class="label">${hintedNodeCount} display label${hintedNodeCount === 1 ? " uses" : "s use"} an unambiguous cached name from the same exact !ID. Records and send targets remain separate.</div>` : ""}
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
                    <div>${this._escape(this._nodeCompactName(node))}</div>
                    <div class="metric">${metric.label}: ${metric.value}</div>
                  </div>
                `;
              }).join("") || `<div class="label">Waiting for RSSI/SNR packets</div>`}
            </div>
          </section>
        </aside>
      </div>
    `;
    this._safeStep("bind_views", "binding", () => this._bindViewControls());
    this._safeStep("bind_composer", "binding", () => this._bindComposer());
    this._safeStep("bind_nodes", "binding", () => this._bindNodeControls());
    this._safeStep("restore_focus", "focus", () => this._restoreComposerFocus(composerFocus));
  }

  _composerFocusState() {
    const active = this._activePanelElement();
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

  _settingsFocusState() {
    const active = this._activePanelElement();
    if (!active || !this.contains(active)) return null;
    const id = typeof active.id === "string" ? active.id : "";
    let isSetting = id.startsWith("meshnet-setting-")
      || [
        "meshnet-settings-gateway",
        "meshnet-settings-preview",
        "meshnet-settings-apply",
        "meshnet-settings-critical",
        "meshnet-settings-reload",
        "meshnet-view-mesh",
        "meshnet-view-settings",
      ].includes(id);
    try {
      isSetting = isSetting
        || (typeof active.hasAttribute === "function"
          && (active.hasAttribute("data-setting-index")
            || active.hasAttribute("data-setting-clear-index")));
    } catch (_ignored) {
      // A disappearing browser control is not an active settings editor.
    }
    if (!isSetting) return null;
    return {
      id,
      start: typeof active.selectionStart === "number" ? active.selectionStart : null,
      end: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
    };
  }

  _panelInteractionActive() {
    if (this._composerFocusState() || this._settingsFocusState()) return true;
    const active = this._activePanelElement();
    if (!active || !this.contains(active)) return false;
    if (active.id === "meshnet-send-button") return true;
    try {
      return typeof active.hasAttribute === "function" && active.hasAttribute("data-message-node");
    } catch (_ignored) {
      return false;
    }
  }

  _handlePollFocusOut(event) {
    if (!this._pollRenderPending) return;
    const next = event && event.relatedTarget;
    // A null target can mean the browser or a shadow boundary received focus.
    // The next poll can flush safely; never risk replacing an in-flight click.
    if (!next || this.contains(next)) return;
    this._queuePendingPollRender();
  }

  _activePanelElement() {
    let active = null;
    try {
      const root = typeof this.getRootNode === "function" ? this.getRootNode() : null;
      active = root && root.activeElement ? root.activeElement : null;
    } catch (_ignored) {
      active = null;
    }
    try {
      if (!active && this.ownerDocument) active = this.ownerDocument.activeElement;
      for (let depth = 0; depth < 8; depth += 1) {
        const nested = active && active.shadowRoot && active.shadowRoot.activeElement;
        if (!nested || nested === active) break;
        active = nested;
      }
    } catch (_ignored) {
      // A closed or changing Home Assistant shadow root has no usable focus.
    }
    return active;
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

  _viewTabs() {
    return `
      <nav class="view-tabs" aria-label="MeshNet views">
        <button id="meshnet-view-mesh" class="view-tab${this._activeView === "mesh" ? " active" : ""}" type="button" data-meshnet-view="mesh"${this._activeView === "mesh" ? ' aria-current="page"' : ""}>Mesh</button>
        <button id="meshnet-view-settings" class="view-tab${this._activeView === "settings" ? " active" : ""}" type="button" data-meshnet-view="settings"${this._activeView === "settings" ? ' aria-current="page"' : ""}>Gateway settings</button>
      </nav>
    `;
  }

  _bindViewControls() {
    this.querySelectorAll("[data-meshnet-view]").forEach((button) => {
      button.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      button.addEventListener("click", () => {
        this._safeStep("settings_event", "binding", () => {
          this._switchView(button.getAttribute("data-meshnet-view"));
        });
      });
    });
  }

  _switchView(view) {
    const next = view === "settings" ? "settings" : "mesh";
    if (this._activeView === next) return;
    this._activeView = next;
    this._safeRender("render");
    if (next === "settings" && !this._settingsSnapshot && this._settingsBusy !== "get") {
      void this._loadGatewaySettings();
    }
  }

  _renderSettings(focusState = null) {
    const snapshot = this._settingsSnapshot;
    const fields = this._settingsFields(snapshot);
    const hasChanges = Object.keys(this._settingsDraft).length > 0;
    const busy = this._settingsBusy != null;
    this.innerHTML = `
      <style>
        :host {
          display: block;
          min-height: 100vh;
          color: var(--primary-text-color);
          background: var(--primary-background-color);
          font-family: var(--paper-font-body1_-_font-family);
        }
        .settings-wrap { max-width: 1040px; margin: 0 auto; padding: 16px; box-sizing: border-box; }
        .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        h1 { margin: 0; font-size: 22px; font-weight: 500; }
        h2 { margin: 0; font-size: 17px; font-weight: 500; }
        h3 { margin: 0; font-size: 15px; font-weight: 500; }
        .view-tabs { display: flex; gap: 6px; margin: 12px 0; }
        .view-tab, .settings-button {
          border: 1px solid var(--divider-color);
          border-radius: 7px;
          padding: 8px 13px;
          color: var(--primary-text-color);
          background: var(--card-background-color);
          cursor: pointer;
          font: inherit;
        }
        .view-tab.active, .settings-button.primary {
          border-color: var(--primary-color);
          color: var(--text-primary-color, #fff);
          background: var(--primary-color);
        }
        button:disabled { cursor: default; opacity: 0.55; }
        .settings-card {
          margin-top: 12px;
          border: 1px solid var(--divider-color);
          border-radius: 9px;
          padding: 14px;
          background: var(--card-background-color);
        }
        .settings-heading { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
        .settings-grid { display: grid; gap: 12px; margin-top: 12px; }
        .settings-field {
          display: grid;
          grid-template-columns: minmax(190px, 0.8fr) minmax(220px, 1.2fr);
          gap: 14px;
          align-items: center;
          padding-top: 11px;
          border-top: 1px solid var(--divider-color);
        }
        .settings-label { display: grid; gap: 3px; min-width: 0; }
        .settings-control { display: grid; gap: 4px; }
        .settings-control input:not([type="checkbox"]), .settings-control select, .gateway-select {
          width: 100%;
          box-sizing: border-box;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          padding: 8px;
          color: var(--primary-text-color);
          background: var(--card-background-color);
          font: inherit;
        }
        .settings-control input[type="checkbox"] { justify-self: start; width: 20px; height: 20px; }
        .settings-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 14px; }
        .settings-preview-change { padding: 8px 0; border-top: 1px solid var(--divider-color); }
        .settings-preview-change:first-of-type { border-top: 0; }
        .critical-box {
          display: flex;
          gap: 8px;
          align-items: start;
          margin-top: 12px;
          padding: 10px;
          border: 1px solid var(--warning-color, #b26a00);
          border-radius: 7px;
        }
        .label, .field-help { color: var(--secondary-text-color); font-size: 12px; }
        .field-help { overflow-wrap: anywhere; }
        .badge { display: inline-block; margin-right: 5px; font-size: 11px; }
        .warn { color: var(--warning-color, #b26a00); }
        .bad { color: var(--error-color, #d32f2f); }
        .good { color: var(--success-color, #168047); }
        .settings-status { min-height: 18px; margin-top: 10px; font-size: 13px; }
        ul.warnings { margin: 8px 0 0; padding-left: 20px; }
        @media (max-width: 720px) {
          .settings-wrap { padding: 10px; }
          .settings-field { grid-template-columns: 1fr; gap: 7px; }
          .settings-heading { display: grid; }
        }
      </style>
      <div class="settings-wrap">
        <div class="toolbar">
          <div>
            <h1>MeshNet</h1>
            <div class="label">Administrator gateway controls</div>
          </div>
          <span class="${snapshot && snapshot.connected ? "good" : "warn"}">${snapshot ? snapshot.connected ? "Gateway online" : "Gateway offline" : "Settings not loaded"}</span>
        </div>
        ${this._viewTabs()}
        <section class="settings-card">
          <div class="settings-heading">
            <div>
              <h2>Gateway settings</h2>
              <div class="label">Changes stay in this browser tab until you preview them. MeshNet never stores a settings draft in browser storage.</div>
            </div>
            <label>
              <span class="label">Gateway</span>
              <select class="gateway-select" id="meshnet-settings-gateway"${busy ? " disabled" : ""}>
                ${this._settingsGatewayOptions()}
              </select>
            </label>
            <button class="settings-button" id="meshnet-settings-reload" type="button"${busy ? " disabled" : ""}>Reload live values</button>
          </div>
          ${this._settingsBusy === "get" && !snapshot ? '<div class="settings-status warn">Loading gateway settings…</div>' : ""}
          ${!snapshot && this._settingsBusy !== "get" ? '<div class="settings-status warn">Choose a gateway to load its supported settings.</div>' : ""}
          ${snapshot ? `
            <div class="field-help">${this._escape(`${snapshot.name} · ${snapshot.protocol} over ${snapshot.transport} · revision ${snapshot.revision}`)}</div>
            ${snapshot.writable ? "" : `<div class="settings-status warn">Read only${snapshot.read_only_reason ? ` · ${this._escape(snapshot.read_only_reason)}` : ""}</div>`}
            ${this._settingsWarningList(snapshot.warnings)}
            <form id="meshnet-settings-form">
              ${snapshot.categories.map((category) => `
                <section class="settings-card">
                  <h3>${this._escape(category.label)}</h3>
                  ${category.description ? `<div class="field-help">${this._escape(category.description)}</div>` : ""}
                  <div class="settings-grid">
                    ${category.fields.map((field) => this._settingsField(
                      field,
                      fields.findIndex((candidate) => candidate.path === field.path),
                      snapshot,
                    )).join("") || '<div class="label">No settings in this category.</div>'}
                  </div>
                </section>
              `).join("") || '<div class="settings-status warn">This gateway did not report editable categories.</div>'}
              <div class="settings-actions">
                <button class="settings-button primary" id="meshnet-settings-preview" type="submit"${busy || !snapshot.writable || !hasChanges || this._settingsPreview ? " disabled" : ""}>${this._settingsBusy === "preview" ? "Preparing preview…" : "Preview changes"}</button>
                <span class="label">Preview is required before Apply. A stale revision is rejected by Home Assistant.</span>
              </div>
            </form>
            ${this._settingsPreviewHtml()}
          ` : ""}
          ${this._settingsWarningList(this._settingsResultWarnings)}
          <div id="meshnet-settings-status" class="settings-status ${this._settingsStatusClass()}" role="status" aria-live="polite">${this._escape(this._settingsStatus ? this._settingsStatus.text : "")}</div>
        </section>
      </div>
    `;
    this._safeStep("bind_views", "binding", () => this._bindViewControls());
    this._safeStep("bind_settings", "binding", () => this._bindSettingsControls());
    this._safeStep("restore_focus", "focus", () => this._restoreComposerFocus(focusState));
  }

  _settingsGatewayOptions() {
    const selected = this._settingsGatewayId
      || (this._settingsSnapshot && this._settingsSnapshot.gateway_id)
      || "";
    const gateways = this._settingsGateways.slice();
    if (selected && !gateways.some((gateway) => gateway.gateway_id === selected)) {
      gateways.push({ gateway_id: selected, name: selected, connected: false });
    }
    if (!gateways.length) return '<option value="">No gateways available</option>';
    return gateways.map((gateway) => {
      const label = gateway.name === gateway.gateway_id
        ? gateway.name
        : `${gateway.name} (${gateway.gateway_id})`;
      return `<option value="${this._escape(gateway.gateway_id)}"${this._selected(selected, gateway.gateway_id)}>${this._escape(`${label} — ${gateway.connected ? "online" : "offline"}`)}</option>`;
    }).join("");
  }

  _settingsFields(snapshot = this._settingsSnapshot) {
    if (!snapshot || !Array.isArray(snapshot.categories)) return [];
    return snapshot.categories.flatMap((category) => category.fields);
  }

  _settingsField(field, index, snapshot) {
    const disabled = this._settingsBusy != null || !snapshot.writable || !field.writable;
    const value = Object.hasOwn(this._settingsDraft, field.path)
      ? this._settingsDraft[field.path]
      : field.value;
    const badges = [
      field.critical ? '<span class="badge warn">Critical</span>' : "",
      field.requires_reconnect ? '<span class="badge warn">Reconnect required</span>' : "",
      field.type === "secret" && field.configured ? '<span class="badge good">Configured</span>' : "",
    ].join("");
    const reason = !snapshot.writable
      ? snapshot.read_only_reason
      : !field.writable
        ? field.read_only_reason
        : "";
    return `
      <div class="settings-field">
        <label class="settings-label" for="meshnet-setting-${index}">
          <span>${this._escape(field.label)}</span>
          <span>${badges}</span>
          ${field.description ? `<span class="field-help">${this._escape(field.description)}</span>` : ""}
          ${reason ? `<span class="field-help warn">${this._escape(reason)}</span>` : ""}
        </label>
        <span class="settings-control">
          ${this._settingsInput(field, index, value, disabled)}
          ${field.unit ? `<span class="field-help">Unit: ${this._escape(field.unit)}</span>` : ""}
        </span>
      </div>
    `;
  }

  _settingsInput(field, index, value, disabled) {
    const id = `meshnet-setting-${index}`;
    const common = `id="${id}" data-setting-index="${index}"${disabled ? " disabled" : ""}`;
    if (field.type === "boolean") {
      return `<input ${common} type="checkbox"${value === true ? " checked" : ""}>`;
    }
    if (field.type === "select") {
      const selectedIndex = field.options.findIndex((option) => this._settingValuesEqual(option.value, value));
      const unknown = selectedIndex < 0 && value != null
        ? `<option value="-1" selected disabled>Current: ${this._escape(this._formatSettingValue(value))}</option>`
        : "";
      return `<select ${common}>${unknown}${field.options.map((option, optionIndex) => `<option value="${optionIndex}"${optionIndex === selectedIndex ? " selected" : ""}>${this._escape(option.label)}</option>`).join("")}</select>`;
    }
    if (field.type === "secret") {
      const staged = Object.hasOwn(this._settingsDraft, field.path);
      const placeholder = staged
        ? "Secret change staged in memory"
        : field.configured
          ? "Configured — leave blank to keep"
          : "Enter a new secret";
      const clear = field.allow_clear && field.configured
        ? `<label class="field-help"><input id="meshnet-setting-clear-${index}" data-setting-clear-index="${index}" type="checkbox"${staged && this._settingsDraft[field.path] && this._settingsDraft[field.path].operation === "clear" ? " checked" : ""}${disabled ? " disabled" : ""}> Clear the configured secret</label>`
        : "";
      return `<input ${common} type="password" value="" placeholder="${this._escape(placeholder)}" autocomplete="new-password" spellcheck="false"${field.max_length != null ? ` maxlength="${field.max_length}"` : ""}>${clear}`;
    }
    if (field.type === "integer" || field.type === "number") {
      const attributes = [
        field.min != null ? `min="${this._escape(field.min)}"` : "",
        field.max != null ? `max="${this._escape(field.max)}"` : "",
        field.step != null ? `step="${this._escape(field.step)}"` : field.type === "integer" ? 'step="1"' : 'step="any"',
      ].filter(Boolean).join(" ");
      return `<input ${common} type="number" ${attributes} value="${this._escape(value == null ? "" : value)}">`;
    }
    return `<input ${common} type="text" value="${this._escape(value == null ? "" : value)}"${field.max_length != null ? ` maxlength="${field.max_length}"` : ""}>`;
  }

  _settingsWarningList(warnings) {
    if (!Array.isArray(warnings) || !warnings.length) return "";
    return `<ul class="warnings warn">${warnings.map((warning) => `<li>${this._escape(warning)}</li>`).join("")}</ul>`;
  }

  _settingsPreviewHtml() {
    const preview = this._settingsPreview;
    if (!preview) return "";
    const confirmation = preview.requires_critical_confirmation ? `
      <label class="critical-box">
        <input id="meshnet-settings-critical" type="checkbox"${this._settingsCriticalConfirmed ? " checked" : ""}>
        <span>I understand that the highlighted critical settings can disrupt radio access or require physical recovery.</span>
      </label>
    ` : "";
    return `
      <section class="settings-card settings-preview" id="meshnet-settings-preview-result">
        <h3>Preview</h3>
        <div class="field-help">Expires ${this._escape(preview.expires_at)}</div>
        ${preview.changes.map((change) => `
          <div class="settings-preview-change">
            <div>${this._escape(change.label)} ${change.critical ? '<span class="badge warn">Critical</span>' : ""}${change.requires_reconnect ? '<span class="badge warn">Reconnect</span>' : ""}</div>
            <div class="field-help">${change.secret ? `Secret value will be ${change.operation === "clear" ? "cleared" : "replaced"}; its value is not displayed.` : `${this._escape(this._formatSettingValue(change.before))} → ${this._escape(this._formatSettingValue(change.after))}`}</div>
          </div>
        `).join("")}
        ${this._settingsWarningList(preview.warnings)}
        ${confirmation}
        <div class="settings-actions">
          <button class="settings-button primary" id="meshnet-settings-apply" type="button"${this._settingsBusy != null || (preview.requires_critical_confirmation && !this._settingsCriticalConfirmed) ? " disabled" : ""}>${this._settingsBusy === "apply" ? "Applying…" : "Apply preview"}</button>
          <span class="label">Only this exact preview and revision can be applied.</span>
        </div>
      </section>
    `;
  }

  _settingsStatusClass() {
    const kind = this._settingsStatus && this._settingsStatus.kind;
    return ["good", "warn", "bad"].includes(kind) ? kind : "";
  }

  _formatSettingValue(value) {
    if (value === true) return "On";
    if (value === false) return "Off";
    if (value == null || value === "") return "Not set";
    return String(value);
  }

  _settingValuesEqual(left, right) {
    return Object.is(left, right);
  }

  _settingValuesEquivalentForServer(left, right) {
    // The Python settings validator treats numeric -0 and 0 as the same
    // value. Draft inputs are strings until submission, so compare only
    // after type coercion and use strict equality to mirror that behavior.
    return left === right;
  }

  _bindSettingsControls() {
    const gateway = this.querySelector("#meshnet-settings-gateway");
    if (gateway) {
      gateway.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      gateway.addEventListener("change", () => {
        this._safeStep("settings_event", "binding", () => {
          void this._loadGatewaySettings(gateway.value);
        });
      });
    }
    const reload = this.querySelector("#meshnet-settings-reload");
    if (reload) {
      reload.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      reload.addEventListener("click", () => {
        this._safeStep("settings_event", "binding", () => {
          void this._loadGatewaySettings(this._settingsGatewayId);
        });
      });
    }
    this.querySelectorAll("[data-setting-index]").forEach((input) => {
      input.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      const eventName = input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input";
      input.addEventListener(eventName, () => {
        this._safeStep("settings_event", "binding", () => {
          this._updateSettingsDraft(input);
        });
      });
    });
    this.querySelectorAll("[data-setting-clear-index]").forEach((input) => {
      input.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      input.addEventListener("change", () => {
        this._safeStep("settings_event", "binding", () => {
          this._updateSecretClearDraft(input);
        });
      });
    });
    const form = this.querySelector("#meshnet-settings-form");
    if (form) {
      form.addEventListener("submit", (event) => {
        this._safeStep("settings_event", "binding", () => this._previewGatewaySettings(event));
      });
    }
    const critical = this.querySelector("#meshnet-settings-critical");
    if (critical) {
      critical.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      critical.addEventListener("change", () => {
        this._settingsCriticalConfirmed = critical.checked === true;
        const apply = this.querySelector("#meshnet-settings-apply");
        if (apply) apply.disabled = !this._settingsCriticalConfirmed || this._settingsBusy != null;
      });
    }
    const apply = this.querySelector("#meshnet-settings-apply");
    if (apply) {
      apply.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      apply.addEventListener("click", () => {
        this._safeStep("settings_event", "binding", () => this._applyGatewaySettings());
      });
    }
  }

  _updateSettingsDraft(input) {
    const index = Number.parseInt(input.getAttribute("data-setting-index"), 10);
    const field = this._settingsFields()[index];
    if (!field || !field.writable || !this._settingsSnapshot || !this._settingsSnapshot.writable) return;
    const value = this._readSettingInput(field, input);
    if ((field.type === "secret" && value === "") || this._settingValuesEqual(value, field.value)) {
      delete this._settingsDraft[field.path];
    } else {
      this._settingsDraft[field.path] = value;
    }
    if (field.type === "secret" && value) {
      const clear = this.querySelector(`#meshnet-setting-clear-${index}`);
      if (clear) clear.checked = false;
    }
    this._invalidateSettingsPreview();
  }

  _updateSecretClearDraft(input) {
    const index = Number.parseInt(input.getAttribute("data-setting-clear-index"), 10);
    const field = this._settingsFields()[index];
    if (!field || field.type !== "secret" || !field.allow_clear || !field.writable) return;
    if (input.checked === true) {
      this._settingsDraft[field.path] = { operation: "clear" };
      const secret = this.querySelector(`#meshnet-setting-${index}`);
      if (secret) secret.value = "";
    } else {
      delete this._settingsDraft[field.path];
    }
    this._invalidateSettingsPreview();
  }

  _invalidateSettingsPreview() {
    this._settingsPreview = null;
    this._settingsCriticalConfirmed = false;
    this._settingsResultWarnings = [];
    this._settingsStatus = Object.keys(this._settingsDraft).length
      ? { kind: "warn", text: "Draft changed. Preview the current draft before applying it." }
      : null;
    const preview = this.querySelector("#meshnet-settings-preview-result");
    if (preview && typeof preview.remove === "function") preview.remove();
    const previewButton = this.querySelector("#meshnet-settings-preview");
    if (previewButton) {
      previewButton.disabled = this._settingsBusy != null
        || !Object.keys(this._settingsDraft).length;
    }
    const status = this.querySelector("#meshnet-settings-status");
    if (status) {
      status.className = `settings-status ${this._settingsStatusClass()}`;
      status.textContent = this._settingsStatus ? this._settingsStatus.text : "";
    }
  }

  _readSettingInput(field, input) {
    if (field.type === "boolean") return input.checked === true;
    if (field.type === "select") {
      const optionIndex = Number.parseInt(input.value, 10);
      return optionIndex >= 0 && optionIndex < field.options.length
        ? field.options[optionIndex].value
        : field.value;
    }
    return input.value;
  }

  _settingsChanges() {
    const changes = {};
    for (const field of this._settingsFields()) {
      if (!Object.hasOwn(this._settingsDraft, field.path)) continue;
      const value = this._coerceSettingValue(
        field,
        this._settingsDraft[field.path],
      );
      if (
        field.type !== "secret"
        && this._settingValuesEquivalentForServer(value, field.value)
      ) {
        // A numeric input such as "17" is not Object.is-equal to the live
        // number 17 while typing. Drop it once coerced so the server may
        // legitimately omit no-op fields from the preview response.
        delete this._settingsDraft[field.path];
        continue;
      }
      changes[field.path] = value;
    }
    return changes;
  }

  _coerceSettingValue(field, value) {
    if (field.type === "boolean") {
      if (typeof value !== "boolean") throw { name: "ValidationError", code: "invalid_format" };
      return value;
    }
    if (field.type === "integer" || field.type === "number") {
      if (typeof value !== "string" && typeof value !== "number") {
        throw { name: "ValidationError", code: "invalid_format" };
      }
      const text = typeof value === "string" ? value.trim() : value;
      const number = field.type === "integer" ? Number(text) : Number(text);
      if (
        text === ""
        || !Number.isFinite(number)
        || (field.type === "integer" && !Number.isSafeInteger(number))
        || (field.min != null && number < field.min)
        || (field.max != null && number > field.max)
      ) throw { name: "ValidationError", code: "invalid_format" };
      return number;
    }
    if (field.type === "select") {
      if (!field.options.some((option) => this._settingValuesEqual(option.value, value))) {
        throw { name: "ValidationError", code: "invalid_format" };
      }
      return value;
    }
    if (field.type === "secret") {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        if (value.operation === "clear" && field.allow_clear) return { operation: "clear" };
        if (value.operation === "replace" && typeof value.value === "string" && value.value) {
          return { operation: "replace", value: value.value };
        }
        throw { name: "ValidationError", code: "invalid_format" };
      }
      if (typeof value !== "string" || !value) {
        throw { name: "ValidationError", code: "invalid_format" };
      }
      if (field.max_length != null && value.length > field.max_length) {
        throw { name: "ValidationError", code: "invalid_format" };
      }
      return { operation: "replace", value };
    }
    if (typeof value !== "string") throw { name: "ValidationError", code: "invalid_format" };
    if (field.max_length != null && value.length > field.max_length) {
      throw { name: "ValidationError", code: "invalid_format" };
    }
    return value;
  }

  async _loadGatewaySettings(gatewayId = "") {
    if (
      this._settingsBusy != null
      || !this._hass
      || typeof this._hass.callWS !== "function"
    ) return;
    const generation = ++this._settingsRequestGeneration;
    this._settingsBusy = "get";
    this._settingsStatus = { kind: "warn", text: "Loading gateway settings…" };
    this._settingsPreview = null;
    this._settingsCriticalConfirmed = false;
    this._settingsResultWarnings = [];
    this._settingsDraft = Object.create(null);
    this._safeRender("render");
    const payload = { type: "meshnet/settings/get" };
    if (gatewayId) payload.gateway_id = gatewayId;
    try {
      const response = await this._withTimeout(this._hass.callWS(payload), 35000);
      if (generation !== this._settingsRequestGeneration) return;
      const validated = this._validateSettingsResponse(response);
      this._settingsGateways = validated.gateways;
      this._settingsSnapshot = validated.selected;
      this._settingsGatewayId = validated.selected
        ? validated.selected.gateway_id
        : gatewayId;
      this._settingsStatus = validated.selected
        ? { kind: "good", text: "Gateway settings loaded. Edit values, then preview." }
        : { kind: "warn", text: "No configurable gateway was returned." };
      this._markOperationSuccess("settings_get");
    } catch (error) {
      if (generation !== this._settingsRequestGeneration) return;
      this._recordFailure(
        "settings_get",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._settingsStatus = { kind: "bad", text: "Gateway settings could not be loaded." };
    } finally {
      if (generation === this._settingsRequestGeneration) {
        this._settingsBusy = null;
        this._safeRender("render");
      }
    }
  }

  async _previewGatewaySettings(event = null) {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (this._settingsBusy != null || !this._settingsSnapshot || !this._settingsSnapshot.writable) return;
    let changes;
    try {
      changes = this._settingsChanges();
    } catch (error) {
      this._recordFailure("settings_preview", "validation", error);
      this._settingsStatus = { kind: "bad", text: "Correct the highlighted setting values before previewing." };
      this._safeRender("render");
      return;
    }
    if (!Object.keys(changes).length) {
      this._settingsStatus = { kind: "warn", text: "Change at least one writable setting first." };
      this._safeRender("render");
      return;
    }
    const snapshot = this._settingsSnapshot;
    const generation = this._settingsRequestGeneration;
    this._settingsBusy = "preview";
    this._settingsPreview = null;
    this._settingsCriticalConfirmed = false;
    this._settingsStatus = { kind: "warn", text: "Preparing a non-destructive preview…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/settings/preview",
        gateway_id: snapshot.gateway_id,
        revision: snapshot.revision,
        changes,
      }), 35000);
      if (
        generation !== this._settingsRequestGeneration
        || this._settingsSnapshot !== snapshot
      ) return;
      this._settingsPreview = this._validateSettingsPreview(
        response,
        snapshot,
        changes,
      );
      this._clearSecretSettingsDrafts(snapshot);
      this._settingsStatus = { kind: "good", text: "Preview ready. Review every change before applying it." };
      this._markOperationSuccess("settings_preview");
    } catch (error) {
      if (
        generation !== this._settingsRequestGeneration
        || this._settingsSnapshot !== snapshot
      ) return;
      // Never retain a submitted credential merely to make retry convenient.
      // The administrator must deliberately type it again after any failed or
      // timed-out preview request.
      this._clearSecretSettingsDrafts(snapshot);
      this._recordFailure(
        "settings_preview",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._settingsStatus = { kind: "bad", text: "The settings preview could not be prepared." };
    } finally {
      if (
        generation === this._settingsRequestGeneration
        && this._settingsSnapshot === snapshot
      ) {
        this._settingsBusy = null;
        this._safeRender("render");
      }
    }
  }

  async _applyGatewaySettings() {
    const snapshot = this._settingsSnapshot;
    const preview = this._settingsPreview;
    const generation = this._settingsRequestGeneration;
    if (this._settingsBusy != null || !snapshot || !preview) return;
    if (preview.requires_critical_confirmation && !this._settingsCriticalConfirmed) {
      this._settingsStatus = { kind: "bad", text: "Confirm the critical-setting warning before applying." };
      this._safeRender("render");
      return;
    }
    this._settingsBusy = "apply";
    this._settingsStatus = { kind: "warn", text: "Applying and verifying the preview…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/settings/apply",
        gateway_id: snapshot.gateway_id,
        revision: snapshot.revision,
        preview_id: preview.preview_id,
        confirm_critical: this._settingsCriticalConfirmed === true,
      }), 130000);
      if (
        generation !== this._settingsRequestGeneration
        || this._settingsSnapshot !== snapshot
        || this._settingsPreview !== preview
      ) return;
      const applied = this._validateSettingsApply(response, preview, snapshot);
      this._clearSecretSettingsDrafts(snapshot);
      if (applied.snapshot) {
        this._settingsSnapshot = applied.snapshot;
        this._settingsGatewayId = applied.snapshot.gateway_id;
      }
      this._settingsDraft = Object.create(null);
      this._settingsPreview = null;
      this._settingsCriticalConfirmed = false;
      this._settingsResultWarnings = applied.warnings;
      const fullyVerified = applied.verified_count > 0 && applied.unverified_count === 0;
      const verification = fullyVerified
        ? "Settings applied and verified."
        : `Settings applied; ${applied.unverified_count || "some"} value(s) could not be verified.`;
      const connectionStatus = applied.connection_recovery_required
        ? " Verify or recover the gateway connection before another settings change."
        : applied.reconnect_required
          ? " The gateway is restarting; wait for it to reconnect."
          : "";
      this._settingsStatus = {
        kind: fullyVerified && !applied.connection_recovery_required ? "good" : "warn",
        text: `${verification}${connectionStatus}${applied.snapshot ? "" : " Reload live values before making another change."}`,
      };
      this._markOperationSuccess("settings_apply");
    } catch (error) {
      if (
        generation !== this._settingsRequestGeneration
        || this._settingsSnapshot !== snapshot
        || this._settingsPreview !== preview
      ) return;
      // Apply previews are single use, and a transport timeout has an unknown
      // outcome. Never offer the same write again from the browser.
      this._settingsPreview = null;
      this._settingsCriticalConfirmed = false;
      this._recordFailure(
        "settings_apply",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._settingsStatus = {
        kind: "warn",
        text: "The apply outcome could not be confirmed. Do not repeat the write. Reload live values and verify the radio before making another change.",
      };
    } finally {
      if (
        generation === this._settingsRequestGeneration
        && (this._settingsSnapshot === snapshot || this._settingsPreview == null)
      ) {
        this._settingsBusy = null;
        this._safeRender("render");
      }
    }
  }

  _clearSecretSettingsDrafts(snapshot = this._settingsSnapshot) {
    for (const field of this._settingsFields(snapshot)) {
      if (field.type === "secret") delete this._settingsDraft[field.path];
    }
    try {
      this.querySelectorAll('input[type="password"][data-setting-index]').forEach((input) => {
        input.value = "";
      });
    } catch (_ignored) {
      // Secret drafts are already gone from JS state; DOM cleanup is best effort.
    }
  }

  _validateSettingsResponse(response) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || !Array.isArray(response.gateways)
      || response.gateways.length > 64
    ) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const gateways = response.gateways.map((gateway) => this._sanitizeSettingsGateway(gateway));
    if (new Set(gateways.map((gateway) => gateway.gateway_id)).size !== gateways.length) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const selected = response.selected == null
      ? null
      : this._sanitizeSettingsSnapshot(response.selected);
    if (selected && !gateways.some((gateway) => gateway.gateway_id === selected.gateway_id)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return { gateways, selected };
  }

  _sanitizeSettingsGateway(gateway) {
    if (!gateway || typeof gateway !== "object" || Array.isArray(gateway)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const gatewayId = this._requiredSettingText(gateway.gateway_id, 128);
    return {
      gateway_id: gatewayId,
      name: this._requiredSettingText(gateway.name || gatewayId, 128),
      protocol: this._requiredSettingText(gateway.protocol, 64),
      transport: this._requiredSettingText(gateway.transport, 64),
      connected: gateway.connected === true,
      writable: gateway.writable === true,
      read_only_reason: this._optionalSettingText(gateway.read_only_reason, 512),
    };
  }

  _sanitizeSettingsSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    if (snapshot.schema_version !== 1) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const revision = snapshot.revision;
    if (typeof revision !== "string" || !/^[0-9a-f]{64}$/.test(revision)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    if (!Array.isArray(snapshot.categories) || snapshot.categories.length > 48) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const seenPaths = new Set();
    const seenCategories = new Set();
    let fieldCount = 0;
    const categories = snapshot.categories.map((category) => {
      if (!category || typeof category !== "object" || Array.isArray(category) || !Array.isArray(category.fields)) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      const key = this._requiredSettingPath(category.key);
      if (seenCategories.has(key)) throw { name: "PanelSchemaError", code: "invalid_format" };
      seenCategories.add(key);
      const fields = category.fields.map((field) => {
        fieldCount += 1;
        if (fieldCount > 384) throw { name: "PanelSchemaError", code: "invalid_format" };
        const sanitized = this._sanitizeSettingsField(field);
        if (seenPaths.has(sanitized.path)) throw { name: "PanelSchemaError", code: "invalid_format" };
        seenPaths.add(sanitized.path);
        return sanitized;
      });
      return {
        key,
        label: this._requiredSettingText(category.label, 128),
        description: this._optionalSettingText(category.description, 512),
        fields,
      };
    });
    const fetchedAt = this._requiredSettingText(snapshot.fetched_at, 64);
    if (!Number.isFinite(Date.parse(fetchedAt))) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return {
      schema_version: snapshot.schema_version,
      gateway_id: this._requiredSettingText(snapshot.gateway_id, 128),
      name: this._requiredSettingText(snapshot.name, 128),
      protocol: this._requiredSettingText(snapshot.protocol, 64),
      transport: this._requiredSettingText(snapshot.transport, 64),
      connected: snapshot.connected === true,
      writable: snapshot.writable === true,
      read_only_reason: this._optionalSettingText(snapshot.read_only_reason, 512),
      revision,
      fetched_at: fetchedAt,
      categories,
      warnings: this._settingsWarnings(snapshot.warnings),
    };
  }

  _sanitizeSettingsField(field) {
    if (!field || typeof field !== "object" || Array.isArray(field)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const type = ["boolean", "integer", "number", "string", "select", "secret"].includes(field.type)
      ? field.type
      : null;
    if (!type) throw { name: "PanelSchemaError", code: "invalid_format" };
    if (Array.isArray(field.options) && field.options.length > 256) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const optionKeys = new Set();
    const options = Array.isArray(field.options)
      ? field.options.map((option) => {
        if (!option || typeof option !== "object" || Array.isArray(option) || option.value == null || !this._settingScalar(option.value)) {
          throw { name: "PanelSchemaError", code: "invalid_format" };
        }
        const optionKey = `${typeof option.value}:${JSON.stringify(option.value)}`;
        if (optionKeys.has(optionKey)) {
          throw { name: "PanelSchemaError", code: "invalid_format" };
        }
        optionKeys.add(optionKey);
        return {
          value: option.value,
          label: this._requiredSettingText(option.label, 128),
        };
      })
      : [];
    if (type === "select" && !options.length) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    let value = type === "secret" ? "" : field.value;
    if (value != null && !this._settingValueMatchesType(type, value)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    if (field.writable === true && type !== "secret" && value == null) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const numeric = (key) => {
      const candidate = field[key];
      if (candidate == null) return null;
      if (!this._settingScalar(candidate) || typeof candidate !== "number") {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      return candidate;
    };
    const min = numeric("min");
    const max = numeric("max");
    const step = numeric("step");
    if (
      ((type === "integer" || type === "number") && field.writable === true
        && (min == null || max == null))
      || (min != null && max != null && min > max)
      || (step != null && step <= 0)
      || ((type === "integer" || type === "number") && value != null
        && ((min != null && value < min) || (max != null && value > max)))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    if (
      type === "select"
      && !options.some((option) => this._settingValuesEqual(option.value, value))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const maxLength = field.max_length == null
      ? null
      : Number.isSafeInteger(field.max_length)
        && field.max_length > 0
        && field.max_length <= 1024
        ? field.max_length
        : NaN;
    if (Number.isNaN(maxLength)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    if (
      type === "string"
      && value != null
      && maxLength != null
      && value.length > maxLength
    ) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return {
      path: this._requiredSettingPath(field.path),
      label: this._requiredSettingText(field.label, 128),
      type,
      value,
      configured: field.configured === true,
      allow_clear: field.allow_clear === true,
      options,
      min,
      max,
      step,
      max_length: maxLength,
      unit: this._optionalSettingText(field.unit, 32),
      description: this._optionalSettingText(field.description, 512),
      writable: field.writable === true,
      read_only_reason: this._optionalSettingText(field.read_only_reason, 256),
      critical: field.critical === true,
      requires_reconnect: field.requires_reconnect === true,
    };
  }

  _validateSettingsPreview(response, snapshot, requestedChanges) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || !Array.isArray(response.changes)
      || !response.changes.length
      || response.changes.length > 64
    ) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    if (
      !snapshot
      || !requestedChanges
      || typeof requestedChanges !== "object"
      || Array.isArray(requestedChanges)
      || response.gateway_id !== snapshot.gateway_id
      || response.revision !== snapshot.revision
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const previewId = this._requiredSettingText(response.preview_id);
    if (previewId.length < 32 || previewId.length > 128) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const fields = new Map(this._settingsFields(snapshot).map((field) => [field.path, field]));
    const expectedPaths = Object.keys(requestedChanges);
    const seenPaths = new Set();
    const changes = response.changes.map((change) => {
      if (!change || typeof change !== "object" || Array.isArray(change)) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      const path = this._requiredSettingPath(change.path);
      const field = fields.get(path);
      if (!field || seenPaths.has(path) || !Object.hasOwn(requestedChanges, path)) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      seenPaths.add(path);
      const secret = change.secret === true;
      if (
        secret !== (field.type === "secret")
        || change.label !== field.label
        || (change.critical === true) !== field.critical
        || (change.requires_reconnect === true) !== field.requires_reconnect
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      if (!secret && (!this._settingScalar(change.before) || !this._settingScalar(change.after))) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      if (
        !secret
        && (
          !this._settingValuesEqual(change.before, field.value)
          || !this._settingValuesEqual(change.after, requestedChanges[path])
        )
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      const requestedOperation = secret && requestedChanges[path]
        ? requestedChanges[path].operation
        : null;
      if (
        secret
        && !["replace", "clear"].includes(change.operation)
        || (secret && change.operation !== requestedOperation)
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      return {
        path,
        label: this._requiredSettingText(change.label, 128),
        before: secret ? null : change.before,
        after: secret ? null : change.after,
        secret,
        operation: secret && change.operation === "clear" ? "clear" : "replace",
        critical: change.critical === true,
        requires_reconnect: change.requires_reconnect === true,
      };
    });
    if (
      seenPaths.size !== expectedPaths.length
      || expectedPaths.some((path) => !seenPaths.has(path))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const requiresCritical = changes.some((change) => change.critical);
    if (response.requires_critical_confirmation !== requiresCritical) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const expiresAt = this._requiredSettingText(response.expires_at, 64);
    if (!Number.isFinite(Date.parse(expiresAt))) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return {
      preview_id: previewId,
      expires_at: expiresAt,
      changes,
      requires_critical_confirmation: requiresCritical,
      warnings: this._settingsWarnings(response.warnings),
    };
  }

  _validateSettingsApply(response, preview, snapshot) {
    const statuses = ["verified", "partially_verified", "applied_unverified"];
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || !statuses.includes(response.status)
      || !preview
      || !snapshot
      || response.gateway_id !== snapshot.gateway_id
      || !Array.isArray(response.verified)
      || !Array.isArray(response.unverified)
      || typeof response.reconnect_required !== "boolean"
      || typeof response.connection_recovery_required !== "boolean"
    ) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const validatedPaths = [response.verified, response.unverified].map((paths) => {
      if (paths.length > 64) throw { name: "PanelSchemaError", code: "invalid_format" };
      const sanitized = paths.map((path) => this._requiredSettingPath(path));
      if (new Set(sanitized).size !== sanitized.length) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      return sanitized;
    });
    const [verified, unverified] = validatedPaths;
    const expectedPaths = preview.changes.map((change) => change.path);
    const combined = [...verified, ...unverified];
    if (
      new Set(combined).size !== combined.length
      || combined.length !== expectedPaths.length
      || expectedPaths.some((path) => !combined.includes(path))
      || (response.status === "verified" && (verified.length === 0 || unverified.length !== 0))
      || (response.status === "partially_verified" && (verified.length === 0 || unverified.length === 0))
      || (response.status === "applied_unverified" && (verified.length !== 0 || unverified.length === 0))
    ) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const sanitizedSnapshot = response.snapshot == null
      ? null
      : this._sanitizeSettingsSnapshot(response.snapshot);
    if (sanitizedSnapshot && sanitizedSnapshot.gateway_id !== snapshot.gateway_id) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return {
      verified_count: verified.length,
      unverified_count: unverified.length,
      reconnect_required: response.reconnect_required,
      connection_recovery_required: response.connection_recovery_required,
      snapshot: sanitizedSnapshot,
      warnings: this._settingsWarnings(response.warnings),
    };
  }

  _requiredSettingText(value, maxLength = 512) {
    if (
      typeof value !== "string"
      || !value.trim()
      || value.length > maxLength
    ) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return value.trim();
  }

  _optionalSettingText(value, maxLength) {
    if (value == null) return "";
    if (typeof value !== "string" || value.length > maxLength) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return value;
  }

  _requiredSettingPath(value) {
    const path = this._requiredSettingText(value);
    const parts = path.toLowerCase().split(".");
    if (
      !/^[a-z0-9][a-z0-9_.-]{0,127}$/.test(path)
      || parts.some((part) => ["__proto__", "prototype", "constructor"].includes(part))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    return path;
  }

  _settingsWarnings(value) {
    if (value == null) return [];
    if (
      !Array.isArray(value)
      || value.length > 16
      || value.some((warning) => typeof warning !== "string" || warning.length > 512)
    ) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return value.slice();
  }

  _settingScalar(value) {
    return value == null
      || typeof value === "string"
      || typeof value === "boolean"
      || (
        typeof value === "number"
        && Number.isFinite(value)
        && Math.abs(value) <= 1_000_000_000_000_000
        && (!Number.isInteger(value) || Number.isSafeInteger(value))
      );
  }

  _settingValueMatchesType(type, value) {
    if (type === "boolean") return typeof value === "boolean";
    if (type === "integer") return Number.isSafeInteger(value);
    if (type === "number") return typeof value === "number" && Number.isFinite(value);
    if (type === "string" || type === "secret") return typeof value === "string";
    return type === "select" && this._settingScalar(value);
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
      field.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
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
    const sendButton = this.querySelector("#meshnet-send-button");
    if (sendButton) {
      sendButton.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
    }
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
      sort.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
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
      button.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
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
        : status === "blocked"
          ? { kind: "bad", text: "Message blocked because the node identity is ambiguous." }
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
    return this._recipientChoices(nodes).some(
      (choice) => !choice.ambiguous && !choice.invalidIdentity
        && choice.value === String(nodeKey),
    );
  }

  _recipientChoices(nodes) {
    const choices = nodes
      .filter((node) => node && node.node_key != null && String(node.node_key))
      .map((node) => ({
        value: String(node.node_key),
        label: this._recipientNodeName(node),
        named: this._nodeHasHumanName(node),
        invalidIdentity: this._meshtasticIdentityInvalid(node),
        node,
      }))
      .filter((choice, index, all) => all.findIndex((item) => item.value === choice.value) === index);
    const meshtasticIdGroups = new Map();
    choices.forEach((choice) => {
      const nodeId = this._meshtasticNodeId(choice.node);
      if (!nodeId) return;
      if (!meshtasticIdGroups.has(nodeId)) meshtasticIdGroups.set(nodeId, []);
      meshtasticIdGroups.get(nodeId).push(choice);
    });
    meshtasticIdGroups.forEach((group) => {
      if (group.length < 2) return;
      group.forEach((choice) => {
        choice.ambiguous = true;
        choice.label = `${choice.label} · ambiguous ID — sending disabled`;
      });
    });
    choices.forEach((choice) => {
      if (!choice.invalidIdentity) return;
      choice.label = `${choice.label} · invalid identity — sending disabled`;
    });
    const labelGroups = new Map();
    choices.forEach((choice) => {
      const key = this._textSortKey(choice.label);
      if (!labelGroups.has(key)) labelGroups.set(key, []);
      labelGroups.get(key).push(choice);
    });
    labelGroups.forEach((group) => {
      if (group.length < 2) return;
      const nodeIds = group.map((choice) => this._nodeLabelText(
        choice.node && choice.node.node_id,
      ));
      const distinctNodeIds = nodeIds.every(Boolean)
        && new Set(nodeIds.map((value) => this._textSortKey(value))).size === group.length;
      group.forEach((choice, index) => {
        choice.label = `${choice.label} · ${distinctNodeIds ? nodeIds[index] : choice.value}`;
      });
    });
    return choices
      .map(({ node: _node, ...choice }) => choice)
      .sort((left, right) => Number(right.named) - Number(left.named)
        || this._compareText(left.label, right.label)
        || this._compareText(left.value, right.value));
  }

  _recipientOptions(nodes) {
    const selected = String(this._draft.recipient || "");
    const choices = this._recipientChoices(nodes);
    if (selected && !choices.some((choice) => choice.value === selected)) {
      choices.push({
        value: selected,
        label: `${selected} (currently unavailable)`,
        unavailable: true,
      });
    }
    const prompt = choices.some((choice) => !choice.ambiguous
      && !choice.invalidIdentity && !choice.unavailable)
      ? "Choose a node…"
      : choices.length
        ? "No unambiguous direct recipients"
        : "No cached nodes available yet";
    return [
      `<option value=""${this._selected(selected, "")}>${prompt}</option>`,
      ...choices.map((choice) => {
        const disabled = choice.ambiguous || choice.invalidIdentity || choice.unavailable
          ? " disabled"
          : "";
        return `<option value="${this._escape(choice.value)}"${this._selected(selected, choice.value)}${disabled}>${this._escape(choice.label)}</option>`;
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
    const retainedRecords = this._metadataCount(safeMetadata, "retained_node_record_count", total);
    const collapsedAliases = this._metadataCount(safeMetadata, "collapsed_alias_record_count", 0);
    const resolvedGroups = this._metadataCount(safeMetadata, "resolved_identity_group_count", 0);
    const current = this._metadataCount(safeMetadata, "current_session_node_count");
    const cached = this._metadataCount(safeMetadata, "cached_only_node_count");
    const analyzed = this._metadataCount(safeMetadata, "analyzed_node_count");
    const omitted = this._metadataCount(safeMetadata, "omitted_node_count", 0);
    const online = this._metadataCount(safeMetadata, "online_node_count");
    const located = this._metadataCount(safeMetadata, "located_node_count");
    const locatedOffline = this._metadataCount(safeMetadata, "located_offline_node_count");
    const mqtt = this._metadataCount(safeMetadata, "mqtt_node_count");
    const mqttUnknown = this._metadataCount(safeMetadata, "mqtt_unknown_node_count");
    const collisionGroups = this._metadataCount(
      safeMetadata,
      "unresolved_identity_group_count",
      this._metadataCount(safeMetadata, "identity_collision_group_count"),
    );
    const collisionNodes = this._metadataCount(
      safeMetadata,
      "unresolved_identity_node_count",
      this._metadataCount(safeMetadata, "identity_collision_node_count"),
    );
    const invalidIdentityRecords = this._metadataCount(
      safeMetadata,
      "invalid_identity_record_count",
      0,
    );
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
          <span>${this._escape(String(total))} distinct · ${this._escape(String(online))} recent · ${this._escape(String(located))} located</span>
        </div>
        ${collapsedAliases > 0 ? `
          <div class="row">
            <span>Identity aliases</span>
            <span>${this._escape(String(collapsedAliases))} collapsed · ${this._escape(String(resolvedGroups))} groups · ${this._escape(String(retainedRecords))} retained records</span>
          </div>
          <div class="diagnostic-detail">Meshtastic records are shown once only when their ID and observed identity proof agree. Original cache records remain stored for safe rollback and are not deleted.</div>
        ` : ""}
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
        ${(typeof collisionGroups === "number" && collisionGroups > 0)
          || (typeof collisionNodes === "number" && collisionNodes > 0)
          || invalidIdentityRecords > 0 ? `
          <div class="row">
            <span>Unresolved identity evidence</span>
            <span>${this._escape(String(collisionGroups))} conflicting groups · ${this._escape(String(collisionNodes))} records · ${this._escape(String(invalidIdentityRecords))} malformed</span>
          </div>
          <div class="diagnostic-detail">MeshNet kept these records separate because their identity evidence conflicts, is incomplete, or is malformed.</div>
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
    const names = this._nodeHumanNames(node);
    if (names.primary && names.short && this._textSortKey(names.primary) !== this._textSortKey(names.short)) {
      return `${names.primary} · ${names.short}`;
    }
    if (names.primary || names.short) return names.primary || names.short;
    const meshId = this._meshtasticNodeId(node);
    if (meshId) return `Unnamed node · ${meshId}`;
    return this._nodeLabelText(node && (node.node_id || node.node_key)) || "Unknown node";
  }

  _recipientNodeName(node) {
    const label = this._nodeName(node);
    const meshId = this._meshtasticNodeId(node);
    const humanNamed = this._nodeHasHumanName(node);
    const identifier = humanNamed && meshId && !label.includes(meshId) ? ` · ${meshId}` : "";
    const hint = node && node._name_hint_exact_node_id === true ? " · cached-name match" : "";
    return `${label}${identifier}${hint}`;
  }

  _nodeCompactName(node) {
    const names = this._nodeHumanNames(node);
    if (names.short) return names.short;
    if (names.primary) return names.primary;
    const meshId = this._meshtasticNodeId(node);
    return meshId || this._nodeName(node);
  }

  _nodeHumanNames(node) {
    const primary = ["long_name", "user_name"]
      .map((field) => this._humanNodeNameField(node, field))
      .find(Boolean) || "";
    const short = this._humanNodeNameField(node, "short_name");
    return { primary, short };
  }

  _humanNodeNameField(node, field) {
    const text = this._nodeLabelText(node && node[field]);
    if (!text) return "";
    const identifiers = [node && node.node_id, node && node.node_key, this._meshtasticNodeId(node)]
      .map((value) => this._nodeLabelText(value))
      .filter(Boolean)
      .map((value) => this._textSortKey(value));
    return identifiers.includes(this._textSortKey(text)) ? "" : text;
  }

  _nodeHasHumanName(node) {
    const names = this._nodeHumanNames(node);
    return Boolean(names.primary || names.short);
  }

  _nodeLabelText(value) {
    return typeof value === "string" ? value.trim().replace(/\s+/gu, " ") : "";
  }

  _nodesWithExactMeshtasticNameHints(nodes) {
    const source = Array.isArray(nodes) ? nodes : [];
    const groups = new Map();
    source.forEach((node, index) => {
      if (this._meshtasticIdentityInvalid(node)) return;
      const meshId = this._meshtasticNodeId(node);
      if (!meshId) return;
      if (!groups.has(meshId)) groups.set(meshId, []);
      groups.get(meshId).push({ node, index });
    });
    const replacements = new Map();
    groups.forEach((members) => {
      if (members.length < 2) return;
      const identityProofs = members.map(({ node }) => this._nodeIdentityProofs(node));
      if (identityProofs.some((proofs) => !proofs.valid)) return;
      const distinctProofs = ["mac", "public_key_canonical", "public_key_explicit"]
        .map((field) => new Set(identityProofs.map((proofs) => proofs[field]).filter(Boolean)));
      if (distinctProofs.some((proofs) => proofs.size > 1)) return;

      const fields = ["long_name", "user_name", "short_name"];
      const uniqueValues = new Map();
      for (const field of fields) {
        const values = new Set(
          members
            .map(({ node }) => this._humanNodeNameField(node, field))
            .filter(Boolean)
            .map((value) => this._textSortKey(value)),
        );
        if (values.size > 1) return;
        if (values.size === 1) uniqueValues.set(field, values.values().next().value);
      }
      if (!uniqueValues.size) return;

      const donor = members.find(({ node }) => {
        return fields.every((field) => {
          const expected = uniqueValues.get(field);
          if (!expected) return true;
          const value = this._humanNodeNameField(node, field);
          return value && this._textSortKey(value) === expected;
        });
      });
      if (!donor) return;
      const donorNames = Object.fromEntries(
        fields
          .map((field) => [field, this._humanNodeNameField(donor.node, field)])
          .filter(([_field, value]) => value),
      );
      members.forEach(({ node, index }) => {
        const inherited = {};
        Object.entries(donorNames).forEach(([field, value]) => {
          if (!this._humanNodeNameField(node, field)) inherited[field] = value;
        });
        if (Object.keys(inherited).length) {
          replacements.set(index, {
            ...node,
            ...inherited,
            _name_hint_exact_node_id: true,
          });
        }
      });
    });
    return source.map((node, index) => replacements.get(index) || node);
  }

  _nodeIdentityProofs(node) {
    const nodeKey = this._nodeLabelText(node && node.node_key);
    const keyLower = nodeKey.toLowerCase();
    const keyMacPresent = keyLower.startsWith("mac:");
    const keyPublicPresent = keyLower.startsWith("pub:");
    const keyMac = keyMacPresent ? nodeKey.slice(nodeKey.indexOf(":") + 1) : "";
    const keyPublic = keyPublicPresent ? nodeKey.slice(nodeKey.indexOf(":") + 1) : "";
    const explicitMacPresent = node && node.mac != null && String(node.mac).trim() !== "";
    const explicitPublicPresent = node && node.public_key != null
      && String(node.public_key).trim() !== "";
    if ((explicitMacPresent && typeof node.mac !== "string")
      || (explicitPublicPresent && typeof node.public_key !== "string")) {
      return { valid: false };
    }
    const explicitMac = explicitMacPresent ? this._strictMac(node.mac) : "";
    const normalizedKeyMac = keyMacPresent ? this._strictMac(keyMac) : "";
    if ((explicitMacPresent && !explicitMac)
      || (keyMacPresent && !normalizedKeyMac)
      || (explicitMac && normalizedKeyMac && explicitMac !== normalizedKeyMac)) {
      return { valid: false };
    }
    const explicitPublic = explicitPublicPresent ? this._strictPublicKey(node.public_key) : "";
    const normalizedKeyPublic = keyPublicPresent ? this._strictPublicKey(keyPublic) : "";
    if ((explicitPublicPresent && !explicitPublic)
      || (keyPublicPresent && !normalizedKeyPublic)
      || (explicitPublic && normalizedKeyPublic
        && explicitPublic !== normalizedKeyPublic)) {
      return { valid: false };
    }
    return {
      valid: true,
      mac: explicitMac || normalizedKeyMac,
      public_key_canonical: explicitPublic || normalizedKeyPublic,
      public_key_explicit: explicitPublic,
    };
  }

  _strictMac(value) {
    const text = typeof value === "string" ? value.trim() : "";
    if (/^[0-9a-f]{12}$/iu.test(text)) return text.toLowerCase();
    if (/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/iu.test(text)
      || /^(?:[0-9a-f]{2}-){5}[0-9a-f]{2}$/iu.test(text)) {
      return text.toLowerCase().replace(/[:-]/gu, "");
    }
    return "";
  }

  _strictPublicKey(value) {
    const text = typeof value === "string" ? value.trim() : "";
    return /^[0-9a-f]{64}$/iu.test(text) ? text.toLowerCase() : "";
  }

  _meshtasticIdentityInvalid(node) {
    if (String(node && node.protocol || "").trim().toLowerCase() !== "meshtastic") return false;
    if (Object.prototype.hasOwnProperty.call(node, "identity_valid")) {
      return node.identity_valid !== true;
    }
    return !this._meshtasticNodeId(node);
  }

  _meshtasticNodeId(node) {
    const protocol = String(node && node.protocol || "").trim().toLowerCase();
    const nodeKey = this._nodeLabelText(node && node.node_key);
    if (protocol !== "meshtastic") return "";
    const keyLower = nodeKey.toLowerCase();
    if (nodeKey && !["meshtastic:", "meshtastic-proof:", "mac:", "pub:"]
      .some((prefix) => keyLower.startsWith(prefix))) {
      return "";
    }
    const keyIsProof = keyLower.startsWith("meshtastic-proof:");
    if (keyIsProof && !/^meshtastic-proof:[0-9a-f]{64}$/iu.test(nodeKey)) return "";
    const keyIsMeshtastic = nodeKey.toLowerCase().startsWith("meshtastic:");
    const explicitValue = node && node.node_id;
    const explicitPresent = explicitValue != null && String(explicitValue).trim() !== "";
    const explicitId = explicitPresent ? this._parseMeshtasticNodeId(explicitValue) : "";
    const keyId = keyIsMeshtastic
      ? this._parseMeshtasticNodeId(nodeKey.slice(nodeKey.indexOf(":") + 1))
      : "";
    if ((explicitPresent && !explicitId) || (keyIsMeshtastic && !keyId)) return "";
    if (explicitId && keyId && explicitId !== keyId) return "";
    return explicitId || keyId;
  }

  _parseMeshtasticNodeId(value) {
    const text = String(value == null ? "" : value).trim();
    let number = null;
    if (/^![0-9a-f]{1,8}$/iu.test(text)) number = Number.parseInt(text.slice(1), 16);
    else if (/^0x[0-9a-f]{1,8}$/iu.test(text)) number = Number.parseInt(text, 16);
    else if (/^[0-9]{1,10}$/u.test(text)) number = Number.parseInt(text, 10);
    return Number.isSafeInteger(number) && number > 0 && number < 0xffffffff
      ? `!${number.toString(16).padStart(8, "0")}`
      : "";
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
    if (mode === "name") {
      const namedDifference = Number(this._nodeHasHumanName(right))
        - Number(this._nodeHasHumanName(left));
      if (namedDifference) return namedDifference;
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
      && !(String(node && node.protocol || "").trim().toLowerCase() === "meshtastic"
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
    if (!node || String(node.protocol || "").trim().toLowerCase() !== "meshcore") return null;
    const routing = node.routing && typeof node.routing === "object" ? node.routing : {};
    const route = Array.isArray(routing.route) ? routing.route : routing.path;
    return Array.isArray(route) ? route : null;
  }

  _hopsGatewayId(node) {
    const connectivity = node && node.connectivity;
    if (String(node && node.protocol || "").trim().toLowerCase() !== "meshtastic") return "";
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
            <text x="${point.x + 19}" y="${point.y + 4}">${this._escape(this._nodeCompactName(node).slice(0, 18))}</text>
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
