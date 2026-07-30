"""Fail-closed orchestration for explicit Meshtastic remote administration.

This module deliberately owns no protobufs and exposes no generic radio-admin
operation.  It binds the reviewed Bluetooth client methods to a short-lived,
single-use preview.  Controller private keys, remote security configuration,
channel PSKs, and destructive commands are outside this boundary.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .const import (
    PROTOCOL_MESHTASTIC,
    REMOTE_ADMIN_WRITABLE_PATHS,
    TRANSPORT_BLUETOOTH,
)

_TARGET_RE = re.compile(r"^![0-9a-f]{8}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_PREVIEW_TTL_SECONDS = 90
_MAX_CHANGES = 32
_MAX_STRING_BYTES = 128
_QUIESCE_TIMEOUT_SECONDS = 5.0

_PUBLIC_ERROR_MESSAGES = {
    "remote_admin_gateway_not_found": "The selected gateway is unavailable",
    "remote_admin_requires_bluetooth": ("Remote administration requires a connected Meshtastic Bluetooth gateway"),
    "remote_admin_target_invalid": "Select one exact Meshtastic node ID",
    "remote_admin_unavailable": "Remote administration is unavailable",
    "remote_admin_snapshot_invalid": "The remote settings response was invalid",
    "remote_admin_revision_conflict": "Reload remote settings before continuing",
    "remote_admin_changes_invalid": "One or more remote settings changes are invalid",
    "remote_admin_preview_expired": "The remote settings preview expired",
    "remote_admin_confirmation_required": ("Confirm the remote radio write before continuing"),
    "remote_admin_target_unknown": "The selected Meshtastic node is unknown",
    "remote_admin_target_public_key_unavailable": (
        "The target public key is unavailable on the controller radio"
    ),
    "remote_admin_controller_public_key_unavailable": (
        "The controller radio public key is unavailable"
    ),
    "remote_admin_controller_unauthorized": (
        "The target does not authorize this controller radio"
    ),
    "remote_admin_session_rejected": (
        "The remote-admin session was rejected; load settings again"
    ),
    "remote_admin_no_route": "No mesh route to the selected target is available",
    "remote_admin_no_response": "The selected target did not respond",
    "remote_admin_duty_cycle_limited": (
        "The radio refused the request because of its duty-cycle limit"
    ),
    "remote_admin_rate_limited": (
        "The radio refused the request because of its rate limit"
    ),
    "remote_admin_command_forbidden": (
        "The requested remote-admin operation is not supported"
    ),
    "remote_admin_unknown_outcome": ("The remote write could not be verified; do not repeat it blindly"),
}
_PASSTHROUGH_PROVIDER_CODES = frozenset(
    {
        "remote_admin_target_invalid",
        "remote_admin_target_unknown",
        "remote_admin_target_public_key_unavailable",
        "remote_admin_controller_public_key_unavailable",
        "remote_admin_controller_unauthorized",
        "remote_admin_session_rejected",
        "remote_admin_no_route",
        "remote_admin_no_response",
        "remote_admin_duty_cycle_limited",
        "remote_admin_rate_limited",
        "remote_admin_command_forbidden",
        "remote_admin_unknown_outcome",
        "remote_admin_unavailable",
    }
)


class RemoteAdminError(RuntimeError):
    """Stable, credential-free failure returned to an administrator."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.public_message = _PUBLIC_ERROR_MESSAGES.get(
            code, "The remote-admin operation failed"
        )
        super().__init__(self.public_message)


@dataclass(slots=True)
class _CachedRemoteSnapshot:
    revision: str
    fields: dict[str, dict[str, Any]] = field(repr=False)


@dataclass(slots=True)
class _RemotePreview:
    preview_id: str
    gateway_id: str
    target_node: str
    revision: str
    paths: tuple[str, ...]
    changes: dict[str, Any] = field(repr=False)
    expires_monotonic: float
    timer: asyncio.TimerHandle | None = field(default=None, repr=False)


class RemoteAdminManager:
    """Own short-lived previews around reviewed gateway methods."""

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator
        self._revision_key = secrets.token_bytes(32)
        self._snapshots: dict[tuple[str, str], _CachedRemoteSnapshot] = {}
        self._previews: dict[str, _RemotePreview] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._accepting = True
        self._active_tasks: dict[asyncio.Task[Any], str] = {}

    async def async_get(self, gateway_id: str, target_node: str) -> dict[str, Any]:
        """Load one exact remote node and return a bounded safe projection."""
        async with self._operation(gateway_id):
            return await self._async_get(gateway_id, target_node)

    async def _async_get(self, gateway_id: str, target_node: str) -> dict[str, Any]:
        """Perform one tracked remote settings read."""
        gateway_id = self._validate_gateway_id(gateway_id)
        target_node = self._validate_target(target_node)
        gateway = self._gateway(gateway_id)
        key = (gateway_id, target_node)
        async with self._locks.setdefault(key, asyncio.Lock()):
            raw = await self._async_get_snapshot(gateway, target_node)
            public, fields = self._project_snapshot(raw, gateway_id=gateway_id, target_node=target_node)
            revision = self._revision(public)
            public["revision"] = revision
            self._snapshots[key] = _CachedRemoteSnapshot(
                revision=revision,
                fields=fields,
            )
            self._invalidate_target_previews(key)
            return public

    async def async_preview(
        self,
        gateway_id: str,
        target_node: str,
        revision: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate a value-bearing draft and retain it only for a short TTL."""
        async with self._operation(gateway_id):
            return await self._async_preview(
                gateway_id,
                target_node,
                revision,
                changes,
            )

    async def _async_preview(
        self,
        gateway_id: str,
        target_node: str,
        revision: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Perform one tracked remote preview operation."""
        gateway_id = self._validate_gateway_id(gateway_id)
        target_node = self._validate_target(target_node)
        revision = self._validate_revision(revision)
        self._gateway(gateway_id)
        key = (gateway_id, target_node)
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._snapshots.get(key)
            if cached is None or not hmac.compare_digest(cached.revision, revision):
                raise RemoteAdminError("remote_admin_revision_conflict")
            validated = self._validate_changes(changes, cached.fields)
            self._invalidate_target_previews(key)
            preview_id = secrets.token_urlsafe(32)
            loop = asyncio.get_running_loop()
            preview = _RemotePreview(
                preview_id=preview_id,
                gateway_id=gateway_id,
                target_node=target_node,
                revision=revision,
                paths=tuple(validated),
                changes=validated,
                expires_monotonic=loop.time() + _PREVIEW_TTL_SECONDS,
            )
            preview.timer = loop.call_later(_PREVIEW_TTL_SECONDS, self._expire_preview, preview_id)
            self._previews[preview_id] = preview
            labels = {path: self._safe_label(cached.fields[path].get("label"), path) for path in validated}
            return {
                "schema_version": 1,
                "preview_id": preview_id,
                "gateway_id": gateway_id,
                "target_node": target_node,
                "revision": revision,
                "changes": [{"path": path, "label": labels[path]} for path in validated],
                "requires_confirmation": True,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=_PREVIEW_TTL_SECONDS)).isoformat(),
            }

    async def async_apply(
        self,
        gateway_id: str,
        target_node: str,
        revision: str,
        preview_id: str,
        *,
        confirm_remote: bool,
    ) -> dict[str, Any]:
        """Consume one confirmed preview, write once, and require readback."""
        async with self._operation(gateway_id):
            return await self._async_apply(
                gateway_id,
                target_node,
                revision,
                preview_id,
                confirm_remote=confirm_remote,
            )

    async def _async_apply(
        self,
        gateway_id: str,
        target_node: str,
        revision: str,
        preview_id: str,
        *,
        confirm_remote: bool,
    ) -> dict[str, Any]:
        """Perform one tracked remote settings write."""
        gateway_id = self._validate_gateway_id(gateway_id)
        target_node = self._validate_target(target_node)
        revision = self._validate_revision(revision)
        if not isinstance(preview_id, str) or not 32 <= len(preview_id) <= 128:
            raise RemoteAdminError("remote_admin_preview_expired")
        if confirm_remote is not True:
            raise RemoteAdminError("remote_admin_confirmation_required")
        gateway = self._gateway(gateway_id)
        key = (gateway_id, target_node)
        async with self._locks.setdefault(key, asyncio.Lock()):
            preview = self._previews.get(preview_id)
            loop = asyncio.get_running_loop()
            if (
                preview is None
                or preview.gateway_id != gateway_id
                or preview.target_node != target_node
                or not hmac.compare_digest(preview.revision, revision)
                or preview.expires_monotonic <= loop.time()
            ):
                self._destroy_preview(preview_id)
                raise RemoteAdminError("remote_admin_preview_expired")

            # Consume before the first possible RF write. An exception after this
            # point has an unknown outcome and must never make blind retry easy.
            changes = dict(preview.changes)
            self._destroy_preview(preview_id)
            try:
                fresh_raw = await self._async_get_snapshot(gateway, target_node)
                fresh_public, fresh_fields = self._project_snapshot(
                    fresh_raw, gateway_id=gateway_id, target_node=target_node
                )
                fresh_revision = self._revision(fresh_public)
                if not hmac.compare_digest(fresh_revision, revision):
                    self._snapshots[key] = _CachedRemoteSnapshot(
                        revision=fresh_revision,
                        fields=fresh_fields,
                    )
                    raise RemoteAdminError("remote_admin_revision_conflict")
                apply_plan = getattr(gateway, "async_apply_remote_settings_plan", None)
                if not callable(apply_plan):
                    raise RemoteAdminError("remote_admin_unavailable")
                provider_result = await apply_plan(target_node, changes)
            except RemoteAdminError:
                raise
            except Exception as err:
                code = getattr(err, "code", None)
                if isinstance(code, str) and code in _PASSTHROUGH_PROVIDER_CODES:
                    raise RemoteAdminError(code) from None
                raise RemoteAdminError("remote_admin_unknown_outcome") from None

            normalized = self._normalize_apply_result(provider_result, preview.paths)
            self._snapshots.pop(key, None)
            return {
                "schema_version": 1,
                "status": normalized["status"],
                "gateway_id": gateway_id,
                "target_node": target_node,
                "verified": normalized["verified"],
                "unverified": normalized["unverified"],
            }

    def invalidate(self) -> None:
        """Destroy cached drafts and revisions during reload/unload."""
        for preview_id in tuple(self._previews):
            self._destroy_preview(preview_id)
        self._snapshots.clear()

    async def async_quiesce(self) -> bool:
        """Fence new work and cancel/drain handler-owned radio operations."""
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
            tasks,
            timeout=_QUIESCE_TIMEOUT_SECONDS,
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        return not pending

    def resume(self) -> bool:
        """Accept work after reload only when no old operation still owns RF."""
        if any(not task.done() for task in self._active_tasks):
            return False
        self._accepting = True
        return True

    @asynccontextmanager
    async def _operation(self, gateway_id: str | None) -> AsyncIterator[None]:
        """Track the current handler task so lifecycle teardown can own it."""
        if not self._accepting:
            raise RemoteAdminError("remote_admin_unavailable")
        task = asyncio.current_task()
        if task is None:
            raise RemoteAdminError("remote_admin_unavailable")
        self._active_tasks[task] = gateway_id or ""
        try:
            if not self._accepting:
                raise RemoteAdminError("remote_admin_unavailable")
            yield
        finally:
            self._active_tasks.pop(task, None)

    def _gateway(self, gateway_id: str) -> Any:
        gateways = getattr(self._coordinator, "gateways", {})
        gateway = gateways.get(gateway_id) if isinstance(gateways, Mapping) else None
        if gateway is None:
            raise RemoteAdminError("remote_admin_gateway_not_found")
        config = getattr(gateway, "config", None)
        status = getattr(gateway, "status", None)
        if (
            getattr(config, "protocol", None) != PROTOCOL_MESHTASTIC
            or getattr(config, "transport", None) != TRANSPORT_BLUETOOTH
            or getattr(status, "connected", None) is not True
        ):
            raise RemoteAdminError("remote_admin_requires_bluetooth")
        return gateway

    async def _async_get_snapshot(self, gateway: Any, target_node: str) -> Mapping[str, Any]:
        getter = getattr(gateway, "async_get_remote_settings_snapshot", None)
        if not callable(getter):
            raise RemoteAdminError("remote_admin_unavailable")
        try:
            result = await getter(target_node)
        except Exception as err:
            code = getattr(err, "code", None)
            if isinstance(code, str) and code in _PASSTHROUGH_PROVIDER_CODES:
                raise RemoteAdminError(code) from None
            raise RemoteAdminError("remote_admin_unavailable") from None
        if not isinstance(result, Mapping):
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        return result

    def _project_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        gateway_id: str,
        target_node: str,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if (
            snapshot.get("source") != "remote_radio"
            or snapshot.get("transport") != TRANSPORT_BLUETOOTH
            or snapshot.get("complete") is not True
        ):
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        controller = snapshot.get("controller")
        target = snapshot.get("target")
        if not isinstance(controller, Mapping) or not isinstance(target, Mapping):
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        controller_id = self._validate_target(controller.get("node_id"))
        target_id = self._validate_target(target.get("node_id"))
        if target_id != target_node:
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        public_key = self._public_key(controller.get("public_key"))

        fields: dict[str, dict[str, Any]] = {}
        categories: list[dict[str, Any]] = []
        raw_categories = snapshot.get("categories")
        if not isinstance(raw_categories, Sequence) or isinstance(raw_categories, (str, bytes, bytearray)):
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        for category in raw_categories:
            if not isinstance(category, Mapping):
                continue
            projected_fields: list[dict[str, Any]] = []
            source_fields = category.get("fields")
            if not isinstance(source_fields, Sequence) or isinstance(source_fields, (str, bytes, bytearray)):
                continue
            for source_field in source_fields:
                if not isinstance(source_field, Mapping):
                    continue
                path = source_field.get("path")
                if path not in REMOTE_ADMIN_WRITABLE_PATHS or source_field.get("writable") is not True:
                    continue
                public_field = self._public_field(path, source_field)
                fields[path] = public_field
                projected_fields.append(dict(public_field))
            if projected_fields:
                categories.append(
                    {
                        "key": self._safe_text(category.get("key"), 64) or "settings",
                        "label": self._safe_text(category.get("label"), 96) or "Settings",
                        "fields": projected_fields,
                    }
                )
        if not fields:
            raise RemoteAdminError("remote_admin_snapshot_invalid")

        public = {
            "schema_version": 1,
            "gateway_id": gateway_id,
            "target_node": target_node,
            "controller": {
                "node_id": controller_id,
                "short_name": self._safe_text(controller.get("short_name"), 64),
                "public_key": public_key,
                "public_key_copy_only": True,
            },
            "target": {
                "node_id": target_id,
                "long_name": self._safe_text(target.get("long_name"), 96),
                "short_name": self._safe_text(target.get("short_name"), 64),
                "public_key_available": target.get("public_key_available") is True,
                "remote_admin_eligible": target.get("remote_admin_eligible") is True,
            },
            "categories": categories,
        }
        if not public["target"]["remote_admin_eligible"]:
            raise RemoteAdminError("remote_admin_unavailable")
        return public, fields

    def _public_field(self, path: str, source: Mapping[str, Any]) -> dict[str, Any]:
        field_type = source.get("type")
        if field_type not in {"boolean", "integer", "number", "select", "string"}:
            # Protobuf enum projections may use "enum" in older cached data.
            field_type = "select" if field_type == "enum" else None
        if field_type is None:
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        value = source.get("value")
        public: dict[str, Any] = {
            "path": path,
            "label": self._safe_label(source.get("label"), path),
            "type": field_type,
            "value": self._validated_value(path, value, field_type, source),
            "writable": True,
        }
        for name in ("min", "max", "step"):
            candidate = source.get(name)
            if (
                isinstance(candidate, (int, float))
                and not isinstance(candidate, bool)
                and math.isfinite(float(candidate))
            ):
                public[name] = candidate
        options = source.get("options")
        if isinstance(options, Sequence) and not isinstance(options, (str, bytes, bytearray)):
            safe_options = []
            for option in options[:64]:
                if isinstance(option, Mapping):
                    option_value = option.get("value")
                    label = self._safe_text(option.get("label"), 96)
                    if isinstance(option_value, (str, int)) and not isinstance(option_value, bool):
                        safe_options.append({"value": option_value, "label": label or str(option_value)})
                elif isinstance(option, (str, int)) and not isinstance(option, bool):
                    safe_options.append({"value": option, "label": str(option)})
            if safe_options:
                public["options"] = safe_options
        return public

    def _validate_changes(
        self,
        changes: Mapping[str, Any],
        fields: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(changes, Mapping) or not 1 <= len(changes) <= _MAX_CHANGES:
            raise RemoteAdminError("remote_admin_changes_invalid")
        validated: dict[str, Any] = {}
        for path, value in changes.items():
            if not isinstance(path, str) or path not in REMOTE_ADMIN_WRITABLE_PATHS:
                raise RemoteAdminError("remote_admin_changes_invalid")
            field_meta = fields.get(path)
            if not isinstance(field_meta, Mapping):
                raise RemoteAdminError("remote_admin_changes_invalid")
            try:
                replacement = self._validated_value(path, value, field_meta.get("type"), field_meta)
            except RemoteAdminError:
                raise
            except Exception:
                raise RemoteAdminError("remote_admin_changes_invalid") from None
            if replacement == field_meta.get("value"):
                continue
            validated[path] = replacement
        if not validated:
            raise RemoteAdminError("remote_admin_changes_invalid")
        return validated

    def _validated_value(
        self,
        path: str,
        value: Any,
        field_type: Any,
        field_meta: Mapping[str, Any],
    ) -> Any:
        if field_type == "boolean":
            if not isinstance(value, bool):
                raise RemoteAdminError("remote_admin_changes_invalid")
            return value
        if field_type == "string":
            if not isinstance(value, str):
                raise RemoteAdminError("remote_admin_changes_invalid")
            byte_length = len(value.encode("utf-8"))
            maximum = 4 if path == "owner.short_name" else 40 if path == "owner.long_name" else _MAX_STRING_BYTES
            if not 1 <= byte_length <= maximum or any(ord(character) < 32 for character in value):
                raise RemoteAdminError("remote_admin_changes_invalid")
            return value
        if field_type in {"integer", "number"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RemoteAdminError("remote_admin_changes_invalid")
            if not math.isfinite(float(value)):
                raise RemoteAdminError("remote_admin_changes_invalid")
            if field_type == "integer" and not isinstance(value, int):
                raise RemoteAdminError("remote_admin_changes_invalid")
            minimum = field_meta.get("min")
            maximum = field_meta.get("max")
            if isinstance(minimum, (int, float)) and value < minimum:
                raise RemoteAdminError("remote_admin_changes_invalid")
            if isinstance(maximum, (int, float)) and value > maximum:
                raise RemoteAdminError("remote_admin_changes_invalid")
            return value
        if field_type == "select":
            options = field_meta.get("options", ())
            allowed = {option.get("value") if isinstance(option, Mapping) else option for option in options}
            if value not in allowed:
                raise RemoteAdminError("remote_admin_changes_invalid")
            return value
        raise RemoteAdminError("remote_admin_changes_invalid")

    def _revision(self, public: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            public,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        return hmac.new(self._revision_key, canonical, hashlib.sha256).hexdigest()

    @staticmethod
    def _normalize_apply_result(result: Any, expected_paths: Sequence[str]) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise RemoteAdminError("remote_admin_unknown_outcome")
        status = result.get("status")
        if status not in {"verified", "readback_mismatch"}:
            raise RemoteAdminError("remote_admin_unknown_outcome")
        expected = set(expected_paths)
        verified = result.get("verified", ())
        unverified = result.get("unverified", ())
        if not isinstance(verified, Sequence) or isinstance(verified, str):
            raise RemoteAdminError("remote_admin_unknown_outcome")
        if not isinstance(unverified, Sequence) or isinstance(unverified, str):
            raise RemoteAdminError("remote_admin_unknown_outcome")
        safe_verified = [path for path in verified if path in expected]
        safe_unverified = [path for path in unverified if path in expected]
        if set(safe_verified) | set(safe_unverified) != expected:
            raise RemoteAdminError("remote_admin_unknown_outcome")
        if status == "verified" and safe_unverified:
            raise RemoteAdminError("remote_admin_unknown_outcome")
        return {
            "status": status,
            "verified": safe_verified,
            "unverified": safe_unverified,
        }

    @staticmethod
    def _validate_gateway_id(value: Any) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip():
            raise RemoteAdminError("remote_admin_gateway_not_found")
        return value

    @staticmethod
    def _validate_target(value: Any) -> str:
        if not isinstance(value, str) or _TARGET_RE.fullmatch(value) is None or value in {"!00000000", "!ffffffff"}:
            raise RemoteAdminError("remote_admin_target_invalid")
        return value

    @staticmethod
    def _validate_revision(value: Any) -> str:
        if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
            raise RemoteAdminError("remote_admin_revision_conflict")
        return value

    @staticmethod
    def _public_key(value: Any) -> str:
        if not isinstance(value, str) or not value.startswith("base64:"):
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        encoded = value.split(":", 1)[1]
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise RemoteAdminError("remote_admin_snapshot_invalid") from None
        if len(decoded) != 32:
            raise RemoteAdminError("remote_admin_snapshot_invalid")
        return "base64:" + base64.b64encode(decoded).decode()

    @staticmethod
    def _safe_text(value: Any, maximum: int) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or len(text.encode("utf-8")) > maximum or any(ord(character) < 32 for character in text):
            return None
        return text

    @classmethod
    def _safe_label(cls, value: Any, path: str) -> str:
        label = cls._safe_text(value, 96)
        if label is not None:
            return label
        return path.rsplit(".", 1)[-1].replace("_", " ").title()

    def _invalidate_target_previews(self, key: tuple[str, str]) -> None:
        for preview_id, preview in tuple(self._previews.items()):
            if (preview.gateway_id, preview.target_node) == key:
                self._destroy_preview(preview_id)

    def _expire_preview(self, preview_id: str) -> None:
        self._destroy_preview(preview_id)

    def _destroy_preview(self, preview_id: str) -> None:
        preview = self._previews.pop(preview_id, None)
        if preview is not None and preview.timer is not None:
            preview.timer.cancel()
            preview.timer = None
