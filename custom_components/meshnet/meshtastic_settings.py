"""Privacy-safe local Meshtastic radio settings support.

Meshtastic sends the local radio configuration as protobuf records during a
``want_config`` exchange.  This module retains detached protobuf copies in
memory and exposes a deliberately smaller, JSON-safe projection for the
Home Assistant UI.  Credential material is never included in that projection.

The write planner builds the same ``AdminMessage`` transaction used by the
official clients.  A transport may enable sending only when it can correlate
every response and verify a newly downloaded post-reboot configuration.
"""

from __future__ import annotations

import base64
import binascii
import copy
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_MAX_STRING_LENGTH = 1024
_MAX_REPEATED_VALUES = 128
_MAX_NESTING_DEPTH = 8
_MAX_PENDING_NODE_USERS = 512

# Matching is intentionally broad.  False positives produce a configured
# boolean, which is preferable to putting an unknown future credential in the
# browser, diagnostics, logs, or websocket trace.
_SECRET_FIELD_PARTS = frozenset(
    {
        "admin_key",
        "api_key",
        "auth",
        "cert",
        "certificate",
        "credential",
        "encryption_key",
        "password",
        "passphrase",
        "private_key",
        "psk",
        "public_key",
        "root_ca",
        "secret",
        "token",
        "username",
    }
)
_IDENTITY_READ_ONLY_FIELDS = frozenset(
    {
        "id",
        "macaddr",
        "mac_address",
        "serial_number",
    }
)
_METADATA_ALLOWLIST = frozenset(
    {
        "can_shutdown",
        "device_state_version",
        "excluded_modules",
        "firmware_version",
        "has_bluetooth",
        "has_eth",
        "has_ethernet",
        "has_pkc",
        "has_remote_hardware",
        "has_wifi",
        "hw_model",
        "position_flags",
        "role",
    }
)
_OWNER_ALLOWLIST = frozenset(
    {
        "is_licensed",
        "is_unmessagable",
        "long_name",
        "short_name",
    }
)
_MY_INFO_ALLOWLIST = frozenset(
    {
        "max_channels",
        "min_app_version",
        "reboot_count",
    }
)
# Every setter replaces a complete protobuf section.  Only fields with a
# deliberately reviewed, path-specific contract are writable.  New firmware
# fields therefore fail closed until their semantics and recovery behavior are
# understood.
_WRITABLE_SETTINGS_PATHS = frozenset(
    {
        "owner.long_name",
        "owner.short_name",
        "config.bluetooth.fixed_pin",
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
    }
)
_PATH_COMPONENT_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*\Z")


class MeshtasticSettingsError(ValueError):
    """Base class for a rejected settings request."""


class MeshtasticSettingsValidationError(MeshtasticSettingsError):
    """A settings path or replacement value was invalid."""


class MeshtasticSettingsStaleError(MeshtasticSettingsError):
    """A plan no longer refers to the current captured configuration."""


class _PrivateSecretRevisionMaterial(dict[str, Any]):
    """Internal mapping whose accidental representation cannot reveal secrets."""

    def __repr__(self) -> str:
        return "<redacted secret revision material>"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class SettingsOperation:
    """One internal AdminMessage operation with a sanitized description."""

    operation: str
    paths: tuple[str, ...] = ()
    connection_critical: bool = False
    message: Any = field(default=None, repr=False, compare=False)

    def public_summary(self) -> dict[str, Any]:
        """Return metadata that cannot expose a replacement value."""
        return {
            "operation": self.operation,
            "paths": list(self.paths),
            "connection_critical": self.connection_critical,
        }


@dataclass(frozen=True, slots=True)
class SettingsPlan:
    """A validated, ordered local-radio settings transaction."""

    revision: int
    transport: str
    changed_paths: tuple[str, ...]
    connection_critical_paths: tuple[str, ...]
    operations: tuple[SettingsOperation, ...] = field(repr=False, compare=False)
    blocked_paths: Mapping[str, str] = field(default_factory=dict)

    @property
    def can_apply(self) -> bool:
        """Return whether the plan has changes and no field-level block."""
        return bool(self.changed_paths) and not self.blocked_paths

    def public_summary(self) -> dict[str, Any]:
        """Return a credential-free plan summary."""
        return {
            "revision": self.revision,
            "transport": self.transport,
            "changed_paths": list(self.changed_paths),
            "connection_critical_paths": list(self.connection_critical_paths),
            "blocked_paths": dict(self.blocked_paths),
            "operations": [item.public_summary() for item in self.operations],
        }

    def read_only_result(self, reason: str) -> dict[str, Any]:
        """Return an explicit non-success result without any submitted values."""
        blocked = dict(self.blocked_paths)
        for path in self.changed_paths:
            blocked.setdefault(path, reason)
        return {
            "success": False,
            "status": "read_only",
            "reason": reason,
            "applied_paths": [],
            "verified": False,
            "blocked_paths": blocked,
            "connection_critical_paths": list(self.connection_critical_paths),
        }


def unavailable_settings_snapshot(*, transport: str, reason: str) -> dict[str, Any]:
    """Return the stable empty snapshot used when local settings are unavailable."""
    return {
        "available": False,
        "complete": False,
        "source": "local_radio",
        "transport": transport,
        "revision": 0,
        "capabilities": {
            "read": False,
            "plan": False,
            "apply": False,
            "apply_reason": reason,
            "transactional": False,
            "verification": False,
        },
        "categories": [],
    }


def _clone_message(value: Any) -> Any:
    """Return a detached protobuf/mapping copy."""
    copier = getattr(value, "CopyFrom", None)
    if callable(copier):
        cloned = type(value)()
        cloned.CopyFrom(value)
        return cloned
    return copy.deepcopy(value)


def _messages_equal(left: Any, right: Any) -> bool:
    """Compare complete protobuf sections without projecting their contents."""
    left_serialize = getattr(left, "SerializeToString", None)
    right_serialize = getattr(right, "SerializeToString", None)
    if callable(left_serialize) and callable(right_serialize):
        try:
            return left_serialize(deterministic=True) == right_serialize(
                deterministic=True
            )
        except TypeError:
            return left_serialize() == right_serialize()
    return left == right


def _field_name(field_descriptor: Any) -> str:
    name = getattr(field_descriptor, "name", "")
    return name if isinstance(name, str) else ""


def _message_fields(message: Any) -> tuple[Any, ...]:
    descriptor = getattr(message, "DESCRIPTOR", None)
    fields = getattr(descriptor, "fields", ())
    return tuple(fields) if isinstance(fields, Sequence) else tuple(fields or ())


def _field_by_name(message: Any, name: str) -> Any | None:
    descriptor = getattr(message, "DESCRIPTOR", None)
    fields_by_name = getattr(descriptor, "fields_by_name", None)
    if isinstance(fields_by_name, Mapping):
        return fields_by_name.get(name)
    for field_descriptor in _message_fields(message):
        if _field_name(field_descriptor) == name:
            return field_descriptor
    return None


def _is_repeated(field_descriptor: Any) -> bool:
    """Support both upb descriptors and legacy Python descriptors."""
    repeated = getattr(field_descriptor, "is_repeated", None)
    if isinstance(repeated, bool):
        return repeated
    return int(getattr(field_descriptor, "label", 0) or 0) == 3


def _which_payload(message: Any) -> str | None:
    which_oneof = getattr(message, "WhichOneof", None)
    if not callable(which_oneof):
        return None
    descriptor = getattr(message, "DESCRIPTOR", None)
    oneofs = getattr(descriptor, "oneofs", ())
    names = [getattr(oneof, "name", "") for oneof in oneofs]
    for name in ("payload_variant", "payload", "variant", *names):
        if not name:
            continue
        try:
            selected = which_oneof(name)
        except ValueError:
            continue
        if isinstance(selected, str) and selected:
            return selected
    return None


def _has_field(message: Any, name: str) -> bool:
    has_field = getattr(message, "HasField", None)
    if callable(has_field):
        try:
            return bool(has_field(name))
        except (ValueError, TypeError):
            pass
    selected = _which_payload(message)
    if selected is not None:
        return selected == name
    value = getattr(message, name, None)
    if value is None:
        return False
    list_fields = getattr(value, "ListFields", None)
    if callable(list_fields):
        return bool(list_fields())
    return bool(value)


def _is_secret_name(name: str) -> bool:
    normalized = name.casefold()
    if (
        normalized == "key"
        or normalized.endswith("_key")
        or normalized == "pin"
        or normalized.endswith("_pin")
        or normalized.startswith("pin_")
    ):
        return True
    return any(part in normalized for part in _SECRET_FIELD_PARTS)


def _snake_name(name: str) -> str:
    """Normalize SDK mapping keys to protobuf-style snake case."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).casefold()


def _configured(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes, bytearray, memoryview, Mapping, Sequence)):
        return bool(value)
    list_fields = getattr(value, "ListFields", None)
    if callable(list_fields):
        return bool(list_fields())
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return True


def _revision_material_bytes(value: Any) -> bytes:
    """Encode a non-scalar secret without hashing or exposing it publicly."""
    serializer = getattr(value, "SerializeToString", None)
    if callable(serializer):
        try:
            return b"p" + serializer(deterministic=True)
        except TypeError:
            return b"p" + serializer()
    if isinstance(value, Mapping):
        encoded = bytearray(b"m")
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key).encode("utf-8")
            item_bytes = _revision_material_bytes(item)
            encoded.extend(len(key).to_bytes(4, "big"))
            encoded.extend(key)
            encoded.extend(len(item_bytes).to_bytes(4, "big"))
            encoded.extend(item_bytes)
        return bytes(encoded)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        encoded = bytearray(b"l")
        for item in value:
            item_bytes = _revision_material_bytes(item)
            encoded.extend(len(item_bytes).to_bytes(4, "big"))
            encoded.extend(item_bytes)
        return bytes(encoded)
    if value is None:
        return b"n"
    if isinstance(value, bool):
        return b"t" if value else b"f"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii")
    if isinstance(value, str):
        return b"s" + value.encode("utf-8")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"b" + bytes(value)
    # An unsupported future SDK value deliberately exceeds the manager's
    # private material bound so the settings snapshot fails closed.
    return b"x" * 2049


def _enum_name(field_descriptor: Any, value: int) -> str | int:
    enum_type = getattr(field_descriptor, "enum_type", None)
    values_by_number = getattr(enum_type, "values_by_number", None)
    try:
        enum_value = values_by_number[value]
    except (KeyError, TypeError):
        enum_value = None
    name = getattr(enum_value, "name", None)
    if isinstance(name, str):
        return name
    return value


def _enum_options(field_descriptor: Any) -> list[str]:
    enum_type = getattr(field_descriptor, "enum_type", None)
    values = getattr(enum_type, "values", ())
    return [
        name
        for enum_value in values or ()
        if isinstance((name := getattr(enum_value, "name", None)), str)
    ]


def _select_options(field_descriptor: Any) -> list[dict[str, str]]:
    return [
        {"value": name, "label": _friendly_label(name)}
        for name in _enum_options(field_descriptor)
    ]


def _json_scalar(field_descriptor: Any, value: Any) -> Any:
    field_type = int(getattr(field_descriptor, "type", 0) or 0)
    if field_type == 14 and isinstance(value, int):
        return _enum_name(field_descriptor, value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_MAX_STRING_LENGTH]
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Unknown byte fields are treated as credentials by the caller.  This
        # fallback exists solely to make the function total and never emits it.
        return None
    return None


def _field_type(field_descriptor: Any, *, secret: bool) -> str:
    if secret:
        return "secret"
    field_type = int(getattr(field_descriptor, "type", 0) or 0)
    if field_type == 8:
        return "boolean"
    if field_type == 9:
        return "string"
    if field_type == 12:
        return "secret"
    if field_type == 14:
        return "select"
    if field_type in {1, 2}:
        return "number"
    if field_type in {3, 4, 5, 6, 7, 13, 15, 16, 17, 18}:
        return "integer"
    return "object"


def _friendly_label(name: str) -> str:
    return name.replace("_", " ").strip().title()


def _read_only_reason(
    path: str,
    field_name: str,
    *,
    metadata: bool,
    transport: str,
) -> str | None:
    if metadata:
        return "radio_metadata_is_read_only"
    if field_name in _IDENTITY_READ_ONLY_FIELDS:
        return "hardware_identity_is_read_only"
    if path.startswith("channel.") and field_name == "index":
        return "channel_index_is_selected_by_category"
    # set_config.security replaces the complete security record, including
    # private/admin keys, and can permanently enable managed mode. Even an
    # apparently harmless field therefore needs a separate recovery workflow.
    if path.startswith("config.security."):
        return "security_settings_require_a_recovery_workflow"
    if transport.casefold() == "bluetooth":
        # Firmware can disable Bluetooth immediately for these setters, before
        # commit/readback can establish what was applied.
        if path.startswith(("module.mqtt.", "module.serial.")):
            return "this_module_can_disable_the_active_bluetooth_transport"
        if path == "config.bluetooth.enabled":
            return "the_active_bluetooth_transport_cannot_disable_itself"
        if path == "config.display.displaymode":
            return "display_mode_can_disable_bluetooth_on_supported_hardware"
    if path not in _WRITABLE_SETTINGS_PATHS:
        return "setting_requires_dedicated_semantic_validation"
    return None


def _project_message_fields(
    message: Any,
    *,
    prefix: str,
    metadata: bool = False,
    allowed_root_fields: frozenset[str] | None = None,
    write_supported: bool,
    transport: str,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Flatten one protobuf message into bounded UI field records."""
    if depth > _MAX_NESTING_DEPTH:
        return []
    result: list[dict[str, Any]] = []
    if isinstance(message, Mapping):
        for raw_key, value in sorted(message.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_key, str):
                continue
            key = _snake_name(raw_key)
            if _PATH_COMPONENT_RE.fullmatch(key) is None:
                continue
            if depth == 0 and allowed_root_fields is not None and key not in allowed_root_fields:
                continue
            path = f"{prefix}.{key}"
            secret = _is_secret_name(key) or isinstance(value, (bytes, bytearray))
            reason = _read_only_reason(
                path,
                key,
                metadata=metadata,
                transport=transport,
            )
            field_value: dict[str, Any] = {
                "path": path,
                "key": key,
                "label": _friendly_label(key),
                "type": "secret" if secret else "string",
                "writable": bool(write_supported and reason is None),
            }
            if reason is not None:
                field_value["read_only_reason"] = reason
            elif not write_supported:
                field_value["read_only_reason"] = (
                    "confirmed_admin_write_and_verification_not_available"
                )
            if secret:
                field_value["configured"] = _configured(value)
                field_value["allow_clear"] = reason is None
            elif isinstance(value, Mapping):
                result.extend(
                    _project_message_fields(
                        value,
                        prefix=path,
                        metadata=metadata,
                        write_supported=write_supported,
                        transport=transport,
                        depth=depth + 1,
                    )
                )
                continue
            elif isinstance(value, (bool, int, float, str)):
                field_value["value"] = value
            else:
                continue
            result.append(field_value)
        return result

    for field_descriptor in _message_fields(message):
        name = _field_name(field_descriptor)
        if not name or _PATH_COMPONENT_RE.fullmatch(name) is None:
            continue
        if depth == 0 and allowed_root_fields is not None and name not in allowed_root_fields:
            continue
        path = f"{prefix}.{name}"
        value = getattr(message, name)
        field_type_number = int(getattr(field_descriptor, "type", 0) or 0)
        repeated = _is_repeated(field_descriptor)
        is_message = field_type_number in {10, 11}
        if is_message and not repeated:
            if _has_field(message, name):
                result.extend(
                    _project_message_fields(
                        value,
                        prefix=path,
                        metadata=metadata,
                        write_supported=write_supported,
                        transport=transport,
                        depth=depth + 1,
                    )
                )
            continue

        secret = _is_secret_name(name) or field_type_number == 12
        reason = _read_only_reason(
            path,
            name,
            metadata=metadata,
            transport=transport,
        )
        projected: dict[str, Any] = {
            "path": path,
            "key": name,
            "label": _friendly_label(name),
            "type": _field_type(field_descriptor, secret=secret),
            "writable": bool(write_supported and reason is None),
        }
        if repeated:
            projected["multiple"] = True
        if projected["type"] == "select":
            projected["options"] = _select_options(field_descriptor)
        if reason is not None:
            projected["read_only_reason"] = reason
        elif not write_supported:
            projected["read_only_reason"] = (
                "confirmed_admin_write_and_verification_not_available"
            )

        if secret:
            projected["configured"] = _configured(value)
            projected["allow_clear"] = reason is None
        elif repeated:
            values = list(value)[:_MAX_REPEATED_VALUES]
            if is_message:
                # Repeated messages can contain future credentials.  Their
                # contents are not useful to a generic editor, so disclose only
                # presence until a specific schema is implemented.
                projected["type"] = "boolean"
                projected["value"] = bool(values)
            else:
                projected["type"] = "string"
                projected["value"] = ", ".join(
                    str(_json_scalar(field_descriptor, item)) for item in values
                )[:_MAX_STRING_LENGTH]
        else:
            projected["value"] = _json_scalar(field_descriptor, value)
        if projected["type"] in {"string", "secret"}:
            projected["max_length"] = _MAX_STRING_LENGTH
        numeric_bounds = _integer_bounds(field_type_number)
        if numeric_bounds is not None and projected["type"] != "secret":
            safe_limit = 2**53 - 1
            projected["min"] = max(numeric_bounds[0], -safe_limit)
            projected["max"] = min(numeric_bounds[1], safe_limit)
        if path == "owner.short_name":
            projected["max_length"] = 4
        elif path == "owner.long_name":
            projected["max_length"] = 40
        elif path.endswith(".fixed_pin"):
            projected["max_length"] = 6
        elif path.endswith(".settings.psk"):
            projected["max_length"] = 44
        result.append(projected)
    return result


def _secret_revision_material(
    message: Any,
    *,
    prefix: str,
    allowed_root_fields: frozenset[str] | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    """Return private raw material for secret fields in the public projection."""
    if depth > _MAX_NESTING_DEPTH:
        return {}
    material: dict[str, Any] = {}
    if isinstance(message, Mapping):
        for raw_key, value in sorted(message.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_key, str):
                continue
            key = _snake_name(raw_key)
            if _PATH_COMPONENT_RE.fullmatch(key) is None:
                continue
            if depth == 0 and allowed_root_fields is not None and key not in allowed_root_fields:
                continue
            path = f"{prefix}.{key}"
            if _is_secret_name(key) or isinstance(value, (bytes, bytearray, memoryview)):
                if isinstance(value, (type(None), bool, int, str)):
                    material[path] = value
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    material[path] = bytes(value)
                else:
                    material[path] = _revision_material_bytes(value)
            elif isinstance(value, Mapping):
                material.update(
                    _secret_revision_material(
                        value,
                        prefix=path,
                        depth=depth + 1,
                    )
                )
        return material

    for field_descriptor in _message_fields(message):
        name = _field_name(field_descriptor)
        if not name or _PATH_COMPONENT_RE.fullmatch(name) is None:
            continue
        if depth == 0 and allowed_root_fields is not None and name not in allowed_root_fields:
            continue
        path = f"{prefix}.{name}"
        value = getattr(message, name)
        field_type_number = int(getattr(field_descriptor, "type", 0) or 0)
        repeated = _is_repeated(field_descriptor)
        if field_type_number in {10, 11} and not repeated:
            if _has_field(message, name):
                material.update(
                    _secret_revision_material(
                        value,
                        prefix=path,
                        depth=depth + 1,
                    )
                )
            continue
        if not (_is_secret_name(name) or field_type_number == 12):
            continue
        if isinstance(value, (type(None), bool, int, str)):
            material[path] = value
        elif isinstance(value, (bytes, bytearray, memoryview)):
            material[path] = bytes(value)
        else:
            material[path] = _revision_material_bytes(value)
    return material


def _category(
    key: str,
    message: Any,
    *,
    write_supported: bool,
    metadata: bool = False,
    allowed_root_fields: frozenset[str] | None = None,
    transport: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": _friendly_label(key.rsplit(".", 1)[-1]),
        "fields": _project_message_fields(
            message,
            prefix=key,
            metadata=metadata,
            allowed_root_fields=allowed_root_fields,
            write_supported=write_supported,
            transport=transport,
        ),
    }


def _section_from_wrapper(wrapper: Any) -> tuple[str, Any] | None:
    selected = _which_payload(wrapper)
    if selected and hasattr(wrapper, selected):
        return selected, _clone_message(getattr(wrapper, selected))
    populated: list[tuple[str, Any]] = []
    for field_descriptor in _message_fields(wrapper):
        name = _field_name(field_descriptor)
        if name and _has_field(wrapper, name):
            populated.append((name, _clone_message(getattr(wrapper, name))))
    return populated[0] if len(populated) == 1 else None


def _connection_critical(path: str, transport: str) -> bool:
    normalized = transport.casefold()
    if path.startswith("config.security."):
        return True
    if normalized == "bluetooth":
        return path.startswith("config.bluetooth.") or path.startswith(
            ("module.mqtt.", "module.serial.")
        )
    if normalized == "tcp":
        return path.startswith("config.network.")
    if normalized == "serial":
        return path.startswith("module.serial.")
    return False


def _critical_change(path: str, transport: str) -> bool:
    """Return whether a change can sever local or mesh reachability."""
    return _connection_critical(path, transport) or path.startswith(
        ("channel.", "config.lora.")
    )


def _decode_bytes(path: str, value: Any) -> bytes:
    if not isinstance(value, str):
        raise MeshtasticSettingsValidationError(
            f"{path} requires a base64-encoded string"
        )
    encoded = value[7:] if value.startswith("base64:") else value
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise MeshtasticSettingsValidationError(
            f"{path} requires valid base64"
        ) from err


def _integer_bounds(field_type: int) -> tuple[int, int] | None:
    return {
        3: (-(2**63), 2**63 - 1),
        4: (0, 2**64 - 1),
        5: (-(2**31), 2**31 - 1),
        6: (0, 2**64 - 1),
        7: (0, 2**32 - 1),
        13: (0, 2**32 - 1),
        15: (-(2**31), 2**31 - 1),
        16: (-(2**63), 2**63 - 1),
        17: (-(2**31), 2**31 - 1),
        18: (-(2**63), 2**63 - 1),
    }.get(field_type)


def _normalized_replacement(
    path: str,
    field_descriptor: Any,
    request: Any,
    *,
    singular: bool = False,
    secret_authorized: bool = False,
) -> Any:
    """Validate one plain or secret-operation replacement without echoing it."""
    secret = _is_secret_name(_field_name(field_descriptor)) or int(
        getattr(field_descriptor, "type", 0) or 0
    ) == 12
    repeated = _is_repeated(field_descriptor)
    if isinstance(request, Mapping) and "operation" in request:
        operation = request.get("operation")
        if not secret:
            raise MeshtasticSettingsValidationError(
                f"{path} does not accept a secret operation"
            )
        if operation == "clear":
            if set(request) != {"operation"}:
                raise MeshtasticSettingsValidationError(
                    f"{path} clear operation has unexpected fields"
                )
            if repeated and not singular:
                request = []
            else:
                clear_type = int(getattr(field_descriptor, "type", 0) or 0)
                if clear_type == 9:
                    request = ""
                elif clear_type == 12:
                    request = b""
                elif clear_type == 8:
                    request = False
                else:
                    request = 0
            secret_authorized = True
        elif operation == "replace":
            if set(request) != {"operation", "value"}:
                raise MeshtasticSettingsValidationError(
                    f"{path} replace operation has unexpected fields"
                )
            request = request.get("value")
            secret_authorized = True
        else:
            raise MeshtasticSettingsValidationError(
                f"{path} has an unsupported secret operation"
            )
    elif secret and not secret_authorized:
        raise MeshtasticSettingsValidationError(
            f"{path} requires an explicit replace or clear operation"
        )

    field_type = int(getattr(field_descriptor, "type", 0) or 0)
    if repeated and not singular:
        if not isinstance(request, list) or len(request) > _MAX_REPEATED_VALUES:
            raise MeshtasticSettingsValidationError(f"{path} requires a bounded list")
        return [
            _normalized_replacement(
                path,
                field_descriptor,
                item,
                singular=True,
                secret_authorized=secret_authorized,
            )
            for item in request
        ]
    if field_type == 8:
        if not isinstance(request, bool):
            raise MeshtasticSettingsValidationError(f"{path} requires a boolean")
        return request
    if field_type == 9:
        if not isinstance(request, str) or len(request) > _MAX_STRING_LENGTH:
            raise MeshtasticSettingsValidationError(f"{path} requires a bounded string")
        return request
    if field_type == 12:
        if request == b"":
            return b""
        return _decode_bytes(path, request)
    if field_type == 14:
        values_by_name = getattr(
            getattr(field_descriptor, "enum_type", None), "values_by_name", {}
        )
        values_by_number = getattr(
            getattr(field_descriptor, "enum_type", None), "values_by_number", {}
        )
        if isinstance(request, str) and request in values_by_name:
            return int(values_by_name[request].number)
        if isinstance(request, int) and not isinstance(request, bool) and request in values_by_number:
            return request
        raise MeshtasticSettingsValidationError(f"{path} requires a known enum value")
    bounds = _integer_bounds(field_type)
    if bounds is not None:
        if secret and secret_authorized and isinstance(request, str):
            if not re.fullmatch(r"-?[0-9]+", request):
                raise MeshtasticSettingsValidationError(
                    f"{path} requires a decimal integer"
                )
            request = int(request)
        if not isinstance(request, int) or isinstance(request, bool):
            raise MeshtasticSettingsValidationError(f"{path} requires an integer")
        if not bounds[0] <= request <= bounds[1]:
            raise MeshtasticSettingsValidationError(f"{path} is outside its numeric range")
        return request
    if field_type in {1, 2}:
        if not isinstance(request, (int, float)) or isinstance(request, bool):
            raise MeshtasticSettingsValidationError(f"{path} requires a number")
        result = float(request)
        if not math.isfinite(result):
            raise MeshtasticSettingsValidationError(f"{path} requires a finite number")
        return result
    raise MeshtasticSettingsValidationError(f"{path} is not generically editable")


def _set_message_path(message: Any, components: Sequence[str], request: Any, path: str) -> None:
    if not components:
        raise MeshtasticSettingsValidationError(f"{path} is incomplete")
    current = message
    for component in components[:-1]:
        field_descriptor = _field_by_name(current, component)
        if field_descriptor is None or int(getattr(field_descriptor, "type", 0) or 0) not in {10, 11}:
            raise MeshtasticSettingsValidationError(f"{path} is not a known settings path")
        if _is_repeated(field_descriptor):
            raise MeshtasticSettingsValidationError(f"{path} cannot address a repeated message")
        current = getattr(current, component)
    name = components[-1]
    field_descriptor = _field_by_name(current, name)
    if field_descriptor is None:
        raise MeshtasticSettingsValidationError(f"{path} is not a known settings path")
    normalized = _normalized_replacement(path, field_descriptor, request)
    if path == "owner.short_name" and (
        not isinstance(normalized, str)
        or not normalized.strip()
        or len(normalized.encode("utf-8")) > 4
    ):
        raise MeshtasticSettingsValidationError(
            "owner.short_name must contain one to four characters"
        )
    if path == "owner.long_name" and (
        not isinstance(normalized, str)
        or not normalized.strip()
        or len(normalized.encode("utf-8")) > 40
    ):
        raise MeshtasticSettingsValidationError(
            "owner.long_name must contain one to forty characters"
        )
    if path.endswith(".fixed_pin"):
        explicit_clear = (
            isinstance(request, Mapping) and request.get("operation") == "clear"
        )
        if (normalized == 0 and not explicit_clear) or (
            normalized != 0 and normalized not in range(100000, 1000000)
        ):
            raise MeshtasticSettingsValidationError(
                f"{path} requires a six-digit PIN"
            )
    if path.endswith(".settings.psk") and len(normalized) not in {0, 1, 16, 32}:
        raise MeshtasticSettingsValidationError(
            f"{path} requires an empty, preset, 128-bit, or 256-bit key"
        )
    if _is_repeated(field_descriptor):
        target = getattr(current, name)
        del target[:]
        target.extend(normalized)
    else:
        setattr(current, name, normalized)


def _admin_message(factory: Callable[[], Any], field_name: str, payload: Any = True) -> Any:
    message = factory()
    target = getattr(message, field_name)
    copy_from = getattr(target, "CopyFrom", None)
    if callable(copy_from):
        copy_from(payload)
    else:
        setattr(message, field_name, payload)
    return message


class MeshtasticSettingsState:
    """In-memory settings records for one physically connected radio."""

    def __init__(self) -> None:
        self._revision = 0
        self._complete = False
        self._configs: dict[str, Any] = {}
        self._modules: dict[str, Any] = {}
        self._channels: dict[int, Any] = {}
        self._owner: Any | None = None
        self._metadata: Any | None = None
        self._my_info: Any | None = None
        self._device_ui: Any | None = None
        self._local_node_num: int | None = None
        self._pending_node_users: dict[int, Any] = {}

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def complete(self) -> bool:
        """Return whether the most recent want_config stream completed."""
        return self._complete

    @property
    def managed(self) -> bool:
        """Return whether firmware rejects local administrative changes."""
        security = self._configs.get("security")
        return bool(
            security is not None and getattr(security, "is_managed", False)
        )

    def admin_channel_index(self) -> int:
        """Return the configured admin channel, matching the official client."""
        for index, channel in sorted(self._channels.items()):
            settings = getattr(channel, "settings", None)
            name = getattr(settings, "name", "")
            if isinstance(name, str) and name.casefold() == "admin":
                return index
        return 0

    def hop_limit(self) -> int:
        """Return a bounded current hop limit for official-style packets."""
        lora = self._configs.get("lora")
        value = getattr(lora, "hop_limit", 0) if lora is not None else 0
        return int(value) if isinstance(value, int) and 0 <= value <= 7 else 0

    def begin_refresh(self) -> None:
        """Discard the prior partial download before one new want_config."""
        self._configs.clear()
        self._modules.clear()
        self._channels.clear()
        self._owner = None
        self._metadata = None
        self._my_info = None
        self._device_ui = None
        self._local_node_num = None
        self._pending_node_users.clear()
        self._complete = False
        self._revision += 1

    def mark_complete(self) -> None:
        if not self._complete:
            self._complete = True
            self._revision += 1

    def capture_from_radio(self, from_radio: Any, *, my_node_num: int | None) -> None:
        """Capture only local configuration-bearing FromRadio variants."""
        changed = False
        if _has_field(from_radio, "config"):
            section = _section_from_wrapper(from_radio.config)
            if section is not None:
                self._configs[section[0]] = section[1]
                changed = True
        module_field = "moduleConfig" if hasattr(from_radio, "moduleConfig") else "module_config"
        if _has_field(from_radio, module_field):
            section = _section_from_wrapper(getattr(from_radio, module_field))
            if section is not None:
                self._modules[section[0]] = section[1]
                changed = True
        if _has_field(from_radio, "channel"):
            channel = _clone_message(from_radio.channel)
            index = int(getattr(channel, "index", len(self._channels)))
            self._channels[index] = channel
            changed = True
        if _has_field(from_radio, "metadata"):
            self._metadata = _clone_message(from_radio.metadata)
            changed = True
        if _has_field(from_radio, "my_info"):
            self._my_info = _clone_message(from_radio.my_info)
            candidate_node_num = getattr(from_radio.my_info, "my_node_num", None)
            if isinstance(candidate_node_num, int) and candidate_node_num >= 0:
                self._local_node_num = candidate_node_num
            changed = True
        device_ui_field = "deviceuiConfig" if hasattr(from_radio, "deviceuiConfig") else "device_ui_config"
        if _has_field(from_radio, device_ui_field):
            self._device_ui = _clone_message(getattr(from_radio, device_ui_field))
            changed = True
        if _has_field(from_radio, "node_info"):
            node_info = from_radio.node_info
            node_num = int(getattr(node_info, "num", -1))
            effective_node_num = (
                my_node_num if my_node_num is not None else self._local_node_num
            )
            if _has_field(node_info, "user"):
                if effective_node_num is not None and node_num == effective_node_num:
                    self._owner = _clone_message(node_info.user)
                    changed = True
                elif effective_node_num is None and node_num >= 0:
                    if (
                        node_num not in self._pending_node_users
                        and len(self._pending_node_users) >= _MAX_PENDING_NODE_USERS
                    ):
                        self._pending_node_users.pop(
                            next(iter(self._pending_node_users)), None
                        )
                    self._pending_node_users[node_num] = _clone_message(
                        node_info.user
                    )
        effective_node_num = (
            my_node_num if my_node_num is not None else self._local_node_num
        )
        if effective_node_num is not None:
            pending_owner = self._pending_node_users.get(effective_node_num)
            if pending_owner is not None:
                self._owner = pending_owner
                changed = True
            self._pending_node_users.clear()
        if changed:
            self._revision += 1

    def capture_native_interface(self, interface: Any) -> None:
        """Capture detached SDK state while called on the interface executor."""
        self.begin_refresh()
        local_node = getattr(interface, "localNode", None)
        local_config = getattr(local_node, "localConfig", None)
        if local_config is not None:
            for field_descriptor in _message_fields(local_config):
                name = _field_name(field_descriptor)
                if name:
                    self._configs[name] = _clone_message(getattr(local_config, name))
        module_config = getattr(local_node, "moduleConfig", None)
        if module_config is None:
            module_config = getattr(local_node, "localModuleConfig", None)
        if module_config is not None:
            for field_descriptor in _message_fields(module_config):
                name = _field_name(field_descriptor)
                if name:
                    self._modules[name] = _clone_message(getattr(module_config, name))
        for offset, channel in enumerate(getattr(local_node, "channels", ()) or ()):
            cloned = _clone_message(channel)
            self._channels[int(getattr(cloned, "index", offset))] = cloned
        metadata = getattr(interface, "metadata", None)
        if metadata is not None:
            self._metadata = _clone_message(metadata)
        my_info = getattr(interface, "myInfo", None)
        if my_info is not None:
            self._my_info = _clone_message(my_info)
        node_num = getattr(local_node, "nodeNum", None)
        nodes_by_num = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_num, Mapping) and node_num in nodes_by_num:
            node = nodes_by_num[node_num]
            owner = node.get("user") if isinstance(node, Mapping) else getattr(node, "user", None)
            if owner is not None:
                self._owner = _clone_message(owner)
        self.mark_complete()

    def public_snapshot(
        self,
        *,
        transport: str,
        write_supported: bool = False,
        apply_reason: str = "confirmed_admin_write_and_verification_not_available",
    ) -> dict[str, Any]:
        """Return a detached JSON-safe settings projection."""
        if self.managed:
            write_supported = False
            apply_reason = "managed_mode_rejects_local_admin_changes"
        categories: list[dict[str, Any]] = []
        if self._owner is not None:
            categories.append(
                _category(
                    "owner",
                    self._owner,
                    write_supported=write_supported,
                    allowed_root_fields=_OWNER_ALLOWLIST,
                    transport=transport,
                )
            )
        for name, message in sorted(self._configs.items()):
            categories.append(
                _category(
                    f"config.{name}",
                    message,
                    write_supported=write_supported,
                    transport=transport,
                )
            )
        for name, message in sorted(self._modules.items()):
            categories.append(
                _category(
                    f"module.{name}",
                    message,
                    write_supported=write_supported,
                    transport=transport,
                )
            )
        for index, message in sorted(self._channels.items()):
            categories.append(
                _category(
                    f"channel.{index}",
                    message,
                    write_supported=write_supported,
                    transport=transport,
                )
            )
        if self._device_ui is not None:
            categories.append(
                _category(
                    "device_ui",
                    self._device_ui,
                    write_supported=write_supported,
                    transport=transport,
                )
            )
        if self._my_info is not None:
            categories.append(
                _category(
                    "local_info",
                    self._my_info,
                    write_supported=False,
                    metadata=True,
                    allowed_root_fields=_MY_INFO_ALLOWLIST,
                    transport=transport,
                )
            )
        if self._metadata is not None:
            categories.append(
                _category(
                    "metadata",
                    self._metadata,
                    write_supported=False,
                    metadata=True,
                    allowed_root_fields=_METADATA_ALLOWLIST,
                    transport=transport,
                )
            )
        secret_revision_material: dict[str, Any] = {}
        if self._owner is not None:
            secret_revision_material.update(
                _secret_revision_material(
                    self._owner,
                    prefix="owner",
                    allowed_root_fields=_OWNER_ALLOWLIST,
                )
            )
        for name, message in sorted(self._configs.items()):
            secret_revision_material.update(
                _secret_revision_material(message, prefix=f"config.{name}")
            )
        for name, message in sorted(self._modules.items()):
            secret_revision_material.update(
                _secret_revision_material(message, prefix=f"module.{name}")
            )
        for index, message in sorted(self._channels.items()):
            secret_revision_material.update(
                _secret_revision_material(message, prefix=f"channel.{index}")
            )
        if self._device_ui is not None:
            secret_revision_material.update(
                _secret_revision_material(self._device_ui, prefix="device_ui")
            )
        if self._my_info is not None:
            secret_revision_material.update(
                _secret_revision_material(
                    self._my_info,
                    prefix="local_info",
                    allowed_root_fields=_MY_INFO_ALLOWLIST,
                )
            )
        if self._metadata is not None:
            secret_revision_material.update(
                _secret_revision_material(
                    self._metadata,
                    prefix="metadata",
                    allowed_root_fields=_METADATA_ALLOWLIST,
                )
            )
        for category in categories:
            for projected_field in category["fields"]:
                critical = _critical_change(projected_field["path"], transport)
                projected_field["critical"] = critical
                if projected_field.get("multiple"):
                    projected_field["writable"] = False
                    projected_field["read_only_reason"] = (
                        "Repeated settings require a dedicated validated editor."
                    )
                projected_field["requires_reconnect"] = bool(
                    projected_field.get("writable")
                )
        any_writable = bool(
            write_supported
            and any(
                field.get("writable")
                for category in categories
                for field in category["fields"]
            )
        )
        if write_supported and not any_writable:
            apply_reason = "no_received_setting_has_a_reviewed_write_contract"
        return {
            "available": bool(categories),
            "complete": self._complete,
            "source": "local_radio",
            "transport": transport,
            "revision": self._revision,
            "capabilities": {
                "read": bool(categories),
                "plan": bool(categories),
                "apply": any_writable,
                "apply_reason": None if any_writable else apply_reason,
                # Firmware's begin/commit mechanism has no abort or rollback;
                # it delays persistence but mutates live state as setters run.
                "transactional": False,
                "begin_commit_supported": True,
                "requires_reboot": True,
                "verification": any_writable,
            },
            "writable": any_writable,
            "read_only_reason": (
                None
                if any_writable
                else apply_reason
            ),
            "categories": categories,
            # Consumed inside GatewaySettingsManager to bind a preview to
            # hidden credential state. It is HMACed with a process-only key
            # and is never included in the WebSocket response.
            "_secret_revision_material": _PrivateSecretRevisionMaterial(
                secret_revision_material
            ),
            "warning_codes": [
                "credentials_are_write_only",
                "meshtastic_transaction_has_no_rollback",
            ],
        }

    def build_plan(
        self,
        changes: Mapping[str, Any],
        *,
        transport: str,
        admin_message_factory: Callable[[], Any],
    ) -> SettingsPlan:
        """Validate flat path changes and build ordered AdminMessages."""
        if not isinstance(changes, Mapping) or not changes:
            raise MeshtasticSettingsValidationError("settings changes cannot be empty")
        if not self._complete:
            raise MeshtasticSettingsStaleError(
                "the local radio settings download is not complete"
            )

        security = self._configs.get("security")
        if security is not None and bool(getattr(security, "is_managed", False)):
            paths: list[str] = []
            for path in changes:
                if not isinstance(path, str) or len(path) > 256:
                    raise MeshtasticSettingsValidationError(
                        "a settings path is invalid"
                    )
                paths.append(path)
            return SettingsPlan(
                revision=self._revision,
                transport=transport,
                changed_paths=tuple(paths),
                connection_critical_paths=tuple(
                    path for path in paths if _critical_change(path, transport)
                ),
                operations=(),
                blocked_paths={
                    path: "managed_mode_rejects_local_admin_changes"
                    for path in paths
                },
            )

        grouped: dict[tuple[str, str], list[tuple[str, list[str], Any]]] = {}
        blocked: dict[str, str] = {}
        critical_paths: list[str] = []
        for path, replacement in changes.items():
            if not isinstance(path, str) or len(path) > 256:
                raise MeshtasticSettingsValidationError("a settings path is invalid")
            components = path.split(".")
            if any(_PATH_COMPONENT_RE.fullmatch(part) is None for part in components):
                # Channel index is the single allowed numeric path component.
                if not (
                    len(components) >= 3
                    and components[0] == "channel"
                    and components[1].isdigit()
                    and all(_PATH_COMPONENT_RE.fullmatch(part) for part in components[2:])
                ):
                    raise MeshtasticSettingsValidationError(f"{path} is not a valid settings path")
            if components[0] in {"config", "module"} and len(components) >= 3:
                group = (components[0], components[1])
                remainder = components[2:]
            elif components[0] == "channel" and len(components) >= 3:
                group = ("channel", components[1])
                remainder = components[2:]
            elif components[0] == "owner" and len(components) >= 2:
                group = ("owner", "owner")
                remainder = components[1:]
            else:
                blocked[path] = "settings_path_is_read_only_or_unknown"
                continue
            final_name = remainder[-1]
            reason = _read_only_reason(
                path,
                final_name,
                metadata=False,
                transport=transport,
            )
            if reason is not None:
                blocked[path] = reason
                continue
            grouped.setdefault(group, []).append((path, remainder, replacement))
            if _critical_change(path, transport):
                critical_paths.append(path)

        regular: list[SettingsOperation] = []
        critical: list[SettingsOperation] = []
        changed_paths: list[str] = []
        for (kind, section), requested in grouped.items():
            source: Any | None
            if kind == "config":
                source = self._configs.get(section)
            elif kind == "module":
                source = self._modules.get(section)
            elif kind == "channel":
                source = self._channels.get(int(section))
            else:
                source = self._owner
            if source is None or not _message_fields(source):
                for path, _, _ in requested:
                    blocked[path] = "settings_section_was_not_received_from_radio"
                continue
            if (
                transport.casefold() == "bluetooth"
                and kind == "config"
                and section == "bluetooth"
                and not bool(getattr(source, "enabled", False))
            ):
                for path, _, _ in requested:
                    blocked[path] = (
                        "the_active_bluetooth_transport_cannot_preserve_a_disabled_state"
                    )
                continue
            if (
                transport.casefold() == "bluetooth"
                and kind == "config"
                and section == "display"
            ):
                mode_field = _field_by_name(source, "displaymode")
                mode = (
                    _enum_name(mode_field, int(source.displaymode))
                    if mode_field is not None
                    else None
                )
                if mode == "COLOR":
                    for path, _, _ in requested:
                        blocked[path] = (
                            "current_display_mode_can_disable_bluetooth_on_reboot"
                        )
                    continue
            updated = _clone_message(source)
            valid_paths: list[str] = []
            for path, remainder, replacement in requested:
                try:
                    _set_message_path(updated, remainder, replacement, path)
                except MeshtasticSettingsValidationError:
                    raise
                valid_paths.append(path)
                changed_paths.append(path)

            if kind == "config":
                wrapper = admin_message_factory().set_config
                target = getattr(wrapper, section, None)
                if target is None or not callable(getattr(target, "CopyFrom", None)):
                    for path in valid_paths:
                        blocked[path] = "installed_meshtastic_package_cannot_write_this_section"
                    continue
                admin = admin_message_factory()
                getattr(admin.set_config, section).CopyFrom(updated)
            elif kind == "module":
                wrapper = admin_message_factory().set_module_config
                target = getattr(wrapper, section, None)
                if target is None or not callable(getattr(target, "CopyFrom", None)):
                    for path in valid_paths:
                        blocked[path] = "installed_meshtastic_package_cannot_write_this_section"
                    continue
                admin = admin_message_factory()
                getattr(admin.set_module_config, section).CopyFrom(updated)
            elif kind == "channel":
                admin = _admin_message(admin_message_factory, "set_channel", updated)
            else:
                admin = _admin_message(admin_message_factory, "set_owner", updated)
            is_critical = any(path in critical_paths for path in valid_paths)
            operation = SettingsOperation(
                operation=f"set_{kind}",
                paths=tuple(valid_paths),
                connection_critical=is_critical,
                message=admin,
            )
            (critical if is_critical else regular).append(operation)

        set_operations = tuple(regular + critical)
        operations: tuple[SettingsOperation, ...]
        if set_operations:
            operations = (
                SettingsOperation(
                    operation="begin_edit_settings",
                    message=_admin_message(
                        admin_message_factory, "begin_edit_settings", True
                    ),
                ),
                *set_operations,
                SettingsOperation(
                    operation="commit_edit_settings",
                    connection_critical=bool(critical),
                    message=_admin_message(
                        admin_message_factory, "commit_edit_settings", True
                    ),
                ),
            )
        else:
            operations = ()
        return SettingsPlan(
            revision=self._revision,
            transport=transport,
            changed_paths=tuple(dict.fromkeys(changed_paths)),
            connection_critical_paths=tuple(dict.fromkeys(critical_paths)),
            operations=operations,
            blocked_paths=blocked,
        )

    def verify_changes(
        self, changes: Mapping[str, Any]
    ) -> tuple[list[str], list[str]]:
        """Compare requested replacements with the latest retained raw state."""
        verified: list[str] = []
        unverified: list[str] = []
        if not self._complete:
            return verified, [path for path in changes if isinstance(path, str)]
        for path, request in changes.items():
            if not isinstance(path, str):
                continue
            components = path.split(".")
            source: Any | None = None
            remainder: list[str] = []
            if components[0] in {"config", "module"} and len(components) >= 3:
                source = (
                    self._configs.get(components[1])
                    if components[0] == "config"
                    else self._modules.get(components[1])
                )
                remainder = components[2:]
            elif (
                components[0] == "channel"
                and len(components) >= 3
                and components[1].isdigit()
            ):
                source = self._channels.get(int(components[1]))
                remainder = components[2:]
            elif components[0] == "owner" and len(components) >= 2:
                source = self._owner
                remainder = components[1:]
            if source is None or not remainder:
                unverified.append(path)
                continue
            current = source
            valid = True
            for component in remainder[:-1]:
                descriptor = _field_by_name(current, component)
                if descriptor is None:
                    valid = False
                    break
                current = getattr(current, component)
            descriptor = (
                _field_by_name(current, remainder[-1]) if valid else None
            )
            if descriptor is None:
                unverified.append(path)
                continue
            try:
                expected = _normalized_replacement(path, descriptor, request)
            except MeshtasticSettingsValidationError:
                unverified.append(path)
                continue
            actual = getattr(current, remainder[-1])
            if _is_repeated(descriptor):
                matches = list(actual) == list(expected)
            elif isinstance(actual, (bytes, bytearray, memoryview)):
                matches = bytes(actual) == bytes(expected)
            else:
                matches = actual == expected
            (verified if matches else unverified).append(path)
        return verified, unverified

    def verify_plan(self, plan: SettingsPlan) -> tuple[list[str], list[str]]:
        """Verify every touched section exactly, including companion fields."""
        verified: list[str] = []
        unverified: list[str] = []
        if not self._complete:
            return verified, list(plan.changed_paths)
        for operation in plan.operations:
            if not operation.paths:
                continue
            expected: Any | None = None
            actual: Any | None = None
            if operation.operation == "set_config":
                section = _section_from_wrapper(operation.message.set_config)
                if section is not None:
                    expected = section[1]
                    actual = self._configs.get(section[0])
            elif operation.operation == "set_module":
                section = _section_from_wrapper(
                    operation.message.set_module_config
                )
                if section is not None:
                    expected = section[1]
                    actual = self._modules.get(section[0])
            elif operation.operation == "set_channel":
                expected = operation.message.set_channel
                actual = self._channels.get(int(getattr(expected, "index", -1)))
            elif operation.operation == "set_owner":
                expected = operation.message.set_owner
                actual = self._owner
            if expected is not None and actual is not None and _messages_equal(
                expected, actual
            ):
                verified.extend(operation.paths)
            else:
                unverified.extend(operation.paths)
        accounted = set(verified) | set(unverified)
        unverified.extend(path for path in plan.changed_paths if path not in accounted)
        return list(dict.fromkeys(verified)), list(dict.fromkeys(unverified))


def state_from_native_interface(interface: Any) -> MeshtasticSettingsState:
    """Build one detached settings state from an official SDK interface."""
    state = MeshtasticSettingsState()
    state.capture_native_interface(interface)
    return state
