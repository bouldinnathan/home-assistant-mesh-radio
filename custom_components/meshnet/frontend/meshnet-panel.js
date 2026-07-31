const MESH_CARD_IDS = Object.freeze([
  "send-message",
  "gateways",
  "remote-admin",
  "traceroute",
  "neighbor-info",
  "panel-diagnostics",
  "nodes",
  "recent-messages",
  "rf-heat",
]);
const MESH_CARD_MIN_WIDTH = 280;
const MESH_CARD_MAX_WIDTH = 720;
const MESH_CARD_MIN_HEIGHT = 120;
const MESH_CARD_MAX_HEIGHT = 1200;

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
    this._messages = [];
    this._messageConversation = "broadcast:0";
    this._messageRequestGeneration = 0;
    this._messageLoading = false;
    this._messageError = null;
    this._graphLimit = 50;
    this._graphPositions = new Map();
    this._graphAnimationFrame = null;
    this._graphAnimationTopology = null;
    this._graphAnimationIterations = 0;
    this._graphDrag = null;
    this._graphDragCleanup = null;
    // Mesh workspace layout belongs only to this attached panel instance. It
    // deliberately never enters browser storage or Home Assistant state.
    this._meshCardOrder = [...MESH_CARD_IDS];
    this._meshCardSizes = new Map();
    this._meshLayoutInteraction = null;
    this._meshLayoutCleanup = null;
    this._remoteGatewayId = "";
    this._remoteTargetNode = "";
    this._remoteSettingsSnapshot = null;
    this._remoteSettingsDraft = {};
    this._remoteSettingsPreview = null;
    this._remoteSettingsStatus = null;
    this._remoteSettingsBusy = null;
    this._remoteSettingsConfirmed = false;
    this._remoteRequestGeneration = 0;
    this._tracerouteGatewayId = "";
    this._tracerouteTargetNode = "";
    this._tracerouteConfirmation = null;
    this._tracerouteResults = {};
    this._tracerouteStatus = null;
    this._tracerouteBusy = false;
    this._tracerouteRequestGeneration = 0;
    this._tracerouteGlobalStatus = null;
    this._tracerouteStatusReady = false;
    this._tracerouteStatusLoading = false;
    this._tracerouteStatusAttempted = false;
    this._tracerouteStatusRequestGeneration = 0;
    this._neighborInfoGatewayId = "";
    this._neighborInfoTargetNode = "";
    this._neighborInfoConfirmation = null;
    this._neighborInfoResult = null;
    this._neighborInfoStatus = null;
    this._neighborInfoBusy = false;
    this._neighborInfoRequestGeneration = 0;
    this._neighborInfoStatusData = null;
    this._neighborInfoStatusReady = false;
    this._neighborInfoStatusLoading = false;
    this._neighborInfoStatusTarget = "";
    this._neighborInfoStatusRequestGeneration = 0;
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
    this._settingsAutoLoadAttempted = false;
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
    this._messageRequestGeneration += 1;
    this._remoteRequestGeneration += 1;
    this._tracerouteRequestGeneration += 1;
    this._tracerouteStatusRequestGeneration += 1;
    this._neighborInfoRequestGeneration += 1;
    this._neighborInfoStatusRequestGeneration += 1;
    this._settingsRequestGeneration += 1;
    this._stopGraphAnimation();
    this._cancelMeshLayoutInteraction();
    this._graphPositions.clear();
    this._meshCardOrder = [...MESH_CARD_IDS];
    this._meshCardSizes.clear();
    this._messages = [];
    this._messageConversation = "broadcast:0";
    this._messageLoading = false;
    this._messageError = null;
    this._draft.message = "";
    this._draft.delivery = "broadcast";
    this._draft.recipient = "";
    this._draft.channel = "0";
    this._remoteGatewayId = "";
    this._remoteTargetNode = "";
    this._remoteSettingsSnapshot = null;
    this._remoteSettingsDraft = {};
    this._remoteSettingsPreview = null;
    this._remoteSettingsStatus = null;
    this._remoteSettingsBusy = null;
    this._remoteSettingsConfirmed = false;
    this._tracerouteGatewayId = "";
    this._tracerouteTargetNode = "";
    this._tracerouteConfirmation = null;
    this._tracerouteResults = {};
    this._tracerouteStatus = null;
    this._tracerouteBusy = false;
    this._tracerouteGlobalStatus = null;
    this._tracerouteStatusReady = false;
    this._tracerouteStatusLoading = false;
    this._tracerouteStatusAttempted = false;
    this._neighborInfoGatewayId = "";
    this._neighborInfoTargetNode = "";
    this._neighborInfoConfirmation = null;
    this._neighborInfoResult = null;
    this._neighborInfoStatus = null;
    this._neighborInfoBusy = false;
    this._neighborInfoStatusData = null;
    this._neighborInfoStatusReady = false;
    this._neighborInfoStatusLoading = false;
    this._neighborInfoStatusTarget = "";
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
    this._settingsAutoLoadAttempted = false;
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
      && !this._settingsAutoLoadAttempted
    ) void this._loadGatewaySettings(this._settingsGatewayId);
  }

  _pollIsCurrent(epoch) {
    return this._connected && this._pollEpoch === epoch;
  }

  async _load(epoch) {
    try {
      await this._refreshSnapshot(epoch, "snapshot_request");
      if (this._activeView === "messages") {
        await this._loadMessages(100, false);
      }
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
      "bind_graph",
      "bind_messages",
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
      "messages_load",
      "settings_get",
      "settings_preview",
      "settings_apply",
      "settings_event",
      "bind_settings",
      "bind_views",
      "bind_remote_controls",
      "remote_control_event",
      "remote_settings_get",
      "remote_settings_preview",
      "remote_settings_apply",
      "traceroute_event",
      "traceroute_request",
      "traceroute_status",
      "neighbor_info_event",
      "neighbor_info_request",
      "neighbor_info_status",
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
      bind_graph: "event_handler",
      bind_messages: "event_handler",
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
      messages_load: "messages",
      settings_get: "settings_get",
      settings_preview: "settings_preview",
      settings_apply: "settings_apply",
      settings_event: "event_handler",
      bind_settings: "event_handler",
      bind_views: "event_handler",
      bind_remote_controls: "event_handler",
      remote_control_event: "event_handler",
      remote_settings_get: "remote_settings_get",
      remote_settings_preview: "remote_settings_preview",
      remote_settings_apply: "remote_settings_apply",
      traceroute_event: "event_handler",
      traceroute_request: "traceroute",
      traceroute_status: "traceroute",
      neighbor_info_event: "event_handler",
      neighbor_info_request: "event_handler",
      neighbor_info_status: "event_handler",
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
      messages: "message_load_failed",
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
    const meshCardScroll = this._captureMeshCardScrollState();
    this._cancelMeshLayoutInteraction();
    if (this._activeView === "settings") {
      this._renderSettings(composerFocus);
      return;
    }
    if (this._activeView === "messages") {
      const messageScroll = this._captureMessageScrollState();
      this._renderMessages(composerFocus, messageScroll);
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
    const neighborInfoNodeKeys = new Set(
      this._remoteNodeCandidates(nodes).map((node) => String(node.node_key)),
    );
    const neighborInfoGatewayAvailable = this._remoteGatewayCandidates(gateways).length > 0;
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
    const topology = this._passiveTopology(nodes, gateways, this._graphLimit);
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
          grid-template-columns: minmax(0, 1fr) max-content;
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
          touch-action: none;
        }
        .topology [data-graph-key] {
          cursor: grab;
        }
        .topology [data-graph-key]:active {
          cursor: grabbing;
        }
        .side {
          display: grid;
          align-content: start;
          justify-items: start;
          gap: 12px;
        }
        .panel {
          padding: 12px;
          box-sizing: border-box;
          width: 360px;
          min-width: ${MESH_CARD_MIN_WIDTH}px;
          max-width: min(${MESH_CARD_MAX_WIDTH}px, 48vw, calc(100vw - 32px));
          min-height: ${MESH_CARD_MIN_HEIGHT}px;
          position: relative;
          overflow: auto;
        }
        .mesh-layout-help {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          box-sizing: border-box;
          width: 360px;
          max-width: calc(100vw - 32px);
          color: var(--secondary-text-color);
          font-size: 11px;
        }
        .mesh-layout-reset,
        .mesh-card-layout-bar button,
        .mesh-card-resize-handle {
          border: 1px solid var(--divider-color);
          border-radius: 5px;
          padding: 3px 6px;
          color: var(--secondary-text-color);
          background: var(--card-background-color);
          cursor: pointer;
          font: inherit;
        }
        .mesh-card-layout-bar {
          display: flex;
          align-items: center;
          justify-content: flex-end;
          gap: 4px;
          margin: -5px -5px 6px 0;
          min-height: 24px;
        }
        .mesh-card-drag-handle {
          cursor: grab !important;
          touch-action: none;
        }
        .mesh-card-drag-handle:active { cursor: grabbing !important; }
        .mesh-card-resize-handle {
          position: absolute;
          right: 3px;
          bottom: 3px;
          z-index: 2;
          cursor: nwse-resize;
          touch-action: none;
        }
        .panel.mesh-card-dragging { opacity: 0.68; }
        .mesh-layout-reset:focus-visible,
        .mesh-card-layout-bar button:focus-visible,
        .mesh-card-resize-handle:focus-visible {
          outline: 2px solid var(--primary-color);
          outline-offset: 2px;
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
        .operator-controls {
          display: grid;
          gap: 9px;
        }
        .operator-controls label {
          display: grid;
          gap: 4px;
          color: var(--secondary-text-color);
          font-size: 12px;
        }
        .operator-controls select,
        .operator-controls input[type="text"],
        .operator-controls input[type="number"] {
          width: 100%;
          box-sizing: border-box;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          padding: 7px;
          color: var(--primary-text-color);
          background: var(--card-background-color);
          font: inherit;
        }
        .operator-actions {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 7px;
        }
        .operator-button {
          border: 1px solid var(--primary-color);
          border-radius: 6px;
          padding: 7px 10px;
          color: var(--primary-color);
          background: transparent;
          cursor: pointer;
          font: inherit;
        }
        .operator-button.primary {
          color: var(--text-primary-color, #fff);
          background: var(--primary-color);
        }
        .operator-button:disabled {
          cursor: default;
          opacity: 0.55;
        }
        .operator-status {
          min-height: 17px;
          overflow-wrap: anywhere;
          font-size: 12px;
        }
        .operator-card {
          display: grid;
          gap: 8px;
          margin-top: 10px;
          padding-top: 10px;
          border-top: 1px solid var(--divider-color);
        }
        .operator-card h3 {
          margin: 0;
          font-size: 14px;
          font-weight: 500;
        }
        .operator-field {
          display: grid;
          gap: 4px;
        }
        .controller-key {
          display: block;
          padding: 7px;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          overflow-wrap: anywhere;
          user-select: all;
          font: 11px var(--code-font-family, monospace);
        }
        .confirmation-box {
          display: grid;
          grid-template-columns: auto 1fr;
          align-items: start;
          gap: 7px;
          padding: 8px;
          border: 1px solid var(--warning-color, #b26a00);
          border-radius: 6px;
          color: var(--primary-text-color);
          font-size: 12px;
        }
        .route-list {
          margin: 2px 0 0;
          padding-left: 20px;
          font-size: 12px;
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
        text.edge-distance {
          fill: var(--primary-text-color);
          stroke: var(--card-background-color);
          stroke-width: 4px;
          paint-order: stroke;
          text-anchor: middle;
          pointer-events: none;
          font-size: 11px;
        }
        .topology-empty {
          fill: var(--secondary-text-color);
          font-size: 16px;
          text-anchor: middle;
        }
        text { fill: var(--primary-text-color); font-size: 12px; }
        @media (max-width: 900px) {
          .wrap { grid-template-columns: 1fr; padding: 10px; }
          .side { width: 100%; }
          .mesh-layout-help { width: 100%; max-width: 100%; }
          .panel[data-mesh-card] {
            min-width: 0;
            width: 100% !important;
            height: auto !important;
            max-width: 100%;
          }
          .mesh-card-resize-handle { display: none; }
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
        <aside class="side" id="meshnet-mesh-cards">
          <div class="mesh-layout-help">
            <span>Drag or use arrows to reorder. Resize from the corner on wider screens. Layout resets when you leave MeshNet.</span>
            <button class="mesh-layout-reset" id="meshnet-layout-reset" type="button">Reset</button>
          </div>
          <section class="panel" data-mesh-card="send-message">
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
          <section class="panel" data-mesh-card="gateways">
            <h2>Gateways</h2>
            ${gateways.map((gateway) => `
              <div class="row">
                <span>${this._escape(gateway.name || gateway.gateway_id)}</span>
                <span class="${gateway.connected ? "good" : "bad"}">${gateway.connected ? "online" : "offline"}</span>
              </div>
            `).join("") || `<div class="label">No gateways configured</div>`}
          </section>
          ${this._remoteAdminPanel(nodes, gateways)}
          ${this._traceroutePanel(nodes, gateways)}
          ${this._neighborInfoPanel(nodes, gateways)}
          ${this._panelDiagnostics(snapshot.panel_metadata, nodes)}
          <section class="panel" data-mesh-card="nodes">
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
                  <button class="node-message" type="button" data-neighbor-info-node="${this._escape(node.node_key)}"${neighborInfoNodeKeys.has(String(node.node_key)) && neighborInfoGatewayAvailable ? "" : ' disabled aria-disabled="true" title="NeighborInfo requires one exact valid Meshtastic node and a connected Bluetooth gateway"'}>NeighborInfo</button>
                </span>
              </div>
            `).join("") || `<div class="label">Waiting for node data</div>`}
            ${sortedNodes.length > 24 ? `<div class="label">Showing 24 of ${sortedNodes.length} nodes</div>` : ""}
            ${unnamedNodeCount ? `<div class="label">${unnamedNodeCount} Meshtastic packet/cache record${unnamedNodeCount === 1 ? " arrived" : "s arrived"} without a NodeInfo name. MeshNet keeps uncertain identities separate.</div>` : ""}
            ${hintedNodeCount ? `<div class="label">${hintedNodeCount} display label${hintedNodeCount === 1 ? " uses" : "s use"} an unambiguous cached name from the same exact !ID. Records and send targets remain separate.</div>` : ""}
            ${favoriteLabelConfigured ? "" : '<div class="label">To pin favorites, add the Home Assistant device label “MeshNet Favorite”.</div>'}
          </section>
          <section class="panel" data-mesh-card="recent-messages">
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
          <section class="panel" data-mesh-card="rf-heat">
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
    this._safeStep("bind_mesh_layout", "binding", () => this._bindMeshCardLayout(meshCardScroll));
    this._safeStep("bind_composer", "binding", () => this._bindComposer());
    this._safeStep("bind_nodes", "binding", () => this._bindNodeControls());
    this._safeStep("bind_remote_controls", "binding", () => this._bindAdvancedControls());
    this._safeStep("bind_graph", "binding", () => this._bindGraphControls(topology));
    this._safeStep("restore_focus", "focus", () => this._restoreComposerFocus(composerFocus));
  }

  _remoteGatewayCandidates(gateways) {
    return (Array.isArray(gateways) ? gateways : []).filter((gateway) => (
      gateway
      && typeof gateway === "object"
      && !Array.isArray(gateway)
      && typeof gateway.gateway_id === "string"
      && gateway.gateway_id.length >= 1
      && gateway.gateway_id.length <= 128
      && gateway.gateway_id === gateway.gateway_id.trim()
      && String(gateway.protocol || "").trim().toLowerCase() === "meshtastic"
      && String(gateway.transport || "").trim().toLowerCase() === "bluetooth"
      && gateway.connected === true
    ));
  }

  _remoteNodeCandidates(nodes) {
    const candidates = (Array.isArray(nodes) ? nodes : []).filter((node) => {
      const nodeId = this._meshtasticNodeId(node);
      if (
        !this._isExactRemoteTarget(nodeId)
        || node.identity_valid !== true
        || node.node_key !== `meshtastic:${nodeId}`
      ) return false;
      return true;
    });
    const counts = new Map();
    candidates.forEach((node) => {
      const nodeId = this._meshtasticNodeId(node);
      counts.set(nodeId, (counts.get(nodeId) || 0) + 1);
    });
    return this._sortNodes(
      candidates.filter((node) => counts.get(this._meshtasticNodeId(node)) === 1),
      "favorites_recent",
    );
  }

  _isExactRemoteTarget(value) {
    return typeof value === "string"
      && /^![0-9a-f]{8}$/.test(value)
      && !["!00000000", "!ffffffff"].includes(value);
  }

  _isExactTracerouteTarget(value) {
    return typeof value === "string"
      && /^meshtastic:![0-9a-f]{8}$/.test(value)
      && !["meshtastic:!00000000", "meshtastic:!ffffffff"].includes(value);
  }

  _operatorGatewayOptions(gateways, selected) {
    if (!gateways.length) return '<option value="">No connected Meshtastic Bluetooth gateway</option>';
    return gateways.map((gateway) => {
      const gatewayId = gateway.gateway_id;
      const name = typeof gateway.name === "string" && gateway.name.trim()
        ? gateway.name.trim()
        : gatewayId;
      const label = name === gatewayId ? name : `${name} (${gatewayId})`;
      return `<option value="${this._escape(gatewayId)}"${this._selected(selected, gatewayId)}>${this._escape(label)}</option>`;
    }).join("");
  }

  _operatorTargetOptions(nodes, selected, { traceroute = false } = {}) {
    if (!nodes.length) return '<option value="">No exact Meshtastic node available</option>';
    return this._sortNodes(nodes, "favorites_recent").map((node) => {
      const nodeId = this._meshtasticNodeId(node);
      const value = traceroute ? `meshtastic:${nodeId}` : nodeId;
      const label = this._nodeName(node);
      const visible = label.includes(nodeId) ? label : `${label} · ${nodeId}`;
      const favorite = node.favorite === true ? "★ " : "";
      const lastSeen = this._humanLastSeen(node.last_heard);
      return `<option value="${this._escape(value)}"${this._selected(selected, value)}>${this._escape(`${favorite}${visible} · ${lastSeen}`)}</option>`;
    }).join("");
  }

  _remoteAdminPanel(nodes, gateways) {
    const compatibleGateways = this._remoteGatewayCandidates(gateways);
    const compatibleNodes = this._remoteNodeCandidates(nodes);
    const selectedGateway = compatibleGateways.some(
      (gateway) => gateway.gateway_id === this._remoteGatewayId,
    ) ? this._remoteGatewayId : compatibleGateways[0] && compatibleGateways[0].gateway_id || "";
    const selectedTarget = compatibleNodes.some(
      (node) => this._meshtasticNodeId(node) === this._remoteTargetNode,
    ) ? this._remoteTargetNode : compatibleNodes[0] ? this._meshtasticNodeId(compatibleNodes[0]) : "";
    const snapshot = this._remoteSettingsSnapshot;
    const snapshotMatches = snapshot
      && snapshot.gateway_id === selectedGateway
      && snapshot.target_node === selectedTarget;
    const fields = snapshotMatches ? this._remoteSettingsFields(snapshot) : [];
    const hasDraft = Object.keys(this._remoteSettingsDraft).length > 0;
    const busy = this._remoteSettingsBusy != null;
    return `
      <section class="panel" id="meshnet-remote-admin-panel" data-mesh-card="remote-admin">
        <h2>Remote node administration</h2>
        <div class="field-help">Explicit Meshtastic Bluetooth requests only. Loading is read-only; every write requires a preview and a separate confirmation.</div>
        <div class="operator-controls">
          <label>Gateway
            <select id="meshnet-remote-gateway"${compatibleGateways.length ? "" : " disabled"}>
              ${this._operatorGatewayOptions(compatibleGateways, selectedGateway)}
            </select>
          </label>
          <label>Exact target node
            <select id="meshnet-remote-target"${compatibleNodes.length ? "" : " disabled"}>
              ${this._operatorTargetOptions(compatibleNodes, selectedTarget)}
            </select>
          </label>
          <div class="operator-actions">
            <button class="operator-button" id="meshnet-remote-load" type="button"${busy || !selectedGateway || !selectedTarget ? " disabled" : ""}>${this._remoteSettingsBusy === "get" ? "Loading…" : "Load / test access"}</button>
          </div>
          ${snapshotMatches ? `
            <section class="operator-card">
              <h3>${this._escape(this._remoteTargetLabel(snapshot.target))}</h3>
              <div class="field-help">Controller ${this._escape(this._remoteControllerLabel(snapshot.controller))}</div>
              <label>Controller public key (copy-only)
                <code class="controller-key" id="meshnet-controller-public-key">${this._escape(snapshot.controller.public_key)}</code>
              </label>
              <div class="operator-actions">
                <button class="operator-button" id="meshnet-controller-key-copy" type="button">Copy controller public key</button>
              </div>
            </section>
            <form class="operator-card" id="meshnet-remote-settings-form">
              ${snapshot.categories.map((category) => `
                <section class="operator-card">
                  <h3>${this._escape(category.label)}</h3>
                  ${category.fields.map((field) => this._remoteSettingsField(
                    field,
                    fields.findIndex((candidate) => candidate.path === field.path),
                  )).join("")}
                </section>
              `).join("")}
              <div class="operator-actions">
                <button class="operator-button primary" id="meshnet-remote-preview" type="submit"${busy || !hasDraft || this._remoteSettingsPreview ? " disabled" : ""}>${this._remoteSettingsBusy === "preview" ? "Preparing preview…" : "Preview remote changes"}</button>
              </div>
            </form>
            ${this._remoteSettingsPreviewPanel()}
          ` : ""}
          <div class="operator-status ${this._remoteSettingsStatusClass()}" id="meshnet-remote-status" role="status" aria-live="polite">${this._escape(this._remoteSettingsStatus ? this._remoteSettingsStatus.text : "")}</div>
        </div>
      </section>
    `;
  }

  _remoteTargetLabel(target) {
    if (!target || typeof target !== "object") return "Remote node";
    const names = [target.long_name, target.short_name]
      .filter((value) => typeof value === "string" && value)
      .join(" · ");
    return names ? `${names} · ${target.node_id}` : target.node_id;
  }

  _remoteControllerLabel(controller) {
    if (!controller || typeof controller !== "object") return "unknown";
    return controller.short_name
      ? `${controller.short_name} · ${controller.node_id}`
      : controller.node_id;
  }

  _remoteSettingsFields(snapshot = this._remoteSettingsSnapshot) {
    if (!snapshot || !Array.isArray(snapshot.categories)) return [];
    return snapshot.categories.flatMap((category) => category.fields);
  }

  _remoteSettingsField(field, index) {
    const value = Object.hasOwn(this._remoteSettingsDraft, field.path)
      ? this._remoteSettingsDraft[field.path]
      : field.value;
    const common = `id="meshnet-remote-setting-${index}" data-remote-setting-index="${index}"${this._remoteSettingsBusy != null || !field.writable ? " disabled" : ""}`;
    let input;
    if (field.type === "boolean") {
      input = `<input ${common} type="checkbox"${value === true ? " checked" : ""}>`;
    } else if (field.type === "select") {
      const selectedIndex = field.options.findIndex(
        (option) => this._settingValuesEqual(option.value, value),
      );
      input = `<select ${common}>${field.options.map((option, optionIndex) => `<option value="${optionIndex}"${optionIndex === selectedIndex ? " selected" : ""}>${this._escape(option.label)}</option>`).join("")}</select>`;
    } else if (field.type === "integer" || field.type === "number") {
      const attributes = [
        field.min != null ? `min="${this._escape(field.min)}"` : "",
        field.max != null ? `max="${this._escape(field.max)}"` : "",
        field.step != null ? `step="${this._escape(field.step)}"` : field.type === "integer" ? 'step="1"' : 'step="any"',
      ].filter(Boolean).join(" ");
      input = `<input ${common} type="number" ${attributes} value="${this._escape(value)}">`;
    } else {
      input = `<input ${common} type="text" value="${this._escape(value)}" maxlength="${field.max_length}">`;
    }
    return `
      <label class="operator-field" for="meshnet-remote-setting-${index}">
        <span>${this._escape(field.label)}</span>
        ${input}
        <span class="field-help">${this._escape(field.path)}</span>
      </label>
    `;
  }

  _remoteSettingsPreviewPanel() {
    const preview = this._remoteSettingsPreview;
    if (!preview) return "";
    return `
      <section class="operator-card" id="meshnet-remote-preview-result">
        <h3>Remote write preview</h3>
        <div class="field-help">Expires ${this._escape(preview.expires_at)}</div>
        <ul class="route-list">
          ${preview.changes.map((change) => `<li>${this._escape(change.label)} <span class="field-help">${this._escape(change.path)}</span></li>`).join("")}
        </ul>
        <label class="confirmation-box">
          <input id="meshnet-remote-confirm" type="checkbox"${this._remoteSettingsConfirmed ? " checked" : ""}>
          <span>I confirm one remote RF write to this exact node. A timeout can have an unknown outcome and will not be retried.</span>
        </label>
        <div class="operator-actions">
          <button class="operator-button primary" id="meshnet-remote-apply" type="button"${this._remoteSettingsBusy != null || !this._remoteSettingsConfirmed ? " disabled" : ""}>${this._remoteSettingsBusy === "apply" ? "Applying…" : "Apply once and verify"}</button>
        </div>
      </section>
    `;
  }

  _remoteSettingsStatusClass() {
    const kind = this._remoteSettingsStatus && this._remoteSettingsStatus.kind;
    return ["good", "warn", "bad"].includes(kind) ? kind : "";
  }

  _traceroutePanel(nodes, gateways) {
    const compatibleGateways = this._remoteGatewayCandidates(gateways);
    const compatibleNodes = this._remoteNodeCandidates(nodes);
    const selectedGateway = compatibleGateways.some(
      (gateway) => gateway.gateway_id === this._tracerouteGatewayId,
    ) ? this._tracerouteGatewayId : compatibleGateways[0] && compatibleGateways[0].gateway_id || "";
    const selectedTarget = compatibleNodes.some(
      (node) => `meshtastic:${this._meshtasticNodeId(node)}` === this._tracerouteTargetNode,
    ) ? this._tracerouteTargetNode : compatibleNodes[0]
      ? `meshtastic:${this._meshtasticNodeId(compatibleNodes[0])}`
      : "";
    const pending = this._tracerouteConfirmation
      && this._tracerouteConfirmation.gateway_id === selectedGateway
      && this._tracerouteConfirmation.target_node === selectedTarget;
    const result = this._tracerouteResultFor(selectedGateway, selectedTarget);
    const cooldown = this._tracerouteCooldownActive(selectedGateway, selectedTarget);
    return `
      <section class="panel" id="meshnet-traceroute-panel" data-mesh-card="traceroute">
        <h2>Manual traceroute</h2>
        <div class="field-help">This sends RF traffic. It is never run by polling, graph animation, startup, or automation. One attempt starts an integration-wide one-minute cooldown.</div>
        <div class="operator-controls">
          <label>Gateway
            <select id="meshnet-traceroute-gateway"${compatibleGateways.length ? "" : " disabled"}>
              ${this._operatorGatewayOptions(compatibleGateways, selectedGateway)}
            </select>
          </label>
          <label>Exact destination
            <select id="meshnet-traceroute-target"${compatibleNodes.length ? "" : " disabled"}>
              ${this._operatorTargetOptions(compatibleNodes, selectedTarget, { traceroute: true })}
            </select>
          </label>
          <div class="operator-actions">
            <button class="operator-button" id="meshnet-traceroute-start" type="button"${this._tracerouteBusy || this._tracerouteStatusLoading || !this._tracerouteStatusReady || cooldown || !selectedGateway || !selectedTarget ? " disabled" : ""}>${this._tracerouteBusy ? "Waiting for route…" : this._tracerouteStatusLoading ? "Checking cooldown…" : "Traceroute"}</button>
            <button class="operator-button" id="meshnet-traceroute-status-reload" type="button"${this._tracerouteStatusLoading || this._tracerouteBusy ? " disabled" : ""}>${this._tracerouteStatusLoading ? "Checking…" : "Reload persisted status"}</button>
          </div>
          ${this._tracerouteGlobalStatusPanel()}
          ${pending ? `
            <div class="confirmation-box">
              <span aria-hidden="true">⚠</span>
              <span>Confirm one unicast RouteDiscovery packet to <strong>${this._escape(selectedTarget)}</strong>. Do not repeat after a timeout.</span>
            </div>
            <div class="operator-actions">
              <button class="operator-button primary" id="meshnet-traceroute-confirm" type="button"${this._tracerouteBusy ? " disabled" : ""}>Confirm one traceroute</button>
              <button class="operator-button" id="meshnet-traceroute-cancel" type="button"${this._tracerouteBusy ? " disabled" : ""}>Cancel</button>
            </div>
          ` : ""}
          ${result ? this._tracerouteResultPanel(result) : ""}
          <div class="operator-status ${this._tracerouteStatusClass()}" id="meshnet-traceroute-status" role="status" aria-live="polite">${this._escape(this._tracerouteStatus ? this._tracerouteStatus.text : "")}</div>
        </div>
      </section>
    `;
  }

  _tracerouteResultPanel(result) {
    const route = result.forward_route || [];
    const reverse = result.reverse_route || [];
    const towards = result.snr_towards || [];
    const back = result.snr_back || [];
    return `
      <section class="operator-card">
        <h3>Most recent explicit result</h3>
        ${result.correlation_id ? `<div class="field-help">Correlation ${this._escape(result.correlation_id)}</div>` : ""}
        <div class="field-help">Gateway: ${this._escape(result.gateway_id)}</div>
        ${result.completed_at ? `<div class="field-help">Completed: ${this._escape(this._timestampDisplay(result.completed_at))}</div>` : ""}
        <div><strong>Forward route</strong>${route.length ? `<ol class="route-list">${route.map((hop) => `<li>${this._escape(hop)}</li>`).join("")}</ol>` : '<div class="field-help">No forward hops reported.</div>'}</div>
        <div><strong>Reverse route</strong>${reverse.length ? `<ol class="route-list">${reverse.map((hop) => `<li>${this._escape(hop)}</li>`).join("")}</ol>` : '<div class="field-help">No reverse hops reported.</div>'}</div>
        ${towards.length ? `<div class="field-help">Forward SNR: ${this._escape(this._formatTracerouteSnr(towards))}</div>` : ""}
        ${back.length ? `<div class="field-help">Reverse SNR: ${this._escape(this._formatTracerouteSnr(back))}</div>` : ""}
        ${result.next_allowed_at ? `<div class="field-help">Next permitted attempt: ${this._escape(this._timestampDisplay(result.next_allowed_at))}</div>` : ""}
      </section>
    `;
  }

  _tracerouteGlobalStatusPanel() {
    const status = this._tracerouteGlobalStatus;
    if (!status) {
      return this._tracerouteStatusLoading
        ? '<div class="field-help">Checking the persisted global cooldown…</div>'
        : '<div class="field-help warn">Persisted cooldown has not been verified. RF control remains locked.</div>';
    }
    if (status.status !== "cooldown") {
      return '<div class="field-help good">No persisted global traceroute cooldown is active.</div>';
    }
    return `
      <div class="operator-card">
        <strong>Global cooldown active</strong>
        <div class="field-help">Reserved by ${this._escape(status.gateway_id)} → ${this._escape(status.target_node)}</div>
        <div class="field-help">Next permitted attempt: ${this._escape(this._timestampDisplay(status.next_allowed_at))}</div>
      </div>
    `;
  }

  _formatTracerouteSnr(values) {
    return values.map((value) => `${Number(value)} dB`).join(" · ");
  }

  _tracerouteStatusClass() {
    const kind = this._tracerouteStatus && this._tracerouteStatus.kind;
    return ["good", "warn", "bad"].includes(kind) ? kind : "";
  }

  _neighborInfoPanel(nodes, gateways) {
    const compatibleGateways = this._remoteGatewayCandidates(gateways);
    const compatibleNodes = this._remoteNodeCandidates(nodes);
    const selectedGateway = compatibleGateways.some(
      (gateway) => gateway.gateway_id === this._neighborInfoGatewayId,
    ) ? this._neighborInfoGatewayId : compatibleGateways[0] && compatibleGateways[0].gateway_id || "";
    const selectedTarget = compatibleNodes.some(
      (node) => `meshtastic:${this._meshtasticNodeId(node)}` === this._neighborInfoTargetNode,
    ) ? this._neighborInfoTargetNode : compatibleNodes[0]
      ? `meshtastic:${this._meshtasticNodeId(compatibleNodes[0])}`
      : "";
    const statusReady = this._neighborInfoStatusReady
      && this._neighborInfoStatusTarget === selectedTarget;
    const pending = this._neighborInfoConfirmation
      && this._neighborInfoConfirmation.gateway_id === selectedGateway
      && this._neighborInfoConfirmation.target_node === selectedTarget;
    const cooldown = statusReady && this._neighborInfoCooldownActive();
    const result = this._neighborInfoResult
      && this._neighborInfoResult.gateway_id === selectedGateway
      && this._neighborInfoResult.source === selectedTarget
      ? this._neighborInfoResult
      : null;
    return `
      <section class="panel" id="meshnet-neighbor-info-panel" data-mesh-card="neighbor-info">
        <h2>NeighborInfo request</h2>
        <div class="field-help warn"><strong>Experimental / newer firmware only.</strong> Verified on firmware 2.7.26 when Neighbor Info is enabled. Older firmware may reject or time out; the official Android app temporarily disabled this control because it was not working as expected.</div>
        <div class="field-help">This submits one application request with no MeshNet retry. Firmware may relay or retransmit reliable traffic. MeshNet never runs it during polling, startup, or graph animation. The server enforces 180-second global and same-target cooldowns.</div>
        <div class="operator-controls">
          <label>Bluetooth gateway
            <select id="meshnet-neighbor-info-gateway"${compatibleGateways.length && !this._neighborInfoBusy && !this._neighborInfoStatusLoading ? "" : " disabled"}>
              ${this._operatorGatewayOptions(compatibleGateways, selectedGateway)}
            </select>
          </label>
          <label>Exact Meshtastic node
            <select id="meshnet-neighbor-info-target"${compatibleNodes.length && !this._neighborInfoBusy && !this._neighborInfoStatusLoading ? "" : " disabled"}>
              ${this._operatorTargetOptions(compatibleNodes, selectedTarget, { traceroute: true })}
            </select>
          </label>
          <div class="operator-actions">
            <button class="operator-button" id="meshnet-neighbor-info-status-load" type="button"${this._neighborInfoBusy || this._neighborInfoStatusLoading || !selectedTarget ? " disabled" : ""}>${this._neighborInfoStatusLoading ? "Checking…" : "Load persisted status"}</button>
            <button class="operator-button" id="meshnet-neighbor-info-start" type="button"${this._neighborInfoBusy || this._neighborInfoStatusLoading || !statusReady || cooldown || !selectedGateway || !selectedTarget ? " disabled" : ""}>${this._neighborInfoBusy ? "Waiting for response…" : "Request NeighborInfo"}</button>
          </div>
          ${this._neighborInfoStatusPanel(statusReady)}
          ${pending ? `
            <div class="confirmation-box">
              <span aria-hidden="true">⚠</span>
              <span>Confirm one experimental NeighborInfo application request to <strong>${this._escape(selectedTarget)}</strong>. A timeout can have an unknown outcome and must not be retried blindly.</span>
            </div>
            <div class="operator-actions">
              <button class="operator-button primary" id="meshnet-neighbor-info-confirm" type="button"${this._neighborInfoBusy ? " disabled" : ""}>Confirm request</button>
              <button class="operator-button" id="meshnet-neighbor-info-cancel" type="button"${this._neighborInfoBusy ? " disabled" : ""}>Cancel</button>
            </div>
          ` : ""}
          ${result ? this._neighborInfoResultPanel(result) : ""}
          <div class="operator-status ${this._neighborInfoStatusClass()}" id="meshnet-neighbor-info-status" role="status" aria-live="polite">${this._escape(this._neighborInfoStatus ? this._neighborInfoStatus.text : "")}</div>
        </div>
      </section>
    `;
  }

  _neighborInfoStatusPanel(statusReady) {
    const status = this._neighborInfoStatusData;
    if (!statusReady || !status) {
      return this._neighborInfoStatusLoading
        ? '<div class="field-help">Checking persisted NeighborInfo cooldowns…</div>'
        : '<div class="field-help warn">Load persisted status for this exact target before RF control is enabled.</div>';
    }
    if (!this._neighborInfoCooldownActive()) {
      return '<div class="field-help good">No persisted NeighborInfo cooldown is active for this request.</div>';
    }
    return `
      <div class="operator-card">
        <strong>NeighborInfo cooldown active</strong>
        <div class="field-help">Global: ${this._escape(status.global_remaining_seconds)}s · same target: ${this._escape(status.target_remaining_seconds)}s</div>
        ${status.next_allowed_at ? `<div class="field-help">Next permitted request: ${this._escape(this._timestampDisplay(status.next_allowed_at))}</div>` : ""}
      </div>
    `;
  }

  _neighborInfoResultPanel(result) {
    return `
      <section class="operator-card">
        <h3>Completed NeighborInfo response</h3>
        <div class="field-help">Gateway: ${this._escape(result.gateway_id)} · source: ${this._escape(result.source)}</div>
        <div class="field-help">Completed: ${this._escape(this._timestampDisplay(result.completed_at))}</div>
        <div class="field-help">Node broadcast interval: ${this._escape(result.node_broadcast_interval_secs)} seconds</div>
        ${result.neighbors.length
          ? `<ol class="route-list">${result.neighbors.map((neighbor) => `<li>${this._escape(neighbor.node_id)} · ${this._escape(neighbor.snr)} dB SNR</li>`).join("")}</ol>`
          : '<div class="field-help">The completed response reported no neighbors.</div>'}
      </section>
    `;
  }

  _neighborInfoStatusClass() {
    const kind = this._neighborInfoStatus && this._neighborInfoStatus.kind;
    return ["good", "warn", "bad"].includes(kind) ? kind : "";
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
      "meshnet-message-conversation",
      "meshnet-graph-limit",
      "meshnet-remote-gateway",
      "meshnet-remote-target",
      "meshnet-remote-load",
      "meshnet-remote-preview",
      "meshnet-remote-confirm",
      "meshnet-remote-apply",
      "meshnet-controller-key-copy",
      "meshnet-traceroute-gateway",
      "meshnet-traceroute-target",
      "meshnet-traceroute-start",
      "meshnet-traceroute-status-reload",
      "meshnet-traceroute-confirm",
      "meshnet-traceroute-cancel",
      "meshnet-neighbor-info-gateway",
      "meshnet-neighbor-info-target",
      "meshnet-neighbor-info-status-load",
      "meshnet-neighbor-info-start",
      "meshnet-neighbor-info-confirm",
      "meshnet-neighbor-info-cancel",
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
      || id.startsWith("meshnet-remote-setting-")
      || [
        "meshnet-settings-gateway",
        "meshnet-settings-preview",
        "meshnet-settings-apply",
        "meshnet-settings-critical",
        "meshnet-settings-reload",
        "meshnet-view-mesh",
        "meshnet-view-messages",
        "meshnet-view-settings",
      ].includes(id);
    try {
      isSetting = isSetting
        || (typeof active.hasAttribute === "function"
          && (active.hasAttribute("data-setting-index")
            || active.hasAttribute("data-setting-clear-index")
            || active.hasAttribute("data-remote-setting-index")));
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
    if (this._graphDrag || this._meshLayoutInteraction) return true;
    if (this._composerFocusState() || this._settingsFocusState()) return true;
    const active = this._activePanelElement();
    if (!active || !this.contains(active)) return false;
    if (["meshnet-send-button", "meshnet-layout-reset"].includes(active.id)) return true;
    try {
      return typeof active.hasAttribute === "function"
        && (active.hasAttribute("data-message-node")
          || active.hasAttribute("data-neighbor-info-node")
          || active.hasAttribute("data-mesh-drag-handle")
          || active.hasAttribute("data-mesh-resize-handle")
          || active.hasAttribute("data-mesh-card-move"));
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
        <button id="meshnet-view-messages" class="view-tab${this._activeView === "messages" ? " active" : ""}" type="button" data-meshnet-view="messages"${this._activeView === "messages" ? ' aria-current="page"' : ""}>Messages</button>
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
    const next = ["mesh", "messages", "settings"].includes(view) ? view : "mesh";
    if (this._activeView === next) return;
    if (this._activeView === "mesh" && next !== "mesh") this._stopGraphAnimation();
    this._activeView = next;
    this._safeRender("render");
    if (next === "settings" && !this._settingsSnapshot && this._settingsBusy !== "get") {
      void this._loadGatewaySettings();
    }
    if (next === "messages" && !this._messageLoading) {
      void this._loadMessages(100);
    }
  }

  _renderSettings(focusState = null) {
    const snapshot = this._settingsSnapshot;
    const fields = this._settingsFields(snapshot);
    const editableCount = this._settingsEditableCount(snapshot);
    const readOnlyCount = fields.length - editableCount;
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
          ${!snapshot && this._settingsBusy !== "get" && !this._settingsStatus ? '<div class="settings-status warn">Choose a gateway to load its supported settings.</div>' : ""}
          ${snapshot ? `
            <div class="field-help">${this._escape(`${snapshot.name} · ${snapshot.protocol} over ${snapshot.transport} · revision ${snapshot.revision}`)}</div>
            <div class="settings-status ${editableCount > 0 ? "good" : "warn"}">${editableCount} editable · ${readOnlyCount} read-only</div>
            ${editableCount === 0 ? `<div class="settings-status warn">${fields.length > 0 ? "No settings can be edited safely right now." : "This gateway did not report any settings."}${snapshot.read_only_reason ? ` ${this._escape(snapshot.read_only_reason)}` : ""}</div>` : ""}
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
              `).join("") || '<div class="settings-status warn">This gateway did not report any settings categories.</div>'}
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

  _settingsEditableCount(snapshot = this._settingsSnapshot) {
    if (!snapshot || !snapshot.writable) return 0;
    return this._settingsFields(snapshot).filter((field) => field.writable).length;
  }

  _settingsField(field, index, snapshot) {
    const disabled = this._settingsBusy != null || !snapshot.writable || !field.writable;
    const value = Object.hasOwn(this._settingsDraft, field.path)
      ? this._settingsDraft[field.path]
      : field.value;
    const badges = [
      snapshot.writable && field.writable ? '<span class="badge good">Editable</span>' : "",
      field.critical ? '<span class="badge warn">Critical</span>' : "",
      field.requires_reconnect ? '<span class="badge warn">Reconnect required</span>' : "",
      field.type === "secret" && field.configured ? '<span class="badge good">Configured</span>' : "",
    ].join("");
    const reason = !field.writable && field.read_only_reason
      ? field.read_only_reason
      : !snapshot.writable
        ? snapshot.read_only_reason
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
    // Home Assistant assigns ``hass`` whenever state changes. A failed schema
    // must not turn those assignments into an unbounded settings-read loop.
    // The visible reload button, gateway selection, or leaving/re-entering the
    // tab can still start an explicit new attempt.
    this._settingsAutoLoadAttempted = true;
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
      const editableCount = this._settingsEditableCount(validated.selected);
      this._settingsStatus = validated.selected
        ? editableCount > 0
          ? { kind: "good", text: "Gateway settings loaded. Edit values, then preview." }
          : { kind: "warn", text: "Gateway settings loaded read-only. Review the explanation above." }
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
    const writable = field.writable === true;
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
    if (type === "select" && writable && !options.length) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    let value = type === "secret" ? "" : field.value;
    if (value != null && !this._settingValueMatchesType(type, value)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    if (writable && type !== "secret" && value == null) {
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
      (writable && (type === "integer" || type === "number")
        && (min == null || max == null))
      || (writable && min != null && max != null && min > max)
      || (writable && step != null && step <= 0)
      || (writable && (type === "integer" || type === "number") && value != null
        && ((min != null && value < min) || (max != null && value > max)))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    if (
      type === "select"
      && writable
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
      && writable
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
      writable,
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
        && (
          Number.isInteger(value)
            ? Number.isSafeInteger(value)
            : Math.abs(value) <= 1_000_000_000_000_000
        )
      );
  }

  _settingValueMatchesType(type, value) {
    if (type === "boolean") return typeof value === "boolean";
    if (type === "integer") return Number.isSafeInteger(value);
    if (type === "number") return typeof value === "number" && Number.isFinite(value);
    if (type === "string" || type === "secret") return typeof value === "string";
    return type === "select" && this._settingScalar(value);
  }

  _messageString(value, maximum = 256, { required = false } = {}) {
    if (value == null && !required) return null;
    if (typeof value !== "string") return null;
    if ((required && !value) || value.length > maximum) return null;
    return value;
  }

  _messageTextIsValid(value) {
    if (typeof value !== "string" || !value) return false;
    try {
      const bytes = new TextEncoder().encode(value);
      if (!bytes.length || bytes.length > 237) return false;
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes) === value;
    } catch (_ignored) {
      return false;
    }
  }

  _messageReactionIsValid(value) {
    if (typeof value !== "string" || Array.from(value).length !== 1) return false;
    try {
      const bytes = new TextEncoder().encode(value);
      return bytes.length >= 1
        && bytes.length <= 4
        && new TextDecoder("utf-8", { fatal: true }).decode(bytes) === value;
    } catch (_ignored) {
      return false;
    }
  }

  _messageMeshPacketId(value) {
    if (value == null) return null;
    if (typeof value !== "string" || !/^meshtastic:[0-9]{1,10}$/.test(value)) return null;
    const packetId = Number.parseInt(value.slice("meshtastic:".length), 10);
    return Number.isSafeInteger(packetId)
      && packetId > 0
      && packetId <= 0xffffffff
      && value === `meshtastic:${packetId}`
      ? value
      : null;
  }

  _sanitizeMessageRecord(record) {
    if (!record || typeof record !== "object" || Array.isArray(record)) return null;
    const messageId = this._messageString(record.message_id, 256, { required: true });
    const meshPacketId = this._messageMeshPacketId(record.mesh_packet_id);
    const protocol = this._messageString(record.protocol, 32, { required: true });
    const gatewayId = this._messageString(record.gateway_id, 128, { required: true });
    const sender = this._messageString(record.sender, 128);
    const receiver = this._messageString(record.receiver, 128);
    const text = record.text;
    const direction = record.direction;
    const timestamp = this._messageString(record.timestamp, 64, { required: true });
    const raw = record.raw == null ? {} : record.raw;
    if (
      !messageId
      || (record.mesh_packet_id != null && meshPacketId == null)
      || !protocol
      || !gatewayId
      || (record.sender != null && sender == null)
      || (record.receiver != null && receiver == null)
      || !this._messageTextIsValid(text)
      || !["rx", "tx"].includes(direction)
      || !timestamp
      || !Number.isFinite(Date.parse(timestamp))
      || !raw
      || typeof raw !== "object"
      || Array.isArray(raw)
    ) return null;

    let channel = null;
    if (record.channel != null) {
      const value = typeof record.channel === "number"
        && Number.isSafeInteger(record.channel)
        ? String(record.channel)
        : record.channel;
      if (typeof value !== "string" || !/^[0-7]$/.test(value)) return null;
      channel = value;
    }
    const messageType = ["broadcast", "direct", "group", "emergency"].includes(
      record.message_type,
    ) ? record.message_type : "broadcast";
    const priority = ["normal", "high", "emergency"].includes(record.priority)
      ? record.priority
      : "normal";
    const encrypted = record.encrypted == null ? null : record.encrypted;
    if (encrypted != null && typeof encrypted !== "boolean") return null;
    const hops = record.hops == null ? null : record.hops;
    if (hops != null && (!Number.isSafeInteger(hops) || hops < 0 || hops > 255)) return null;

    const explicitDelivery = Object.prototype.hasOwnProperty.call(record, "delivery");
    let delivery = explicitDelivery ? record.delivery : null;
    if (delivery != null && !["broadcast", "channel", "direct", "unknown"].includes(delivery)) {
      return null;
    }
    let peerNodeKey = this._messageString(record.peer_node_key, 128);
    if (record.peer_node_key != null && peerNodeKey == null) return null;
    if (!delivery) {
      if (messageType === "direct" && direction === "tx" && receiver) {
        delivery = "direct";
        peerNodeKey = peerNodeKey || receiver;
      } else if (this._messageIsBroadcastReceiver(receiver) && channel != null) {
        delivery = channel === "0" ? "broadcast" : "channel";
      } else {
        delivery = "unknown";
      }
    }
    if (delivery === "direct" && !peerNodeKey) return null;
    if (delivery === "broadcast" && channel !== "0") return null;
    if (delivery === "channel" && (channel == null || channel === "0")) return null;

    const reactionPresent = Object.prototype.hasOwnProperty.call(record, "reaction")
      && record.reaction != null;
    const replyPresent = Object.prototype.hasOwnProperty.call(record, "reply_to_message_id")
      && record.reply_to_message_id != null;
    if (reactionPresent && !replyPresent) return null;
    const reaction = reactionPresent ? record.reaction : null;
    const replyToMessageId = replyPresent
      ? this._messageString(record.reply_to_message_id, 256, { required: true })
      : null;
    if (reactionPresent && (!this._messageReactionIsValid(reaction) || !replyToMessageId)) {
      return null;
    }

    const safeRaw = {};
    if (["blocked", "queued", "sent", "failed"].includes(raw.status)) {
      safeRaw.status = raw.status;
    }
    if (typeof raw.last_error_code === "string" && raw.last_error_code.length <= 64) {
      safeRaw.last_error_code = raw.last_error_code;
    }
    return {
      message_id: messageId,
      mesh_packet_id: meshPacketId,
      protocol,
      gateway_id: gatewayId,
      sender,
      receiver,
      channel,
      text,
      message_type: messageType,
      priority,
      encrypted,
      hops,
      timestamp,
      direction,
      delivery,
      peer_node_key: delivery === "direct" ? peerNodeKey : null,
      reaction,
      reply_to_message_id: replyToMessageId,
      raw: safeRaw,
    };
  }

  _messageIsBroadcastReceiver(value) {
    if (value == null) return false;
    const normalized = String(value).trim().toLowerCase();
    return ["^all", "!ffffffff", "ffffffff", "4294967295"].includes(normalized);
  }

  _validateMessagesResponse(response) {
    if (!Array.isArray(response)) {
      throw { name: "PanelSchemaError", code: "messages_not_array" };
    }
    return response.slice(-500)
      .map((record) => this._sanitizeMessageRecord(record))
      .filter((record) => record != null);
  }

  async _loadMessages(limit = 100, render = true) {
    const boundedLimit = Number.isSafeInteger(limit) && limit >= 1 && limit <= 500
      ? limit
      : 100;
    const generation = ++this._messageRequestGeneration;
    this._messageLoading = true;
    this._messageError = null;
    try {
      const response = await this._withTimeout(
        this._hass.callWS({ type: "meshnet/messages", limit: boundedLimit }),
        15000,
      );
      const messages = this._validateMessagesResponse(response);
      if (generation !== this._messageRequestGeneration) return messages;
      this._messages = messages;
      this._messageLoading = false;
      this._messageError = null;
      this._markOperationSuccess("messages_load");
      if (render && this._activeView === "messages") this._safeRender("render");
      return messages;
    } catch (error) {
      if (generation !== this._messageRequestGeneration) return [];
      this._messageLoading = false;
      this._messageError = "Message history unavailable";
      if (!this._failureWasRecorded(error)) {
        this._recordFailure(
          "messages_load",
          this._safeErrorCode(error) === "timeout" ? "timeout" : "websocket",
          error,
        );
      }
      if (render && this._activeView === "messages") this._safeRender("render");
      return [];
    }
  }

  _messageConversationKey(message) {
    if (!message || typeof message !== "object") return "unknown";
    if (message.delivery === "direct" && typeof message.peer_node_key === "string"
      && message.peer_node_key) {
      return `direct:${message.peer_node_key}`;
    }
    if (message.delivery === "channel" && /^[1-7]$/.test(String(message.channel))) {
      return `channel:${message.channel}`;
    }
    if (message.delivery === "broadcast" && String(message.channel) === "0") {
      return "broadcast:0";
    }
    return "unknown";
  }

  _messageNodeForPeer(peerKey, nodes) {
    const peer = String(peerKey || "");
    const canonicalPeer = this._parseMeshtasticNodeId(peer);
    return nodes.find((node) => {
      if (!node || typeof node !== "object") return false;
      if (String(node.node_key || "") === peer || String(node.node_id || "") === peer) return true;
      const nodeId = this._meshtasticNodeId(node);
      return Boolean(canonicalPeer && nodeId === canonicalPeer);
    }) || null;
  }

  _messagePeerLabel(peerKey, nodes) {
    const node = this._messageNodeForPeer(peerKey, nodes);
    if (!node) return String(peerKey || "Unknown peer");
    const label = this._nodeName(node);
    const nodeId = this._meshtasticNodeId(node) || String(node.node_id || node.node_key || "");
    return nodeId && !label.includes(nodeId) ? `${label} · ${nodeId}` : label;
  }

  _messageConversations(messages, nodes) {
    const safeNodes = Array.isArray(nodes)
      ? nodes.filter((node) => node && typeof node === "object" && !Array.isArray(node))
      : [];
    const groups = new Map([
      ["broadcast:0", {
        key: "broadcast:0",
        kind: "broadcast",
        label: "Broadcast / Primary",
        messages: [],
      }],
    ]);
    (Array.isArray(messages) ? messages : []).forEach((candidate) => {
      const message = this._sanitizeMessageRecord(candidate);
      if (!message) return;
      const key = this._messageConversationKey(message);
      if (!groups.has(key)) {
        let kind = "unknown";
        let label = "Unknown delivery";
        if (key === "broadcast:0") {
          kind = "broadcast";
          label = "Broadcast / Primary";
        } else if (key.startsWith("channel:")) {
          kind = "channel";
          label = `Channel ${key.slice("channel:".length)}`;
        } else if (key.startsWith("direct:")) {
          kind = "direct";
          label = this._messagePeerLabel(key.slice("direct:".length), safeNodes);
        }
        groups.set(key, { key, kind, label, messages: [] });
      }
      groups.get(key).messages.push(message);
    });
    const rank = (conversation) => conversation.kind === "broadcast"
      ? 0
      : conversation.kind === "channel"
        ? 1
        : conversation.kind === "direct"
          ? 2
          : 3;
    return [...groups.values()].sort((left, right) => rank(left) - rank(right)
      || (left.kind === "channel" ? Number(left.key.slice(8)) - Number(right.key.slice(8)) : 0)
      || this._compareText(left.label, right.label)
      || this._compareText(left.key, right.key));
  }

  _selectMessageConversation(key, conversations, syncDraft = false) {
    const selected = conversations.find((conversation) => conversation.key === key)
      || conversations.find((conversation) => conversation.key === "broadcast:0")
      || conversations[0]
      || { key: "broadcast:0", kind: "broadcast", messages: [] };
    this._messageConversation = selected.key;
    if (syncDraft) {
      if (selected.kind === "direct") {
        this._draft.delivery = "direct";
        this._draft.recipient = selected.key.slice("direct:".length);
      } else {
        this._draft.delivery = "broadcast";
        this._draft.recipient = "";
        this._draft.channel = selected.kind === "channel"
          ? selected.key.slice("channel:".length)
          : "0";
      }
    }
    return selected;
  }

  _messageTimestampLabel(value) {
    const timestamp = Date.parse(value);
    return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : "Unknown time";
  }

  _messageTimelineEntries(messages) {
    const safeMessages = (Array.isArray(messages) ? messages : [])
      .map((message) => this._sanitizeMessageRecord(message))
      .filter((message) => message != null);
    const targets = new Map();
    const registerTarget = (identifier, message) => {
      if (!identifier) return;
      if (targets.has(identifier) && targets.get(identifier) !== message) {
        targets.set(identifier, null);
      } else if (!targets.has(identifier)) {
        targets.set(identifier, message);
      }
    };
    safeMessages.forEach((message) => {
      if (message.reaction != null) return;
      registerTarget(message.message_id, message);
      registerTarget(message.mesh_packet_id, message);
    });
    const attached = new Set();
    const reactionsByTarget = new Map();
    safeMessages.forEach((message) => {
      if (message.reaction == null) return;
      const target = targets.get(message.reply_to_message_id);
      if (!target) return;
      attached.add(message);
      if (!reactionsByTarget.has(target)) reactionsByTarget.set(target, new Map());
      const groups = reactionsByTarget.get(target);
      if (!groups.has(message.reaction)) {
        groups.set(message.reaction, {
          reaction: message.reaction,
          count: 0,
          senders: new Set(),
        });
      }
      const group = groups.get(message.reaction);
      group.count += 1;
      group.senders.add(message.sender || "unknown");
    });
    return safeMessages
      .filter((message) => !attached.has(message))
      .map((message) => ({
        message,
        orphan_reaction: message.reaction != null,
        reactions: [...(reactionsByTarget.get(message) || new Map()).values()].map(
          (group) => ({
            reaction: group.reaction,
            count: group.count,
            senders: [...group.senders].sort((left, right) => this._compareText(left, right)),
          }),
        ),
      }));
  }

  _messageReactionBadges(reactions) {
    if (!Array.isArray(reactions) || !reactions.length) return "";
    return `<div class="message-reactions" aria-label="Reactions">${reactions.map((group) => {
      const senders = group.senders.join(", ");
      const description = `${group.reaction} from ${senders}`;
      return `<span class="message-reaction" title="${this._escape(description)}" aria-label="${this._escape(description)}"><span aria-hidden="true">${this._escape(group.reaction)}</span>${group.count > 1 ? ` <span aria-hidden="true">${group.count}</span>` : ""}</span>`;
    }).join("")}</div>`;
  }

  _messageTimelineRow(entry) {
    const message = entry.message;
    const receiver = message.receiver
      || (message.channel == null ? "unknown" : `Channel ${message.channel}`);
    const status = message.raw.status ? ` · ${this._escape(message.raw.status)}` : "";
    const meta = `${this._escape(message.sender || "unknown")} → ${this._escape(receiver)} · ${this._escape(this._messageTimestampLabel(message.timestamp))}${status}`;
    const body = entry.orphan_reaction
      ? `<div class="orphan-reaction"><span class="message-reaction" aria-hidden="true">${this._escape(message.reaction)}</span><span>Reaction to a message that is not available in this conversation or loaded history: <code>${this._escape(message.reply_to_message_id)}</code></span></div>`
      : `<div>${this._escape(message.text)}</div>${this._messageReactionBadges(entry.reactions)}`;
    return `
      <article class="message-row ${message.direction === "tx" ? "tx" : "rx"}${entry.orphan_reaction ? " reaction-orphan" : ""}" data-message-id="${this._escape(message.message_id)}">
        ${body}
        <div class="message-meta">${meta}</div>
      </article>
    `;
  }

  _captureMessageScrollState() {
    if (typeof this.querySelector !== "function") return null;
    const timeline = this.querySelector("#meshnet-message-timeline");
    if (!timeline || typeof timeline.getAttribute !== "function") return null;
    const conversationKey = timeline.getAttribute("data-conversation-key");
    const scrollTop = timeline.scrollTop;
    const scrollHeight = timeline.scrollHeight;
    const clientHeight = timeline.clientHeight;
    if (
      typeof conversationKey !== "string"
      || !conversationKey
      || ![scrollTop, scrollHeight, clientHeight].every(
        (value) => typeof value === "number" && Number.isFinite(value) && value >= 0,
      )
    ) return null;
    const maximumScroll = Math.max(0, scrollHeight - clientHeight);
    return {
      conversation_key: conversationKey,
      scroll_top: Math.min(maximumScroll, scrollTop),
      near_bottom: maximumScroll - scrollTop <= 64,
    };
  }

  _restoreMessageScrollState(state, conversationKey) {
    if (typeof this.querySelector !== "function") return;
    const timeline = this.querySelector("#meshnet-message-timeline");
    if (!timeline) return;
    const scrollHeight = timeline.scrollHeight;
    const clientHeight = timeline.clientHeight;
    if (
      typeof scrollHeight !== "number"
      || !Number.isFinite(scrollHeight)
      || typeof clientHeight !== "number"
      || !Number.isFinite(clientHeight)
    ) return;
    const maximumScroll = Math.max(0, scrollHeight - clientHeight);
    if (
      !state
      || state.conversation_key !== conversationKey
      || state.near_bottom === true
    ) {
      timeline.scrollTop = maximumScroll;
      return;
    }
    const previous = typeof state.scroll_top === "number" && Number.isFinite(state.scroll_top)
      ? state.scroll_top
      : 0;
    timeline.scrollTop = Math.min(maximumScroll, Math.max(0, previous));
  }

  _renderMessages(focusState = null, scrollState = null) {
    const snapshot = this._snapshot || { nodes: {}, gateways: {}, recent_messages: [] };
    const nodes = this._nodesWithExactMeshtasticNameHints(
      Object.values(snapshot.nodes || {}).filter(
        (node) => node && typeof node === "object" && !Array.isArray(node),
      ),
    );
    const gateways = Object.values(snapshot.gateways || {}).filter(
      (gateway) => gateway && typeof gateway === "object" && !Array.isArray(gateway),
    );
    const conversations = this._messageConversations(this._messages, nodes);
    const selected = this._selectMessageConversation(this._messageConversation, conversations);
    const timelineEntries = this._messageTimelineEntries(selected.messages).slice(-200);
    const directDelivery = this._draft.delivery === "direct";
    const recipientCount = this._recipientChoices(nodes)
      .filter((choice) => !choice.ambiguous && !choice.invalidIdentity).length;
    this.innerHTML = `
      <style>
        :host { display: block; min-height: 100vh; color: var(--primary-text-color); background: var(--primary-background-color); font-family: var(--paper-font-body1_-_font-family); }
        .messages-wrap { max-width: 1180px; margin: 0 auto; padding: 16px; box-sizing: border-box; }
        .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        h1 { margin: 0; font-size: 22px; font-weight: 500; }
        .view-tabs { display: flex; gap: 6px; margin: 12px 0; }
        .view-tab, button { border: 1px solid var(--divider-color); border-radius: 7px; padding: 8px 12px; color: var(--primary-text-color); background: var(--card-background-color); cursor: pointer; font: inherit; }
        .view-tab.active, .send-button { border-color: var(--primary-color); color: var(--text-primary-color, #fff); background: var(--primary-color); }
        button:disabled { cursor: default; opacity: 0.55; }
        .messages-grid { display: grid; grid-template-columns: minmax(210px, 290px) minmax(0, 1fr); gap: 12px; }
        .card { border: 1px solid var(--divider-color); border-radius: 9px; background: var(--card-background-color); padding: 12px; min-width: 0; }
        .conversation-select, .composer select, .composer textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--divider-color); border-radius: 6px; padding: 8px; color: var(--primary-text-color); background: var(--card-background-color); font: inherit; }
        .timeline { min-height: 260px; max-height: 52vh; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
        .message-row { max-width: min(82%, 720px); border-radius: 9px; padding: 9px 11px; background: var(--secondary-background-color); overflow-wrap: anywhere; }
        .message-row.tx { align-self: end; background: color-mix(in srgb, var(--primary-color) 18%, var(--card-background-color)); }
        .message-reactions { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
        .message-reaction { display: inline-flex; align-items: center; gap: 3px; border: 1px solid var(--divider-color); border-radius: 999px; padding: 2px 7px; background: var(--card-background-color); font-size: 13px; }
        .orphan-reaction { display: flex; align-items: center; gap: 7px; color: var(--secondary-text-color); font-size: 12px; }
        .orphan-reaction code { overflow-wrap: anywhere; }
        .message-meta, .label, .field-help { color: var(--secondary-text-color); font-size: 11px; }
        .composer { display: grid; gap: 9px; border-top: 1px solid var(--divider-color); padding-top: 12px; }
        .composer label { display: grid; gap: 4px; color: var(--secondary-text-color); font-size: 12px; }
        .composer-controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
        .composer textarea { min-height: 84px; resize: vertical; }
        .send-status { min-height: 18px; font-size: 12px; }
        .good { color: var(--success-color, #168047); } .warn { color: var(--warning-color, #b26a00); } .bad { color: var(--error-color, #d32f2f); }
        @media (max-width: 780px) { .messages-wrap { padding: 10px; } .messages-grid { grid-template-columns: 1fr; } }
      </style>
      <div class="messages-wrap">
        <div class="toolbar">
          <div><h1>MeshNet Messages</h1><div class="label">Cached local message history</div></div>
          <button id="meshnet-messages-reload" type="button"${this._messageLoading ? " disabled" : ""}>${this._messageLoading ? "Loading…" : "Reload history"}</button>
        </div>
        ${this._viewTabs()}
        <div class="messages-grid">
          <aside class="card">
            <label class="label" for="meshnet-message-conversation">Conversation</label>
            <select class="conversation-select" id="meshnet-message-conversation">
              ${conversations.map((conversation) => `<option value="${this._escape(conversation.key)}"${this._selected(selected.key, conversation.key)}>${this._escape(conversation.label)} · ${conversation.messages.length}</option>`).join("")
                || '<option value="broadcast:0">Broadcast / Primary · 0</option>'}
            </select>
            <p class="field-help">Direct threads use exact node identity. Duplicate names never merge conversations.</p>
            ${this._messageError ? `<p class="bad">${this._escape(this._messageError)}</p>` : ""}
          </aside>
          <main class="card">
            <div class="timeline" id="meshnet-message-timeline" data-conversation-key="${this._escape(selected.key)}" aria-live="polite">
              ${timelineEntries.map((entry) => this._messageTimelineRow(entry)).join("") || '<div class="label">No messages in this conversation.</div>'}
            </div>
            <form class="composer" id="meshnet-send-form">
              <div class="composer-controls">
                <label>Delivery
                  <select id="meshnet-delivery">
                    <option value="broadcast"${this._selected(this._draft.delivery, "broadcast")}>Broadcast / channel</option>
                    <option value="direct"${this._selected(this._draft.delivery, "direct")}>Direct</option>
                  </select>
                </label>
                <label>Channel
                  <select id="meshnet-channel">
                    ${Array.from({ length: 8 }, (_item, channel) => `<option value="${channel}"${this._selected(this._draft.channel, channel)}>${channel === 0 ? "Primary (0)" : `Channel ${channel}`}</option>`).join("")}
                  </select>
                </label>
              </div>
              <label>Direct recipient
                <select id="meshnet-recipient"${directDelivery && recipientCount ? " required" : " disabled"}>${this._recipientOptions(nodes)}</select>
              </label>
              <label>Gateway
                <select id="meshnet-gateway">${this._gatewayOptions(gateways)}</select>
              </label>
              <label>Message
                <textarea id="meshnet-message" required placeholder="Type a local mesh message">${this._escape(this._draft.message)}</textarea>
              </label>
              <label>Priority
                <select id="meshnet-priority">
                  <option value="normal"${this._selected(this._draft.priority, "normal")}>Normal</option>
                  <option value="high"${this._selected(this._draft.priority, "high")}>High</option>
                  <option value="emergency"${this._selected(this._draft.priority, "emergency")}>Emergency</option>
                </select>
              </label>
              <button class="send-button" id="meshnet-send-button" type="submit"${this._sending || (directDelivery && !recipientCount) ? " disabled" : ""}>${this._sending ? "Sending…" : "Send"}</button>
              <div class="send-status ${this._statusClass()}" role="status" aria-live="polite">${this._escape(this._sendStatus ? this._sendStatus.text : "")}</div>
            </form>
          </main>
        </div>
      </div>
    `;
    this._safeStep("bind_views", "binding", () => this._bindViewControls());
    this._safeStep("bind_messages", "binding", () => this._bindMessageControls(conversations));
    this._safeStep("bind_composer", "binding", () => this._bindComposer());
    this._safeStep("restore_focus", "focus", () => this._restoreComposerFocus(focusState));
    this._safeStep("restore_focus", "focus", () => this._restoreMessageScrollState(
      scrollState,
      selected.key,
    ));
  }

  _bindMessageControls(conversations) {
    const selector = this.querySelector("#meshnet-message-conversation");
    if (selector) {
      selector.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      selector.addEventListener("change", () => {
        this._selectMessageConversation(selector.value, conversations, true);
        this._safeRender("render");
      });
    }
    const reload = this.querySelector("#meshnet-messages-reload");
    if (reload) reload.addEventListener("click", () => void this._loadMessages(100));
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

  _bindAdvancedControls() {
    const remoteGateway = this.querySelector("#meshnet-remote-gateway");
    const remoteTarget = this.querySelector("#meshnet-remote-target");
    const remoteLoad = this.querySelector("#meshnet-remote-load");
    const remoteForm = this.querySelector("#meshnet-remote-settings-form");
    const remoteConfirm = this.querySelector("#meshnet-remote-confirm");
    const remoteApply = this.querySelector("#meshnet-remote-apply");
    const copyKey = this.querySelector("#meshnet-controller-key-copy");
    const traceGateway = this.querySelector("#meshnet-traceroute-gateway");
    const traceTarget = this.querySelector("#meshnet-traceroute-target");
    const traceStart = this.querySelector("#meshnet-traceroute-start");
    const traceStatusReload = this.querySelector("#meshnet-traceroute-status-reload");
    const traceConfirm = this.querySelector("#meshnet-traceroute-confirm");
    const traceCancel = this.querySelector("#meshnet-traceroute-cancel");
    const neighborGateway = this.querySelector("#meshnet-neighbor-info-gateway");
    const neighborTarget = this.querySelector("#meshnet-neighbor-info-target");
    const neighborStatusLoad = this.querySelector("#meshnet-neighbor-info-status-load");
    const neighborStart = this.querySelector("#meshnet-neighbor-info-start");
    const neighborConfirm = this.querySelector("#meshnet-neighbor-info-confirm");
    const neighborCancel = this.querySelector("#meshnet-neighbor-info-cancel");
    [
      remoteGateway,
      remoteTarget,
      remoteLoad,
      remoteConfirm,
      remoteApply,
      copyKey,
      traceGateway,
      traceTarget,
      traceStart,
      traceStatusReload,
      traceConfirm,
      traceCancel,
      neighborGateway,
      neighborTarget,
      neighborStatusLoad,
      neighborStart,
      neighborConfirm,
      neighborCancel,
    ].filter(Boolean).forEach((control) => {
      control.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
    });
    if (remoteGateway) {
      remoteGateway.addEventListener("change", () => {
        this._resetRemoteSelection(remoteGateway.value, remoteTarget && remoteTarget.value || "");
        this._safeRender("render");
      });
    }
    if (remoteTarget) {
      remoteTarget.addEventListener("change", () => {
        this._resetRemoteSelection(remoteGateway && remoteGateway.value || "", remoteTarget.value);
        this._safeRender("render");
      });
    }
    if (remoteLoad) {
      remoteLoad.addEventListener("click", () => {
        this._safeStep("remote_control_event", "binding", () => {
          void this._loadRemoteSettings(
            remoteGateway && remoteGateway.value || "",
            remoteTarget && remoteTarget.value || "",
          );
        });
      });
    }
    this.querySelectorAll("[data-remote-setting-index]").forEach((input) => {
      input.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      const eventName = input.type === "checkbox" || input.tagName === "SELECT"
        ? "change"
        : "input";
      input.addEventListener(eventName, () => {
        this._safeStep("remote_control_event", "binding", () => {
          this._updateRemoteSettingsDraft(input);
        });
      });
    });
    if (remoteForm) {
      remoteForm.addEventListener("submit", (event) => {
        this._safeStep(
          "remote_control_event",
          "binding",
          () => this._previewRemoteSettings(event),
        );
      });
    }
    if (remoteConfirm) {
      remoteConfirm.addEventListener("change", () => {
        this._remoteSettingsConfirmed = remoteConfirm.checked === true;
        if (remoteApply) {
          remoteApply.disabled = !this._remoteSettingsConfirmed
            || this._remoteSettingsBusy != null;
        }
      });
    }
    if (remoteApply) {
      remoteApply.addEventListener("click", () => {
        this._safeStep(
          "remote_control_event",
          "binding",
          () => this._applyRemoteSettings(),
        );
      });
    }
    if (copyKey) {
      copyKey.addEventListener("click", () => {
        this._safeStep("remote_control_event", "binding", () => {
          void this._copyControllerPublicKey();
        });
      });
    }
    if (traceGateway) {
      traceGateway.addEventListener("change", () => {
        this._tracerouteGatewayId = traceGateway.value;
        this._tracerouteConfirmation = null;
        this._tracerouteStatus = null;
        this._safeRender("render");
      });
    }
    if (traceTarget) {
      traceTarget.addEventListener("change", () => {
        this._tracerouteTargetNode = traceTarget.value;
        this._tracerouteConfirmation = null;
        this._tracerouteStatus = null;
        this._safeRender("render");
      });
    }
    const requestTrace = () => this._requestTraceroute(
      traceGateway && traceGateway.value || this._tracerouteGatewayId,
      traceTarget && traceTarget.value || this._tracerouteTargetNode,
    );
    if (traceStart) {
      traceStart.addEventListener("click", () => {
        this._safeStep("traceroute_event", "binding", () => requestTrace());
      });
    }
    if (traceStatusReload) {
      traceStatusReload.addEventListener("click", () => {
        this._safeStep("traceroute_event", "binding", () => {
          void this._loadTracerouteStatus({ force: true });
        });
      });
    }
    if (traceConfirm) {
      traceConfirm.addEventListener("click", () => {
        this._safeStep("traceroute_event", "binding", () => requestTrace());
      });
    }
    if (traceCancel) {
      traceCancel.addEventListener("click", () => {
        this._tracerouteConfirmation = null;
        this._tracerouteStatus = { kind: "warn", text: "Traceroute cancelled before transmission." };
        this._safeRender("render");
      });
    }
    if (neighborGateway) {
      neighborGateway.addEventListener("change", () => {
        this._neighborInfoGatewayId = neighborGateway.value;
        this._neighborInfoConfirmation = null;
        this._neighborInfoStatus = null;
        this._safeRender("render");
      });
    }
    if (neighborTarget) {
      neighborTarget.addEventListener("change", () => {
        this._resetNeighborInfoTarget(neighborTarget.value);
        this._safeRender("render");
      });
    }
    if (neighborStatusLoad) {
      neighborStatusLoad.addEventListener("click", () => {
        this._safeStep("neighbor_info_event", "binding", () => {
          void this._loadNeighborInfoStatus(
            neighborTarget && neighborTarget.value || this._neighborInfoTargetNode,
          );
        });
      });
    }
    const requestNeighborInfo = () => this._requestNeighborInfo(
      neighborGateway && neighborGateway.value || this._neighborInfoGatewayId,
      neighborTarget && neighborTarget.value || this._neighborInfoTargetNode,
    );
    if (neighborStart) {
      neighborStart.addEventListener("click", () => {
        this._safeStep("neighbor_info_event", "binding", () => requestNeighborInfo());
      });
    }
    if (neighborConfirm) {
      neighborConfirm.addEventListener("click", () => {
        this._safeStep("neighbor_info_event", "binding", () => requestNeighborInfo());
      });
    }
    if (neighborCancel) {
      neighborCancel.addEventListener("click", () => {
        this._neighborInfoConfirmation = null;
        this._neighborInfoStatus = { kind: "warn", text: "NeighborInfo request cancelled before transmission." };
        this._safeRender("render");
      });
    }
    this._maybeLoadTracerouteStatus();
  }

  _resetNeighborInfoTarget(targetNode) {
    this._neighborInfoStatusRequestGeneration += 1;
    this._neighborInfoTargetNode = targetNode;
    this._neighborInfoConfirmation = null;
    this._neighborInfoResult = null;
    this._neighborInfoStatus = null;
    this._neighborInfoStatusData = null;
    this._neighborInfoStatusReady = false;
    this._neighborInfoStatusLoading = false;
    this._neighborInfoStatusTarget = "";
  }

  _resetRemoteSelection(gatewayId, targetNode) {
    this._remoteRequestGeneration += 1;
    this._remoteGatewayId = gatewayId;
    this._remoteTargetNode = targetNode;
    this._remoteSettingsSnapshot = null;
    this._remoteSettingsDraft = {};
    this._remoteSettingsPreview = null;
    this._remoteSettingsConfirmed = false;
    this._remoteSettingsStatus = null;
    this._remoteSettingsBusy = null;
  }

  _updateRemoteSettingsDraft(input) {
    const index = Number.parseInt(input.getAttribute("data-remote-setting-index"), 10);
    const field = this._remoteSettingsFields()[index];
    if (!field || !field.writable || !this._remoteSettingsSnapshot) return;
    const value = this._readRemoteSettingInput(field, input);
    if (this._settingValuesEqual(value, field.value)) {
      delete this._remoteSettingsDraft[field.path];
    } else {
      this._remoteSettingsDraft[field.path] = value;
    }
    this._remoteSettingsPreview = null;
    this._remoteSettingsConfirmed = false;
    this._remoteSettingsStatus = Object.keys(this._remoteSettingsDraft).length
      ? { kind: "warn", text: "Draft changed. Preview it before any remote write." }
      : null;
    const preview = this.querySelector("#meshnet-remote-preview-result");
    if (preview && typeof preview.remove === "function") preview.remove();
    const button = this.querySelector("#meshnet-remote-preview");
    if (button) button.disabled = !Object.keys(this._remoteSettingsDraft).length;
  }

  _readRemoteSettingInput(field, input) {
    if (field.type === "boolean") return input.checked === true;
    if (field.type === "select") {
      const optionIndex = Number.parseInt(input.value, 10);
      return optionIndex >= 0 && optionIndex < field.options.length
        ? field.options[optionIndex].value
        : field.value;
    }
    return input.value;
  }

  async _copyControllerPublicKey() {
    const snapshot = this._remoteSettingsSnapshot;
    const clipboard = typeof navigator !== "undefined" && navigator.clipboard;
    if (!snapshot || !clipboard || typeof clipboard.writeText !== "function") {
      this._remoteSettingsStatus = {
        kind: "warn",
        text: "Clipboard access is unavailable. Select and copy the displayed value manually.",
      };
      this._safeRender("render");
      return;
    }
    try {
      await clipboard.writeText(snapshot.controller.public_key);
      this._remoteSettingsStatus = { kind: "good", text: "Controller public key copied." };
    } catch (error) {
      this._recordFailure("remote_control_event", "lifecycle", error);
      this._remoteSettingsStatus = {
        kind: "warn",
        text: "Clipboard access was denied. Select and copy the displayed value manually.",
      };
    }
    this._safeRender("render");
  }

  _remoteSettingsChanges() {
    const fields = new Map(
      this._remoteSettingsFields().map((field) => [field.path, field]),
    );
    const draftPaths = Object.keys(this._remoteSettingsDraft);
    if (!draftPaths.length || draftPaths.length > 32) {
      throw { name: "ValidationError", code: "invalid_format" };
    }
    const changes = {};
    draftPaths.forEach((path) => {
      const field = fields.get(path);
      if (!field || !field.writable) {
        throw { name: "ValidationError", code: "invalid_format" };
      }
      const value = this._coerceRemoteSettingValue(
        field,
        this._remoteSettingsDraft[path],
      );
      if (!this._settingValuesEqual(value, field.value)) changes[path] = value;
    });
    if (!Object.keys(changes).length) {
      throw { name: "ValidationError", code: "invalid_format" };
    }
    return changes;
  }

  _coerceRemoteSettingValue(field, value) {
    if (field.type === "boolean") {
      if (typeof value !== "boolean") throw { name: "ValidationError", code: "invalid_format" };
      return value;
    }
    if (field.type === "integer" || field.type === "number") {
      if (!["string", "number"].includes(typeof value) || typeof value === "boolean") {
        throw { name: "ValidationError", code: "invalid_format" };
      }
      const text = typeof value === "string" ? value.trim() : value;
      const number = Number(text);
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
    if (field.type !== "string" || typeof value !== "string") {
      throw { name: "ValidationError", code: "invalid_format" };
    }
    const byteLength = new TextEncoder().encode(value).length;
    if (
      byteLength < 1
      || byteLength > field.max_length
      || [...value].some((character) => character.codePointAt(0) < 32)
    ) throw { name: "ValidationError", code: "invalid_format" };
    return value;
  }

  async _loadRemoteSettings(gatewayId, targetNode) {
    if (
      this._remoteSettingsBusy != null
      || !this._hass
      || typeof this._hass.callWS !== "function"
    ) return;
    if (!this._validOperatorGatewayId(gatewayId) || !this._isExactRemoteTarget(targetNode)) {
      this._remoteSettingsStatus = { kind: "bad", text: "Choose one exact gateway and target node." };
      this._safeRender("render");
      return;
    }
    const generation = ++this._remoteRequestGeneration;
    this._remoteGatewayId = gatewayId;
    this._remoteTargetNode = targetNode;
    this._remoteSettingsSnapshot = null;
    this._remoteSettingsDraft = {};
    this._remoteSettingsPreview = null;
    this._remoteSettingsConfirmed = false;
    this._remoteSettingsBusy = "get";
    this._remoteSettingsStatus = { kind: "warn", text: "Loading remote settings with one read-only request…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/remote_settings/get",
        gateway_id: gatewayId,
        target_node: targetNode,
      }), 65000);
      if (generation !== this._remoteRequestGeneration) return;
      this._remoteSettingsSnapshot = this._sanitizeRemoteSettingsSnapshot(
        response,
        gatewayId,
        targetNode,
      );
      this._remoteSettingsStatus = {
        kind: "good",
        text: "Remote settings loaded. Edit returned fields, then preview.",
      };
      this._markOperationSuccess("remote_settings_get");
    } catch (error) {
      if (generation !== this._remoteRequestGeneration) return;
      this._remoteSettingsSnapshot = null;
      this._remoteSettingsDraft = {};
      this._remoteSettingsPreview = null;
      this._remoteSettingsConfirmed = false;
      this._recordFailure(
        "remote_settings_get",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._remoteSettingsStatus = {
        kind: "bad",
        text: this._remoteAdminErrorText(error, "get"),
      };
    } finally {
      if (generation === this._remoteRequestGeneration) {
        this._remoteSettingsBusy = null;
        this._safeRender("render");
      }
    }
  }

  async _previewRemoteSettings(event = null) {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    const snapshot = this._remoteSettingsSnapshot;
    if (
      this._remoteSettingsBusy != null
      || !snapshot
      || !this._hass
      || typeof this._hass.callWS !== "function"
    ) return;
    let changes;
    try {
      changes = this._remoteSettingsChanges();
    } catch (error) {
      this._remoteSettingsDraft = {};
      this._remoteSettingsPreview = null;
      this._remoteSettingsConfirmed = false;
      this._recordFailure("remote_settings_preview", "validation", error);
      this._remoteSettingsStatus = { kind: "bad", text: "The remote settings draft was invalid and has been cleared." };
      this._safeRender("render");
      return;
    }
    const generation = this._remoteRequestGeneration;
    this._remoteSettingsBusy = "preview";
    this._remoteSettingsPreview = null;
    this._remoteSettingsConfirmed = false;
    this._remoteSettingsStatus = { kind: "warn", text: "Preparing a value-free remote write preview…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/remote_settings/preview",
        gateway_id: snapshot.gateway_id,
        target_node: snapshot.target_node,
        revision: snapshot.revision,
        changes,
      }), 65000);
      if (
        generation !== this._remoteRequestGeneration
        || this._remoteSettingsSnapshot !== snapshot
      ) return;
      this._remoteSettingsPreview = this._validateRemoteSettingsPreview(
        response,
        snapshot,
        changes,
      );
      this._remoteSettingsStatus = {
        kind: "good",
        text: "Preview ready. Review it and explicitly confirm the one remote write.",
      };
      this._markOperationSuccess("remote_settings_preview");
    } catch (error) {
      if (
        generation !== this._remoteRequestGeneration
        || this._remoteSettingsSnapshot !== snapshot
      ) return;
      this._remoteSettingsDraft = {};
      this._remoteSettingsPreview = null;
      this._remoteSettingsConfirmed = false;
      this._recordFailure(
        "remote_settings_preview",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._remoteSettingsStatus = {
        kind: "bad",
        text: this._remoteAdminErrorText(error, "preview"),
      };
    } finally {
      if (
        generation === this._remoteRequestGeneration
        && this._remoteSettingsSnapshot === snapshot
      ) {
        this._remoteSettingsBusy = null;
        this._safeRender("render");
      }
    }
  }

  async _applyRemoteSettings() {
    const snapshot = this._remoteSettingsSnapshot;
    const preview = this._remoteSettingsPreview;
    if (this._remoteSettingsBusy != null || !snapshot || !preview) return;
    if (this._remoteSettingsConfirmed !== true) {
      this._remoteSettingsStatus = {
        kind: "bad",
        text: "Confirm the one remote radio write before applying it.",
      };
      this._safeRender("render");
      return;
    }
    const generation = this._remoteRequestGeneration;
    this._remoteSettingsBusy = "apply";
    this._remoteSettingsStatus = { kind: "warn", text: "Applying once and verifying by readback…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/remote_settings/apply",
        gateway_id: snapshot.gateway_id,
        target_node: snapshot.target_node,
        revision: snapshot.revision,
        preview_id: preview.preview_id,
        confirm_remote: true,
      }), 130000);
      if (
        generation !== this._remoteRequestGeneration
        || this._remoteSettingsSnapshot !== snapshot
        || this._remoteSettingsPreview !== preview
      ) return;
      const applied = this._validateRemoteSettingsApply(response, snapshot, preview);
      this._remoteSettingsDraft = {};
      this._remoteSettingsPreview = null;
      this._remoteSettingsConfirmed = false;
      this._remoteSettingsStatus = applied.status === "verified"
        ? { kind: "good", text: "Remote settings applied and verified." }
        : { kind: "warn", text: "Remote write completed, but readback did not verify every setting. Inspect the node locally before another write." };
      this._markOperationSuccess("remote_settings_apply");
    } catch (error) {
      if (
        generation !== this._remoteRequestGeneration
        || this._remoteSettingsSnapshot !== snapshot
        || this._remoteSettingsPreview !== preview
      ) return;
      this._remoteSettingsDraft = {};
      this._remoteSettingsPreview = null;
      this._remoteSettingsConfirmed = false;
      this._recordFailure(
        "remote_settings_apply",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._remoteSettingsStatus = {
        kind: "warn",
        text: this._remoteAdminErrorText(error, "apply"),
      };
    } finally {
      if (generation === this._remoteRequestGeneration) {
        this._remoteSettingsBusy = null;
        this._safeRender("render");
      }
    }
  }

  _validOperatorGatewayId(value) {
    return typeof value === "string"
      && value.length >= 1
      && value.length <= 128
      && value === value.trim();
  }

  _operatorErrorCode(error) {
    try {
      return error && typeof error.code === "string" ? error.code : "";
    } catch (_ignored) {
      return "";
    }
  }

  _remoteAdminErrorText(error, phase = "get") {
    const fixed = {
      remote_admin_gateway_not_found: "The selected gateway is unavailable. Refresh the panel and choose a connected gateway.",
      remote_admin_requires_bluetooth: "Choose a connected Meshtastic Bluetooth gateway; remote administration never falls back to another transport.",
      remote_admin_target_invalid: "Choose one exact Meshtastic node ID. Names and broadcast destinations are not accepted.",
      remote_admin_target_unknown: "The target is not in the controller radio’s node database. Wait for NodeInfo, then load again.",
      remote_admin_target_public_key_unavailable: "The target public key is unavailable. Wait for verified NodeInfo before testing access again.",
      remote_admin_controller_public_key_unavailable: "The controller public key is unavailable. Inspect the local radio with the official app.",
      remote_admin_controller_unauthorized: "The target has not authorized this controller public key. Add the displayed key locally on the target, then load again.",
      remote_admin_session_rejected: "The remote administration session was rejected or expired. Load settings again; no write was retried.",
      remote_admin_no_route: "No mesh route to the target is available. Check passive last-seen evidence and try later.",
      remote_admin_no_response: "The target did not respond. Check passive last-seen evidence and try later.",
      remote_admin_duty_cycle_limited: "The radio refused more traffic because of duty-cycle limits. Wait before another explicit request.",
      remote_admin_rate_limited: "Remote administration is rate-limited. Wait before another explicit request.",
      remote_admin_command_forbidden: "That remote operation is outside MeshNet’s reviewed settings allowlist. Use the official app locally.",
      remote_admin_snapshot_invalid: "The remote settings response failed validation. No fields were retained; inspect the target locally.",
      remote_admin_revision_conflict: "Remote settings changed after loading. Reload the target before preparing another preview.",
      remote_admin_changes_invalid: "The remote settings draft was rejected and cleared. Reload live values before editing again.",
      remote_admin_preview_expired: "The remote preview expired or was already consumed. Reload the target before another write.",
      remote_admin_confirmation_required: "Explicitly confirm the exact remote radio write before applying it.",
      remote_admin_unknown_outcome: "The remote write could not be verified. Do not repeat it blindly; reload and inspect the node first.",
      remote_admin_unavailable: "Remote administration is unavailable. Confirm the Bluetooth gateway is connected and supported.",
      unauthorized: "Administrator access is required for remote node administration.",
    };
    const mapped = fixed[this._operatorErrorCode(error)];
    if (mapped) return mapped;
    if (phase === "preview") {
      return "The preview failed validation or could not be prepared. The draft has been cleared.";
    }
    if (phase === "apply") {
      return "The remote write outcome is unknown. Do not repeat it blindly; reload and inspect the node first.";
    }
    return "Remote settings could not be loaded. Verify the gateway and target, then try one explicit load.";
  }

  _tracerouteErrorText(error, phase = "request") {
    const fixed = {
      traceroute_gateway_not_found: "The selected traceroute gateway is unavailable. Refresh and choose a connected gateway.",
      traceroute_requires_bluetooth: "Choose a connected Meshtastic Bluetooth gateway for traceroute.",
      traceroute_gateway_disconnected: "The selected traceroute gateway is disconnected. Reconnect it before trying again.",
      traceroute_target_invalid: "Choose one exact Meshtastic node. Names and broadcast destinations are not accepted.",
      traceroute_target_unknown: "The destination is not in the current node database. Wait for NodeInfo before trying again.",
      traceroute_target_self: "Choose a remote node; a gateway cannot traceroute itself.",
      traceroute_cooldown: "The global traceroute cooldown is active. Wait and reload persisted status before another attempt.",
      traceroute_rate_limited: "Traceroute is rate-limited. Wait and reload persisted status before another attempt.",
      traceroute_duty_cycle_limited: "The radio refused more traffic because of duty-cycle limits. Wait before another attempt.",
      traceroute_no_route: "No route was found. The global cooldown may still be active; do not retry blindly.",
      traceroute_no_response: "The destination did not respond. The global cooldown may still be active; do not retry blindly.",
      traceroute_timeout: "Traceroute timed out. RF may have been sent and the global cooldown is active; do not retry blindly.",
      traceroute_invalid_response: "The traceroute response failed validation. RF may have been sent; reload persisted status.",
      traceroute_failed: "Traceroute did not complete. RF may have been sent and the global cooldown may be active; do not retry blindly.",
      traceroute_status_failed: "Persisted traceroute status could not be verified. RF control remains locked; reload status before trying again.",
      unauthorized: "Administrator access is required to view traceroute status or send a traceroute.",
    };
    const mapped = fixed[this._operatorErrorCode(error)];
    if (mapped) return mapped;
    return phase === "status"
      ? "Persisted traceroute status could not be verified. RF control remains locked; reload status before trying again."
      : "Traceroute did not return a verified result. RF may have been sent and the global cooldown is active; do not retry blindly.";
  }

  _remoteAllowedSettingPaths() {
    return new Set([
      "owner.long_name",
      "owner.short_name",
      "config.display.compass_north_top",
      "config.display.compass_orientation",
      "config.display.enable_message_bubbles",
      "config.display.flip_screen",
      "config.display.gps_format",
      "config.display.heading_bold",
      "config.display.units",
      "config.display.use_12h_clock",
      "config.display.use_long_node_name",
      "config.display.wake_on_tap_or_motion",
    ]);
  }

  _sanitizeRemoteSettingsSnapshot(response, gatewayId, targetNode) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || response.schema_version !== 1
      || response.gateway_id !== gatewayId
      || response.target_node !== targetNode
      || typeof response.revision !== "string"
      || !/^[0-9a-f]{64}$/.test(response.revision)
      || !response.controller
      || typeof response.controller !== "object"
      || Array.isArray(response.controller)
      || response.controller.public_key_copy_only !== true
      || !response.target
      || typeof response.target !== "object"
      || Array.isArray(response.target)
      || !Array.isArray(response.categories)
      || response.categories.length > 8
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const controllerNode = this._requiredRemoteNodeId(response.controller.node_id);
    const targetId = this._requiredRemoteNodeId(response.target.node_id);
    if (targetId !== targetNode) throw { name: "PanelSchemaError", code: "invalid_format" };
    const allowedCategories = new Set(["owner", "config.display"]);
    const seenCategories = new Set();
    const seenPaths = new Set();
    let fieldCount = 0;
    const categories = response.categories.map((category) => {
      if (
        !category
        || typeof category !== "object"
        || Array.isArray(category)
        || !Array.isArray(category.fields)
        || category.fields.length > 32
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      const key = this._requiredSettingPath(category.key);
      if (!allowedCategories.has(key) || seenCategories.has(key)) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      seenCategories.add(key);
      const fields = category.fields.map((field) => {
        fieldCount += 1;
        if (fieldCount > 32) throw { name: "PanelSchemaError", code: "invalid_format" };
        const sanitized = this._sanitizeRemoteSettingsField(field, key);
        if (seenPaths.has(sanitized.path)) {
          throw { name: "PanelSchemaError", code: "invalid_format" };
        }
        seenPaths.add(sanitized.path);
        return sanitized;
      });
      if (!fields.length) throw { name: "PanelSchemaError", code: "invalid_format" };
      return {
        key,
        label: this._requiredSettingText(category.label, 96),
        fields,
      };
    });
    if (!categories.length || !fieldCount) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return {
      schema_version: 1,
      gateway_id: gatewayId,
      target_node: targetNode,
      revision: response.revision,
      controller: {
        node_id: controllerNode,
        short_name: this._optionalSettingText(response.controller.short_name, 64),
        public_key: this._sanitizeControllerPublicKey(response.controller.public_key),
        public_key_copy_only: true,
      },
      target: {
        node_id: targetId,
        long_name: this._optionalSettingText(response.target.long_name, 96),
        short_name: this._optionalSettingText(response.target.short_name, 64),
        public_key_available: response.target.public_key_available === true,
        remote_admin_eligible: response.target.remote_admin_eligible !== false,
      },
      categories,
    };
  }

  _requiredRemoteNodeId(value) {
    if (!this._isExactRemoteTarget(value)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return value;
  }

  _sanitizeControllerPublicKey(value) {
    if (typeof value !== "string" || !/^base64:[A-Za-z0-9+/]{43}=$/.test(value)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    try {
      const decoder = typeof atob === "function" ? atob : null;
      if (!decoder || decoder(value.slice(7)).length !== 32) {
        throw new Error("invalid");
      }
    } catch (_ignored) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return value;
  }

  _sanitizeRemoteSettingsField(field, categoryKey) {
    if (!field || typeof field !== "object" || Array.isArray(field)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const path = this._requiredSettingPath(field.path);
    const allowed = this._remoteAllowedSettingPaths();
    if (
      !allowed.has(path)
      || (categoryKey === "owner" && !path.startsWith("owner."))
      || (categoryKey === "config.display" && !path.startsWith("config.display."))
      || field.writable !== true
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const type = ["boolean", "integer", "number", "select", "string"].includes(field.type)
      ? field.type
      : null;
    if (!type) throw { name: "PanelSchemaError", code: "invalid_format" };
    const numeric = (name) => {
      if (field[name] == null) return null;
      return typeof field[name] === "number" && Number.isFinite(field[name])
        ? field[name]
        : NaN;
    };
    const min = numeric("min");
    const max = numeric("max");
    const step = numeric("step");
    if (
      [min, max, step].some(Number.isNaN)
      || (min != null && max != null && min > max)
      || (step != null && step <= 0)
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const options = type === "select"
      ? this._sanitizeRemoteOptions(field.options)
      : [];
    const maxLength = path === "owner.short_name" ? 4 : path === "owner.long_name" ? 40 : 128;
    const sanitized = {
      path,
      label: this._requiredSettingText(field.label, 96),
      type,
      value: field.value,
      writable: true,
      options,
      min,
      max,
      step,
      max_length: maxLength,
    };
    try {
      const validatedValue = this._coerceRemoteSettingValue(sanitized, field.value);
      if (!this._settingValuesEqual(validatedValue, field.value)) {
        throw new Error("coercion mismatch");
      }
    } catch (_ignored) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return sanitized;
  }

  _sanitizeRemoteOptions(value) {
    if (!Array.isArray(value) || !value.length || value.length > 64) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const seen = new Set();
    return value.map((option) => {
      if (!option || typeof option !== "object" || Array.isArray(option)) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      const optionValue = option.value;
      if (
        !["string", "number"].includes(typeof optionValue)
        || typeof optionValue === "boolean"
        || (typeof optionValue === "number" && !Number.isSafeInteger(optionValue))
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      const identity = `${typeof optionValue}:${String(optionValue)}`;
      if (seen.has(identity)) throw { name: "PanelSchemaError", code: "invalid_format" };
      seen.add(identity);
      return {
        value: optionValue,
        label: this._requiredSettingText(option.label, 96),
      };
    });
  }

  _validateRemoteSettingsPreview(response, snapshot, changes) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || response.schema_version !== 1
      || response.gateway_id !== snapshot.gateway_id
      || response.target_node !== snapshot.target_node
      || response.revision !== snapshot.revision
      || typeof response.preview_id !== "string"
      || response.preview_id.length < 32
      || response.preview_id.length > 128
      || response.requires_confirmation !== true
      || !Array.isArray(response.changes)
      || !response.changes.length
      || response.changes.length > 32
      || typeof response.expires_at !== "string"
      || !Number.isFinite(Date.parse(response.expires_at))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const fields = new Map(
      this._remoteSettingsFields(snapshot).map((field) => [field.path, field]),
    );
    const expected = Object.keys(changes);
    const seen = new Set();
    const previewChanges = response.changes.map((change) => {
      if (!change || typeof change !== "object" || Array.isArray(change)) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      const path = this._requiredSettingPath(change.path);
      const field = fields.get(path);
      if (!field || !Object.hasOwn(changes, path) || seen.has(path) || change.label !== field.label) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      seen.add(path);
      return { path, label: field.label };
    });
    if (seen.size !== expected.length || expected.some((path) => !seen.has(path))) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return {
      preview_id: response.preview_id,
      gateway_id: snapshot.gateway_id,
      target_node: snapshot.target_node,
      revision: snapshot.revision,
      changes: previewChanges,
      requires_confirmation: true,
      expires_at: response.expires_at,
    };
  }

  _validateRemoteSettingsApply(response, snapshot, preview) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || response.schema_version !== 1
      || !["verified", "readback_mismatch"].includes(response.status)
      || response.gateway_id !== snapshot.gateway_id
      || response.target_node !== snapshot.target_node
      || !Array.isArray(response.verified)
      || !Array.isArray(response.unverified)
      || response.verified.length > 32
      || response.unverified.length > 32
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const expected = preview.changes.map((change) => change.path);
    const verified = response.verified.map((path) => this._requiredSettingPath(path));
    const unverified = response.unverified.map((path) => this._requiredSettingPath(path));
    const combined = [...verified, ...unverified];
    if (
      new Set(combined).size !== combined.length
      || combined.length !== expected.length
      || expected.some((path) => !combined.includes(path))
      || (response.status === "verified" && (!verified.length || unverified.length))
      || (response.status === "readback_mismatch" && !unverified.length)
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    return { status: response.status, verified, unverified };
  }

  _maybeLoadTracerouteStatus() {
    if (
      !this._connected
      || this._activeView !== "mesh"
      || this._tracerouteStatusAttempted
      || this._tracerouteStatusLoading
      || !this._hass
      || typeof this._hass.callWS !== "function"
    ) return;
    void this._loadTracerouteStatus();
  }

  async _loadTracerouteStatus({ force = false } = {}) {
    if (
      this._tracerouteStatusLoading
      || this._tracerouteBusy
      || !this._hass
      || typeof this._hass.callWS !== "function"
      || (!force && this._tracerouteStatusAttempted)
    ) return;
    const generation = ++this._tracerouteStatusRequestGeneration;
    this._tracerouteStatusAttempted = true;
    this._tracerouteStatusLoading = true;
    this._tracerouteStatusReady = false;
    this._tracerouteConfirmation = null;
    this._tracerouteStatus = { kind: "warn", text: "Checking the persisted global traceroute cooldown…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/traceroute/status",
      }), 15000);
      if (generation !== this._tracerouteStatusRequestGeneration) return;
      const status = this._sanitizeTracerouteStatus(response);
      this._tracerouteGlobalStatus = status;
      this._tracerouteStatusReady = true;
      if (status.result) {
        this._tracerouteResults[status.target_node] = status.result;
      }
      this._tracerouteStatus = this._tracerouteCooldownActive("", "")
        ? {
          kind: "warn",
          text: `Global cooldown active until ${this._timestampDisplay(status.next_allowed_at)}.`,
        }
        : { kind: "good", text: "Persisted traceroute status loaded. No global cooldown is active." };
      this._markOperationSuccess("traceroute_status");
    } catch (error) {
      if (generation !== this._tracerouteStatusRequestGeneration) return;
      this._tracerouteGlobalStatus = null;
      this._tracerouteStatusReady = false;
      this._recordFailure(
        "traceroute_status",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._tracerouteStatus = {
        kind: "bad",
        text: this._tracerouteErrorText(error, "status"),
      };
    } finally {
      if (generation === this._tracerouteStatusRequestGeneration) {
        this._tracerouteStatusLoading = false;
        this._safeRender("render");
      }
    }
  }

  _sanitizeTracerouteStatus(response) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || response.schema_version !== 1
      || response.scope !== "integration"
      || !["available", "cooldown"].includes(response.status)
      || typeof response.reserved !== "boolean"
      || !Number.isSafeInteger(response.remaining_seconds)
      || response.remaining_seconds < 0
      || response.remaining_seconds > 86400
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const gatewayId = response.gateway_id == null ? null : response.gateway_id;
    const targetNode = response.target_node == null ? null : response.target_node;
    if (
      (gatewayId == null) !== (targetNode == null)
      || (gatewayId != null && !this._validOperatorGatewayId(gatewayId))
      || (targetNode != null && !this._isExactTracerouteTarget(targetNode))
      || (response.status === "cooldown" && (
        response.reserved !== true
        || response.remaining_seconds < 1
        || gatewayId == null
      ))
      || (response.status === "available" && (
        response.reserved !== false
        || response.remaining_seconds !== 0
      ))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const timestamp = (value, required = false) => {
      if (value == null && !required) return null;
      return this._validatedTimestamp(value);
    };
    const reservedAt = timestamp(response.reserved_at, response.status === "cooldown");
    const nextAllowedAt = timestamp(response.next_allowed_at, response.status === "cooldown");
    const resultUpdatedAt = timestamp(response.result_updated_at);
    let result = null;
    if (response.result != null) {
      if (gatewayId == null || targetNode == null || resultUpdatedAt == null) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      result = this._sanitizeTracerouteResult(response.result, gatewayId, targetNode, {
        persisted: true,
      });
      result.next_allowed_at = nextAllowedAt;
    }
    return {
      schema_version: 1,
      status: response.status,
      reserved: response.reserved,
      gateway_id: gatewayId,
      target_node: targetNode,
      reserved_at: reservedAt,
      next_allowed_at: nextAllowedAt,
      remaining_seconds: response.remaining_seconds,
      result_updated_at: resultUpdatedAt,
      result,
      loaded_at_ms: Date.now(),
    };
  }

  async _requestTraceroute(gatewayId, targetNode) {
    if (this._tracerouteBusy) return;
    if (!this._validOperatorGatewayId(gatewayId) || !this._isExactTracerouteTarget(targetNode)) {
      this._tracerouteConfirmation = null;
      this._tracerouteStatus = { kind: "bad", text: "Choose one exact gateway and Meshtastic destination." };
      this._safeRender("render");
      return;
    }
    this._tracerouteGatewayId = gatewayId;
    this._tracerouteTargetNode = targetNode;
    if (!this._tracerouteStatusReady) {
      this._tracerouteConfirmation = null;
      this._tracerouteStatus = {
        kind: "bad",
        text: "Load the persisted traceroute status before enabling RF control.",
      };
      this._safeRender("render");
      return;
    }
    if (this._tracerouteCooldownActive(gatewayId, targetNode)) {
      const nextAllowedAt = this._tracerouteGlobalStatus
        && this._tracerouteGlobalStatus.next_allowed_at;
      this._tracerouteConfirmation = null;
      this._tracerouteStatus = {
        kind: "warn",
        text: nextAllowedAt
          ? `Global cooldown active until ${this._timestampDisplay(nextAllowedAt)}.`
          : "Global cooldown is active. Reload persisted status before another traceroute.",
      };
      this._safeRender("render");
      return;
    }
    if (
      !this._tracerouteConfirmation
      || this._tracerouteConfirmation.gateway_id !== gatewayId
      || this._tracerouteConfirmation.target_node !== targetNode
    ) {
      this._tracerouteConfirmation = { gateway_id: gatewayId, target_node: targetNode };
      this._tracerouteStatus = {
        kind: "warn",
        text: "Review the RF notice, then confirm this one traceroute.",
      };
      this._safeRender("render");
      return;
    }
    if (!this._hass || typeof this._hass.callWS !== "function") return;
    const generation = ++this._tracerouteRequestGeneration;
    this._tracerouteConfirmation = null;
    this._tracerouteBusy = true;
    this._tracerouteStatus = { kind: "warn", text: "One traceroute submitted; waiting for the correlated result…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/traceroute",
        gateway_id: gatewayId,
        target_node: targetNode,
      }), 130000);
      if (generation !== this._tracerouteRequestGeneration) return;
      const result = this._sanitizeTracerouteResult(response, gatewayId, targetNode);
      this._tracerouteResults[targetNode] = result;
      const nextAllowedAt = result.next_allowed_at
        || new Date(Date.now() + 60 * 1000).toISOString();
      this._tracerouteGlobalStatus = {
        schema_version: 1,
        status: "cooldown",
        reserved: true,
        gateway_id: gatewayId,
        target_node: targetNode,
        reserved_at: result.completed_at || new Date(Date.now()).toISOString(),
        next_allowed_at: nextAllowedAt,
        remaining_seconds: 60,
        result_updated_at: result.completed_at,
        result,
        loaded_at_ms: Date.now(),
      };
      this._tracerouteStatusReady = true;
      this._tracerouteStatus = result.next_allowed_at
        ? { kind: "good", text: `Traceroute complete. Next permitted attempt: ${this._timestampDisplay(result.next_allowed_at)}.` }
        : { kind: "good", text: "Traceroute complete. The server still enforces the one-minute cooldown." };
      this._markOperationSuccess("traceroute_request");
    } catch (error) {
      if (generation !== this._tracerouteRequestGeneration) return;
      this._recordFailure(
        "traceroute_request",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      const failedAt = Date.now();
      this._tracerouteGlobalStatus = {
        schema_version: 1,
        status: "cooldown",
        reserved: true,
        gateway_id: gatewayId,
        target_node: targetNode,
        reserved_at: new Date(failedAt).toISOString(),
        next_allowed_at: new Date(failedAt + 60 * 1000).toISOString(),
        remaining_seconds: 60,
        result_updated_at: null,
        result: null,
        loaded_at_ms: failedAt,
      };
      this._tracerouteStatusReady = true;
      this._tracerouteStatus = {
        kind: "warn",
        text: this._tracerouteErrorText(error, "request"),
      };
    } finally {
      if (generation === this._tracerouteRequestGeneration) {
        this._tracerouteBusy = false;
        this._safeRender("render");
      }
    }
  }

  _sanitizeTracerouteResult(response, gatewayId, targetNode, { persisted = false } = {}) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || response.schema_version !== 1
      || response.gateway_id !== gatewayId
      || response.destination !== targetNode
      || (!persisted && response.status !== "complete")
      || (!persisted && (
        typeof response.correlation_id !== "string"
        || response.correlation_id.length < 1
        || response.correlation_id.length > 128
      ))
      || (persisted && (
        Object.hasOwn(response, "status")
        || Object.hasOwn(response, "correlation_id")
      ))
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const route = (value) => {
      if (value == null) return [];
      if (
        !Array.isArray(value)
        || value.length > 64
        || value.some((hop) => !this._isExactTracerouteTarget(hop))
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      return value.slice();
    };
    const forwardRoute = route(response.forward_route);
    const reverseRoute = route(response.reverse_route);
    if (response.source != null && !this._isExactTracerouteTarget(response.source)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    if (
      response.channel != null
      && (!Number.isSafeInteger(response.channel) || response.channel < 0 || response.channel > 7)
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const completedAt = response.completed_at == null
      ? null
      : this._validatedTimestamp(response.completed_at);
    const nextAllowedAt = response.next_allowed_at == null
      ? null
      : this._validatedTimestamp(response.next_allowed_at);
    const snr = (value) => {
      if (value == null) return [];
      if (
        !Array.isArray(value)
        || value.length > 64
        || value.some((item) => (
          typeof item !== "number"
          || !Number.isFinite(item)
          || item < -128
          || item > 128
        ))
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      return value.slice();
    };
    return {
      schema_version: 1,
      status: "complete",
      gateway_id: gatewayId,
      destination: targetNode,
      correlation_id: persisted ? null : response.correlation_id,
      source: response.source || null,
      channel: response.channel == null ? null : response.channel,
      completed_at: completedAt,
      forward_route: forwardRoute,
      reverse_route: reverseRoute,
      snr_towards: snr(response.snr_towards),
      snr_back: snr(response.snr_back),
      next_allowed_at: nextAllowedAt,
    };
  }

  async _loadNeighborInfoStatus(targetNode) {
    if (
      this._neighborInfoStatusLoading
      || this._neighborInfoBusy
      || !this._isExactTracerouteTarget(targetNode)
      || !this._hass
      || typeof this._hass.callWS !== "function"
    ) return;
    const generation = ++this._neighborInfoStatusRequestGeneration;
    this._neighborInfoTargetNode = targetNode;
    this._neighborInfoStatusTarget = targetNode;
    this._neighborInfoStatusLoading = true;
    this._neighborInfoStatusReady = false;
    this._neighborInfoStatusData = null;
    this._neighborInfoConfirmation = null;
    this._neighborInfoStatus = { kind: "warn", text: "Checking persisted NeighborInfo cooldowns…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/neighbor_info/status",
        target_node: targetNode,
      }), 15000);
      if (generation !== this._neighborInfoStatusRequestGeneration) return;
      const status = this._sanitizeNeighborInfoStatus(response, targetNode);
      this._neighborInfoStatusData = status;
      this._neighborInfoStatusReady = true;
      this._neighborInfoStatusTarget = targetNode;
      this._neighborInfoResult = status.result;
      this._neighborInfoStatus = this._neighborInfoCooldownActive()
        ? { kind: "warn", text: `NeighborInfo cooldown active for ${status.remaining_seconds} more seconds.` }
        : { kind: "good", text: "Persisted NeighborInfo status loaded. One explicit request may be prepared." };
      this._markOperationSuccess("neighbor_info_status");
    } catch (error) {
      if (generation !== this._neighborInfoStatusRequestGeneration) return;
      this._neighborInfoStatusData = null;
      this._neighborInfoStatusReady = false;
      this._neighborInfoResult = null;
      this._recordFailure(
        "neighbor_info_status",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      this._neighborInfoStatus = {
        kind: "bad",
        text: this._neighborInfoErrorText(error, "status"),
      };
    } finally {
      if (generation === this._neighborInfoStatusRequestGeneration) {
        this._neighborInfoStatusLoading = false;
        this._safeRender("render");
      }
    }
  }

  _sanitizeNeighborInfoStatus(response, targetNode) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || response.schema_version !== 1
      || response.scope !== "integration_and_target"
      || response.target_node !== targetNode
      || !this._isExactTracerouteTarget(response.target_node)
      || !["available", "cooldown"].includes(response.status)
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const seconds = (value) => {
      if (!Number.isSafeInteger(value) || value < 0 || value > 86400) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      return value;
    };
    const globalRemaining = seconds(response.global_remaining_seconds);
    const targetRemaining = seconds(response.target_remaining_seconds);
    const remaining = seconds(response.remaining_seconds);
    if (
      remaining !== Math.max(globalRemaining, targetRemaining)
      || (response.status === "available" && remaining !== 0)
      || (response.status === "cooldown" && remaining < 1)
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const timestamp = (value, required = false) => {
      if (value == null && !required) return null;
      return this._validatedTimestamp(value);
    };
    const gatewayId = response.gateway_id == null ? null : response.gateway_id;
    if (gatewayId != null && !this._validOperatorGatewayId(gatewayId)) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    const nextAllowedAt = timestamp(response.next_allowed_at, remaining > 0);
    const reservedAt = timestamp(response.reserved_at);
    const resultUpdatedAt = timestamp(response.result_updated_at);
    let result = null;
    if (response.result != null) {
      const resultGateway = response.result && response.result.gateway_id;
      if (!this._validOperatorGatewayId(resultGateway) || !resultUpdatedAt) {
        throw { name: "PanelSchemaError", code: "invalid_format" };
      }
      result = this._sanitizeNeighborInfoResult(response.result, resultGateway, targetNode, {
        persisted: true,
        nextAllowedAt,
      });
    }
    return {
      schema_version: 1,
      scope: "integration_and_target",
      target_node: targetNode,
      status: response.status,
      global_remaining_seconds: globalRemaining,
      target_remaining_seconds: targetRemaining,
      remaining_seconds: remaining,
      next_allowed_at: nextAllowedAt,
      gateway_id: gatewayId,
      reserved_at: reservedAt,
      result_updated_at: resultUpdatedAt,
      result,
      loaded_at_ms: Date.now(),
    };
  }

  _sanitizeNeighborInfoResult(
    response,
    gatewayId,
    targetNode,
    { persisted = false, nextAllowedAt = null } = {},
  ) {
    if (
      !response
      || typeof response !== "object"
      || Array.isArray(response)
      || response.schema_version !== 1
      || response.gateway_id !== gatewayId
      || response.source !== targetNode
      || !this._isExactTracerouteTarget(response.destination)
      || response.channel !== 0
      || !Number.isSafeInteger(response.node_broadcast_interval_secs)
      || response.node_broadcast_interval_secs < 0
      || response.node_broadcast_interval_secs > 31536000
      || !Array.isArray(response.neighbors)
      || response.neighbors.length > 10
    ) throw { name: "PanelSchemaError", code: "invalid_format" };
    const seen = new Set();
    const neighbors = response.neighbors.map((neighbor) => {
      if (
        !neighbor
        || typeof neighbor !== "object"
        || Array.isArray(neighbor)
        || !this._isExactTracerouteTarget(neighbor.node_id)
        || seen.has(neighbor.node_id)
        || typeof neighbor.snr !== "number"
        || !Number.isFinite(neighbor.snr)
        || neighbor.snr < -128
        || neighbor.snr > 128
      ) throw { name: "PanelSchemaError", code: "invalid_format" };
      seen.add(neighbor.node_id);
      return { node_id: neighbor.node_id, snr: neighbor.snr };
    });
    const completedAt = this._validatedTimestamp(response.completed_at);
    const allowedAt = persisted
      ? nextAllowedAt
      : this._validatedTimestamp(response.next_allowed_at);
    return {
      schema_version: 1,
      gateway_id: gatewayId,
      source: targetNode,
      destination: response.destination,
      channel: 0,
      node_broadcast_interval_secs: response.node_broadcast_interval_secs,
      neighbors,
      completed_at: completedAt,
      next_allowed_at: allowedAt,
    };
  }

  _neighborInfoCooldownActive() {
    const status = this._neighborInfoStatusData;
    if (!this._neighborInfoStatusReady || !status || status.status !== "cooldown") return false;
    const absolute = Date.parse(status.next_allowed_at);
    const relative = Number.isFinite(status.loaded_at_ms)
      ? status.loaded_at_ms + status.remaining_seconds * 1000
      : Number.NaN;
    return (Number.isFinite(absolute) && absolute > Date.now())
      || (Number.isFinite(relative) && relative > Date.now());
  }

  async _requestNeighborInfo(gatewayId, targetNode) {
    if (this._neighborInfoBusy) return;
    if (!this._validOperatorGatewayId(gatewayId) || !this._isExactTracerouteTarget(targetNode)) {
      this._neighborInfoConfirmation = null;
      this._neighborInfoStatus = { kind: "bad", text: "Choose one exact Bluetooth gateway and Meshtastic node." };
      this._safeRender("render");
      return;
    }
    this._neighborInfoGatewayId = gatewayId;
    this._neighborInfoTargetNode = targetNode;
    if (!this._neighborInfoStatusReady || this._neighborInfoStatusTarget !== targetNode) {
      this._neighborInfoConfirmation = null;
      this._neighborInfoStatus = { kind: "bad", text: "Load persisted NeighborInfo status for this exact target before enabling RF control." };
      this._safeRender("render");
      return;
    }
    if (this._neighborInfoCooldownActive()) {
      this._neighborInfoConfirmation = null;
      this._neighborInfoStatus = { kind: "warn", text: "A global or same-target NeighborInfo cooldown is active. Wait and reload persisted status." };
      this._safeRender("render");
      return;
    }
    if (
      !this._neighborInfoConfirmation
      || this._neighborInfoConfirmation.gateway_id !== gatewayId
      || this._neighborInfoConfirmation.target_node !== targetNode
    ) {
      this._neighborInfoConfirmation = { gateway_id: gatewayId, target_node: targetNode };
      this._neighborInfoStatus = { kind: "warn", text: "Review the experimental RF notice, then confirm this one NeighborInfo request." };
      this._safeRender("render");
      return;
    }
    if (!this._hass || typeof this._hass.callWS !== "function") return;
    const generation = ++this._neighborInfoRequestGeneration;
    this._neighborInfoConfirmation = null;
    this._neighborInfoBusy = true;
    this._neighborInfoResult = null;
    this._neighborInfoStatus = { kind: "warn", text: "One NeighborInfo request submitted; waiting for a validated response…" };
    this._safeRender("render");
    try {
      const response = await this._withTimeout(this._hass.callWS({
        type: "meshnet/neighbor_info",
        gateway_id: gatewayId,
        target_node: targetNode,
      }), 130000);
      if (generation !== this._neighborInfoRequestGeneration) return;
      const result = this._sanitizeNeighborInfoResult(response, gatewayId, targetNode);
      const now = Date.now();
      this._neighborInfoResult = result;
      this._neighborInfoStatusData = {
        schema_version: 1,
        scope: "integration_and_target",
        target_node: targetNode,
        status: "cooldown",
        global_remaining_seconds: 180,
        target_remaining_seconds: 180,
        remaining_seconds: 180,
        next_allowed_at: result.next_allowed_at,
        gateway_id: gatewayId,
        reserved_at: result.completed_at,
        result_updated_at: result.completed_at,
        result,
        loaded_at_ms: now,
      };
      this._neighborInfoStatusReady = true;
      this._neighborInfoStatusTarget = targetNode;
      this._neighborInfoStatus = { kind: "good", text: "NeighborInfo response validated. Cooldowns are active; do not request again yet." };
      this._markOperationSuccess("neighbor_info_request");
    } catch (error) {
      if (generation !== this._neighborInfoRequestGeneration) return;
      this._recordFailure(
        "neighbor_info_request",
        this._safeErrorCode(error) === "timeout" ? "timeout" : this._safeErrorType(error) === "PanelSchemaError" ? "schema" : "websocket",
        error,
      );
      const now = Date.now();
      this._neighborInfoResult = null;
      this._neighborInfoStatusData = {
        schema_version: 1,
        scope: "integration_and_target",
        target_node: targetNode,
        status: "cooldown",
        global_remaining_seconds: 180,
        target_remaining_seconds: 180,
        remaining_seconds: 180,
        next_allowed_at: new Date(now + 180000).toISOString(),
        gateway_id: gatewayId,
        reserved_at: new Date(now).toISOString(),
        result_updated_at: null,
        result: null,
        loaded_at_ms: now,
      };
      this._neighborInfoStatusReady = true;
      this._neighborInfoStatusTarget = targetNode;
      this._neighborInfoStatus = { kind: "warn", text: this._neighborInfoErrorText(error, "request") };
    } finally {
      if (generation === this._neighborInfoRequestGeneration) {
        this._neighborInfoBusy = false;
        this._safeRender("render");
      }
    }
  }

  _neighborInfoErrorText(error, phase = "request") {
    const fixed = {
      neighbor_info_status_failed: "Persisted NeighborInfo cooldowns could not be verified. RF control remains locked.",
      neighbor_info_failed: "NeighborInfo did not return a validated response. RF may have been sent; do not retry until persisted status is reloaded after the cooldown.",
      unauthorized: "Administrator access is required for NeighborInfo controls.",
    };
    const mapped = fixed[this._operatorErrorCode(error)];
    if (mapped) return mapped;
    return phase === "status"
      ? "Persisted NeighborInfo cooldowns could not be verified. RF control remains locked."
      : "NeighborInfo failed or timed out. RF may have been sent; do not retry blindly.";
  }

  _validatedTimestamp(value) {
    if (typeof value !== "string" || value.length > 64 || !Number.isFinite(Date.parse(value))) {
      throw { name: "PanelSchemaError", code: "invalid_format" };
    }
    return value;
  }

  _timestampDisplay(value) {
    const timestamp = Date.parse(value);
    return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : "unknown time";
  }

  _tracerouteResultFor(gatewayId, targetNode) {
    const result = this._tracerouteResults[targetNode];
    return result && result.gateway_id === gatewayId ? result : null;
  }

  _tracerouteCooldownActive(gatewayId, targetNode) {
    const global = this._tracerouteGlobalStatus;
    if (global) {
      if (global.status === "cooldown" && global.reserved === true) {
        const deadline = Date.parse(global.next_allowed_at);
        const relativeDeadline = Number.isFinite(global.loaded_at_ms)
          ? global.loaded_at_ms + global.remaining_seconds * 1000
          : Number.NaN;
        if (
          (Number.isFinite(deadline) && deadline > Date.now())
          || (Number.isFinite(relativeDeadline) && relativeDeadline > Date.now())
        ) return true;
      }
      // A successfully loaded integration-wide "available" record is more
      // authoritative than a result timestamp interpreted on the browser clock.
      return false;
    }
    return Object.values(this._tracerouteResults).some((result) => (
      result
      && result.next_allowed_at
      && Date.parse(result.next_allowed_at) > Date.now()
    ));
  }

  _isMeshCardId(value) {
    return typeof value === "string" && MESH_CARD_IDS.includes(value);
  }

  _orderedMeshCards(availableIds) {
    if (!Array.isArray(availableIds)) return [];
    const available = new Set(
      availableIds.filter((cardId) => this._isMeshCardId(cardId)),
    );
    return this._meshCardOrder.filter((cardId) => available.has(cardId));
  }

  _moveMeshCard(cardId, direction) {
    if (!this._isMeshCardId(cardId) || !["earlier", "later"].includes(direction)) {
      return false;
    }
    const index = this._meshCardOrder.indexOf(cardId);
    const nextIndex = direction === "earlier" ? index - 1 : index + 1;
    if (index < 0 || nextIndex < 0 || nextIndex >= this._meshCardOrder.length) {
      return false;
    }
    const next = [...this._meshCardOrder];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    this._meshCardOrder = next;
    this._applyMeshCardOrder();
    return true;
  }

  _moveMeshCardTo(cardId, targetId, after = false) {
    if (
      !this._isMeshCardId(cardId)
      || !this._isMeshCardId(targetId)
      || cardId === targetId
      || typeof after !== "boolean"
    ) return false;
    const next = this._meshCardOrder.filter((candidate) => candidate !== cardId);
    const targetIndex = next.indexOf(targetId);
    if (targetIndex < 0) return false;
    next.splice(targetIndex + (after ? 1 : 0), 0, cardId);
    if (next.length !== MESH_CARD_IDS.length || new Set(next).size !== next.length) {
      return false;
    }
    this._meshCardOrder = next;
    this._applyMeshCardOrder();
    return true;
  }

  _setMeshCardSize(cardId, width, height) {
    if (
      !this._isMeshCardId(cardId)
      || !Number.isSafeInteger(width)
      || !Number.isSafeInteger(height)
      || width <= 0
      || height <= 0
    ) return false;
    const size = {
      width: Math.min(MESH_CARD_MAX_WIDTH, Math.max(MESH_CARD_MIN_WIDTH, width)),
      height: Math.min(MESH_CARD_MAX_HEIGHT, Math.max(MESH_CARD_MIN_HEIGHT, height)),
    };
    this._meshCardSizes.set(cardId, size);
    this._applyMeshCardSize(cardId);
    return true;
  }

  _applyMeshCardSize(cardId) {
    if (!this._isMeshCardId(cardId) || typeof this.querySelector !== "function") return;
    const card = this.querySelector(`[data-mesh-card="${cardId}"]`);
    if (!card || !card.style) return;
    const size = this._meshCardSizes.get(cardId);
    card.style.width = size ? `${size.width}px` : "";
    card.style.height = size ? `${size.height}px` : "";
  }

  _applyMeshCardOrder() {
    if (typeof this.querySelector !== "function") return;
    const container = this.querySelector("#meshnet-mesh-cards");
    if (!container || typeof container.querySelectorAll !== "function") return;
    const cards = [...container.querySelectorAll("[data-mesh-card]")].filter(
      (card) => card && this._isMeshCardId(card.getAttribute("data-mesh-card")),
    );
    const byId = new Map(cards.map(
      (card) => [card.getAttribute("data-mesh-card"), card],
    ));
    for (const cardId of this._orderedMeshCards([...byId.keys()])) {
      const card = byId.get(cardId);
      if (card && typeof container.appendChild === "function") container.appendChild(card);
      this._applyMeshCardSize(cardId);
    }
  }

  _resetMeshCardLayout() {
    this._meshCardOrder = [...MESH_CARD_IDS];
    this._meshCardSizes.clear();
    this._applyMeshCardOrder();
  }

  _meshCardControls(cardId) {
    const label = cardId.replaceAll("-", " ");
    return `
      <div class="mesh-card-layout-bar" data-mesh-card-controls="${cardId}" aria-label="${this._escape(label)} card layout controls">
        <button type="button" data-mesh-card-move="earlier" aria-label="Move ${this._escape(label)} card earlier" title="Move earlier">↑</button>
        <button type="button" class="mesh-card-drag-handle" data-mesh-drag-handle="${cardId}" draggable="true" aria-label="Drag ${this._escape(label)} card" title="Drag to move">⠿</button>
        <button type="button" data-mesh-card-move="later" aria-label="Move ${this._escape(label)} card later" title="Move later">↓</button>
      </div>
      <button type="button" class="mesh-card-resize-handle" data-mesh-resize-handle="${cardId}" aria-label="Resize ${this._escape(label)} card" title="Drag to resize; arrow keys resize">↘</button>
    `;
  }

  _captureMeshCardScrollState() {
    const state = new Map();
    if (typeof this.querySelectorAll !== "function") return state;
    this.querySelectorAll("[data-mesh-card]").forEach((card) => {
      const cardId = card && typeof card.getAttribute === "function"
        ? card.getAttribute("data-mesh-card")
        : null;
      if (!this._isMeshCardId(cardId)) return;
      const top = card.scrollTop;
      const left = card.scrollLeft;
      if (Number.isFinite(top) && top >= 0 && Number.isFinite(left) && left >= 0) {
        state.set(cardId, { top, left });
      }
    });
    return state;
  }

  _restoreMeshCardScrollState(state) {
    if (!(state instanceof Map) || typeof this.querySelector !== "function") return;
    state.forEach((position, cardId) => {
      if (!this._isMeshCardId(cardId) || !position) return;
      const card = this.querySelector(`[data-mesh-card="${cardId}"]`);
      if (!card) return;
      card.scrollTop = position.top;
      card.scrollLeft = position.left;
    });
  }

  _cancelMeshLayoutInteraction() {
    const cleanup = this._meshLayoutCleanup;
    this._meshLayoutCleanup = null;
    if (typeof cleanup === "function") {
      try {
        cleanup();
      } catch (_ignored) {
        // A disappearing pointer target has no remaining layout ownership.
      }
    }
    this._meshLayoutInteraction = null;
    if (typeof this.querySelectorAll === "function") {
      this.querySelectorAll(".mesh-card-dragging").forEach(
        (card) => card.classList && card.classList.remove("mesh-card-dragging"),
      );
    }
  }

  _finishMeshLayoutInteraction() {
    this._cancelMeshLayoutInteraction();
    this._queuePendingPollRender();
  }

  _startMeshCardResize(event, card, handle, cardId) {
    if (
      !event
      || (event.button != null && event.button !== 0)
      || !card
      || !handle
      || !this._isMeshCardId(cardId)
      || typeof card.getBoundingClientRect !== "function"
    ) return;
    const startX = event.clientX;
    const startY = event.clientY;
    const rect = card.getBoundingClientRect();
    if (
      ![startX, startY, rect.width, rect.height].every(
        (value) => typeof value === "number" && Number.isFinite(value),
      )
    ) return;
    if (typeof event.preventDefault === "function") event.preventDefault();
    if (typeof event.stopPropagation === "function") event.stopPropagation();
    this._cancelMeshLayoutInteraction();
    const pointerId = event.pointerId;
    this._meshLayoutInteraction = { kind: "resize", card_id: cardId };
    if (typeof handle.setPointerCapture === "function" && pointerId != null) {
      try {
        handle.setPointerCapture(pointerId);
      } catch (_ignored) {
        // Pointer capture is an enhancement; bound handle listeners still work.
      }
    }
    const move = (moveEvent) => {
      if (
        !this._meshLayoutInteraction
        || this._meshLayoutInteraction.card_id !== cardId
        || (pointerId != null && moveEvent.pointerId != null && moveEvent.pointerId !== pointerId)
      ) return;
      const width = Math.round(rect.width + moveEvent.clientX - startX);
      const height = Math.round(rect.height + moveEvent.clientY - startY);
      this._setMeshCardSize(cardId, width, height);
    };
    const finish = (finishEvent) => {
      if (
        !this._meshLayoutInteraction
        || this._meshLayoutInteraction.kind !== "resize"
        || this._meshLayoutInteraction.card_id !== cardId
      ) return;
      if (pointerId != null && finishEvent.pointerId != null && finishEvent.pointerId !== pointerId) return;
      this._finishMeshLayoutInteraction();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
    handle.addEventListener("lostpointercapture", finish);
    const windowTarget = typeof window !== "undefined"
      && typeof window.addEventListener === "function"
      && typeof window.removeEventListener === "function"
      ? window
      : null;
    if (windowTarget) {
      // Pointer capture is not guaranteed (notably through nested shadow roots).
      // Window fallbacks keep resize functional and prevent refresh polling
      // from remaining locked when capture is unavailable or lost.
      windowTarget.addEventListener("pointermove", move);
      windowTarget.addEventListener("pointerup", finish);
      windowTarget.addEventListener("pointercancel", finish);
    }
    this._meshLayoutCleanup = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      handle.removeEventListener("lostpointercapture", finish);
      if (windowTarget) {
        windowTarget.removeEventListener("pointermove", move);
        windowTarget.removeEventListener("pointerup", finish);
        windowTarget.removeEventListener("pointercancel", finish);
      }
      if (typeof handle.releasePointerCapture === "function" && pointerId != null) {
        try {
          handle.releasePointerCapture(pointerId);
        } catch (_ignored) {
          // Capture may already have ended with pointerup/cancel.
        }
      }
    };
  }

  _resizeMeshCardFromKeyboard(event, card, cardId) {
    if (
      !event
      || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)
      || !card
      || typeof card.getBoundingClientRect !== "function"
      || !this._isMeshCardId(cardId)
    ) return;
    const rect = card.getBoundingClientRect();
    const step = event.shiftKey ? 100 : 32;
    const widthDelta = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
    const heightDelta = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
    if (typeof event.preventDefault === "function") event.preventDefault();
    this._setMeshCardSize(
      cardId,
      Math.round(rect.width + widthDelta),
      Math.round(rect.height + heightDelta),
    );
  }

  _bindMeshCardLayout(scrollState = new Map()) {
    this._applyMeshCardOrder();
    if (typeof this.querySelector !== "function") return;
    const container = this.querySelector("#meshnet-mesh-cards");
    if (!container || typeof container.querySelectorAll !== "function") return;
    const cards = [...container.querySelectorAll("[data-mesh-card]")];
    cards.forEach((card) => {
      const cardId = card.getAttribute("data-mesh-card");
      if (!this._isMeshCardId(cardId)) return;
      if (typeof card.insertAdjacentHTML === "function") {
        card.insertAdjacentHTML("afterbegin", this._meshCardControls(cardId));
      }
      card.querySelectorAll(
        "[data-mesh-drag-handle], [data-mesh-resize-handle], [data-mesh-card-move]",
      ).forEach((control) => {
        control.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      });
      card.addEventListener("dragover", (event) => {
        if (!this._meshLayoutInteraction || this._meshLayoutInteraction.kind !== "drag") return;
        event.preventDefault();
        const rect = card.getBoundingClientRect();
        card.dataset.meshDropAfter = String(event.clientY >= rect.top + rect.height / 2);
      });
      card.addEventListener("drop", (event) => {
        if (!this._meshLayoutInteraction || this._meshLayoutInteraction.kind !== "drag") return;
        event.preventDefault();
        const draggedId = this._meshLayoutInteraction.card_id;
        const after = card.dataset.meshDropAfter === "true";
        this._moveMeshCardTo(draggedId, cardId, after);
        this._finishMeshLayoutInteraction();
      });
      const drag = card.querySelector("[data-mesh-drag-handle]");
      if (drag) {
        drag.addEventListener("dragstart", (event) => {
          this._cancelMeshLayoutInteraction();
          this._meshLayoutInteraction = { kind: "drag", card_id: cardId };
          card.classList.add("mesh-card-dragging");
          if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", cardId);
          }
        });
        drag.addEventListener("dragend", () => this._finishMeshLayoutInteraction());
      }
      card.querySelectorAll("[data-mesh-card-move]").forEach((button) => {
        button.addEventListener("click", () => {
          const direction = button.getAttribute("data-mesh-card-move");
          this._moveMeshCard(cardId, direction);
          if (typeof button.focus === "function") button.focus();
        });
      });
      const resize = card.querySelector("[data-mesh-resize-handle]");
      if (resize) {
        resize.addEventListener("pointerdown", (event) => {
          this._startMeshCardResize(event, card, resize, cardId);
        });
        resize.addEventListener("keydown", (event) => {
          this._resizeMeshCardFromKeyboard(event, card, cardId);
        });
      }
    });
    const reset = this.querySelector("#meshnet-layout-reset");
    if (reset) {
      reset.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      reset.addEventListener("click", () => this._resetMeshCardLayout());
    }
    this._restoreMeshCardScrollState(scrollState);
  }

  _openNeighborInfoForNode(nodeKey) {
    const requested = String(nodeKey || "");
    const snapshot = this._snapshot;
    const nodes = snapshot && snapshot.nodes && typeof snapshot.nodes === "object"
      ? Object.values(snapshot.nodes)
      : [];
    const candidates = this._remoteNodeCandidates(nodes);
    const matches = candidates.filter((node) => node.node_key === requested);
    if (matches.length !== 1) return false;
    const gateways = snapshot && snapshot.gateways && typeof snapshot.gateways === "object"
      ? Object.values(snapshot.gateways)
      : [];
    const compatibleGateways = this._remoteGatewayCandidates(gateways);
    if (!compatibleGateways.length) return false;
    const selectedGateway = compatibleGateways.some(
      (gateway) => gateway.gateway_id === this._neighborInfoGatewayId,
    ) ? this._neighborInfoGatewayId : compatibleGateways[0].gateway_id;
    const nodeId = this._meshtasticNodeId(matches[0]);
    const targetNode = `meshtastic:${nodeId}`;
    if (requested !== targetNode || !this._isExactTracerouteTarget(targetNode)) return false;

    this._neighborInfoGatewayId = selectedGateway;
    this._resetNeighborInfoTarget(targetNode);
    this._neighborInfoStatus = {
      kind: "warn",
      text: "NeighborInfo target selected. Load persisted status before RF controls are enabled.",
    };
    this._safeRender("render");
    const reveal = () => {
      const panel = this.querySelector("#meshnet-neighbor-info-panel");
      if (panel && typeof panel.scrollIntoView === "function") {
        panel.scrollIntoView({ block: "start" });
      }
      const statusLoad = this.querySelector("#meshnet-neighbor-info-status-load");
      if (statusLoad && typeof statusLoad.focus === "function") statusLoad.focus();
    };
    if (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(reveal);
    } else {
      reveal();
    }
    return true;
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
    this.querySelectorAll("[data-neighbor-info-node]").forEach((button) => {
      button.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      button.addEventListener("click", () => {
        this._safeStep("node_neighbor_info_event", "binding", () => {
          if (!this._openNeighborInfoForNode(button.getAttribute("data-neighbor-info-node"))) {
            this._recordFailure(
              "neighbor_info_shortcut_invalid",
              "validation",
              { name: "ValidationError", code: "target_unavailable" },
            );
          }
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
    const choices = this._sortNodes(Array.isArray(nodes) ? nodes : [], "favorites_recent")
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
    choices.forEach((choice) => {
      const favorite = choice.node && choice.node.favorite === true ? "★ " : "";
      const lastSeen = this._humanLastSeen(choice.node && choice.node.last_heard);
      choice.label = `${favorite}${choice.label} · ${lastSeen}`;
    });
    return choices
      .map(({ node: _node, ...choice }) => choice);
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
      <section class="panel" data-mesh-card="panel-diagnostics">
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

  _neighborIdentifiers(node, nowMs = Date.now()) {
    if (!node || String(node.protocol || "").trim().toLowerCase() !== "meshtastic") return null;
    if (this._meshtasticIdentityInvalid(node)) return null;
    const routing = node.routing && typeof node.routing === "object" && !Array.isArray(node.routing)
      ? node.routing
      : null;
    if (!routing || routing.neighbors_via_mqtt !== false) return null;
    const provenance = routing.neighbors_provenance == null
      ? "passive"
      : ["passive", "manual_request"].includes(routing.neighbors_provenance)
        ? routing.neighbors_provenance
        : null;
    if (!provenance) return null;
    if (!Array.isArray(routing.neighbors) || routing.neighbors.length > 10) return null;
    if (!Number.isSafeInteger(routing.neighbor_count)
      || routing.neighbor_count < 0
      || routing.neighbor_count > 10
      || routing.neighbor_count !== routing.neighbors.length) {
      return null;
    }
    const observedAt = typeof routing.neighbors_updated_at === "string"
      && routing.neighbors_updated_at.length <= 64
      ? this._timestampMs(routing.neighbors_updated_at)
      : null;
    // NeighborInfo is intentionally short-lived graph evidence. One hour
    // bounds stale topology, while five minutes tolerates HA/browser skew.
    const maximumAgeMs = 60 * 60 * 1000;
    const maximumFutureSkewMs = 5 * 60 * 1000;
    if (observedAt == null
      || !Number.isFinite(nowMs)
      || nowMs - observedAt > maximumAgeMs
      || observedAt - nowMs > maximumFutureSkewMs) {
      return null;
    }
    const source = this._meshtasticNodeId(node);
    if (!source || String(node.node_id || "") !== source) return null;
    const neighbors = [...new Set(routing.neighbors.filter((value) => {
      if (typeof value !== "string" || value.length > 9) return false;
      const canonical = this._parseMeshtasticNodeId(value);
      return Boolean(canonical && value === canonical);
    }))];
    return { source, neighbors, provenance };
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

  _normalizeGraphLimit(value) {
    if (typeof value === "string" && !["20", "50", "100"].includes(value)) return 50;
    const parsed = typeof value === "string" ? Number.parseInt(value, 10) : value;
    return [20, 50, 100].includes(parsed) ? parsed : 50;
  }

  _recentGraphNodes(nodes, limit = this._graphLimit) {
    const boundedLimit = this._normalizeGraphLimit(limit);
    return (Array.isArray(nodes) ? nodes : [])
      .filter((node) => node && node.node_key != null && String(node.node_key))
      .map((node, index) => ({ node, index }))
      .sort((left, right) => this._compareLastSeen(left.node, right.node)
        || this._compareText(left.node.node_key, right.node.node_key)
        || left.index - right.index)
      .slice(0, boundedLimit)
      .map((item) => item.node);
  }

  _graphLocation(value, { zeroPairIsMissing = false } = {}) {
    const source = value && typeof value === "object" ? value : null;
    if (source && Object.prototype.hasOwnProperty.call(source, "precision_bits")) {
      const precisionBits = source.precision_bits;
      // Meshtastic documents 19 bits as roughly 45 m precision. Coarser
      // privacy-obfuscated positions are useful on a map, but not as physical
      // spring lengths because they can imply a route geometry that is not real.
      if (!Number.isSafeInteger(precisionBits) || precisionBits < 19 || precisionBits > 32) {
        return null;
      }
    }
    const latitude = this._validCoordinate(source && source.latitude, -90, 90);
    const longitude = this._validCoordinate(source && source.longitude, -180, 180);
    if (latitude == null || longitude == null) return null;
    if (zeroPairIsMissing && latitude === 0 && longitude === 0) return null;
    return { latitude, longitude };
  }

  _nodeGraphLocation(node) {
    return this._graphLocation(node && node.location, {
      zeroPairIsMissing: String(node && node.protocol || "").trim().toLowerCase() === "meshtastic",
    });
  }

  _gatewayGraphLocation(gateway, nodes) {
    const direct = this._graphLocation(gateway && gateway.location, {
      zeroPairIsMissing: true,
    });
    if (direct) return { ...direct, source: "radio_gps", label: "Radio GPS" };

    const localNodeId = gateway && gateway.local_node_id != null
      ? String(gateway.local_node_id)
      : "";
    if (localNodeId) {
      const canonicalLocalId = this._parseMeshtasticNodeId(localNodeId);
      const localNode = (Array.isArray(nodes) ? nodes : []).find((node) => {
        if (!node || typeof node !== "object") return false;
        if (String(node.node_key || "") === localNodeId || String(node.node_id || "") === localNodeId) {
          return true;
        }
        return Boolean(canonicalLocalId && this._meshtasticNodeId(node) === canonicalLocalId);
      });
      const radio = this._nodeGraphLocation(localNode);
      if (radio) return { ...radio, source: "radio_gps", label: "Radio GPS" };
    }

    const fallback = this._graphLocation(this._hass && this._hass.config, {
      zeroPairIsMissing: true,
    });
    return fallback ? {
      ...fallback,
      source: "home_assistant_fallback",
      label: "Home Assistant location fallback",
    } : null;
  }

  _haversineMeters(left, right) {
    const a = this._graphLocation(left);
    const b = this._graphLocation(right);
    if (!a || !b) return null;
    const radians = (degrees) => degrees * Math.PI / 180;
    const latitudeDelta = radians(b.latitude - a.latitude);
    const longitudeDelta = radians(b.longitude - a.longitude);
    const latitudeA = radians(a.latitude);
    const latitudeB = radians(b.latitude);
    const haversine = Math.sin(latitudeDelta / 2) ** 2
      + Math.cos(latitudeA) * Math.cos(latitudeB) * Math.sin(longitudeDelta / 2) ** 2;
    const bounded = Math.min(1, Math.max(0, haversine));
    const distance = 6371008.8 * 2 * Math.atan2(Math.sqrt(bounded), Math.sqrt(1 - bounded));
    return Number.isFinite(distance) ? distance : null;
  }

  _edgeTargetLength(distanceMeters) {
    const neutral = 140;
    if (typeof distanceMeters !== "number" || !Number.isFinite(distanceMeters) || distanceMeters < 0) {
      return neutral;
    }
    const compressed = 70 + 42 * Math.log10(1 + distanceMeters / 100);
    return Math.min(320, Math.max(70, compressed));
  }

  _formatEdgeDistanceMiles(distanceMeters) {
    if (
      typeof distanceMeters !== "number"
      || !Number.isFinite(distanceMeters)
      || distanceMeters < 0
    ) return null;
    const miles = distanceMeters / 1609.344;
    return `${miles < 10 ? miles.toFixed(1) : Math.round(miles)} mi`;
  }

  _passiveTopology(nodes, gateways, limit) {
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
      if (route) {
        for (let index = 1; index < route.length; index += 1) {
          const left = this._resolveRouteIdentifier(allAliases, route[index - 1]);
          const right = this._resolveRouteIdentifier(allAliases, route[index]);
          if (left && right && left !== right) {
            evidenceNodes.add(left);
            evidenceNodes.add(right);
          }
        }
      }
      const neighborEvidence = this._neighborIdentifiers(node);
      if (!neighborEvidence) return;
      const source = this._resolveRouteIdentifier(allAliases, neighborEvidence.source);
      neighborEvidence.neighbors.forEach((neighbor) => {
        const target = this._resolveRouteIdentifier(allAliases, neighbor);
        if (source && target && source !== target) {
          evidenceNodes.add(source);
          evidenceNodes.add(target);
        }
      });
    });

    const explicitLimit = arguments.length >= 3;
    const orderedNodes = explicitLimit
      ? this._recentGraphNodes(allNodes, limit)
      : this._sortNodes(allNodes, "favorites_recent").sort((left, right) => {
        const evidenceDifference = Number(evidenceNodes.has(String(right.node_key)))
          - Number(evidenceNodes.has(String(left.node_key)));
        return evidenceDifference || this._compareNodes(left, right, "favorites_recent");
      });
    // Calls from older extensions omitted the limit and retain the historical
    // 36-node projection. The built-in panel always supplies its 20/50/100
    // selector value.
    const visibleNodes = explicitLimit ? orderedNodes : orderedNodes.slice(0, 36);
    const orderedGateways = [...allGateways].sort((left, right) => {
      const evidenceDifference = Number(evidenceGateways.has(String(right.gateway_id)))
        - Number(evidenceGateways.has(String(left.gateway_id)));
      return evidenceDifference
        || this._compareText(left.name || left.gateway_id, right.name || right.gateway_id)
        || this._compareText(left.gateway_id, right.gateway_id);
    });
    const visibleGateways = orderedGateways.slice(0, 8).map((gateway) => ({
      ...gateway,
      _graph_location: this._gatewayGraphLocation(gateway, allNodes),
    }));
    const visibleAliases = this._aliasIndex(visibleNodes);
    const gatewayKeys = new Map(
      visibleGateways.map((gateway) => [String(gateway.gateway_id), `gateway:${gateway.gateway_id}`]),
    );
    const endpointLocations = new Map();
    visibleNodes.forEach((node) => {
      endpointLocations.set(`node:${node.node_key}`, this._nodeGraphLocation(node));
    });
    visibleGateways.forEach((gateway) => {
      endpointLocations.set(`gateway:${gateway.gateway_id}`, gateway._graph_location || null);
    });
    const edges = [];
    const edgeKeys = new Set();
    const edgeByKey = new Map();
    const addEdge = (from, to, type, { provenance = null } = {}) => {
      if (!from || !to || from === to) return;
      const endpoints = [from, to].sort();
      const key = `${type}:${endpoints[0]}:${endpoints[1]}`;
      if (edgeKeys.has(key)) {
        const existing = edgeByKey.get(key);
        if (existing && provenance === "manual_request") {
          existing.provenance = "manual_request";
        }
        return;
      }
      edgeKeys.add(key);
      const distance = this._haversineMeters(
        endpointLocations.get(from),
        endpointLocations.get(to),
      );
      const edge = {
        from,
        to,
        type,
        provenance,
        distance_meters: distance,
        target_length: this._edgeTargetLength(distance),
      };
      edges.push(edge);
      edgeByKey.set(key, edge);
    };

    visibleNodes.forEach((node) => {
      const hops = node.connectivity && node.connectivity.hops;
      const gatewayKey = gatewayKeys.get(this._hopsGatewayId(node));
      if (typeof hops === "number" && Number.isFinite(hops) && hops === 0 && gatewayKey) {
        addEdge(gatewayKey, `node:${node.node_key}`, "direct");
      }
      const neighborEvidence = this._neighborIdentifiers(node);
      if (neighborEvidence) {
        const source = this._resolveRouteIdentifier(visibleAliases, neighborEvidence.source);
        neighborEvidence.neighbors.forEach((neighbor) => {
          const target = this._resolveRouteIdentifier(visibleAliases, neighbor);
          if (source && target) {
            // NeighborInfo establishes a cached evidence edge. Only independently
            // validated node coordinates may size its physical-distance spring.
            addEdge(`node:${source}`, `node:${target}`, "neighbor", {
              provenance: neighborEvidence.provenance,
            });
          }
        });
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
    const points = this._ensureGraphPositions(topology, width, height);
    const shown = topology.totalNodes > topology.nodes.length
      ? `Showing ${topology.nodes.length} of ${topology.totalNodes} nodes`
      : `${topology.nodes.length} nodes`;
    const fallbackCount = topology.gateways.filter(
      (gateway) => gateway._graph_location
        && gateway._graph_location.source === "home_assistant_fallback",
    ).length;
    return `
      <section class="topology">
        <div class="topology-heading">
          <strong class="topology-copy">
            <span>Cached evidence topology — no traceroutes sent automatically</span>
            <span class="topology-note">Edges are last received evidence, not a live route. NeighborInfo evidence expires after one hour; explicitly requested evidence is labeled. Distance changes spring length only.</span>
            ${fallbackCount ? `<span class="topology-note">${fallbackCount} gateway${fallbackCount === 1 ? " uses" : "s use"} Home Assistant location fallback.</span>` : ""}
          </strong>
          <label class="sort-control">Most recent
            <select id="meshnet-graph-limit">
              ${[20, 50, 100].map((limit) => `<option value="${limit}"${this._selected(this._graphLimit, limit)}>${limit}</option>`).join("")}
            </select>
          </label>
          <span class="label">${shown} · ${topology.gateways.length} gateways</span>
        </div>
        <svg id="meshnet-topology-graph" viewBox="0 0 ${width} ${height}" role="img" aria-label="Cached mesh evidence topology; no traceroutes sent automatically">
        ${topology.edges.map((edge) => {
          const a = points.get(edge.from);
          const b = points.get(edge.to);
          if (!a || !b) return "";
          const miles = this._formatEdgeDistanceMiles(edge.distance_meters);
          const distance = miles == null
            ? "Physical distance unavailable"
            : `${miles} (${Math.round(edge.distance_meters)} m) great-circle distance`;
          const evidence = edge.type === "neighbor"
            ? edge.provenance === "manual_request"
              ? "Requested NeighborInfo"
              : "Fresh local-RF NeighborInfo"
            : edge.type === "direct" ? "Direct local-RF observation" : "Cached route evidence";
          const line = `<line class="link ${edge.type === "direct" ? "direct-link" : "route-link"}" data-graph-from="${this._escape(edge.from)}" data-graph-to="${this._escape(edge.to)}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"><title>${this._escape(`${evidence}; ${distance}`)}</title></line>`;
          const label = miles == null ? "" : `<text class="edge-distance" aria-hidden="true" data-graph-distance-from="${this._escape(edge.from)}" data-graph-distance-to="${this._escape(edge.to)}" x="${(a.x + b.x) / 2}" y="${(a.y + b.y) / 2 - 4}">${this._escape(miles)}</text>`;
          return `${line}${label}`;
        }).join("")}
        ${topology.gateways.map((gateway) => {
          const point = points.get(`gateway:${gateway.gateway_id}`);
          return `
          <g data-graph-key="${this._escape(`gateway:${gateway.gateway_id}`)}">
            <title>${this._escape(gateway.name || gateway.gateway_id)}</title>
            <circle class="gateway ${gateway.connected ? "" : "offline"}" cx="${point.x}" cy="${point.y}" r="18"></circle>
            <text x="${point.x + 24}" y="${point.y + 4}">${this._escape(String(gateway.name || gateway.gateway_id).slice(0, 20))}</text>
          </g>`;
        }).join("")}
        ${topology.nodes.map((node) => {
          const point = points.get(`node:${node.node_key}`);
          return `
          <g data-graph-key="${this._escape(`node:${node.node_key}`)}">
            <title>${this._escape(this._nodeName(node))}</title>
            <circle class="node ${node.online ? "" : "offline"}" cx="${point.x}" cy="${point.y}" r="14"></circle>
            <text x="${point.x + 19}" y="${point.y + 4}">${this._escape(this._nodeCompactName(node).slice(0, 18))}</text>
          </g>`;
        }).join("")}
        ${topology.edges.length ? "" : '<text class="topology-empty" x="500" y="610">No cached connection evidence yet</text>'}
        </svg>
      </section>
    `;
  }

  _ensureGraphPositions(topology, width, height) {
    const keys = [
      ...topology.gateways.map((gateway) => `gateway:${gateway.gateway_id}`),
      ...topology.nodes.map((node) => `node:${node.node_key}`),
    ];
    const visible = new Set(keys);
    [...this._graphPositions.keys()].forEach((key) => {
      if (!visible.has(key)) this._graphPositions.delete(key);
    });
    keys.forEach((key, index) => {
      if (this._graphPositions.has(key)) return;
      const isGateway = key.startsWith("gateway:");
      const angle = (Math.PI * 2 * index) / Math.max(keys.length, 1) - Math.PI / 2;
      const radius = Math.min(width, height) * (isGateway ? 0.22 : 0.36);
      this._graphPositions.set(key, {
        x: width / 2 + Math.cos(angle) * radius,
        y: height / 2 + Math.sin(angle) * radius,
        vx: 0,
        vy: 0,
        fixed: false,
      });
    });
    return this._graphPositions;
  }

  _finiteGraphNumber(value, fallback = 0) {
    return typeof value === "number" && Number.isFinite(value) ? value : fallback;
  }

  _forceStep(positions, edges, options = {}) {
    const width = Math.max(100, this._finiteGraphNumber(options.width, 1000));
    const height = Math.max(100, this._finiteGraphNumber(options.height, 640));
    const maximumPadding = Math.max(0, Math.min(width, height) / 2 - 1);
    const padding = Math.min(
      maximumPadding,
      Math.max(0, this._finiteGraphNumber(options.padding, 24)),
    );
    const source = positions instanceof Map ? positions : new Map();
    const keys = [...source.keys()].slice(0, 108);
    const result = new Map();
    keys.forEach((key, index) => {
      const original = source.get(key) || {};
      const angle = (Math.PI * 2 * index) / Math.max(keys.length, 1);
      const fallbackX = width / 2 + Math.cos(angle) * Math.min(width, height) * 0.2;
      const fallbackY = height / 2 + Math.sin(angle) * Math.min(width, height) * 0.2;
      result.set(key, {
        x: Math.min(width - padding, Math.max(padding, this._finiteGraphNumber(original.x, fallbackX))),
        y: Math.min(height - padding, Math.max(padding, this._finiteGraphNumber(original.y, fallbackY))),
        vx: Math.min(30, Math.max(-30, this._finiteGraphNumber(original.vx, 0))),
        vy: Math.min(30, Math.max(-30, this._finiteGraphNumber(original.vy, 0))),
        fixed: original.fixed === true,
      });
    });

    for (let leftIndex = 0; leftIndex < keys.length; leftIndex += 1) {
      const left = result.get(keys[leftIndex]);
      for (let rightIndex = leftIndex + 1; rightIndex < keys.length; rightIndex += 1) {
        const right = result.get(keys[rightIndex]);
        let dx = right.x - left.x;
        let dy = right.y - left.y;
        if (dx === 0 && dy === 0) {
          dx = (rightIndex % 2 ? 1 : -1) * 0.01;
          dy = 0.01;
        }
        const distanceSquared = Math.max(100, dx * dx + dy * dy);
        const distance = Math.sqrt(distanceSquared);
        const force = Math.min(2.4, 8500 / distanceSquared);
        const fx = force * dx / distance;
        const fy = force * dy / distance;
        if (!left.fixed) { left.vx -= fx; left.vy -= fy; }
        if (!right.fixed) { right.vx += fx; right.vy += fy; }
      }
    }

    (Array.isArray(edges) ? edges : []).slice(0, 512).forEach((edge) => {
      const left = result.get(edge && edge.from);
      const right = result.get(edge && edge.to);
      if (!left || !right) return;
      const dx = right.x - left.x;
      const dy = right.y - left.y;
      const distance = Math.max(0.01, Math.sqrt(dx * dx + dy * dy));
      const rawTarget = edge.target_length == null ? edge.targetLength : edge.target_length;
      const target = typeof rawTarget === "number" && Number.isFinite(rawTarget)
        ? Math.min(400, Math.max(40, rawTarget))
        : this._edgeTargetLength(null);
      const force = Math.min(2.5, Math.max(-2.5, (distance - target) * 0.018));
      const fx = force * dx / distance;
      const fy = force * dy / distance;
      if (!left.fixed) { left.vx += fx; left.vy += fy; }
      if (!right.fixed) { right.vx -= fx; right.vy -= fy; }
    });

    result.forEach((point) => {
      if (point.fixed) {
        point.vx = 0;
        point.vy = 0;
        return;
      }
      point.vx = Math.min(30, Math.max(-30, point.vx * 0.82));
      point.vy = Math.min(30, Math.max(-30, point.vy * 0.82));
      point.x = Math.min(width - padding, Math.max(padding, point.x + point.vx));
      point.y = Math.min(height - padding, Math.max(padding, point.y + point.vy));
    });
    return result;
  }

  _applyGraphPositions() {
    if (typeof this.querySelector !== "function") return;
    const svg = this.querySelector("#meshnet-topology-graph");
    if (!svg || typeof svg.querySelectorAll !== "function") return;
    svg.querySelectorAll("[data-graph-from][data-graph-to]").forEach((line) => {
      const left = this._graphPositions.get(line.getAttribute("data-graph-from"));
      const right = this._graphPositions.get(line.getAttribute("data-graph-to"));
      if (!left || !right) return;
      line.setAttribute("x1", String(left.x));
      line.setAttribute("y1", String(left.y));
      line.setAttribute("x2", String(right.x));
      line.setAttribute("y2", String(right.y));
    });
    svg.querySelectorAll("[data-graph-distance-from][data-graph-distance-to]").forEach((label) => {
      const left = this._graphPositions.get(label.getAttribute("data-graph-distance-from"));
      const right = this._graphPositions.get(label.getAttribute("data-graph-distance-to"));
      if (!left || !right) return;
      label.setAttribute("x", String((left.x + right.x) / 2));
      label.setAttribute("y", String((left.y + right.y) / 2 - 4));
    });
    svg.querySelectorAll("[data-graph-key]").forEach((group) => {
      const point = this._graphPositions.get(group.getAttribute("data-graph-key"));
      if (!point || typeof group.querySelector !== "function") return;
      const circle = group.querySelector("circle");
      const text = group.querySelector("text");
      if (circle) {
        circle.setAttribute("cx", String(point.x));
        circle.setAttribute("cy", String(point.y));
      }
      if (text) {
        const radius = circle && Number(circle.getAttribute("r")) === 18 ? 24 : 19;
        text.setAttribute("x", String(point.x + radius));
        text.setAttribute("y", String(point.y + 4));
      }
    });
  }

  _startGraphAnimation(topology) {
    this._stopGraphAnimation();
    this._graphAnimationTopology = topology;
    this._graphAnimationIterations = 0;
    const reducedMotion = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion) {
      for (let iteration = 0; iteration < 24; iteration += 1) {
        this._graphPositions = this._forceStep(this._graphPositions, topology.edges, {
          width: 1000,
          height: 640,
          padding: 24,
        });
      }
      this._applyGraphPositions();
      return;
    }
    if (typeof window.requestAnimationFrame !== "function") return;
    const tick = () => {
      this._graphAnimationFrame = null;
      if (!this._connected || this._activeView !== "mesh" || this._graphAnimationTopology !== topology) {
        return;
      }
      this._graphPositions = this._forceStep(this._graphPositions, topology.edges, {
        width: 1000,
        height: 640,
        padding: 24,
      });
      this._applyGraphPositions();
      this._graphAnimationIterations += 1;
      if (this._graphAnimationIterations < 240) {
        this._graphAnimationFrame = window.requestAnimationFrame(tick);
      }
    };
    this._graphAnimationFrame = window.requestAnimationFrame(tick);
  }

  _stopGraphAnimation() {
    if (this._graphAnimationFrame != null && typeof window.cancelAnimationFrame === "function") {
      window.cancelAnimationFrame(this._graphAnimationFrame);
    }
    this._graphAnimationFrame = null;
    this._graphAnimationTopology = null;
    this._graphAnimationIterations = 0;
    if (typeof this._graphDragCleanup === "function") {
      const cleanup = this._graphDragCleanup;
      this._graphDragCleanup = null;
      cleanup();
    }
    this._graphDrag = null;
  }

  _bindGraphControls(topology) {
    if (typeof this.querySelector !== "function") return;
    const limit = this.querySelector("#meshnet-graph-limit");
    if (limit) {
      limit.addEventListener("focusout", (event) => this._handlePollFocusOut(event));
      limit.addEventListener("change", () => {
        this._graphLimit = this._normalizeGraphLimit(limit.value);
        this._safeRender("render");
      });
    }
    const svg = this.querySelector("#meshnet-topology-graph");
    if (!svg) return;
    this._startGraphAnimation(topology);
    this._graphDragCleanup = this._bindGraphDrag(svg);
  }

  _bindGraphDrag(svg) {
    if (!svg || typeof svg.addEventListener !== "function") return () => {};
    const pointForEvent = (event) => {
      const bounds = svg.getBoundingClientRect();
      const viewBox = svg.viewBox && svg.viewBox.baseVal
        ? svg.viewBox.baseVal
        : { x: 0, y: 0, width: 1000, height: 640 };
      const width = bounds.width > 0 ? bounds.width : 1;
      const height = bounds.height > 0 ? bounds.height : 1;
      return {
        x: Math.min(viewBox.x + viewBox.width, Math.max(viewBox.x,
          viewBox.x + (event.clientX - bounds.left) * viewBox.width / width)),
        y: Math.min(viewBox.y + viewBox.height, Math.max(viewBox.y,
          viewBox.y + (event.clientY - bounds.top) * viewBox.height / height)),
      };
    };
    const pointerDown = (event) => {
      const element = event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-graph-key]")
        : null;
      const key = element && element.dataset && element.dataset.graphKey
        ? element.dataset.graphKey
        : element && typeof element.getAttribute === "function"
          ? element.getAttribute("data-graph-key")
          : "";
      const position = this._graphPositions.get(key);
      if (!position) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      position.fixed = true;
      position.vx = 0;
      position.vy = 0;
      this._graphDrag = { key, pointerId: event.pointerId };
      if (typeof svg.setPointerCapture === "function") {
        try { svg.setPointerCapture(event.pointerId); } catch (_ignored) { /* best effort */ }
      }
    };
    const pointerMove = (event) => {
      if (!this._graphDrag || this._graphDrag.pointerId !== event.pointerId) return;
      const position = this._graphPositions.get(this._graphDrag.key);
      if (!position) return;
      const point = pointForEvent(event);
      position.x = point.x;
      position.y = point.y;
      position.vx = 0;
      position.vy = 0;
      position.fixed = true;
      if (typeof event.preventDefault === "function") event.preventDefault();
      this._applyGraphPositions();
    };
    const pointerEnd = (event) => {
      if (!this._graphDrag || this._graphDrag.pointerId !== event.pointerId) return;
      const position = this._graphPositions.get(this._graphDrag.key);
      if (position) position.fixed = false;
      if (typeof svg.releasePointerCapture === "function") {
        try { svg.releasePointerCapture(event.pointerId); } catch (_ignored) { /* best effort */ }
      }
      this._graphDrag = null;
    };
    const listeners = [
      ["pointerdown", pointerDown],
      ["pointermove", pointerMove],
      ["pointerup", pointerEnd],
      ["pointercancel", pointerEnd],
    ];
    listeners.forEach(([name, callback]) => svg.addEventListener(name, callback));
    let active = true;
    const cleanup = () => {
      if (!active) return;
      active = false;
      listeners.forEach(([name, callback]) => svg.removeEventListener(name, callback));
      this._graphDrag = null;
      if (this._graphDragCleanup === cleanup) this._graphDragCleanup = null;
    };
    return cleanup;
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
