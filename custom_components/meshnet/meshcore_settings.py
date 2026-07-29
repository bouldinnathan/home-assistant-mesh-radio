"""Validated local settings support for MeshCore companion radios.

The MeshCore companion protocol permits one outstanding command at a time and
does not provide transactions or rollback.  This adapter therefore runs a
fully validated plan under the owning client's command lock, sends each write
once, and re-reads the affected setting.  A timeout is an unknown outcome: the
write is never retried.

PINs and channel secrets are deliberately retained only in short-lived local
variables.  Snapshots and results expose configured booleans, never values.
"""

from __future__ import annotations

import asyncio
import hmac
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .const import (
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_NATIVE,
    TRANSPORT_REST,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)
from .gateway import GatewayError
from .sensitive_logging import suppress_sensitive_library_logs

if TYPE_CHECKING:
    from .meshcore_client import MeshCoreClient

_LOCAL_TRANSPORTS = frozenset(
    {
        TRANSPORT_BLUETOOTH,
        TRANSPORT_NATIVE,
        TRANSPORT_SERIAL,
        TRANSPORT_TCP,
    }
)
_BRIDGE_TRANSPORTS = frozenset({TRANSPORT_MQTT, TRANSPORT_REST})
_CHANNEL_PATH_RE = re.compile(r"^channels\.(\d+)\.(name|secret)$")
_MAX_PROJECTED_CHANNELS = 16
_CHANNEL_SECRET_BYTES = 16
_TELEMETRY_OPTIONS = [
    {"value": 0, "label": "Disabled"},
    {"value": 1, "label": "On request"},
    {"value": 2, "label": "Always"},
]
_RADIO_PATHS = (
    "radio.frequency_mhz",
    "radio.bandwidth_khz",
    "radio.spreading_factor",
    "radio.coding_rate",
)
_OTHER_PARAMETER_PATHS = {
    "position.advertise": "adv_loc_policy",
    "telemetry.base_mode": "telemetry_mode_base",
    "telemetry.location_mode": "telemetry_mode_loc",
    "telemetry.environment_mode": "telemetry_mode_env",
    "contacts.manual_add": "manual_add_contacts",
}
_OTHER_PARAMETER_DOMAINS = {
    "adv_loc_policy": {0, 1},
    "telemetry_mode_base": {0, 1, 2},
    "telemetry_mode_loc": {0, 1, 2},
    "telemetry_mode_env": {0, 1, 2},
    "manual_add_contacts": {0, 1},
    "multi_acks": {0, 1},
}
_OTHER_PARAMETER_REQUIRED_KEYS = frozenset(_OTHER_PARAMETER_DOMAINS)


class MeshCoreSettingsError(GatewayError):
    """Base class for a sanitized MeshCore settings failure."""


class _PrivateSecretRevisionMaterial(dict[str, Any]):
    """Internal mapping whose accidental representation cannot reveal secrets."""

    def __repr__(self) -> str:
        return "<redacted secret revision material>"

    __str__ = __repr__


class MeshCoreSettingsUnavailable(MeshCoreSettingsError):
    """Raised when a live, local companion connection is unavailable."""


class MeshCoreSettingsValidationError(MeshCoreSettingsError):
    """Raised when a settings plan is not hardware-safe."""


class MeshCoreSettingsRejected(MeshCoreSettingsError):
    """Raised when firmware explicitly rejects one command."""


class MeshCoreSettingsUnknownState(MeshCoreSettingsError):
    """Raised when a timed-out write cannot be confirmed by a live re-read."""

    def __init__(self, path: str, applied_paths: list[str] | None = None) -> None:
        super().__init__(f"MeshCore setting {path} has an unknown device state")
        self.path = path
        self.applied_paths = tuple(applied_paths or ())


class _SensitiveConnectionUpdates(dict[str, str | None]):
    """Internal persistence handoff whose normal rendering is credential-safe."""

    def __repr__(self) -> str:
        return "{'pin': '<redacted>'}" if self.get("pin") else "{'pin': None}"

    __str__ = __repr__


@dataclass(slots=True)
class _RawSettings:
    """Live radio data that must never leave this module unsanitized."""

    device: dict[str, Any] = field(repr=False)
    self_info: dict[str, Any] = field(repr=False)
    channels: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)
    unreadable_channels: set[int] = field(default_factory=set)
    projected_channel_count: int = 0


def _event_type_name(event: Any) -> str:
    event_type = getattr(event, "type", event)
    value = getattr(event_type, "value", event_type)
    return str(value).rsplit(".", 1)[-1].lower()


def _error_is_timeout(event: Any) -> bool:
    payload = getattr(event, "payload", None)
    reason = payload.get("reason") if isinstance(payload, Mapping) else payload
    normalized = str(reason or "").casefold()
    return "timeout" in normalized or "no_event_received" in normalized


def _result_payload(event: Any, expected: str) -> dict[str, Any]:
    event_name = _event_type_name(event)
    if event_name in {"command_error", "error"} or event_name.endswith("error"):
        if _error_is_timeout(event):
            raise MeshCoreSettingsUnknownState(expected)
        raise MeshCoreSettingsRejected(f"MeshCore rejected {expected}")
    if event_name != expected:
        raise MeshCoreSettingsUnknownState(expected)
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        raise MeshCoreSettingsUnknownState(expected)
    return dict(payload)


def _ensure_write_ack(event: Any, path: str) -> None:
    event_name = _event_type_name(event)
    if event_name in {"command_error", "error"} or event_name.endswith("error"):
        if _error_is_timeout(event):
            raise MeshCoreSettingsUnknownState(path)
        raise MeshCoreSettingsRejected(f"MeshCore rejected setting {path}")
    if event_name != "command_ok":
        raise MeshCoreSettingsUnknownState(path)


def _int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return converted if math.isfinite(converted) else default


def _safe_number(
    value: Any, *, minimum: float, maximum: float
) -> float | None:
    """Return one explicit finite in-range number without invented defaults."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        return None
    return converted


def _safe_integer_member(value: Any, allowed: set[int]) -> bool:
    """Require an exact integer/bool member for grouped command preservation."""
    if isinstance(value, bool):
        converted = int(value)
    elif isinstance(value, int):
        converted = value
    else:
        return False
    return converted in allowed


def _safe_utf8_name(value: Any) -> str | None:
    """Return a bounded, explicit UTF-8 channel name safe to preserve."""
    if not isinstance(value, str) or not value or value.startswith("#"):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if 1 <= len(encoded) <= 32 else None


def _channel_secret(payload: Mapping[str, Any]) -> bytes | None:
    value = payload.get("channel_secret")
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes) and len(value) == _CHANNEL_SECRET_BYTES:
        return value
    return None


def _field(
    path: str,
    label: str,
    field_type: str,
    value: Any = None,
    *,
    writable: bool = False,
    critical: bool = False,
    requires_reconnect: bool = False,
    description: str | None = None,
    read_only_reason: str | None = None,
    **constraints: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": path,
        "label": label,
        "type": field_type,
        "value": value,
        "writable": writable,
        "critical": critical,
        "requires_reconnect": requires_reconnect,
    }
    if description:
        item["description"] = description
    if read_only_reason:
        item["read_only_reason"] = read_only_reason
    item.update(constraints)
    return item


def _secret_field(
    path: str,
    label: str,
    *,
    configured: bool,
    writable: bool,
    allow_clear: bool,
    max_length: int,
    critical: bool = True,
    requires_reconnect: bool = False,
    description: str | None = None,
    read_only_reason: str | None = None,
) -> dict[str, Any]:
    item = _field(
        path,
        label,
        "secret",
        writable=writable,
        critical=critical,
        requires_reconnect=requires_reconnect,
        description=description,
        read_only_reason=read_only_reason,
        max_length=max_length,
    )
    item.pop("value", None)
    item["configured"] = configured
    item["allow_clear"] = allow_clear
    return item


def _category(
    key: str,
    label: str,
    fields: list[dict[str, Any]],
    description: str | None = None,
) -> dict[str, Any]:
    category: dict[str, Any] = {"key": key, "label": label, "fields": fields}
    if description:
        category["description"] = description
    return category


def _read_only_snapshot(transport: str, reason: str) -> dict[str, Any]:
    return {
        "writable": False,
        "read_only_reason": reason,
        "warning_codes": [],
        "categories": [
            _category(
                "access",
                "Settings access",
                [
                    _field(
                        "access.transport",
                        "Transport",
                        "string",
                        transport,
                        read_only_reason=reason,
                    ),
                    _field(
                        "access.settings",
                        "Radio settings",
                        "string",
                        "Read-only",
                        read_only_reason=reason,
                    ),
                ],
            )
        ],
    }


def _flatten_fields(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        field["path"]: field
        for category in snapshot.get("categories", [])
        if isinstance(category, Mapping)
        for field in category.get("fields", [])
        if isinstance(field, dict) and isinstance(field.get("path"), str)
    }


def _project_snapshot(raw: _RawSettings, commands: Any) -> dict[str, Any]:
    device = raw.device
    self_info = raw.self_info
    protocol_version = _int_value(device.get("fw ver"))
    other_group_safe = bool(
        _OTHER_PARAMETER_REQUIRED_KEYS.issubset(self_info)
        and all(
            _safe_integer_member(self_info.get(key), allowed)
            for key, allowed in _OTHER_PARAMETER_DOMAINS.items()
        )
    )
    set_other = bool(
        callable(getattr(commands, "set_other_params_from_infos", None))
        and protocol_version >= 7
        and other_group_safe
    )
    repeat_supported = protocol_version >= 9 and "repeat" in device
    repeat_enabled = repeat_supported and bool(device.get("repeat"))
    radio_reason = (
        "Repeater radio changes are read-only because this settings schema does "
        "not yet project the firmware's disjoint allowed-frequency ranges."
        if repeat_enabled
        else (
            "Custom radio changes require firmware- and region-specific allowed "
            "bands and parameter combinations that this interface cannot yet "
            "validate safely."
        )
    )
    max_tx_power = _int_value(self_info.get("max_tx_power"), -1)
    set_tx_power = (
        callable(getattr(commands, "set_tx_power", None)) and max_tx_power >= 0
    )
    latitude = _safe_number(
        self_info.get("adv_lat"), minimum=-90.0, maximum=90.0
    )
    longitude = _safe_number(
        self_info.get("adv_lon"), minimum=-180.0, maximum=180.0
    )
    coordinates_safe = latitude is not None and longitude is not None
    coordinate_reason = (
        None
        if coordinates_safe
        else "The device did not report a complete, finite coordinate pair."
    )
    other_reason = (
        None
        if other_group_safe
        else "The device did not report a complete, safely representable parameter group."
    )

    categories: list[dict[str, Any]] = [
        _category(
            "device",
            "Device",
            [
                _field("device.model", "Model", "string", str(device.get("model") or "Unknown")),
                _field(
                    "device.firmware_version",
                    "Firmware version",
                    "string",
                    str(device.get("ver") or device.get("fw_build") or "Unknown"),
                ),
                _field(
                    "device.protocol_version",
                    "Companion protocol version",
                    "integer",
                    _int_value(device.get("fw ver")),
                ),
                _field(
                    "device.max_channels",
                    "Channel capacity",
                    "integer",
                    max(0, _int_value(device.get("max_channels"))),
                ),
            ],
        ),
        _category(
            "identity",
            "Identity",
            [
                _field(
                    "identity.name",
                    "Radio name",
                    "string",
                    str(self_info.get("name") or ""),
                    writable=callable(getattr(commands, "set_name", None)),
                    max_length=32,
                    description="One to 32 UTF-8 bytes; the public key is never exposed here.",
                )
            ],
        ),
        _category(
            "position",
            "Position",
            [
                _field(
                    "position.latitude",
                    "Latitude",
                    "number",
                    latitude,
                    writable=(
                        coordinates_safe
                        and callable(getattr(commands, "set_coords", None))
                    ),
                    min=-90,
                    max=90,
                    step=0.000001,
                    read_only_reason=coordinate_reason,
                ),
                _field(
                    "position.longitude",
                    "Longitude",
                    "number",
                    longitude,
                    writable=(
                        coordinates_safe
                        and callable(getattr(commands, "set_coords", None))
                    ),
                    min=-180,
                    max=180,
                    step=0.000001,
                    read_only_reason=coordinate_reason,
                ),
                _field(
                    "position.advertise",
                    "Advertise position",
                    "boolean",
                    bool(_int_value(self_info.get("adv_loc_policy"))),
                    writable=set_other,
                    description="Controls whether this radio includes its position in advertisements.",
                    read_only_reason=other_reason,
                ),
            ],
        ),
        _category(
            "radio",
            "LoRa radio",
            [
                _field(
                    "radio.frequency_mhz",
                    "Frequency",
                    "number",
                    _float_value(self_info.get("radio_freq")),
                    writable=False,
                    critical=True,
                    unit="MHz",
                    min=150,
                    max=2500,
                    step=0.001,
                    read_only_reason=radio_reason,
                ),
                _field(
                    "radio.bandwidth_khz",
                    "Bandwidth",
                    "number",
                    _float_value(self_info.get("radio_bw")),
                    writable=False,
                    critical=True,
                    unit="kHz",
                    min=7,
                    max=500,
                    step=0.001,
                    read_only_reason=radio_reason,
                ),
                _field(
                    "radio.spreading_factor",
                    "Spreading factor",
                    "integer",
                    _int_value(self_info.get("radio_sf")),
                    writable=False,
                    critical=True,
                    min=5,
                    max=12,
                    step=1,
                    read_only_reason=radio_reason,
                ),
                _field(
                    "radio.coding_rate",
                    "Coding rate",
                    "integer",
                    _int_value(self_info.get("radio_cr")),
                    writable=False,
                    critical=True,
                    min=5,
                    max=8,
                    step=1,
                    read_only_reason=radio_reason,
                ),
                _field(
                    "radio.tx_power_dbm",
                    "Transmit power",
                    "integer",
                    _int_value(self_info.get("tx_power")),
                    writable=set_tx_power,
                    critical=True,
                    unit="dBm",
                    min=0,
                    max=max(0, max_tx_power),
                    step=1,
                    description=(
                        "Negative powers are intentionally excluded because meshcore 2.3.7 "
                        "cannot encode them safely."
                    ),
                    read_only_reason=(
                        None if set_tx_power else "The device did not report a safe power limit."
                    ),
                ),
                _field(
                    "radio.repeat",
                    "Repeater mode",
                    "boolean",
                    repeat_enabled,
                    read_only_reason=(
                        (
                            "Changing client repeat mode changes RF airtime and requires "
                            "region-aware validation that this settings schema does not yet expose."
                        )
                        if repeat_supported
                        else "This companion firmware does not report repeat-mode support."
                    ),
                ),
                _field(
                    "radio.tuning",
                    "Radio tuning",
                    "string",
                    "Not exposed",
                    read_only_reason=(
                        "meshcore 2.3.7 exposes raw tuning integers without portable units or "
                        "hardware-safe ranges."
                    ),
                ),
            ],
        ),
        _category(
            "telemetry",
            "Telemetry",
            [
                _field(
                    "telemetry.base_mode",
                    "Base telemetry",
                    "select",
                    _int_value(self_info.get("telemetry_mode_base")),
                    writable=set_other,
                    options=_TELEMETRY_OPTIONS,
                    read_only_reason=other_reason,
                ),
                _field(
                    "telemetry.location_mode",
                    "Location telemetry",
                    "select",
                    _int_value(self_info.get("telemetry_mode_loc")),
                    writable=set_other,
                    options=_TELEMETRY_OPTIONS,
                    read_only_reason=other_reason,
                ),
                _field(
                    "telemetry.environment_mode",
                    "Environment telemetry",
                    "select",
                    _int_value(self_info.get("telemetry_mode_env")),
                    writable=set_other,
                    options=_TELEMETRY_OPTIONS,
                    read_only_reason=other_reason,
                ),
            ],
        ),
        _category(
            "contacts",
            "Contacts",
            [
                _field(
                    "contacts.manual_add",
                    "Require manual contact approval",
                    "boolean",
                    bool(self_info.get("manual_add_contacts")),
                    writable=set_other,
                    read_only_reason=other_reason,
                ),
                _field(
                    "contacts.auto_add",
                    "Automatic contact policy",
                    "string",
                    "Read-only",
                    read_only_reason=(
                        "meshcore 2.3.7 cannot safely preserve the firmware's optional "
                        "maximum-hop byte when changing this setting."
                    ),
                ),
            ],
        ),
        _category(
            "routing",
            "Routing",
            [
                _field(
                    "routing.path_hash_mode",
                    "Path hash mode",
                    "integer",
                    _int_value(device.get("path_hash_mode")),
                    read_only_reason=(
                        "Changing path-hash width requires coordinated network migration and "
                        "is not a safe per-gateway operation."
                    ),
                ),
                _field(
                    "routing.default_flood_scope",
                    "Default flood scope",
                    "string",
                    "Read-only",
                    read_only_reason=(
                        "The meshcore 2.3.7 clear-scope command is not reliably accepted by "
                        "all supported companion firmware."
                    ),
                ),
                _field(
                    "routing.multi_acks",
                    "Multiple acknowledgements",
                    "integer",
                    _int_value(self_info.get("multi_acks")),
                    read_only_reason=(
                        "Extra acknowledgements increase RF airtime; this remains read-only "
                        "until airtime and regional safeguards are modeled."
                    ),
                ),
            ],
        ),
        _category(
            "security",
            "Security",
            [
                _secret_field(
                    "security.pin",
                    "Bluetooth PIN",
                    configured=_int_value(device.get("ble_pin")) != 0,
                    writable=(
                        protocol_version >= 3
                        and callable(getattr(commands, "set_devicepin", None))
                    ),
                    allow_clear=True,
                    max_length=6,
                    requires_reconnect=True,
                    description=(
                        "Enter exactly six digits. The value is write-only and is changed last."
                    ),
                ),
                _field(
                    "security.private_key_management",
                    "Private key management",
                    "string",
                    "Not exposed",
                    read_only_reason=(
                        "Private-key import and export are deliberately outside this interface."
                    ),
                ),
            ],
        ),
        _category(
            "system",
            "System",
            [
                _field(
                    "system.factory_reset",
                    "Factory reset",
                    "boolean",
                    False,
                    read_only_reason=(
                        "Factory reset and firmware operations are deliberately outside this "
                        "settings interface."
                    ),
                ),
                _field(
                    "system.custom_variables",
                    "Firmware custom variables",
                    "string",
                    "Not exposed",
                    read_only_reason=(
                        "Custom variables are firmware-specific and have no validated schema."
                    ),
                ),
            ],
        ),
    ]

    channel_fields: list[dict[str, Any]] = []
    set_channel_available = protocol_version >= 3 and callable(
        getattr(commands, "set_channel", None)
    )
    for index, channel in sorted(raw.channels.items()):
        reported_name = channel.get("channel_name")
        name = reported_name if isinstance(reported_name, str) else ""
        secret = _channel_secret(channel)
        conventional_hash_name = name.startswith("#")
        configured_channel = bool(name and secret and any(secret))
        safe_name = _safe_utf8_name(reported_name) is not None
        name_writable = (
            set_channel_available
            and configured_channel
            and safe_name
            and not conventional_hash_name
        )
        read_only_reason = (
            "Names beginning with # derive their key from the name in meshcore 2.3.7; "
            "editing one implicitly changes a secret and is not exposed as a name-only write."
            if conventional_hash_name
            else (
                "Empty channel slots require an atomic name-and-secret editor and remain "
                "read-only in this schema."
                if not configured_channel
                else (
                    "The existing channel name is not safe to preserve byte-for-byte."
                    if not safe_name
                    else None
                )
            )
        )
        channel_fields.extend(
            [
                _field(
                    f"channels.{index}.name",
                    f"Channel {index} name",
                    "string",
                    name,
                    writable=name_writable,
                    critical=True,
                    max_length=32,
                    read_only_reason=read_only_reason,
                ),
                _secret_field(
                    f"channels.{index}.secret",
                    f"Channel {index} secret",
                    configured=bool(secret and any(secret)),
                    writable=(
                        set_channel_available
                        and configured_channel
                        and safe_name
                        and not conventional_hash_name
                    ),
                    # Clearing a channel key also clears its name in the
                    # companion protocol.  Do not offer that hidden second
                    # mutation through a one-field preview.
                    allow_clear=False,
                    max_length=32,
                    description=(
                        "Enter exactly 32 hexadecimal characters. Channel removal remains "
                        "read-only because it would also clear the channel name."
                    ),
                    read_only_reason=read_only_reason,
                ),
            ]
        )
    if channel_fields:
        categories.insert(
            -2,
            _category(
                "channels",
                "Channels",
                channel_fields,
                "Secrets are never returned; only their configured state is shown.",
            ),
        )

    warning_codes = [
        "meshcore_commands_have_no_rollback",
        "meshcore_advanced_settings_read_only",
    ]
    if raw.unreadable_channels:
        warning_codes.append("meshcore_unreadable_channels_omitted")
    reported_channels = max(0, _int_value(device.get("max_channels")))
    if reported_channels > raw.projected_channel_count:
        warning_codes.append("meshcore_channel_projection_bounded")
    private_material: dict[str, Any] = {
        "security.pin": device.get("ble_pin")
    }
    for index, channel in sorted(raw.channels.items()):
        private_material[f"channels.{index}.secret"] = _channel_secret(
            channel
        )
    return {
        "writable": True,
        "categories": categories,
        "warning_codes": warning_codes,
        # Consumed only by GatewaySettingsManager for a process-keyed stale
        # preview check; repr deliberately remains redacted.
        "_secret_revision_material": _PrivateSecretRevisionMaterial(
            private_material
        ),
    }


class MeshCoreSettingsAdapter:
    """Read and safely mutate one MeshCore client's physically connected radio."""

    def __init__(self, client: MeshCoreClient) -> None:
        self._client = client

    async def async_get_settings_snapshot(self) -> dict[str, Any]:
        transport = self._client.config.transport
        if transport in _BRIDGE_TRANSPORTS:
            return _read_only_snapshot(
                transport,
                "MQTT and REST bridge payloads have no standardized, verifiable MeshCore settings contract.",
            )
        if transport not in _LOCAL_TRANSPORTS:
            return _read_only_snapshot(
                transport, "This transport has no validated MeshCore settings interface."
            )
        interface, lifecycle_epoch = self._connected_interface()

        def fence() -> None:
            self._ensure_current(interface, lifecycle_epoch)

        async with self._client._native_command_lock:
            fence()
            # DEVICE_INFO and CHANNEL_INFO responses contain the current PIN
            # and channel keys.  meshcore 2.3.7 logs raw frames at DEBUG, so
            # suppress its entire logger namespace for the full live read.
            async with suppress_sensitive_library_logs("meshcore"):
                raw = await self._read_raw(interface, fence)
            fence()
        return _project_snapshot(raw, interface.commands)

    async def async_apply_settings_plan(
        self, changes: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(changes, Mapping) or not changes:
            raise MeshCoreSettingsValidationError("A non-empty settings plan is required")
        if self._client.config.transport not in _LOCAL_TRANSPORTS:
            raise MeshCoreSettingsUnavailable(
                "MeshCore settings writes require a native BLE, serial, or TCP connection"
            )
        interface, lifecycle_epoch = self._connected_interface()

        def fence() -> None:
            self._ensure_current(interface, lifecycle_epoch)

        async with self._client._native_command_lock:
            fence()
            # The SDK logs outgoing bytes and PIN arguments.  Guard the entire
            # read/validate/write/verify sequence so neither old nor new
            # credentials can enter Home Assistant logs.
            async with suppress_sensitive_library_logs("meshcore"):
                raw = await self._read_raw(interface, fence)
                snapshot = _project_snapshot(raw, interface.commands)
                normalized = self._validate_plan(changes, snapshot)
                result = await self._apply_locked(
                    interface,
                    lifecycle_epoch,
                    raw,
                    normalized,
                )
            fence()
            return result

    def _connected_interface(self) -> tuple[Any, int]:
        interface = self._client._meshcore
        lifecycle_epoch = self._client._lifecycle_epoch
        if interface is None or not self._client.status.connected:
            raise MeshCoreSettingsUnavailable(
                "MeshCore settings require a connected local companion radio"
            )
        if getattr(interface, "commands", None) is None:
            raise MeshCoreSettingsUnavailable(
                "The connected MeshCore interface has no command channel"
            )
        return interface, lifecycle_epoch

    def _ensure_current(self, interface: Any, lifecycle_epoch: int) -> None:
        if (
            self._client._meshcore is not interface
            or not self._client._lifecycle_is_current(lifecycle_epoch)
            or not self._client.status.connected
        ):
            raise MeshCoreSettingsUnavailable(
                "The MeshCore connection changed during the settings operation"
            )

    @staticmethod
    async def _fenced_call(
        operation: Callable[[], Awaitable[Any]], fence: Callable[[], None]
    ) -> Any:
        """Run one command only while its captured connection remains current."""
        fence()
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            # If shutdown/reconnect raced the provider failure, preserve the
            # lifecycle failure instead of misreporting a device rejection.
            fence()
            raise
        fence()
        return result

    async def _read_raw(
        self, interface: Any, fence: Callable[[], None]
    ) -> _RawSettings:
        commands = interface.commands
        query = getattr(commands, "send_device_query", None)
        appstart = getattr(commands, "send_appstart", None)
        if not callable(query) or not callable(appstart):
            raise MeshCoreSettingsUnavailable(
                "The installed MeshCore SDK cannot read companion settings"
            )
        try:
            device = _result_payload(
                await self._fenced_call(query, fence), "device_info"
            )
            self_info = _result_payload(
                await self._fenced_call(appstart, fence), "self_info"
            )
        except MeshCoreSettingsError:
            raise
        except TimeoutError as err:
            raise MeshCoreSettingsUnavailable(
                "Timed out reading MeshCore settings"
            ) from err
        except Exception:
            raise MeshCoreSettingsUnavailable(
                "MeshCore returned invalid device settings"
            ) from None

        max_channels = max(0, _int_value(device.get("max_channels")))
        projected_count = min(max_channels, _MAX_PROJECTED_CHANNELS)
        raw = _RawSettings(
            device=device,
            self_info=self_info,
            projected_channel_count=projected_count,
        )
        get_channel = getattr(commands, "get_channel", None)
        if not callable(get_channel):
            raw.unreadable_channels.update(range(projected_count))
            return raw
        for index in range(projected_count):
            try:
                channel = _result_payload(
                    await self._fenced_call(
                        lambda index=index: get_channel(index), fence
                    ),
                    "channel_info",
                )
            except MeshCoreSettingsRejected:
                raw.unreadable_channels.add(index)
                continue
            except MeshCoreSettingsUnknownState:
                raise
            except MeshCoreSettingsUnavailable:
                raise
            except TimeoutError as err:
                raise MeshCoreSettingsUnavailable(
                    "Timed out reading MeshCore channel settings"
                ) from err
            except Exception:
                raise MeshCoreSettingsUnavailable(
                    "MeshCore returned invalid channel settings"
                ) from None
            raw.channels[index] = channel
        return raw

    @staticmethod
    def _validate_plan(
        changes: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        if len(changes) > 64:
            raise MeshCoreSettingsValidationError("Too many settings changes")
        fields = _flatten_fields(snapshot)
        normalized: dict[str, Any] = {}
        for path, requested in changes.items():
            field = fields.get(path)
            if field is None or not field.get("writable"):
                raise MeshCoreSettingsValidationError(
                    f"MeshCore setting {path} is unavailable or read-only"
                )
            field_type = field["type"]
            if field_type == "secret":
                if not isinstance(requested, Mapping):
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} requires a secret operation"
                    )
                operation = requested.get("operation")
                if operation == "clear" and set(requested) == {"operation"}:
                    if not field.get("allow_clear"):
                        raise MeshCoreSettingsValidationError(
                            f"MeshCore setting {path} cannot be cleared"
                        )
                    normalized[path] = {"operation": "clear"}
                    continue
                if operation != "replace" or set(requested) != {
                    "operation",
                    "value",
                }:
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} has an invalid secret operation"
                    )
                value = requested.get("value")
                if not isinstance(value, str) or not value:
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} requires a non-empty secret"
                    )
                if len(value) > int(field.get("max_length", 2048)):
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} is too long"
                    )
                normalized[path] = {"operation": "replace", "value": value}
                continue

            if field_type == "boolean":
                if not isinstance(requested, bool):
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} must be true or false"
                    )
            elif field_type == "integer":
                if isinstance(requested, bool) or not isinstance(requested, int):
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} must be an integer"
                    )
            elif field_type == "number":
                if (
                    isinstance(requested, bool)
                    or not isinstance(requested, (int, float))
                    or not math.isfinite(float(requested))
                ):
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} must be a finite number"
                    )
            elif field_type == "string":
                if not isinstance(requested, str):
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} must be text"
                    )
                if len(requested) > int(field.get("max_length", 1024)):
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} is too long"
                    )
            elif field_type == "select":
                allowed = {
                    option["value"]
                    for option in field.get("options", [])
                    if isinstance(option, Mapping) and "value" in option
                }
                if requested not in allowed:
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} has an unsupported value"
                    )
            else:
                raise MeshCoreSettingsValidationError(
                    f"MeshCore setting {path} has an unsupported type"
                )

            minimum = field.get("min")
            maximum = field.get("max")
            if minimum is not None and requested < minimum:
                raise MeshCoreSettingsValidationError(
                    f"MeshCore setting {path} is below the hardware-safe range"
                )
            if maximum is not None and requested > maximum:
                raise MeshCoreSettingsValidationError(
                    f"MeshCore setting {path} is above the hardware-safe range"
                )
            normalized[path] = requested

        name = normalized.get("identity.name")
        if name is not None and (not name or len(name.encode("utf-8")) > 32):
            raise MeshCoreSettingsValidationError(
                "MeshCore radio names must contain one to 32 UTF-8 bytes"
            )
        for path, value in normalized.items():
            match = _CHANNEL_PATH_RE.fullmatch(path)
            if not match:
                continue
            if match.group(2) == "name" and _safe_utf8_name(value) is None:
                raise MeshCoreSettingsValidationError(
                    f"MeshCore setting {path} is not a safe name-only channel change"
                )
            if match.group(2) == "secret" and value["operation"] == "replace":
                secret_text = value["value"]
                if len(secret_text) != 32:
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} must be exactly 32 hexadecimal characters"
                    )
                try:
                    bytes.fromhex(secret_text)
                except ValueError as err:
                    raise MeshCoreSettingsValidationError(
                        f"MeshCore setting {path} must be hexadecimal"
                    ) from err
        pin_change = normalized.get("security.pin")
        if pin_change and pin_change["operation"] == "replace":
            pin = pin_change["value"]
            if len(pin) != 6 or not pin.isascii() or not pin.isdigit() or pin[0] == "0":
                raise MeshCoreSettingsValidationError(
                    "MeshCore Bluetooth PIN must be six digits from 100000 to 999999"
                )

        # Validate grouped command state before any write is sent.  A radio or
        # coordinate command always transmits the complete group, including
        # fields that were not edited in this plan.
        if any(path in normalized for path in _RADIO_PATHS):
            for path in _RADIO_PATHS:
                field = fields[path]
                final_value = normalized.get(path, field.get("value"))
                if (
                    isinstance(final_value, bool)
                    or not isinstance(final_value, (int, float))
                    or not math.isfinite(float(final_value))
                    or final_value < field["min"]
                    or final_value > field["max"]
                ):
                    raise MeshCoreSettingsValidationError(
                        "The current MeshCore radio group is not safe to rewrite"
                    )
        if any(
            path in normalized
            for path in ("position.latitude", "position.longitude")
        ):
            for path in ("position.latitude", "position.longitude"):
                field = fields[path]
                final_value = normalized.get(path, field.get("value"))
                if (
                    isinstance(final_value, bool)
                    or not isinstance(final_value, (int, float))
                    or not math.isfinite(float(final_value))
                    or final_value < field["min"]
                    or final_value > field["max"]
                ):
                    raise MeshCoreSettingsValidationError(
                        "The current MeshCore coordinate pair is not safe to rewrite"
                    )

        channel_indexes = {
            int(match.group(1))
            for path in normalized
            if (match := _CHANNEL_PATH_RE.fullmatch(path))
        }
        for index in channel_indexes:
            name_path = f"channels.{index}.name"
            secret_path = f"channels.{index}.secret"
            secret_change = normalized.get(secret_path)
            if (
                name_path in normalized
                and secret_change
                and secret_change["operation"] == "clear"
            ):
                raise MeshCoreSettingsValidationError(
                    "A cleared MeshCore channel cannot also receive a name"
                )
            if secret_change and secret_change["operation"] == "replace":
                final_name = normalized.get(name_path, fields[name_path].get("value"))
                if not isinstance(final_name, str) or not final_name:
                    raise MeshCoreSettingsValidationError(
                        "A MeshCore channel secret requires a configured channel name"
                    )
        return normalized

    async def _apply_locked(
        self,
        interface: Any,
        lifecycle_epoch: int,
        raw: _RawSettings,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        commands = interface.commands
        applied: list[str] = []
        verified: list[str] = []
        warnings: list[str] = []
        connection_updates: _SensitiveConnectionUpdates | None = None

        def result_payload() -> dict[str, Any]:
            result: dict[str, Any] = {
                "applied": sorted(set(applied)),
                "verified": sorted(set(verified)),
                "reconnect_required": (
                    "security.pin" in applied
                    and self._client.config.transport == TRANSPORT_BLUETOOTH
                ),
                "warnings": warnings,
            }
            if connection_updates is not None:
                # Consumed by the coordinator-level config-entry owner. The
                # protocol-neutral public result intentionally never forwards it.
                result["connection_updates"] = connection_updates
            return result

        def stopped_result() -> dict[str, Any]:
            if set(changes) - set(applied):
                warnings.append("plan_stopped_after_unverified_write")
            return result_payload()

        def fence() -> None:
            self._ensure_current(interface, lifecycle_epoch)

        fence()
        if any(path in changes for path in _RADIO_PATHS):
            # Keep this defense at the execution boundary as well as in the
            # projected schema. Firmware- and region-specific RF combinations
            # are not modeled, so this adapter must never call set_radio.
            raise MeshCoreSettingsValidationError(
                "Custom MeshCore radio settings are read-only"
            )

        if "identity.name" in changes:
            desired_name = changes["identity.name"]

            async def verify_name() -> bool:
                info = await self._read_self_info(commands)
                raw.self_info = info
                return str(info.get("name") or "") == desired_name

            if not await self._write_then_verify(
                ["identity.name"],
                lambda: commands.set_name(desired_name),
                verify_name,
                applied,
                verified,
                warnings,
                fence=fence,
            ):
                return stopped_result()

        coordinate_paths = [
            path
            for path in ("position.latitude", "position.longitude")
            if path in changes
        ]
        if coordinate_paths:
            latitude = _safe_number(
                changes.get("position.latitude", raw.self_info.get("adv_lat")),
                minimum=-90.0,
                maximum=90.0,
            )
            longitude = _safe_number(
                changes.get("position.longitude", raw.self_info.get("adv_lon")),
                minimum=-180.0,
                maximum=180.0,
            )
            if latitude is None or longitude is None:
                raise MeshCoreSettingsValidationError(
                    "The current MeshCore coordinate pair is not safe to rewrite"
                )

            async def verify_coordinates() -> bool:
                info = await self._read_self_info(commands)
                raw.self_info = info
                # set_coords always writes the pair. Confirm the preserved
                # coordinate too so a one-field edit cannot hide a second
                # firmware-side mutation.
                return math.isclose(
                    _float_value(info.get("adv_lat")),
                    latitude,
                    abs_tol=0.000001,
                ) and math.isclose(
                    _float_value(info.get("adv_lon")),
                    longitude,
                    abs_tol=0.000001,
                )

            if not await self._write_then_verify(
                coordinate_paths,
                lambda: commands.set_coords(latitude, longitude),
                verify_coordinates,
                applied,
                verified,
                warnings,
                fence=fence,
            ):
                return stopped_result()

        other_paths = [path for path in _OTHER_PARAMETER_PATHS if path in changes]
        if other_paths:
            desired_info = dict(raw.self_info)
            for path in other_paths:
                key = _OTHER_PARAMETER_PATHS[path]
                value = changes[path]
                if path == "position.advertise":
                    value = int(value)
                desired_info[key] = value
            if not all(
                _safe_integer_member(desired_info.get(key), allowed)
                for key, allowed in _OTHER_PARAMETER_DOMAINS.items()
            ):
                raise MeshCoreSettingsValidationError(
                    "The current MeshCore parameter group is not safe to rewrite"
                )

            async def verify_other() -> bool:
                info = await self._read_self_info(commands)
                raw.self_info = info
                # This firmware command rewrites the complete parameter
                # group. Verify the preserved values as well as the fields
                # explicitly requested by the user.
                for key, allowed in _OTHER_PARAMETER_DOMAINS.items():
                    actual = info.get(key)
                    expected = desired_info.get(key)
                    if (
                        not _safe_integer_member(actual, allowed)
                        or int(actual) != int(expected)
                    ):
                        return False
                return True

            if not await self._write_then_verify(
                other_paths,
                lambda: commands.set_other_params_from_infos(desired_info),
                verify_other,
                applied,
                verified,
                warnings,
                fence=fence,
            ):
                return stopped_result()

        # Critical RF-power changes run only after all noncritical mutations.
        if "radio.tx_power_dbm" in changes:
            tx_power = int(changes["radio.tx_power_dbm"])
            current_max_tx_power = _int_value(
                raw.self_info.get("max_tx_power"), -1
            )
            if tx_power < 0 or tx_power > current_max_tx_power:
                raise MeshCoreSettingsValidationError(
                    "The current MeshCore transmit-power limit is not safe to write"
                )

            async def verify_tx_power() -> bool:
                info = await self._read_self_info(commands)
                raw.self_info = info
                return _int_value(info.get("tx_power"), -1) == tx_power

            if not await self._write_then_verify(
                ["radio.tx_power_dbm"],
                lambda: commands.set_tx_power(tx_power),
                verify_tx_power,
                applied,
                verified,
                warnings,
                fence=fence,
            ):
                return stopped_result()

        channel_indexes = sorted(
            {
                int(match.group(1))
                for path in changes
                if (match := _CHANNEL_PATH_RE.fullmatch(path))
            }
        )
        for index in channel_indexes:
            existing = raw.channels[index]
            name_path = f"channels.{index}.name"
            secret_path = f"channels.{index}.secret"
            channel_paths = [path for path in (name_path, secret_path) if path in changes]
            channel_name = changes.get(name_path, existing.get("channel_name"))
            existing_secret = _channel_secret(existing)
            if existing_secret is None:
                raise MeshCoreSettingsValidationError(
                    f"MeshCore channel {index} has no safely readable secret"
                )
            channel_secret = existing_secret
            secret_change = changes.get(secret_path)
            if secret_change:
                if secret_change["operation"] == "clear":
                    channel_name = ""
                    channel_secret = bytes(_CHANNEL_SECRET_BYTES)
                else:
                    channel_secret = bytes.fromhex(secret_change["value"])
            if _safe_utf8_name(channel_name) is None:
                raise MeshCoreSettingsValidationError(
                    f"MeshCore channel {index} has no safely preservable name"
                )

            async def verify_channel(
                index: int = index,
                expected_name: str = channel_name,
                expected_secret: bytes = channel_secret,
            ) -> bool:
                channel = await self._read_channel(commands, index)
                raw.channels[index] = channel
                actual_secret = _channel_secret(channel)
                return (
                    str(channel.get("channel_name") or "") == expected_name
                    and actual_secret is not None
                    and hmac.compare_digest(actual_secret, expected_secret)
                )

            if not await self._write_then_verify(
                channel_paths,
                lambda index=index, channel_name=channel_name, channel_secret=channel_secret: (
                    commands.set_channel(index, channel_name, channel_secret)
                ),
                verify_channel,
                applied,
                verified,
                warnings,
                fence=fence,
            ):
                return stopped_result()

        # Connection credentials are intentionally changed last.  The saved
        # runtime PIN is updated only after the radio read-back confirms it.
        if "security.pin" in changes:
            pin_change = changes["security.pin"]
            pin_text = "0" if pin_change["operation"] == "clear" else pin_change["value"]
            pin_value = int(pin_text)

            async def verify_pin() -> bool:
                device = await self._read_device_info(commands)
                raw.device = device
                return _int_value(device.get("ble_pin"), -1) == pin_value

            if not await self._write_then_verify(
                ["security.pin"],
                lambda: commands.set_devicepin(pin_value),
                verify_pin,
                applied,
                verified,
                warnings,
                fence=fence,
            ):
                return stopped_result()
            if (
                "security.pin" in verified
                and (
                    self._client.config.transport == TRANSPORT_BLUETOOTH
                    or "pin" in self._client.config.options
                )
            ):
                # The adapter cannot mutate Home Assistant connection state.
                # Its verified, repr-redacted handoff is consumed by the
                # coordinator, which first commits the config-entry update and
                # only then changes the running GatewayConfig.
                connection_updates = _SensitiveConnectionUpdates(
                    pin=pin_text if pin_value else None
                )

        return result_payload()

    async def _write_then_verify(
        self,
        paths: list[str],
        write: Callable[[], Awaitable[Any]],
        verify: Callable[[], Awaitable[bool]],
        applied: list[str],
        verified: list[str],
        warnings: list[str],
        *,
        fence: Callable[[], None],
    ) -> bool:
        label = paths[0]
        uncertain = False
        try:
            result = await self._fenced_call(write, fence)
            _ensure_write_ack(result, label)
        except asyncio.CancelledError:
            raise
        except MeshCoreSettingsUnknownState:
            fence()
            uncertain = True
        except TimeoutError:
            fence()
            uncertain = True
        except MeshCoreSettingsRejected:
            fence()
            raise
        except Exception:
            fence()
            # Third-party exception text is not trusted: some SDK paths embed
            # command arguments.  Keep the public/loggable exception fixed and
            # do not chain credential-bearing details into a traceback.
            raise MeshCoreSettingsRejected(
                f"MeshCore rejected setting {label}"
            ) from None

        try:
            confirmed = await self._fenced_call(verify, fence)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            fence()
            if uncertain:
                raise MeshCoreSettingsUnknownState(label, applied) from err
            applied.extend(paths)
            warnings.append("write_acknowledged_readback_unavailable")
            return False

        if uncertain and not confirmed:
            raise MeshCoreSettingsUnknownState(label, applied)
        applied.extend(paths)
        if confirmed:
            verified.extend(paths)
            if uncertain:
                warnings.append("write_confirmed_after_timeout_without_retry")
            return True
        warnings.append("write_acknowledged_readback_mismatch")
        return False

    @staticmethod
    async def _read_self_info(commands: Any) -> dict[str, Any]:
        try:
            return _result_payload(await commands.send_appstart(), "self_info")
        except TimeoutError as err:
            raise MeshCoreSettingsUnavailable(
                "Timed out verifying MeshCore settings"
            ) from err

    @staticmethod
    async def _read_device_info(commands: Any) -> dict[str, Any]:
        try:
            return _result_payload(await commands.send_device_query(), "device_info")
        except TimeoutError as err:
            raise MeshCoreSettingsUnavailable(
                "Timed out verifying MeshCore device settings"
            ) from err

    @staticmethod
    async def _read_channel(commands: Any, index: int) -> dict[str, Any]:
        try:
            return _result_payload(await commands.get_channel(index), "channel_info")
        except TimeoutError as err:
            raise MeshCoreSettingsUnavailable(
                "Timed out verifying MeshCore channel settings"
            ) from err
