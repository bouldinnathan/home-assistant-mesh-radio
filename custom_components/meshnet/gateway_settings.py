"""Safe, protocol-neutral gateway settings transactions.

The browser never receives a raw radio command API.  It reads a bounded schema,
submits typed field changes for a server-generated diff, then applies a
single-use in-memory preview. Draft and preview secrets are write-only and are
not persisted; a verified MeshCore connection PIN is the narrow reconnect
credential exception handled by the config-entry owner.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import math
import re
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .const import MAX_PANEL_GATEWAYS, TRANSPORT_MQTT, TRANSPORT_REST
from .models import stable_json

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_PREVIEW_TTL_SECONDS = 300
SETTINGS_READ_TIMEOUT_SECONDS = 30
SETTINGS_READ_TIMEOUT_MARGIN_SECONDS = 1.0
SETTINGS_APPLY_TIMEOUT_SECONDS = 120
SETTINGS_QUIESCE_TIMEOUT_SECONDS = 5
MAX_SETTINGS_CHANGES = 64
MAX_SETTINGS_CATEGORIES = 48
MAX_SETTINGS_FIELDS = 384
MAX_SETTING_STRING_LENGTH = 1024
MAX_SECRET_LENGTH = 2048
MAX_PREVIEWS = 64
MAX_SAFE_INTEGER = 2**53 - 1

_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SECRET_MARKERS = (
    "password",
    "passphrase",
    "private_key",
    "privatekey",
    "pre_shared_key",
    "preshared_key",
    "psk",
    "secret",
    "token",
    "credential",
    "admin_key",
    "adminkey",
    "pin",
)
_FORBIDDEN_PATH_PARTS = frozenset({"__proto__", "prototype", "constructor"})
_FIELD_TYPES = frozenset(
    {"boolean", "integer", "number", "string", "select", "secret"}
)
_SAFE_BACKEND_WARNING_MESSAGES = {
    "write_acknowledged_readback_unavailable": (
        "The radio acknowledged a change, but MeshNet could not confirm it by "
        "reading the setting back."
    ),
    "write_confirmed_after_timeout_without_retry": (
        "A write timed out, but a fresh device read confirmed the change; "
        "MeshNet did not retry it."
    ),
    "write_acknowledged_readback_mismatch": (
        "The radio acknowledged a change, but its live value did not match."
    ),
    "post_commit_readback_unavailable": (
        "The radio accepted the settings transaction, but the post-commit "
        "readback was unavailable."
    ),
    "post_commit_readback_mismatch": (
        "The post-commit device read did not match every requested setting."
    ),
    "plan_stopped_after_unverified_write": (
        "MeshNet stopped the settings plan after an unverified write; later "
        "previewed values were not sent."
    ),
}
_SAFE_SNAPSHOT_WARNING_MESSAGES = {
    "credentials_are_write_only": (
        "Credential values are never returned; only whether each credential "
        "is configured is shown."
    ),
    "meshtastic_transaction_has_no_rollback": (
        "The firmware transaction has no rollback; MeshNet sends each operation "
        "once and requires a fresh post-reboot readback."
    ),
    "meshcore_commands_have_no_rollback": (
        "MeshCore has no settings transaction or guaranteed rollback. Each "
        "command is sent once and read back."
    ),
    "meshcore_advanced_settings_read_only": (
        "Repeater mode, tuning, auto-add details, flood scope, custom variables, "
        "private keys, and reset remain read-only."
    ),
    "meshcore_unreadable_channels_omitted": (
        "One or more channel slots could not be read and were omitted."
    ),
    "meshcore_channel_projection_bounded": (
        "The device reported more channel slots than this bounded interface "
        "reads at once."
    ),
}
_SAFE_READ_ONLY_REASON_MESSAGES = {
    "managed_mode_rejects_local_admin_changes": (
        "This radio is in managed mode; its firmware rejects local settings "
        "changes."
    ),
    "confirmed_admin_write_and_verification_not_available": (
        "This connection cannot safely confirm and verify settings changes."
    ),
    "no_received_setting_has_a_reviewed_write_contract": (
        "The radio did not return any setting that MeshNet can edit safely."
    ),
    "radio_metadata_is_read_only": "Radio metadata is read-only.",
    "hardware_identity_is_read_only": "Hardware identity is read-only.",
    "channel_index_is_selected_by_category": (
        "The channel index is selected by its category and is read-only."
    ),
    "security_settings_require_a_recovery_workflow": (
        "Security settings require a dedicated recovery workflow."
    ),
    "this_module_can_disable_the_active_bluetooth_transport": (
        "This module can disable the active Bluetooth connection."
    ),
    "the_active_bluetooth_transport_cannot_disable_itself": (
        "The active Bluetooth connection cannot disable itself."
    ),
    "display_mode_can_disable_bluetooth_on_supported_hardware": (
        "This display mode can disable Bluetooth on supported hardware."
    ),
    "setting_requires_dedicated_semantic_validation": (
        "This setting needs dedicated validation before MeshNet can edit it."
    ),
}
_CAPABILITY_REASON_CODES = frozenset(
    {
        "managed_mode_rejects_local_admin_changes",
        "confirmed_admin_write_and_verification_not_available",
        "no_received_setting_has_a_reviewed_write_contract",
    }
)
_SETTINGS_READ_FAILURE_OUTCOMES = (
    "timeout",
    "provider_error",
    "invalid_snapshot",
)
_SAFE_DIAGNOSTIC_PROTOCOLS = frozenset({"meshtastic", "meshcore"})
_SAFE_DIAGNOSTIC_TRANSPORTS = frozenset(
    {"bluetooth", "mqtt", "native", "rest", "serial", "tcp"}
)
_CONNECTION_SETTINGS_SAVE_WARNING = (
    "The radio accepted a connection credential, but Home Assistant could not "
    "save it. Update the gateway connection credential before restarting."
)
_CONNECTION_SETTINGS_RECOVERY_WARNING = (
    "The connection credential was not fully verified and saved. Verify or "
    "recover the gateway connection before restarting or changing more settings."
)
_CONNECTION_UPDATES_ABSENT = object()
_CONNECTION_UPDATES_INVALID = object()
_SECRET_REVISION_MATERIAL_KEY = "_secret_revision_material"


class GatewaySettingsError(RuntimeError):
    """Base class with a stable, non-secret browser error."""

    code = "settings_error"
    public_message = "The gateway settings operation failed"


class GatewaySettingsNotFound(GatewaySettingsError):
    """Raised when the selected gateway no longer exists."""

    code = "settings_gateway_not_found"
    public_message = "The selected gateway was not found"


class GatewaySettingsUnavailable(GatewaySettingsError):
    """Raised when live settings cannot safely be read or written."""

    code = "settings_unavailable"
    public_message = "Live settings are unavailable for this gateway"


class GatewaySettingsValidationError(GatewaySettingsError):
    """Raised for a rejected field or value."""

    code = "settings_invalid"
    public_message = "One or more settings changes are invalid"


class GatewaySettingsConflict(GatewaySettingsError):
    """Raised when a preview is stale or belongs to another device state."""

    code = "settings_conflict"
    public_message = "Gateway settings changed; reload and preview again"


class GatewaySettingsPreviewExpired(GatewaySettingsError):
    """Raised for an absent, expired, or already consumed preview."""

    code = "settings_preview_expired"
    public_message = "The settings preview expired; preview the changes again"


class GatewaySettingsConfirmationRequired(GatewaySettingsError):
    """Raised when critical changes were not separately confirmed."""

    code = "settings_confirmation_required"
    public_message = "Confirm the connection-critical changes before applying"


@dataclass(slots=True)
class _Preview:
    """One single-use settings plan retained only in process memory."""

    preview_id: str
    gateway_id: str
    revision: str
    expires_monotonic: float
    public_changes: list[dict[str, Any]]
    changes: dict[str, Any] = field(repr=False)
    critical: bool = False
    expiry_handle: asyncio.TimerHandle | None = field(
        default=None, repr=False
    )


def validate_changes_payload(value: Any) -> dict[str, Any]:
    """Bound the generic JSON object accepted by the WebSocket schema."""
    if not isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        raise ValueError("settings changes must be an object")
    if not 1 <= len(value) <= MAX_SETTINGS_CHANGES:
        raise ValueError("settings change count is outside the supported range")
    bounded: dict[str, Any] = {}
    for raw_path, raw_value in value.items():
        if not isinstance(raw_path, str) or not _safe_path(raw_path):
            raise ValueError("settings change contains an invalid path")
        if isinstance(raw_value, (list, tuple, set, frozenset)):
            raise ValueError("settings change contains an unsupported value")
        if isinstance(raw_value, Mapping):
            operation = raw_value.get("operation")
            if operation == "clear" and set(raw_value) == {"operation"}:
                bounded[raw_path] = {"operation": "clear"}
                continue
            if operation == "replace" and set(raw_value) == {
                "operation",
                "value",
            }:
                secret_value = raw_value.get("value")
                if not isinstance(secret_value, str):
                    raise ValueError("secret replacement must be text")
                if not 1 <= len(secret_value) <= MAX_SECRET_LENGTH:
                    raise ValueError("secret replacement length is invalid")
                bounded[raw_path] = {
                    "operation": "replace",
                    "value": secret_value,
                }
                continue
            raise ValueError("settings change contains an invalid operation")
        if raw_value is not None and not isinstance(
            raw_value, (str, bool, int, float)
        ):
            raise ValueError("settings change contains an unsupported value")
        if isinstance(raw_value, str) and len(raw_value) > MAX_SETTING_STRING_LENGTH:
            raise ValueError("settings change text is too long")
        if (
            isinstance(raw_value, int)
            and not isinstance(raw_value, bool)
            and abs(raw_value) > MAX_SAFE_INTEGER
        ):
            raise ValueError("settings change integer is too large")
        if isinstance(raw_value, float) and not math.isfinite(raw_value):
            raise ValueError("settings change number must be finite")
        if isinstance(raw_value, float) and abs(raw_value) > 1_000_000_000_000_000:
            raise ValueError("settings change number is too large")
        bounded[raw_path] = raw_value
    return bounded


def _safe_path(path: str) -> bool:
    return bool(
        _PATH_RE.fullmatch(path)
        and not (_FORBIDDEN_PATH_PARTS & set(path.casefold().split(".")))
    )


def _looks_secret(*values: Any) -> bool:
    text = " ".join(value for value in values if isinstance(value, str)).casefold()
    return any(marker in text for marker in _SECRET_MARKERS)


def _safe_scalar(value: Any) -> str | bool | int | float | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_SETTING_STRING_LENGTH else None
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INTEGER else None
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and abs(value) <= 1_000_000_000_000_000
    ):
        return value
    return None


def _public_read_only_reason(value: Any, *, maximum: int) -> str | None:
    """Return fixed friendly text for known codes or one bounded provider reason."""
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    return _SAFE_READ_ONLY_REASON_MESSAGES.get(value, value)


def _diagnostic_enum(value: Any, allowed: frozenset[str]) -> str:
    """Return an allowlisted diagnostic enum without arbitrary config text."""
    return value if isinstance(value, str) and value in allowed else "unknown"


def _provider_read_timeout_seconds() -> float:
    """Leave the aggregate deadline time to classify and unwind a stuck read."""
    margin = min(
        SETTINGS_READ_TIMEOUT_MARGIN_SECONDS,
        SETTINGS_READ_TIMEOUT_SECONDS * 0.25,
    )
    return max(0.001, SETTINGS_READ_TIMEOUT_SECONDS - margin)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


class GatewaySettingsManager:
    """Coordinate read, preview, apply, stale detection, and verification."""

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator
        self._locks: dict[str, asyncio.Lock] = {}
        self._previews: dict[str, _Preview] = {}
        self._accepting = True
        self._active_tasks: dict[asyncio.Task[Any], str] = {}
        self._settings_read_attempt_count = 0
        self._settings_read_success_count = 0
        self._settings_read_failure_counts = {
            outcome: 0 for outcome in _SETTINGS_READ_FAILURE_OUTCOMES
        }
        self._capability_observations: dict[str, dict[str, Any]] = {}
        # Keep low-entropy credentials (notably six-digit PINs) from becoming
        # guessable through the public revision. The key exists only for this
        # manager lifetime, just like its in-memory previews.
        self._secret_revision_key = secrets.token_bytes(32)

    def invalidate(self) -> None:
        """Forget all secret-bearing previews during reload or shutdown."""
        for preview_id in tuple(self._previews):
            self._discard_preview(preview_id)
        self._capability_observations.clear()

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return cached aggregate capability data without reading a gateway."""
        self._cleanup_previews()
        gateways = sorted(
            getattr(self._coordinator, "gateways", {}).items(),
            key=lambda item: item[0],
        )
        observations: list[dict[str, Any]] = []
        for index, (gateway_id, gateway) in enumerate(
            gateways[:MAX_PANEL_GATEWAYS], start=1
        ):
            cached = self._capability_observations.get(gateway_id)
            if cached is None:
                config = getattr(gateway, "config", None)
                cached = {
                    "protocol": _diagnostic_enum(
                        getattr(config, "protocol", None),
                        _SAFE_DIAGNOSTIC_PROTOCOLS,
                    ),
                    "transport": _diagnostic_enum(
                        getattr(config, "transport", None),
                        _SAFE_DIAGNOSTIC_TRANSPORTS,
                    ),
                    "observed": False,
                    "has_successful_snapshot": False,
                    "last_read_outcome": "not_attempted",
                    "capability_state": "unknown",
                }
            observations.append(
                {"diagnostic_id": f"gateway_{index:03d}", **copy.deepcopy(cached)}
            )
        return {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "pending_preview_count": len(self._previews),
            "gateway_lock_count": len(self._locks),
            "held_gateway_lock_count": sum(
                lock.locked() for lock in self._locks.values()
            ),
            "accepting_operations": self._accepting,
            "active_operation_count": len(self._active_tasks),
            "preview_ttl_seconds": SETTINGS_PREVIEW_TTL_SECONDS,
            "read_timeout_seconds": SETTINGS_READ_TIMEOUT_SECONDS,
            "provider_read_timeout_seconds": _provider_read_timeout_seconds(),
            "apply_timeout_seconds": SETTINGS_APPLY_TIMEOUT_SECONDS,
            "max_changes_per_preview": MAX_SETTINGS_CHANGES,
            "previews_persisted": False,
            "secrets_returned": False,
            "raw_commands_exposed": False,
            "capability_data_is_cached_only": True,
            "settings_read_attempt_count": self._settings_read_attempt_count,
            "settings_read_success_count": self._settings_read_success_count,
            "settings_read_failure_counts": dict(
                self._settings_read_failure_counts
            ),
            "capability_observations": observations,
            "capability_observation_truncated": (
                len(gateways) > MAX_PANEL_GATEWAYS
            ),
        }

    async def async_quiesce(self) -> bool:
        """Fence new work and cancel/drain handler-owned operations."""
        self._accepting = False
        self.invalidate()
        current = asyncio.current_task()
        tasks = {
            task
            for task in self._active_tasks
            if task is not current and not task.done()
        }
        for task in tasks:
            task.cancel()
        if not tasks:
            return True
        done, pending = await asyncio.wait(
            tasks, timeout=SETTINGS_QUIESCE_TIMEOUT_SECONDS
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        return not pending

    def resume(self) -> bool:
        """Allow work after a completed gateway reload, but never over old work."""
        if any(not task.done() for task in self._active_tasks):
            return False
        self._accepting = True
        return True

    @asynccontextmanager
    async def _operation(self, gateway_id: str | None) -> AsyncIterator[None]:
        """Own one HA handler task so unload can cancel it before transport stop."""
        if not self._accepting:
            raise GatewaySettingsUnavailable
        task = asyncio.current_task()
        if task is None:
            raise GatewaySettingsUnavailable
        self._active_tasks[task] = gateway_id or ""
        try:
            if not self._accepting:
                raise GatewaySettingsUnavailable
            yield
        finally:
            self._active_tasks.pop(task, None)

    async def async_get(self, gateway_id: str | None = None) -> dict[str, Any]:
        """Track and return gateway choices plus one live snapshot."""
        async with self._operation(gateway_id):
            try:
                # Include lock contention in the server deadline. A browser
                # that gives up must never leave a late settings read running.
                async with asyncio.timeout(SETTINGS_READ_TIMEOUT_SECONDS):
                    return await self._async_get(gateway_id)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise GatewaySettingsUnavailable from None

    async def _async_get(self, gateway_id: str | None = None) -> dict[str, Any]:
        """Return gateway choices and one sanitized live settings snapshot."""
        gateways = self._gateway_choices()
        if not gateways:
            raise GatewaySettingsUnavailable
        selected_id = gateway_id or self._preferred_gateway_id(gateways)
        gateway = self._coordinator.gateways.get(selected_id)
        if gateway is None:
            raise GatewaySettingsNotFound
        async with self._lock(selected_id):
            selected = await self._async_read_gateway(gateway)
        return {"gateways": gateways, "selected": selected}

    async def async_preview(
        self,
        *,
        gateway_id: str,
        revision: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Track and create one short-lived server-owned preview."""
        async with self._operation(gateway_id):
            try:
                # The panel waits longer than this aggregate deadline. Include
                # lock wait so a timed-out client cannot unknowingly create a
                # secret-bearing preview after it has abandoned the request.
                async with asyncio.timeout(SETTINGS_READ_TIMEOUT_SECONDS):
                    return await self._async_preview(
                        gateway_id=gateway_id,
                        revision=revision,
                        changes=changes,
                    )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise GatewaySettingsUnavailable from None

    async def _async_preview(
        self,
        *,
        gateway_id: str,
        revision: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate changes and create a short-lived, single-use plan."""
        bounded = validate_changes_payload(changes)
        gateway = self._coordinator.gateways.get(gateway_id)
        if gateway is None:
            raise GatewaySettingsNotFound
        async with self._lock(gateway_id):
            snapshot = await self._async_read_gateway(gateway)
            if not snapshot.get("writable"):
                raise GatewaySettingsUnavailable
            if not secrets.compare_digest(snapshot["revision"], str(revision)):
                raise GatewaySettingsConflict
            fields = self._fields_by_path(snapshot)
            normalized, public_changes = self._validate_against_fields(
                fields, bounded
            )
            critical = any(change["critical"] for change in public_changes)
            preview_id = secrets.token_urlsafe(32)
            expires_monotonic = time.monotonic() + SETTINGS_PREVIEW_TTL_SECONDS
            self._cleanup_previews()
            for stale_id in [
                stale_id
                for stale_id, stale in self._previews.items()
                if stale.gateway_id == gateway_id
            ]:
                self._discard_preview(stale_id)
            preview = _Preview(
                preview_id=preview_id,
                gateway_id=gateway_id,
                revision=snapshot["revision"],
                expires_monotonic=expires_monotonic,
                public_changes=public_changes,
                changes=normalized,
                critical=critical,
            )
            preview.expiry_handle = asyncio.get_running_loop().call_later(
                SETTINGS_PREVIEW_TTL_SECONDS,
                self._expire_preview,
                preview_id,
            )
            self._previews[preview_id] = preview
            self._trim_previews()
        return {
            "preview_id": preview_id,
            "expires_at": (
                datetime.now(UTC)
                + timedelta(seconds=SETTINGS_PREVIEW_TTL_SECONDS)
            ).isoformat(),
            "gateway_id": gateway_id,
            "revision": snapshot["revision"],
            "changes": public_changes,
            "requires_critical_confirmation": critical,
            "warnings": self._preview_warnings(public_changes),
        }

    async def async_apply(
        self,
        *,
        gateway_id: str,
        revision: str,
        preview_id: str,
        confirm_critical: bool,
    ) -> dict[str, Any]:
        """Track and consume one exact preview once."""
        async with self._operation(gateway_id):
            return await self._async_apply(
                gateway_id=gateway_id,
                revision=revision,
                preview_id=preview_id,
                confirm_critical=confirm_critical,
            )

    async def _async_apply(
        self,
        *,
        gateway_id: str,
        revision: str,
        preview_id: str,
        confirm_critical: bool,
    ) -> dict[str, Any]:
        """Consume and apply an unchanged server-side preview exactly once."""
        self._cleanup_previews()
        preview = self._discard_preview(preview_id)
        if (
            preview is None
            or preview.gateway_id != gateway_id
            or not secrets.compare_digest(preview.revision, str(revision))
        ):
            raise GatewaySettingsPreviewExpired
        if preview.critical and confirm_critical is not True:
            # Confirmation failures consume the preview so it cannot later be
            # replayed from a stale browser state.
            raise GatewaySettingsConfirmationRequired
        gateway = self._coordinator.gateways.get(gateway_id)
        if gateway is None:
            raise GatewaySettingsNotFound

        try:
            # This is one aggregate deadline: lock wait, stale-state read,
            # exactly-once provider write, and best-effort readback all finish
            # before the browser's longer timeout.  A timeout cancels the
            # operation and never starts a retry.
            async with asyncio.timeout(SETTINGS_APPLY_TIMEOUT_SECONDS):
                async with self._lock(gateway_id):
                    current = await self._async_read_gateway(gateway)
                    if not secrets.compare_digest(
                        current["revision"], preview.revision
                    ):
                        raise GatewaySettingsConflict
                    apply_method = getattr(
                        gateway, "async_apply_settings_plan", None
                    )
                    if not callable(apply_method) or not current.get("writable"):
                        raise GatewaySettingsUnavailable
                    try:
                        backend_result = await apply_method(
                            copy.deepcopy(preview.changes)
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise GatewaySettingsUnavailable from None

                    after: dict[str, Any] | None = None
                    try:
                        after = await self._async_read_gateway(gateway)
                    except GatewaySettingsError:
                        after = None
                    result = self._verified_result(
                        preview=preview,
                        before=current,
                        after=after,
                        backend_result=backend_result,
                    )
                    pin_changed = "security.pin" in preview.changes
                    connection_updates = self._connection_updates(
                        backend_result, changes=preview.changes
                    )
                    pin_verified = "security.pin" in result["verified"]
                    valid_connection_updates = isinstance(
                        connection_updates, dict
                    )
                    connection_recovery_required = bool(
                        pin_changed
                        and (not pin_verified or not valid_connection_updates)
                    )
                    if pin_changed and pin_verified and valid_connection_updates:
                        persist = getattr(
                            self._coordinator,
                            "async_persist_gateway_connection_updates",
                            None,
                        )
                        try:
                            if not callable(persist):
                                raise GatewaySettingsUnavailable
                            await persist(gateway_id, connection_updates)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # The radio write has already been verified. Report
                            # the persistence problem with fixed text instead
                            # of making a user blindly retry the radio write.
                            result["warnings"].append(
                                _CONNECTION_SETTINGS_SAVE_WARNING
                            )
                            connection_recovery_required = True
                    elif pin_changed:
                        result["warnings"].append(
                            _CONNECTION_SETTINGS_SAVE_WARNING
                            if pin_verified
                            and connection_updates is _CONNECTION_UPDATES_INVALID
                            else _CONNECTION_SETTINGS_RECOVERY_WARNING
                        )
                    result["connection_recovery_required"] = (
                        connection_recovery_required
                    )
                    if after is not None:
                        result["snapshot"] = after
                    return result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # A radio timeout is an unknown state. The one-use preview is gone
            # and neither this layer nor an adapter may retry it.
            raise GatewaySettingsUnavailable from None

    def _gateway_choices(self) -> list[dict[str, Any]]:
        return [
            {
                "gateway_id": gateway_id,
                "name": gateway.config.name,
                "protocol": gateway.config.protocol,
                "transport": gateway.config.transport,
                "connected": bool(gateway.status.connected),
                "locally_managed": gateway.config.transport
                not in {TRANSPORT_MQTT, TRANSPORT_REST},
            }
            for gateway_id, gateway in sorted(
                self._coordinator.gateways.items(), key=lambda item: item[0]
            )
        ]

    @staticmethod
    def _preferred_gateway_id(gateways: list[dict[str, Any]]) -> str:
        preferred = next(
            (
                item
                for item in gateways
                if item["connected"] and item["locally_managed"]
            ),
            None,
        )
        if preferred is None:
            preferred = next(
                (item for item in gateways if item["locally_managed"]),
                gateways[0],
            )
        return str(preferred["gateway_id"])

    def _lock(self, gateway_id: str) -> asyncio.Lock:
        return self._locks.setdefault(gateway_id, asyncio.Lock())

    async def _async_read_gateway(self, gateway: Any) -> dict[str, Any]:
        self._settings_read_attempt_count += 1
        read_method = getattr(gateway, "async_get_settings_snapshot", None)
        if not callable(read_method):
            self._record_settings_read_failure(gateway, "provider_error")
            raise GatewaySettingsUnavailable
        try:
            raw = await asyncio.wait_for(
                read_method(), timeout=_provider_read_timeout_seconds()
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._record_settings_read_failure(gateway, "timeout")
            raise GatewaySettingsUnavailable from None
        except Exception as err:
            self._record_settings_read_failure(gateway, "provider_error")
            raise GatewaySettingsUnavailable from err
        try:
            snapshot = self._sanitize_snapshot(gateway, raw)
        except asyncio.CancelledError:
            raise
        except GatewaySettingsError:
            self._record_settings_read_failure(gateway, "invalid_snapshot")
            raise
        except Exception:
            self._record_settings_read_failure(gateway, "invalid_snapshot")
            raise GatewaySettingsUnavailable from None
        self._settings_read_success_count += 1
        return snapshot

    def _record_settings_read_failure(self, gateway: Any, outcome: str) -> None:
        """Retain only one fixed failure category and prior aggregate counts."""
        if outcome not in self._settings_read_failure_counts:
            outcome = "provider_error"
        self._settings_read_failure_counts[outcome] += 1
        config = getattr(gateway, "config", None)
        gateway_id = str(getattr(config, "gateway_id", ""))
        if not gateway_id:
            return
        previous = self._capability_observations.get(gateway_id)
        if previous is None:
            previous = {
                "protocol": _diagnostic_enum(
                    getattr(config, "protocol", None),
                    _SAFE_DIAGNOSTIC_PROTOCOLS,
                ),
                "transport": _diagnostic_enum(
                    getattr(config, "transport", None),
                    _SAFE_DIAGNOSTIC_TRANSPORTS,
                ),
                "observed": False,
                "has_successful_snapshot": False,
                "capability_state": "unknown",
            }
        self._capability_observations[gateway_id] = {
            **previous,
            "last_read_outcome": outcome,
        }

    def _sanitize_snapshot(self, gateway: Any, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise GatewaySettingsUnavailable
        categories: list[dict[str, Any]] = []
        field_count = 0
        source_category_count = 0
        source_field_count = 0
        claimed_writable_field_count = 0
        seen_paths: set[str] = set()
        raw_categories = raw.get("categories")
        if not isinstance(raw_categories, list):
            raw_categories = []
        for raw_category in raw_categories[:MAX_SETTINGS_CATEGORIES]:
            source_category_count += 1
            if not isinstance(raw_category, Mapping):
                continue
            key = raw_category.get("key")
            label = raw_category.get("label")
            if not isinstance(key, str) or not _safe_path(key):
                continue
            if not isinstance(label, str) or not 1 <= len(label) <= 128:
                continue
            fields: list[dict[str, Any]] = []
            raw_fields = raw_category.get("fields")
            if not isinstance(raw_fields, list):
                raw_fields = []
            for raw_field in raw_fields:
                if source_field_count >= MAX_SETTINGS_FIELDS:
                    break
                source_field_count += 1
                if (
                    isinstance(raw_field, Mapping)
                    and raw_field.get("writable") is True
                ):
                    claimed_writable_field_count += 1
                field = self._sanitize_field(raw_field)
                if field is None:
                    continue
                if field["path"] in seen_paths:
                    raise GatewaySettingsUnavailable
                seen_paths.add(field["path"])
                fields.append(field)
                field_count += 1
            if not fields:
                continue
            category = {"key": key, "label": label, "fields": fields}
            description = raw_category.get("description")
            if isinstance(description, str) and len(description) <= 512:
                category["description"] = description
            categories.append(category)

        raw_warning_codes = raw.get("warning_codes", [])
        warnings = [
            _SAFE_SNAPSHOT_WARNING_MESSAGES[code]
            for code in raw_warning_codes[:16]
            if isinstance(code, str)
            and code in _SAFE_SNAPSHOT_WARNING_MESSAGES
        ] if isinstance(raw_warning_codes, list) else []
        any_writable = any(
            field["writable"]
            for category in categories
            for field in category["fields"]
        )
        connected = bool(gateway.status.connected)
        available = raw.get("available", True) is True
        complete = raw.get("complete", True) is True
        source_writable = raw.get("writable", any_writable) is True
        writable = bool(
            source_writable
            and any_writable
            and connected
            and available
            and complete
        )
        raw_read_only_reason = raw.get("read_only_reason")
        read_only_reason = _public_read_only_reason(
            raw_read_only_reason, maximum=512
        )
        if not writable:
            if not connected:
                read_only_reason = "Connect this gateway before editing its settings."
            elif not available:
                read_only_reason = "The radio did not provide live settings."
            elif not complete:
                read_only_reason = (
                    "The live settings read was incomplete; reconnect and reload "
                    "before editing."
                )
            elif not read_only_reason:
                read_only_reason = "This gateway is read-only."
        else:
            read_only_reason = None

        schema_writable_field_count = sum(
            field["writable"]
            for category in categories
            for field in category["fields"]
        )
        editable_field_count = schema_writable_field_count if writable else 0
        capability_reason = raw_read_only_reason
        raw_capabilities = raw.get("capabilities")
        if (
            capability_reason not in _CAPABILITY_REASON_CODES
            and isinstance(raw_capabilities, Mapping)
        ):
            candidate = raw_capabilities.get("apply_reason")
            capability_reason = (
                candidate if candidate in _CAPABILITY_REASON_CODES else None
            )
        capability_state = self._capability_state(
            connected=connected,
            available=available,
            complete=complete,
            writable=writable,
            source_writable=source_writable,
            claimed_writable_field_count=claimed_writable_field_count,
            schema_writable_field_count=schema_writable_field_count,
            capability_reason=capability_reason,
        )
        gateway_id = str(gateway.config.gateway_id)
        capability_observation = {
            "protocol": _diagnostic_enum(
                gateway.config.protocol, _SAFE_DIAGNOSTIC_PROTOCOLS
            ),
            "transport": _diagnostic_enum(
                gateway.config.transport, _SAFE_DIAGNOSTIC_TRANSPORTS
            ),
            "observed": True,
            "has_successful_snapshot": True,
            "last_read_outcome": "success",
            "capability_state": capability_state,
            "connected": connected,
            "source_available": available,
            "source_complete": complete,
            "source_writable": source_writable,
            "source_category_count": source_category_count,
            "sanitized_category_count": len(categories),
            "source_field_count": source_field_count,
            "sanitized_field_count": field_count,
            "claimed_writable_field_count": claimed_writable_field_count,
            "schema_writable_field_count": schema_writable_field_count,
            "editable_field_count": editable_field_count,
            "read_only_field_count": max(0, field_count - editable_field_count),
            "contract_downgraded_field_count": max(
                0,
                claimed_writable_field_count - schema_writable_field_count,
            ),
        }

        secret_paths = {
            field["path"]
            for category in categories
            for field in category["fields"]
            if field["type"] == "secret"
        }
        secret_revision_fingerprint = self._secret_revision_fingerprint(
            raw.get(_SECRET_REVISION_MATERIAL_KEY, _CONNECTION_UPDATES_ABSENT),
            secret_paths=secret_paths,
        )
        revision_material = {
            "gateway_id": gateway.config.gateway_id,
            "protocol": gateway.config.protocol,
            "transport": gateway.config.transport,
            "connected": connected,
            "available": available,
            "complete": complete,
            "writable": writable,
            "read_only_reason": read_only_reason,
            # Include the whole sanitized contract so a preview becomes stale
            # if any type, bounds, option set, or other safety metadata changes.
            "categories": categories,
            # This keyed intermediate is revision input only. It and the raw
            # adapter material are never copied to the public snapshot.
            "secret_state": secret_revision_fingerprint,
        }
        revision = hashlib.sha256(
            stable_json(revision_material).encode("utf-8")
        ).hexdigest()
        self._capability_observations[gateway_id] = capability_observation
        return {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "gateway_id": gateway.config.gateway_id,
            "name": gateway.config.name,
            "protocol": gateway.config.protocol,
            "transport": gateway.config.transport,
            "connected": connected,
            "writable": writable,
            "read_only_reason": read_only_reason,
            "revision": revision,
            "fetched_at": _iso_now(),
            "categories": categories,
            "warnings": warnings,
        }

    @staticmethod
    def _capability_state(
        *,
        connected: bool,
        available: bool,
        complete: bool,
        writable: bool,
        source_writable: bool,
        claimed_writable_field_count: int,
        schema_writable_field_count: int,
        capability_reason: Any,
    ) -> str:
        """Collapse capability decisions to a fixed non-identifying enum."""
        if not connected:
            return "disconnected"
        if not available:
            return "unavailable"
        if not complete:
            return "incomplete"
        if writable:
            return "writable"
        if capability_reason == "managed_mode_rejects_local_admin_changes":
            return "managed_mode"
        if (
            claimed_writable_field_count > 0
            and schema_writable_field_count == 0
        ):
            return "all_claimed_writable_fields_rejected"
        if source_writable != bool(schema_writable_field_count):
            return "source_contract_inconsistent"
        return "no_writable_fields"

    def _secret_revision_fingerprint(
        self, raw: Any, *, secret_paths: set[str]
    ) -> str | None:
        """Key private adapter secret state without exposing an offline oracle.

        Adapters may provide ``_secret_revision_material`` as a mapping from a
        sanitized secret field path to its current raw scalar state. The input
        is consumed solely here and is never copied into the public snapshot.
        """
        if raw is _CONNECTION_UPDATES_ABSENT:
            return None
        if (
            not isinstance(raw, Mapping)
            or isinstance(raw, (str, bytes))
            or len(raw) > MAX_SETTINGS_FIELDS
        ):
            raise GatewaySettingsUnavailable

        digest = hmac.new(self._secret_revision_key, digestmod=hashlib.sha256)
        paths = list(raw)
        if any(
            not isinstance(path, str)
            or not _safe_path(path)
            or path not in secret_paths
            for path in paths
        ):
            raise GatewaySettingsUnavailable
        for path in sorted(paths):
            if not isinstance(path, str):  # Narrowed by the fail-closed check.
                raise GatewaySettingsUnavailable
            value = raw[path]
            path_bytes = path.encode("utf-8")
            if value is None:
                type_marker, value_bytes = b"n", b""
            elif isinstance(value, bool):
                type_marker, value_bytes = b"t", b"1" if value else b"0"
            elif isinstance(value, int):
                if value.bit_length() > 64:
                    raise GatewaySettingsUnavailable
                type_marker, value_bytes = b"i", str(value).encode("ascii")
            elif isinstance(value, str):
                type_marker, value_bytes = b"s", value.encode("utf-8")
            elif isinstance(value, bytes):
                type_marker, value_bytes = b"b", value
            else:
                raise GatewaySettingsUnavailable
            if len(value_bytes) > MAX_SECRET_LENGTH:
                raise GatewaySettingsUnavailable
            digest.update(len(path_bytes).to_bytes(2, "big"))
            digest.update(path_bytes)
            digest.update(type_marker)
            digest.update(len(value_bytes).to_bytes(4, "big"))
            digest.update(value_bytes)
        return digest.hexdigest()

    @staticmethod
    def _sanitize_field(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        path = raw.get("path")
        label = raw.get("label")
        field_type = raw.get("type")
        if (
            not isinstance(path, str)
            or not _safe_path(path)
            or not isinstance(label, str)
            or not 1 <= len(label) <= 128
            or field_type not in _FIELD_TYPES
        ):
            return None
        secret = field_type == "secret" or _looks_secret(path, label)
        if secret:
            field_type = "secret"
        field: dict[str, Any] = {
            "path": path,
            "label": label,
            "type": field_type,
            "writable": raw.get("writable") is True,
            "critical": raw.get("critical") is True,
            "requires_reconnect": raw.get("requires_reconnect") is True,
        }
        if secret:
            field["value"] = None
            field["configured"] = raw.get("configured") is True
            field["allow_clear"] = raw.get("allow_clear") is True
        else:
            field["value"] = _safe_scalar(raw.get("value"))
        unsafe_contract = False
        for key, maximum in (("description", 512), ("unit", 32)):
            value = raw.get(key)
            if isinstance(value, str) and len(value) <= maximum:
                field[key] = value
        read_only_reason = _public_read_only_reason(
            raw.get("read_only_reason"), maximum=256
        )
        if read_only_reason is not None:
            field["read_only_reason"] = read_only_reason
        for key in ("min", "max", "step"):
            if key not in raw:
                continue
            value = _safe_scalar(raw.get(key))
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                unsafe_contract = True
                continue
            field[key] = value
        max_length = raw.get("max_length")
        if "max_length" in raw and (
            not isinstance(max_length, int)
            or isinstance(max_length, bool)
            or max_length < 1
        ):
            unsafe_contract = True
        elif isinstance(max_length, int) and not isinstance(max_length, bool):
            field["max_length"] = max(1, min(max_length, MAX_SETTING_STRING_LENGTH))
        if field_type == "select":
            options: list[dict[str, Any]] = []
            seen_options: set[tuple[type[Any], Any]] = set()
            for option in raw.get("options", [])[:256] if isinstance(raw.get("options"), list) else []:
                if not isinstance(option, Mapping):
                    continue
                value = _safe_scalar(option.get("value"))
                option_label = option.get("label")
                if value is None or not isinstance(option_label, str) or len(option_label) > 128:
                    unsafe_contract = True
                    continue
                option_key = (type(value), value)
                if option_key in seen_options:
                    unsafe_contract = True
                    continue
                seen_options.add(option_key)
                options.append({"value": value, "label": option_label})
            field["options"] = options
            if not isinstance(raw.get("options"), list) or not options:
                unsafe_contract = True

        if field_type == "boolean":
            unsafe_contract = unsafe_contract or not isinstance(
                field.get("value"), bool
            )
        elif field_type == "integer":
            current = field.get("value")
            unsafe_contract = unsafe_contract or (
                isinstance(current, bool) or not isinstance(current, int)
            )
        elif field_type == "number":
            current = field.get("value")
            unsafe_contract = unsafe_contract or (
                isinstance(current, bool)
                or not isinstance(current, (int, float))
            )
        elif field_type == "string":
            unsafe_contract = unsafe_contract or not isinstance(
                field.get("value"), str
            )
        elif field_type == "select":
            allowed = [option["value"] for option in field.get("options", [])]
            unsafe_contract = unsafe_contract or not any(
                type(field.get("value")) is type(option)
                and field.get("value") == option
                for option in allowed
            )

        if field_type in {"integer", "number"}:
            minimum = field.get("min")
            maximum = field.get("max")
            step = field.get("step")
            current = field.get("value")
            unsafe_contract = unsafe_contract or (
                minimum is None
                or maximum is None
                or minimum > maximum
                or (step is not None and step <= 0)
                or (
                    isinstance(current, (int, float))
                    and not isinstance(current, bool)
                    and not minimum <= current <= maximum
                )
            )

        if unsafe_contract and field["writable"]:
            field["writable"] = False
            field["read_only_reason"] = (
                "Invalid or incomplete safety metadata; this field is read-only."
            )
        return field

    @staticmethod
    def _fields_by_path(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            field["path"]: field
            for category in snapshot.get("categories", [])
            for field in category.get("fields", [])
        }

    def _validate_against_fields(
        self,
        fields: Mapping[str, dict[str, Any]],
        changes: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        normalized: dict[str, Any] = {}
        public: list[dict[str, Any]] = []
        for path, requested in changes.items():
            field = fields.get(path)
            if field is None or not field.get("writable"):
                raise GatewaySettingsValidationError
            value, before, after, is_secret = self._validate_value(field, requested)
            if not is_secret and value == field.get("value"):
                continue
            normalized[path] = value
            public.append(
                {
                    "path": path,
                    "label": field["label"],
                    "before": before,
                    "after": after,
                    "secret": is_secret,
                    "critical": bool(field.get("critical")),
                    "requires_reconnect": bool(field.get("requires_reconnect")),
                    **(
                        {"operation": value["operation"]}
                        if is_secret and isinstance(value, Mapping)
                        else {}
                    ),
                }
            )
        if not normalized:
            raise GatewaySettingsValidationError
        ordered = sorted(
            public, key=lambda item: (item["critical"], item["path"])
        )
        normalized = {item["path"]: normalized[item["path"]] for item in ordered}
        return normalized, ordered

    @staticmethod
    def _validate_value(
        field: Mapping[str, Any], requested: Any
    ) -> tuple[Any, Any, Any, bool]:
        field_type = field["type"]
        if field_type == "secret":
            if not isinstance(requested, Mapping):
                raise GatewaySettingsValidationError
            operation = requested.get("operation")
            if operation == "clear":
                if not field.get("allow_clear") or not field.get("configured"):
                    raise GatewaySettingsValidationError
                return (
                    {"operation": "clear"},
                    "Configured" if field.get("configured") else "Not configured",
                    "Will be cleared",
                    True,
                )
            if operation != "replace":
                raise GatewaySettingsValidationError
            value = requested.get("value")
            if not isinstance(value, str) or not value:
                raise GatewaySettingsValidationError
            max_length = min(
                int(field.get("max_length", MAX_SECRET_LENGTH)), MAX_SECRET_LENGTH
            )
            if len(value) > max_length:
                raise GatewaySettingsValidationError
            return (
                {"operation": "replace", "value": value},
                "Configured" if field.get("configured") else "Not configured",
                "Will be replaced",
                True,
            )

        if isinstance(requested, Mapping) or requested is None:
            raise GatewaySettingsValidationError
        if field_type == "boolean":
            if not isinstance(requested, bool):
                raise GatewaySettingsValidationError
            value: Any = requested
        elif field_type == "integer":
            if isinstance(requested, bool) or not isinstance(requested, int):
                raise GatewaySettingsValidationError
            value = requested
        elif field_type == "number":
            if isinstance(requested, bool) or not isinstance(requested, (int, float)):
                raise GatewaySettingsValidationError
            value = requested
            if not math.isfinite(float(value)):
                raise GatewaySettingsValidationError
        elif field_type in {"string", "select"}:
            if not isinstance(requested, (str, int, float)) or isinstance(requested, bool):
                raise GatewaySettingsValidationError
            value = requested
            if isinstance(value, str):
                maximum = int(field.get("max_length", MAX_SETTING_STRING_LENGTH))
                if len(value) > maximum:
                    raise GatewaySettingsValidationError
        else:
            raise GatewaySettingsValidationError

        if field_type in {"integer", "number"}:
            minimum = field.get("min")
            maximum = field.get("max")
            if minimum is not None and value < minimum:
                raise GatewaySettingsValidationError
            if maximum is not None and value > maximum:
                raise GatewaySettingsValidationError
        if field_type == "select":
            allowed = [option["value"] for option in field.get("options", [])]
            if not any(
                type(value) is type(option) and value == option
                for option in allowed
            ):
                raise GatewaySettingsValidationError
        return value, field.get("value"), value, False

    @staticmethod
    def _preview_warnings(changes: list[dict[str, Any]]) -> list[str]:
        warnings = [
            "The radio protocol has no guaranteed rollback. MeshNet applies "
            "validated changes once and verifies by reading the device again."
        ]
        if any(change["critical"] for change in changes):
            warnings.append(
                "Connection-critical changes run last and may intentionally "
                "disconnect this gateway."
            )
        if any(change["secret"] for change in changes):
            warnings.append(
                "Secret values are write-only, held briefly in memory, and "
                "never returned by this API."
            )
        return warnings

    def _cleanup_previews(self) -> None:
        now = time.monotonic()
        for preview_id in [
            preview_id
            for preview_id, preview in self._previews.items()
            if preview.expires_monotonic <= now
        ]:
            self._discard_preview(preview_id)

    def _trim_previews(self) -> None:
        overflow = len(self._previews) - MAX_PREVIEWS
        if overflow <= 0:
            return
        oldest = sorted(
            self._previews.values(), key=lambda preview: preview.expires_monotonic
        )[:overflow]
        for preview in oldest:
            self._discard_preview(preview.preview_id)

    def _expire_preview(self, preview_id: str) -> None:
        """Destroy one preview when its event-loop TTL elapses."""
        self._discard_preview(preview_id)

    def _discard_preview(self, preview_id: str) -> _Preview | None:
        preview = self._previews.pop(preview_id, None)
        if preview is not None and preview.expiry_handle is not None:
            preview.expiry_handle.cancel()
            preview.expiry_handle = None
        return preview

    def _verified_result(
        self,
        *,
        preview: _Preview,
        before: Mapping[str, Any],
        after: Mapping[str, Any] | None,
        backend_result: Any,
    ) -> dict[str, Any]:
        changed_paths = set(preview.changes)
        backend = backend_result if isinstance(backend_result, Mapping) else {}
        backend_verified = {
            path
            for path in backend.get("verified", [])
            if isinstance(path, str) and path in changed_paths
        }
        verified = set(backend_verified)
        # An adapter-provided list is authoritative.  It may deliberately
        # withhold verification when a grouped firmware command changed an
        # unedited companion field, even if the one visible requested value
        # happens to match on the generic post-read snapshot.
        backend_has_verification = isinstance(backend.get("verified"), list)
        if after is not None and not backend_has_verification:
            after_fields = self._fields_by_path(after)
            for path, requested in preview.changes.items():
                field = after_fields.get(path)
                if field is None:
                    continue
                if isinstance(requested, Mapping):
                    operation = requested.get("operation")
                    expected_configured = operation == "replace"
                    if field.get("configured") is expected_configured:
                        verified.add(path)
                elif field.get("value") == requested:
                    verified.add(path)
        unverified = sorted(changed_paths - verified)
        status = (
            "verified"
            if not unverified
            else "partially_verified"
            if verified
            else "applied_unverified"
        )
        reconnect_required = bool(backend.get("reconnect_required")) or any(
            change["requires_reconnect"] for change in preview.public_changes
        )
        warnings = self._safe_backend_warnings(backend)
        return {
            "status": status,
            "gateway_id": preview.gateway_id,
            "verified": sorted(verified),
            "unverified": unverified,
            "reconnect_required": reconnect_required,
            "warnings": warnings,
        }

    @staticmethod
    def _safe_backend_warnings(backend: Mapping[str, Any]) -> list[str]:
        """Translate exact internal warning codes; never forward backend text."""
        raw_codes = backend.get("warning_codes", backend.get("warnings", []))
        if not isinstance(raw_codes, list):
            return []
        return [
            _SAFE_BACKEND_WARNING_MESSAGES[code]
            for code in raw_codes[:16]
            if isinstance(code, str) and code in _SAFE_BACKEND_WARNING_MESSAGES
        ]

    @staticmethod
    def _connection_updates(
        backend_result: Any, *, changes: Mapping[str, Any]
    ) -> dict[str, str | None] | object:
        """Consume a narrowly typed adapter-to-config handoff.

        Verification is checked by the caller before persistence. This mapping
        is internal only and is deliberately not copied into the public apply
        result or exception text.
        """
        if (
            not isinstance(backend_result, Mapping)
            or "connection_updates" not in backend_result
        ):
            return _CONNECTION_UPDATES_ABSENT
        raw = backend_result.get("connection_updates")
        requested = changes.get("security.pin")
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"pin"}
            or not isinstance(requested, Mapping)
        ):
            return _CONNECTION_UPDATES_INVALID
        pin = raw.get("pin")
        operation = requested.get("operation")
        if pin is None and operation == "clear":
            return {"pin": None}
        if (
            not isinstance(pin, str)
            or len(pin) != 6
            or not pin.isascii()
            or not pin.isdigit()
            or not 100000 <= int(pin) <= 999999
            or operation != "replace"
            or not secrets.compare_digest(pin, str(requested.get("value", "")))
        ):
            return _CONNECTION_UPDATES_INVALID
        return {"pin": pin}
