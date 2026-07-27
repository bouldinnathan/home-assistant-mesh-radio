"""Guided configuration flow for MeshNet."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import json
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .config_helpers import (
    DEFAULT_MQTT_TOPICS,
    DEFAULT_TCP_PORTS,
    default_gateway_name,
    gateway_from_form,
    gateway_summary,
    supported_transports,
    validate_gateway_dict,
)
from .const import (
    CONF_API_KEY,
    CONF_API_URL,
    CONF_BLE_ADDRESS,
    CONF_GATEWAYS,
    CONF_HISTORY_DAYS,
    CONF_MQTT_TOPIC,
    CONF_NODE_TIMEOUT,
    CONF_PROTOCOL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PATH,
    CONF_TRANSPORT,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_NODE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_MQTT,
    TRANSPORT_NATIVE,
    TRANSPORT_REST,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)
from .serial_devices import SerialDevice, discover_serial_devices


CONF_ACTION = "action"
CONF_ADD_ANOTHER = "add_another"
CONF_VERIFY_CONNECTION = "verify_connection"
CONF_BAUDRATE = "baudrate"
CONF_DEBUG = "debug"
CONF_PIN = "pin"
CONF_PUBLISH_TOPIC = "publish_topic"
CONF_SEND_URL = "send_url"
CONF_MQTT_NODE_ID = "mqtt_node_id"
CONF_CONFIRM = "confirm"
CONF_GATEWAY = "gateway"
CONF_GATEWAYS_JSON = "gateways_json"

PROTOCOL_LABELS = {
    PROTOCOL_MESHTASTIC: "Meshtastic",
    PROTOCOL_MESHCORE: "MeshCore",
}

TRANSPORT_LABELS = {
    TRANSPORT_TCP: "Wi-Fi / Ethernet (TCP)",
    TRANSPORT_SERIAL: "USB serial",
    TRANSPORT_BLUETOOTH: "Local Bluetooth adapter",
    TRANSPORT_MQTT: "MQTT JSON bridge (advanced)",
    TRANSPORT_REST: "REST JSON bridge (advanced)",
    TRANSPORT_NATIVE: "Native SDK over serial (advanced)",
}

OPTIONS_ACTIONS = {
    "add_gateway": "Add a gateway",
    "edit_gateway": "Edit a gateway",
    "remove_gateway": "Remove a gateway",
    "general": "History and timing",
    "advanced": "Advanced JSON editor",
}


class CannotConnectError(Exception):
    """Raised when a gateway endpoint cannot be reached."""


class InvalidAuthError(Exception):
    """Raised when a gateway rejects supplied credentials."""


class MissingDependencyError(Exception):
    """Raised when an optional Home Assistant integration is unavailable."""


class MeshNetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a guided config flow for MeshNet."""

    VERSION = 1

    def __init__(self) -> None:
        self._gateways: list[dict[str, Any]] = []
        self._protocol = PROTOCOL_MESHTASTIC
        self._transport = TRANSPORT_TCP

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Choose the radio platform for the next gateway."""
        if user_input is not None:
            self._protocol = user_input[CONF_PROTOCOL]
            return await self.async_step_connection()
        return self.async_show_form(
            step_id="user",
            data_schema=_protocol_schema(self._protocol),
        )

    async def async_step_connection(self, user_input: dict[str, Any] | None = None):
        """Choose one connection method supported by the platform."""
        transports = _ui_transports(self._protocol)
        if user_input is not None:
            self._transport = user_input[CONF_TRANSPORT]
            return await self.async_step_gateway()
        if self._transport not in transports:
            self._transport = transports[0]
        return self.async_show_form(
            step_id="connection",
            data_schema=_transport_schema(self._protocol, self._transport),
            description_placeholders={"protocol": PROTOCOL_LABELS[self._protocol]},
        )

    async def async_step_gateway(self, user_input: dict[str, Any] | None = None):
        """Collect and validate only the fields used by this transport."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                gateway = gateway_from_form(self._protocol, self._transport, user_input)
                _validate_unique_gateways([*self._gateways, gateway])
                if user_input.get(CONF_VERIFY_CONNECTION, True):
                    await async_validate_connection(self.hass, gateway)
            except ValueError:
                errors["base"] = "invalid_gateway"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except MissingDependencyError:
                errors["base"] = "missing_dependency"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            else:
                self._gateways.append(gateway)
                return await self.async_step_more()
        form_defaults = user_input or {}
        serial_field, serial_default = await _async_serial_field(
            self.hass,
            transport=self._transport,
            current=form_defaults.get(CONF_SERIAL_PATH),
        )
        return self.async_show_form(
            step_id="gateway",
            data_schema=_gateway_schema(
                self._protocol,
                self._transport,
                defaults=form_defaults,
                serial_field=serial_field,
                serial_default=serial_default,
            ),
            errors=errors,
            description_placeholders={
                "protocol": PROTOCOL_LABELS[self._protocol],
                "transport": TRANSPORT_LABELS[self._transport],
            },
        )

    async def async_step_more(self, user_input: dict[str, Any] | None = None):
        """Offer another gateway before creating the single hub entry."""
        if user_input is not None:
            if user_input[CONF_ADD_ANOTHER]:
                self._protocol = PROTOCOL_MESHTASTIC
                self._transport = TRANSPORT_TCP
                return await self.async_step_user()
            return await self.async_step_settings()
        return self.async_show_form(
            step_id="more",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADD_ANOTHER, default=False): cv.boolean}
            ),
            description_placeholders={"gateways": gateway_summary(self._gateways)},
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None):
        """Collect global retention settings and create the entry."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="MeshNet",
                data={
                    CONF_GATEWAYS: deepcopy(self._gateways),
                    CONF_NODE_TIMEOUT: user_input[CONF_NODE_TIMEOUT],
                    CONF_HISTORY_DAYS: user_input[CONF_HISTORY_DAYS],
                },
            )
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(),
            description_placeholders={"gateway_count": str(len(self._gateways))},
        )

    async def async_step_import(self, user_input: dict[str, Any]):
        """Import YAML-style config into a config entry."""
        gateways = user_input.get(CONF_GATEWAYS)
        if isinstance(gateways, dict):
            gateways = [
                {
                    **value,
                    "name": value.get("name") or key,
                    "gateway_id": value.get("gateway_id") or key,
                }
                for key, value in gateways.items()
                if isinstance(value, dict)
            ]
        if not isinstance(gateways, list):
            return self.async_abort(reason="invalid_import")
        try:
            validated = [validate_gateway_dict(item) for item in gateways]
            _validate_unique_gateways(validated)
        except ValueError:
            return self.async_abort(reason="invalid_import")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=user_input.get(CONF_NAME, "MeshNet"),
            data={
                CONF_GATEWAYS: validated,
                CONF_NODE_TIMEOUT: user_input.get(
                    CONF_NODE_TIMEOUT, DEFAULT_NODE_TIMEOUT
                ),
                CONF_HISTORY_DAYS: user_input.get(
                    CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS
                ),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "MeshNetOptionsFlow":
        """Return the modern options flow (HA 2024.11 and newer)."""
        return MeshNetOptionsFlow()


class MeshNetOptionsFlow(config_entries.OptionsFlow):
    """Form-driven add, edit, remove, and advanced options flow."""

    def __init__(self) -> None:
        self._protocol = PROTOCOL_MESHTASTIC
        self._transport = TRANSPORT_TCP
        self._selected_gateway_id: str | None = None

    def _gateways(self) -> list[dict[str, Any]]:
        gateways = self.config_entry.options.get(CONF_GATEWAYS)
        if gateways is None:
            gateways = self.config_entry.data.get(CONF_GATEWAYS, [])
        return deepcopy(list(gateways))

    def _save(self, **updates: Any):
        options = dict(self.config_entry.options)
        options.update(updates)
        return self.async_create_entry(title="", data=options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Choose an easy gateway-management action."""
        if user_input is not None:
            action = user_input[CONF_ACTION]
            return await getattr(self, f"async_step_{action}")()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACTION, default="add_gateway"): vol.In(
                        OPTIONS_ACTIONS
                    )
                }
            ),
            description_placeholders={
                "gateway_count": str(len(self._gateways()))
            },
        )

    async def async_step_add_gateway(
        self, user_input: dict[str, Any] | None = None
    ):
        """Choose the protocol for a new gateway."""
        if user_input is not None:
            self._protocol = user_input[CONF_PROTOCOL]
            return await self.async_step_add_connection()
        return self.async_show_form(
            step_id="add_gateway", data_schema=_protocol_schema(self._protocol)
        )

    async def async_step_add_connection(
        self, user_input: dict[str, Any] | None = None
    ):
        """Choose the connection for a new gateway."""
        if user_input is not None:
            self._transport = user_input[CONF_TRANSPORT]
            return await self.async_step_add_details()
        transports = _ui_transports(self._protocol)
        if self._transport not in transports:
            self._transport = transports[0]
        return self.async_show_form(
            step_id="add_connection",
            data_schema=_transport_schema(self._protocol, self._transport),
            description_placeholders={"protocol": PROTOCOL_LABELS[self._protocol]},
        )

    async def async_step_add_details(
        self, user_input: dict[str, Any] | None = None
    ):
        """Validate and save a new gateway."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                gateway = gateway_from_form(self._protocol, self._transport, user_input)
                gateways = [*self._gateways(), gateway]
                _validate_unique_gateways(gateways)
                if user_input.get(CONF_VERIFY_CONNECTION, True):
                    await async_validate_connection(self.hass, gateway)
            except ValueError:
                errors["base"] = "invalid_gateway"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except MissingDependencyError:
                errors["base"] = "missing_dependency"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            else:
                return self._save(**{CONF_GATEWAYS: gateways})
        form_defaults = user_input or {}
        serial_field, serial_default = await _async_serial_field(
            self.hass,
            transport=self._transport,
            current=form_defaults.get(CONF_SERIAL_PATH),
        )
        return self.async_show_form(
            step_id="add_details",
            data_schema=_gateway_schema(
                self._protocol,
                self._transport,
                defaults=form_defaults,
                serial_field=serial_field,
                serial_default=serial_default,
            ),
            errors=errors,
            description_placeholders={
                "protocol": PROTOCOL_LABELS[self._protocol],
                "transport": TRANSPORT_LABELS[self._transport],
            },
        )

    async def async_step_edit_gateway(
        self, user_input: dict[str, Any] | None = None
    ):
        """Select a gateway to edit."""
        gateways = self._gateways()
        if not gateways:
            return self.async_abort(reason="no_gateways")
        choices = {
            gateway["gateway_id"]: f"{gateway['name']} ({gateway['protocol']} / {gateway['transport']})"
            for gateway in gateways
        }
        if user_input is not None:
            self._selected_gateway_id = user_input[CONF_GATEWAY]
            selected = _find_gateway(gateways, self._selected_gateway_id)
            self._protocol = selected[CONF_PROTOCOL]
            self._transport = selected[CONF_TRANSPORT]
            return await self.async_step_edit_details()
        return self.async_show_form(
            step_id="edit_gateway",
            data_schema=vol.Schema(
                {vol.Required(CONF_GATEWAY): vol.In(choices)}
            ),
        )

    async def async_step_edit_details(
        self, user_input: dict[str, Any] | None = None
    ):
        """Edit and validate a selected gateway."""
        gateways = self._gateways()
        selected = _find_gateway(gateways, self._selected_gateway_id)
        defaults = _gateway_form_defaults(selected)
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                replacement = gateway_from_form(
                    self._protocol,
                    self._transport,
                    user_input,
                    gateway_id=selected["gateway_id"],
                )
                updated = [
                    replacement
                    if gateway["gateway_id"] == selected["gateway_id"]
                    else gateway
                    for gateway in gateways
                ]
                _validate_unique_gateways(updated)
                if user_input.get(CONF_VERIFY_CONNECTION, True):
                    await async_validate_connection(self.hass, replacement)
            except ValueError:
                errors["base"] = "invalid_gateway"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except MissingDependencyError:
                errors["base"] = "missing_dependency"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            else:
                return self._save(**{CONF_GATEWAYS: updated})
        form_defaults = user_input or defaults
        serial_field, serial_default = await _async_serial_field(
            self.hass,
            transport=self._transport,
            current=form_defaults.get(CONF_SERIAL_PATH),
        )
        return self.async_show_form(
            step_id="edit_details",
            data_schema=_gateway_schema(
                self._protocol,
                self._transport,
                defaults=form_defaults,
                serial_field=serial_field,
                serial_default=serial_default,
            ),
            errors=errors,
            description_placeholders={
                "protocol": PROTOCOL_LABELS[self._protocol],
                "transport": TRANSPORT_LABELS[self._transport],
            },
        )

    async def async_step_remove_gateway(
        self, user_input: dict[str, Any] | None = None
    ):
        """Remove a gateway after explicit confirmation."""
        gateways = self._gateways()
        if not gateways:
            return self.async_abort(reason="no_gateways")
        choices = {
            gateway["gateway_id"]: gateway["name"] for gateway in gateways
        }
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_CONFIRM]:
                errors["base"] = "confirmation_required"
            else:
                remaining = [
                    gateway
                    for gateway in gateways
                    if gateway["gateway_id"] != user_input[CONF_GATEWAY]
                ]
                return self._save(**{CONF_GATEWAYS: remaining})
        return self.async_show_form(
            step_id="remove_gateway",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GATEWAY): vol.In(choices),
                    vol.Required(CONF_CONFIRM, default=False): cv.boolean,
                }
            ),
            errors=errors,
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ):
        """Edit retention and polling settings."""
        if user_input is not None:
            return self._save(**user_input)
        return self.async_show_form(
            step_id="general",
            data_schema=_settings_schema(
                node_timeout=self.config_entry.options.get(
                    CONF_NODE_TIMEOUT,
                    self.config_entry.data.get(
                        CONF_NODE_TIMEOUT, DEFAULT_NODE_TIMEOUT
                    ),
                ),
                history_days=self.config_entry.options.get(
                    CONF_HISTORY_DAYS,
                    self.config_entry.data.get(
                        CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS
                    ),
                ),
                scan_interval=self.config_entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
                include_scan_interval=True,
            ),
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ):
        """Keep a JSON escape hatch for custom bridge options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                parsed = json.loads(user_input[CONF_GATEWAYS_JSON])
                if not isinstance(parsed, list):
                    raise ValueError("gateways_json must be a list")
                gateways = [validate_gateway_dict(item) for item in parsed]
                _validate_unique_gateways(gateways)
            except (TypeError, ValueError, json.JSONDecodeError):
                errors[CONF_GATEWAYS_JSON] = "invalid_gateways"
            else:
                return self._save(**{CONF_GATEWAYS: gateways})
        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GATEWAYS_JSON,
                        default=json.dumps(
                            self._gateways(), indent=2, sort_keys=True
                        ),
                    ): cv.string
                }
            ),
            errors=errors,
        )


def _protocol_schema(default: str) -> vol.Schema:
    return vol.Schema(
        {vol.Required(CONF_PROTOCOL, default=default): vol.In(PROTOCOL_LABELS)}
    )


def _transport_schema(protocol: str, default: str) -> vol.Schema:
    labels = {
        transport: TRANSPORT_LABELS[transport]
        for transport in _ui_transports(protocol)
    }
    return vol.Schema(
        {vol.Required(CONF_TRANSPORT, default=default): vol.In(labels)}
    )


def _ui_transports(protocol: str) -> tuple[str, ...]:
    """Hide MeshCore's legacy ``native`` alias, which is identical to serial."""
    return tuple(
        transport
        for transport in supported_transports(protocol)
        if transport != TRANSPORT_NATIVE
    )


def _gateway_schema(
    protocol: str,
    transport: str,
    *,
    defaults: dict[str, Any] | None = None,
    serial_field: Any | None = None,
    serial_default: str | None = None,
) -> vol.Schema:
    defaults = defaults or {}
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_NAME,
            default=defaults.get(
                CONF_NAME, default_gateway_name(protocol, transport)
            ),
        ): cv.string,
    }

    if transport == TRANSPORT_TCP:
        fields[vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, ""))] = cv.string
        port_default = defaults.get(CONF_PORT, DEFAULT_TCP_PORTS.get(protocol))
        port_marker = (
            vol.Required(CONF_PORT, default=port_default)
            if port_default is not None
            else vol.Required(CONF_PORT)
        )
        fields[port_marker] = vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        )
    elif transport in {TRANSPORT_SERIAL, TRANSPORT_NATIVE}:
        serial_default = defaults.get(CONF_SERIAL_PATH) or serial_default
        marker = (
            vol.Required(CONF_SERIAL_PATH, default=serial_default)
            if serial_default
            else vol.Required(CONF_SERIAL_PATH)
        )
        fields[marker] = serial_field or TextSelector(TextSelectorConfig())
        if protocol == PROTOCOL_MESHCORE:
            fields[
                vol.Required(
                    CONF_BAUDRATE, default=defaults.get(CONF_BAUDRATE, 115200)
                )
            ] = vol.All(vol.Coerce(int), vol.Range(min=1200, max=3000000))
            fields[
                vol.Required(CONF_DEBUG, default=defaults.get(CONF_DEBUG, False))
            ] = cv.boolean
    elif transport == TRANSPORT_BLUETOOTH:
        fields[
            vol.Required(
                CONF_BLE_ADDRESS, default=defaults.get(CONF_BLE_ADDRESS, "")
            )
        ] = cv.string
        if protocol == PROTOCOL_MESHCORE:
            fields[vol.Optional(CONF_PIN, default=defaults.get(CONF_PIN, ""))] = cv.string
    elif transport == TRANSPORT_MQTT:
        fields[
            vol.Required(
                CONF_MQTT_TOPIC,
                default=defaults.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPICS[protocol]),
            )
        ] = cv.string
        fields[
            vol.Optional(
                CONF_PUBLISH_TOPIC,
                default=defaults.get(CONF_PUBLISH_TOPIC, ""),
            )
        ] = cv.string
        if protocol == PROTOCOL_MESHTASTIC:
            fields[
                vol.Optional(
                    CONF_MQTT_NODE_ID,
                    default=defaults.get(CONF_MQTT_NODE_ID, ""),
                )
            ] = cv.string
    elif transport == TRANSPORT_REST:
        fields[
            vol.Required(CONF_API_URL, default=defaults.get(CONF_API_URL, ""))
        ] = cv.string
        fields[
            vol.Optional(CONF_API_KEY, default=defaults.get(CONF_API_KEY, ""))
        ] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
        fields[
            vol.Optional(CONF_SEND_URL, default=defaults.get(CONF_SEND_URL, ""))
        ] = cv.string

    fields[
        vol.Required(
            CONF_VERIFY_CONNECTION,
            default=defaults.get(CONF_VERIFY_CONNECTION, True),
        )
    ] = cv.boolean
    return vol.Schema(fields)


def _settings_schema(
    *,
    node_timeout: int = DEFAULT_NODE_TIMEOUT,
    history_days: int = DEFAULT_HISTORY_DAYS,
    scan_interval: int = DEFAULT_SCAN_INTERVAL,
    include_scan_interval: bool = False,
) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_NODE_TIMEOUT, default=node_timeout): vol.All(
            vol.Coerce(int), vol.Range(min=30, max=86400)
        ),
        vol.Required(CONF_HISTORY_DAYS, default=history_days): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=3650)
        ),
    }
    if include_scan_interval:
        fields[
            vol.Required(CONF_SCAN_INTERVAL, default=scan_interval)
        ] = vol.All(vol.Coerce(int), vol.Range(min=5, max=3600))
    return vol.Schema(fields)


async def _async_serial_field(
    hass: Any,
    *,
    transport: str,
    current: str | None = None,
) -> tuple[Any | None, str | None]:
    """Build a non-blocking picker containing local USB serial paths."""
    if transport not in {TRANSPORT_SERIAL, TRANSPORT_NATIVE}:
        return None, None

    devices = await hass.async_add_executor_job(discover_serial_devices)
    return _serial_field(devices, current=current)


def _serial_field(
    devices: list[SerialDevice], *, current: str | None = None
) -> tuple[Any, str | None]:
    """Build a dropdown that always accepts an advanced manual path."""
    options = [
        SelectOptionDict(value=device.path, label=device.label) for device in devices
    ]
    detected_paths = {device.path for device in devices}
    if current and current not in detected_paths:
        options.insert(
            0,
            SelectOptionDict(
                value=current,
                label=f"Currently configured (not detected) — {current}",
            ),
        )

    default = current or (devices[0].path if devices else None)
    if not options:
        return TextSelector(TextSelectorConfig()), default
    return (
        SelectSelector(
            SelectSelectorConfig(
                options=options,
                mode=SelectSelectorMode.DROPDOWN,
                custom_value=True,
            )
        ),
        default,
    )


def _gateway_form_defaults(gateway: dict[str, Any]) -> dict[str, Any]:
    defaults = dict(gateway)
    defaults.update(gateway.get("options") or {})
    defaults[CONF_VERIFY_CONNECTION] = True
    return defaults


def _find_gateway(
    gateways: list[dict[str, Any]], gateway_id: str | None
) -> dict[str, Any]:
    for gateway in gateways:
        if gateway["gateway_id"] == gateway_id:
            return gateway
    raise ValueError(f"Unknown gateway: {gateway_id}")


def _validate_unique_gateways(gateways: list[dict[str, Any]]) -> None:
    ids = [gateway["gateway_id"] for gateway in gateways]
    if len(ids) != len(set(ids)):
        raise ValueError("gateway IDs must be unique")


async def async_validate_connection(hass: Any, gateway: dict[str, Any]) -> None:
    """Perform a quick, non-destructive preflight from Home Assistant itself."""
    transport = gateway[CONF_TRANSPORT]
    if transport == TRANSPORT_TCP:
        await _async_probe_tcp(gateway[CONF_HOST], gateway[CONF_PORT])
        return
    if transport in {TRANSPORT_SERIAL, TRANSPORT_NATIVE}:
        readable, writable = await hass.async_add_executor_job(
            _serial_access, gateway[CONF_SERIAL_PATH]
        )
        if not readable or not writable:
            raise CannotConnectError(
                "Serial device is missing or is not readable/writable by Home Assistant"
            )
        return
    if transport == TRANSPORT_MQTT:
        configured = "mqtt" in hass.config.components
        if not configured:
            with suppress(Exception):
                configured = bool(hass.config_entries.async_entries("mqtt"))
        if not configured:
            raise MissingDependencyError("Configure Home Assistant MQTT first")
        return
    if transport == TRANSPORT_REST:
        await _async_probe_rest(hass, gateway)
        return
    # BLE SDKs need to own the connection in order to verify it.  Address
    # presence is validated structurally; runtime setup reports adapter errors.


async def _async_probe_tcp(host: str, port: int) -> None:
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(5):
            _reader, writer = await asyncio.open_connection(host, port)
    except (TimeoutError, OSError) as err:
        raise CannotConnectError(str(err)) from err
    finally:
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


async def _async_probe_rest(hass: Any, gateway: dict[str, Any]) -> None:
    import aiohttp

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    headers = {}
    if gateway.get(CONF_API_KEY):
        headers["Authorization"] = f"Bearer {gateway[CONF_API_KEY]}"
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(8):
            async with session.get(gateway[CONF_API_URL], headers=headers) as response:
                if response.status in {401, 403}:
                    raise InvalidAuthError
                if response.status >= 400:
                    raise CannotConnectError(f"HTTP {response.status}")
    except InvalidAuthError:
        raise
    except (TimeoutError, OSError, aiohttp.ClientError) as err:
        raise CannotConnectError(str(err)) from err


def _serial_access(path: str) -> tuple[bool, bool]:
    return os.path.exists(path) and os.access(path, os.R_OK), os.path.exists(
        path
    ) and os.access(path, os.W_OK)


# Backward-compatible helper names used by existing imports and older tests.
def _validate_gateway_dict(data: dict[str, Any]) -> dict[str, Any]:
    return validate_gateway_dict(data)


def _validate_transport_requirements(data: dict[str, Any]) -> None:
    validate_gateway_dict(data)


def _single_gateway_schema() -> vol.Schema:
    """Return a legacy-compatible schema for third-party callers.

    The Home Assistant UI no longer uses this all-fields form.
    """
    return _gateway_schema(PROTOCOL_MESHTASTIC, TRANSPORT_TCP)
