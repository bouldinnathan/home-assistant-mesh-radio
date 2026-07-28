"""Static safety checks for the dependency-free MeshNet panel."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


PANEL = Path("custom_components/meshnet/frontend/meshnet-panel.js")


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_panel_javascript_parses() -> None:
    """Keep malformed JavaScript out of a HACS release."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    subprocess.run([node, "--check", str(PANEL)], check=True, capture_output=True, text=True)


def test_send_composer_uses_snapshot_identifiers_and_safe_markup() -> None:
    """Recipients and gateways must come from the snapshot and be HTML escaped."""
    source = _source()

    assert "${this._recipientOptions(nodes)}" in source
    assert "${this._gatewayOptions(gateways)}" in source
    assert '<option value=""${this._selected(selected, "")}>Broadcast</option>' in source
    assert '<option value=""${this._selected(selected, "")}>Automatic</option>' in source
    assert "value: String(node.node_key)" in source
    assert "value: String(gateway.gateway_id)" in source
    assert 'value="${this._escape(choice.value)}"' in source
    assert "this._escape(choice.label + suffix)" in source
    assert "this._escape(choice.label + identifier + state)" in source
    assert '<textarea id="meshnet-message"' in source
    assert "${this._escape(this._draft.message)}" in source


def test_send_composer_builds_direct_and_broadcast_websocket_requests() -> None:
    """The panel must use the admin websocket API with bounded user choices."""
    source = _source()

    assert 'type: "meshnet/send_message"' in source
    assert 'message_type: recipient ? "direct" : "broadcast"' in source
    assert "if (recipient) payload.target_node = recipient" in source
    assert "if (gateway) payload.gateway_id = gateway" in source
    assert 'const channel = /^[0-7]$/.test(draft.channel) ? draft.channel : "0"' in source
    assert '["normal", "high", "emergency"].includes(draft.priority)' in source
    assert "const result = await this._hass.callWS(payload)" in source


def test_send_composer_preserves_drafts_and_prevents_duplicate_submits() -> None:
    """Periodic full renders must not erase an in-progress message."""
    source = _source()

    assert "this._draft = {" in source
    assert 'const eventName = key === "message" ? "input" : "change"' in source
    assert "this._draft[key] = field.value" in source
    assert "const composerFocus = this._composerFocusState()" in source
    assert "this._restoreComposerFocus(composerFocus)" in source
    for field_id in (
        "meshnet-recipient",
        "meshnet-gateway",
        "meshnet-message",
        "meshnet-channel",
        "meshnet-priority",
    ):
        assert f'"{field_id}"' in source
    assert "this.ownerDocument.activeElement" in source
    assert "this.contains(active)" in source
    assert 'typeof active.selectionStart === "number"' in source
    assert "field.focus()" in source
    assert "field.setSelectionRange(state.start, state.end)" in source
    assert "if (this._sending) return" in source
    assert 'this._sending ? " disabled" : ""' in source
    assert "if (this._draft.message === draft.message) this._draft.message = \"\"" in source


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
        "  async _refreshSnapshot() {", 1
    )[0]

    assert "snapshot = await this._refreshSnapshot()" in send_method
    assert 'status === "sent"' in send_method
    assert 'text: "Message sent."' in send_method
    assert 'text: "Message queued for delivery."' in send_method
    assert 'text: "Message could not be submitted."' in send_method
    assert "_err.message" not in send_method
    assert "String(_err)" not in send_method
    assert "${this._escape(this._sendStatus ? this._sendStatus.text : \"\")}" in source
