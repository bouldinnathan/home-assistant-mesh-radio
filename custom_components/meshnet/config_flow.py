"""Guided configuration flow for MeshNet."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from copy import deepcopy
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .bluetooth_devices import (
    BluetoothDevice,
    async_discover_meshtastic_devices,
    bluetooth_select_options,
    normalize_bluetooth_address,
)
from .bluetooth_pairing import (
    AmbiguousBluetoothDeviceError,
    BluetoothDeviceNotFoundError,
    BluetoothPairingManager,
    BluetoothUnavailableError,
    InvalidBluetoothAddressError,
    InvalidPinError,
    NotMeshtasticDeviceError,
    PairingAttempt,
    PairingCancelledError,
    PairingCleanupIncompleteError,
    PairingError,
    PairingOwnershipPendingError,
    PairingRateLimitedError,
    PairingRejectedError,
    PairingResult,
    PairingStateError,
    PairingTimeoutError,
    PinPromptTimeoutError,
    ProvisionalBond,
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
    CONF_BLUETOOTH_ADAPTER,
    CONF_BLUETOOTH_ADAPTER_ADDRESS,
    CONF_BLUETOOTH_BOND_MANAGED,
    CONF_GATEWAY_ID,
    CONF_GATEWAYS,
    CONF_HISTORY_DAYS,
    CONF_MAINTENANCE_ENABLED,
    CONF_MAINTENANCE_GATEWAY_ID,
    CONF_MAINTENANCE_INTERVAL,
    CONF_MAINTENANCE_MAX_REQUESTS,
    CONF_MAINTENANCE_QUIET_TIME,
    CONF_MQTT_TOPIC,
    CONF_NODE_TIMEOUT,
    CONF_PROTOCOL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PATH,
    CONF_TRANSPORT,
    DATA_BLUETOOTH_PAIRING,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MAINTENANCE_ENABLED,
    DEFAULT_MAINTENANCE_INTERVAL,
    DEFAULT_MAINTENANCE_MAX_REQUESTS,
    DEFAULT_MAINTENANCE_QUIET_TIME,
    DEFAULT_NODE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAINTENANCE_MAX_INTERVAL_SECONDS,
    MAINTENANCE_MAX_QUIET_SECONDS,
    MAINTENANCE_MAX_REQUESTS,
    MAINTENANCE_MIN_INTERVAL_SECONDS,
    MAINTENANCE_MIN_QUIET_SECONDS,
    MAINTENANCE_MIN_REQUESTS,
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

_LOGGER = logging.getLogger(__name__)

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
CONF_PAIRING_PIN = "pairing_pin"
CONF_READY_TO_PAIR = "ready_to_pair"
CONF_REMOVE_BLUETOOTH_BOND = "remove_bluetooth_bond"

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
    "maintenance": "Automatic network maintenance",
    "advanced": "Advanced JSON editor",
}


class CannotConnectError(Exception):
    """Raised when a gateway endpoint cannot be reached."""


class InvalidAuthError(Exception):
    """Raised when a gateway rejects supplied credentials."""


class MissingDependencyError(Exception):
    """Raised when an optional Home Assistant integration is unavailable."""


class BluetoothOwnershipChangeError(ValueError):
    """Raised when an edit would orphan or forge an owned Bluetooth bond."""


class BluetoothGuidedSetupRequiredError(ValueError):
    """Raised when an unpaired Bluetooth gateway bypasses the pairing wizard."""


class _MeshtasticPairingFlowMixin:
    """Shared, PIN-safe pairing steps for config and options flows."""

    def _init_pairing_flow(self) -> None:
        self._pairing_gateway: dict[str, Any] | None = None
        self._pairing_return_step: str | None = None
        self._pairing_error: str | None = None
        self._pairing_attempt: PairingAttempt | None = None
        self._pairing_begin_task: asyncio.Task[PairingAttempt] | None = None
        self._pairing_submit_task: asyncio.Task[PairingResult] | None = None
        self._pairing_result: PairingResult | None = None
        self._provisional_bonds: set[tuple[str, str, str]] = set()

    def _remember_provisional_bond(self, bond: ProvisionalBond | None) -> None:
        """Retain a non-secret cleanup key until Home Assistant commits it."""
        if bond is not None:
            self._provisional_bonds.add(
                (bond.adapter, bond.adapter_address, bond.address)
            )

    def _remember_provisional_bonds(
        self, bonds: tuple[ProvisionalBond, ...]
    ) -> None:
        """Retain all cleanup keys from one ambiguous pairing transaction."""
        for bond in bonds:
            self._remember_provisional_bond(bond)

    def _discard_provisional_bonds(
        self, bonds: tuple[ProvisionalBond, ...]
    ) -> None:
        """Drop superseded aliases without touching the current BlueZ bond."""
        for bond in bonds:
            self._provisional_bonds.discard(
                (bond.adapter, bond.adapter_address, bond.address)
            )

    def _remember_pairing_error(self, error: PairingError) -> None:
        """Preserve rollback ownership before reducing an error to a UI key."""
        if isinstance(error, PairingCleanupIncompleteError):
            self._remember_provisional_bonds(error.bonds)
        self._pairing_error = _pairing_error_key(error)

    async def _async_prepare_gateway(
        self,
        gateway: dict[str, Any],
        *,
        return_step: str,
    ):
        """Start an explicit pairing confirmation for Meshtastic BLE."""
        if not _is_meshtastic_bluetooth(gateway):
            await async_validate_connection(self.hass, gateway)
            return None
        self._pairing_gateway = deepcopy(gateway)
        self._pairing_return_step = return_step
        self._pairing_error = None
        return await self.async_step_pair_intro()

    async def async_step_pair_intro(
        self, user_input: dict[str, Any] | None = None
    ):
        """Require explicit readiness before creating a Bluetooth bond."""
        if self._pairing_gateway is None:
            return self.async_abort(reason="pairing_state_lost")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_READY_TO_PAIR, False):
                errors["base"] = "pairing_confirmation_required"
            else:
                if self._pairing_begin_task is None:
                    manager = _async_pairing_manager(self.hass)
                    self._pairing_begin_task = self.hass.async_create_task(
                        manager.async_begin(
                            self._pairing_gateway[CONF_BLE_ADDRESS]
                        )
                    )
                return await self.async_step_pairing()
        return self.async_show_form(
            step_id="pair_intro",
            data_schema=vol.Schema(
                {vol.Required(CONF_READY_TO_PAIR, default=False): cv.boolean}
            ),
            errors=errors,
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ):
        """Show bounded progress while BlueZ begins one exact pairing."""
        del user_input
        task = self._pairing_begin_task
        if task is None:
            return self.async_abort(reason="pairing_state_lost")
        if not task.done():
            return self.async_show_progress(
                step_id="pairing",
                progress_action="pairing",
                progress_task=task,
            )
        try:
            attempt = task.result()
        except PairingError as err:
            self._remember_pairing_error(err)
            return self.async_show_progress_done(
                next_step_id=self._pairing_return_step or "user"
            )
        except asyncio.CancelledError:
            self._pairing_error = "pairing_cancelled"
            return self.async_show_progress_done(
                next_step_id=self._pairing_return_step or "user"
            )
        self._pairing_attempt = attempt
        provisional_bonds = getattr(attempt, "provisional_bonds", ())
        if not provisional_bonds and (
            provisional_bond := getattr(attempt, "provisional_bond", None)
        ):
            provisional_bonds = (provisional_bond,)
        if provisional_bonds:
            self._remember_provisional_bonds(provisional_bonds)
            self._pairing_error = "pairing_cleanup_incomplete"
            return self.async_show_progress_done(
                next_step_id=self._pairing_return_step or "user"
            )
        if attempt.requires_pin:
            return self.async_show_progress_done(next_step_id="pair_pin")
        result = attempt.result
        if result is None:
            self._pairing_error = "pairing_failed"
            return self.async_show_progress_done(
                next_step_id=self._pairing_return_step or "user"
            )
        self._pairing_result = result
        return self.async_show_progress_done(next_step_id="pair_finish")

    async def async_step_pair_pin(
        self, user_input: dict[str, Any] | None = None
    ):
        """Collect one six-digit PIN without retaining it in flow state."""
        if self._pairing_submit_task is not None:
            if user_input is not None:
                user_input.clear()
            return await self.async_step_pairing_submit()
        attempt = self._pairing_attempt
        if attempt is None or not attempt.requires_pin:
            return self.async_abort(reason="pairing_state_lost")
        errors: dict[str, str] = {}
        if user_input is not None:
            pin = user_input.get(CONF_PAIRING_PIN)
            if not isinstance(pin, str) or not pin.isascii() or not (
                len(pin) == 6 and pin.isdecimal()
            ):
                errors[CONF_PAIRING_PIN] = "invalid_pin"
            else:
                self._pairing_submit_task = self.hass.async_create_task(
                    attempt.async_submit_pin(pin)
                )
                # Do not retain the submitted secret in a form-default mapping.
                user_input.clear()
                pin = ""
                return await self.async_step_pairing_submit()
        return self.async_show_form(
            step_id="pair_pin",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_PIN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_pairing_submit(
        self, user_input: dict[str, Any] | None = None
    ):
        """Show progress while BlueZ verifies the submitted PIN and bond."""
        del user_input
        task = self._pairing_submit_task
        if task is None:
            return self.async_abort(reason="pairing_state_lost")
        if not task.done():
            return self.async_show_progress(
                step_id="pairing_submit",
                progress_action="verifying_pairing",
                progress_task=task,
            )
        try:
            self._pairing_result = task.result()
        except PairingError as err:
            self._remember_pairing_error(err)
            return self.async_show_progress_done(
                next_step_id=self._pairing_return_step or "user"
            )
        except asyncio.CancelledError:
            self._pairing_error = "pairing_cancelled"
            return self.async_show_progress_done(
                next_step_id=self._pairing_return_step or "user"
            )
        return self.async_show_progress_done(next_step_id="pair_finish")

    async def async_step_pair_finish(
        self, user_input: dict[str, Any] | None = None
    ):
        """Persist only verified, non-secret pairing metadata."""
        del user_input
        gateway = self._pairing_gateway
        result = self._pairing_result
        if gateway is None or result is None:
            return self.async_abort(reason="pairing_state_lost")
        gateway[CONF_BLE_ADDRESS] = result.address
        self._discard_provisional_bonds(
            getattr(self._pairing_attempt, "retired_bonds", ())
        )
        options = dict(gateway.get("options") or {})
        options[CONF_BLUETOOTH_ADAPTER] = result.adapter
        options[CONF_BLUETOOTH_ADAPTER_ADDRESS] = result.adapter_address
        if result.bond_created:
            options[CONF_BLUETOOTH_BOND_MANAGED] = True
        elif not options.get(CONF_BLUETOOTH_BOND_MANAGED):
            options.pop(CONF_BLUETOOTH_BOND_MANAGED, None)
        gateway["options"] = options
        if result.bond_created:
            self._remember_provisional_bond(
                ProvisionalBond(
                    address=result.address,
                    adapter=result.adapter,
                    adapter_address=result.adapter_address,
                )
            )
        self._clear_pairing_tasks()
        return await self._async_save_paired_gateway(gateway)

    @callback
    def async_remove(self) -> None:
        """Cancel abandoned work and preserve ambiguous external bonds."""
        if not (
            self._provisional_bonds
            or self._pairing_attempt is not None
            or self._pairing_begin_task is not None
            or self._pairing_submit_task is not None
            or self._pairing_result is not None
        ):
            return
        self.hass.async_create_task(self._async_cleanup_removed_pairing_flow())

    async def _async_cleanup_removed_pairing_flow(self) -> None:
        """Finish active work and release ambiguous process-local bond proof."""
        begin_task = self._pairing_begin_task
        submit_task = self._pairing_submit_task
        attempt = self._pairing_attempt
        result = self._pairing_result

        for task in (begin_task, submit_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (begin_task, submit_task):
            if task is not None:
                with suppress(BaseException):
                    await task
                if task.done() and not task.cancelled():
                    with suppress(BaseException):
                        error = task.exception()
                        if isinstance(error, PairingCleanupIncompleteError):
                            self._remember_provisional_bonds(error.bonds)

        if attempt is None and begin_task is not None and begin_task.done():
            with suppress(BaseException):
                attempt = begin_task.result()
        if result is None and submit_task is not None and submit_task.done():
            with suppress(BaseException):
                result = submit_task.result()
        if attempt is not None:
            await attempt.async_cancel()
            attempt_bonds = getattr(attempt, "provisional_bonds", ())
            if not attempt_bonds and (
                attempt_bond := getattr(attempt, "provisional_bond", None)
            ):
                attempt_bonds = (attempt_bond,)
            self._remember_provisional_bonds(attempt_bonds)
            self._discard_provisional_bonds(
                getattr(attempt, "retired_bonds", ())
            )
            if result is None:
                with suppress(BaseException):
                    result = attempt.result
        if result is not None and result.bond_created:
            self._remember_provisional_bond(
                ProvisionalBond(
                    address=result.address,
                    adapter=result.adapter,
                    adapter_address=result.adapter_address,
                )
            )

        # A CREATE_ENTRY result is only a request.  Home Assistant applies the
        # config entry/options afterward, then calls async_remove().  Inspect
        # that committed in-memory state here instead of clearing ownership at
        # form-return time, which could orphan a bond if finalization aborted.
        manager = _async_pairing_manager(self.hass)
        committed_bonds = self._provisional_bonds.intersection(
            _persisted_owned_bond_keys(self.hass)
        )
        for adapter, adapter_address, address in committed_bonds:
            # The config entry is now the durable authority.  Do not leave an
            # ephemeral generation-less proof that could later claim a bond
            # another client removed and recreated at the same address.
            manager.release_created(
                address,
                adapter=adapter,
                adapter_address=adapter_address,
            )
        self._provisional_bonds.difference_update(committed_bonds)

        for adapter, adapter_address, address in tuple(self._provisional_bonds):
            # BlueZ has no bond-generation token.  Once the transactional
            # rollback window has ended, automatically calling RemoveDevice
            # could delete a bond another client recreated at the same path.
            # Preserve external state and merely forget our ephemeral proof.
            manager.release_created(
                address,
                adapter=adapter,
                adapter_address=adapter_address,
            )
            self._provisional_bonds.discard(
                (adapter, adapter_address, address)
            )
        self._clear_pairing_tasks()

    def _consume_pairing_error(
        self, step_id: str
    ) -> tuple[str | None, dict[str, Any]]:
        """Return one pending safe error and the non-secret gateway defaults."""
        if self._pairing_return_step != step_id or self._pairing_error is None:
            return None, {}
        error = self._pairing_error
        defaults = deepcopy(self._pairing_gateway or {})
        self._clear_pairing_tasks()
        return error, defaults

    def _clear_pairing_tasks(self) -> None:
        """Drop flow references after backend cleanup has completed."""
        self._pairing_gateway = None
        self._pairing_return_step = None
        self._pairing_error = None
        self._pairing_attempt = None
        self._pairing_begin_task = None
        self._pairing_submit_task = None
        self._pairing_result = None

    async def _async_save_paired_gateway(self, gateway: dict[str, Any]):
        raise NotImplementedError


class MeshNetConfigFlow(
    _MeshtasticPairingFlowMixin,
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Handle a guided config flow for MeshNet."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._gateways: list[dict[str, Any]] = []
        self._protocol = PROTOCOL_MESHTASTIC
        self._transport = TRANSPORT_TCP
        self._discovered_ble_address: str | None = None
        self._init_pairing_flow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Choose the radio platform for the next gateway."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._protocol = user_input[CONF_PROTOCOL]
            return await self.async_step_connection()
        return self.async_show_form(
            step_id="user",
            data_schema=_protocol_schema(self._protocol),
        )

    async def async_step_bluetooth(self, discovery_info: Any):
        """Pre-fill the guided flow from a Meshtastic advertisement."""
        try:
            address = normalize_bluetooth_address(discovery_info.address)
        except (AttributeError, ValueError):
            return self.async_abort(reason="invalid_discovery")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self._protocol = PROTOCOL_MESHTASTIC
        self._transport = TRANSPORT_BLUETOOTH
        self._discovered_ble_address = address
        return await self.async_step_gateway()

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
        pairing_error, pairing_defaults = self._consume_pairing_error("gateway")
        if pairing_error:
            errors["base"] = pairing_error
        if user_input is not None:
            try:
                gateway = gateway_from_form(self._protocol, self._transport, user_input)
                _validate_unique_gateways([*self._gateways, gateway])
                if _is_meshtastic_bluetooth(gateway):
                    return await self._async_prepare_gateway(
                        gateway, return_step="gateway"
                    )
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
        form_defaults = user_input or pairing_defaults
        if not form_defaults and self._discovered_ble_address:
            form_defaults = {CONF_BLE_ADDRESS: self._discovered_ble_address}
        serial_field, serial_default = await _async_serial_field(
            self.hass,
            transport=self._transport,
            current=form_defaults.get(CONF_SERIAL_PATH),
        )
        bluetooth_field, bluetooth_default = await _async_bluetooth_field(
            self.hass,
            protocol=self._protocol,
            transport=self._transport,
            current=form_defaults.get(CONF_BLE_ADDRESS),
        )
        return self.async_show_form(
            step_id="gateway",
            data_schema=_gateway_schema(
                self._protocol,
                self._transport,
                defaults=form_defaults,
                serial_field=serial_field,
                serial_default=serial_default,
                bluetooth_field=bluetooth_field,
                bluetooth_default=bluetooth_default,
            ),
            errors=errors,
            description_placeholders={
                "protocol": PROTOCOL_LABELS[self._protocol],
                "transport": TRANSPORT_LABELS[self._transport],
            },
        )

    async def _async_save_paired_gateway(self, gateway: dict[str, Any]):
        """Persist a verified bond immediately so it cannot become orphaned."""
        _validate_unique_gateways([*self._gateways, gateway])
        self._gateways.append(gateway)
        self._discovered_ble_address = None
        result = self.async_create_entry(
            title="MeshNet",
            data={
                CONF_GATEWAYS: deepcopy(self._gateways),
                CONF_NODE_TIMEOUT: DEFAULT_NODE_TIMEOUT,
                CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
            },
        )
        return result

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
            validated = [
                _strip_untrusted_bluetooth_ownership(validate_gateway_dict(item))
                for item in gateways
            ]
            _validate_unique_gateways(validated)
            if any(_is_meshtastic_bluetooth(item) for item in validated):
                raise BluetoothGuidedSetupRequiredError
        except BluetoothGuidedSetupRequiredError:
            return self.async_abort(reason="bluetooth_requires_gui")
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
    ) -> MeshNetOptionsFlow:
        """Return the modern options flow (HA 2024.11 and newer)."""
        return MeshNetOptionsFlow()


class MeshNetOptionsFlow(_MeshtasticPairingFlowMixin, config_entries.OptionsFlow):
    """Form-driven add, edit, remove, and advanced options flow."""

    def __init__(self) -> None:
        self._protocol = PROTOCOL_MESHTASTIC
        self._transport = TRANSPORT_TCP
        self._selected_gateway_id: str | None = None
        self._pairing_save_action: str | None = None
        self._init_pairing_flow()

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
        pairing_error, pairing_defaults = self._consume_pairing_error(
            "add_details"
        )
        if pairing_error:
            errors["base"] = pairing_error
        if user_input is not None:
            try:
                gateway = gateway_from_form(self._protocol, self._transport, user_input)
                gateways = [*self._gateways(), gateway]
                _validate_unique_gateways(gateways)
                if _is_meshtastic_bluetooth(gateway):
                    self._pairing_save_action = "add"
                    return await self._async_prepare_gateway(
                        gateway, return_step="add_details"
                    )
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
        form_defaults = user_input or pairing_defaults
        serial_field, serial_default = await _async_serial_field(
            self.hass,
            transport=self._transport,
            current=form_defaults.get(CONF_SERIAL_PATH),
        )
        bluetooth_field, bluetooth_default = await _async_bluetooth_field(
            self.hass,
            protocol=self._protocol,
            transport=self._transport,
            current=form_defaults.get(CONF_BLE_ADDRESS),
        )
        return self.async_show_form(
            step_id="add_details",
            data_schema=_gateway_schema(
                self._protocol,
                self._transport,
                defaults=form_defaults,
                serial_field=serial_field,
                serial_default=serial_default,
                bluetooth_field=bluetooth_field,
                bluetooth_default=bluetooth_default,
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
        pairing_error, pairing_defaults = self._consume_pairing_error(
            "edit_details"
        )
        if pairing_error:
            errors["base"] = pairing_error
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
                if _is_meshtastic_bluetooth(replacement):
                    if (
                        _has_meshnet_owned_bond(selected)
                        and selected.get(CONF_BLE_ADDRESS)
                        != replacement.get(CONF_BLE_ADDRESS)
                    ):
                        raise BluetoothOwnershipChangeError
                    _preserve_bluetooth_ownership(selected, replacement)
                    if _has_meshnet_owned_bond(selected):
                        # Name/options edits keep the exact already-verified
                        # adapter-scoped historical record. Moving that marker
                        # requires guided removal followed by a fresh add.
                        return self._save(**{CONF_GATEWAYS: updated})
                    self._pairing_save_action = "edit"
                    return await self._async_prepare_gateway(
                        replacement, return_step="edit_details"
                    )
                if user_input.get(CONF_VERIFY_CONNECTION, True):
                    await async_validate_connection(self.hass, replacement)
            except BluetoothOwnershipChangeError:
                errors["base"] = "owned_bond_requires_guided_change"
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
        form_defaults = user_input or pairing_defaults or defaults
        serial_field, serial_default = await _async_serial_field(
            self.hass,
            transport=self._transport,
            current=form_defaults.get(CONF_SERIAL_PATH),
        )
        bluetooth_field, bluetooth_default = await _async_bluetooth_field(
            self.hass,
            protocol=self._protocol,
            transport=self._transport,
            current=form_defaults.get(CONF_BLE_ADDRESS),
        )
        return self.async_show_form(
            step_id="edit_details",
            data_schema=_gateway_schema(
                self._protocol,
                self._transport,
                defaults=form_defaults,
                serial_field=serial_field,
                serial_default=serial_default,
                bluetooth_field=bluetooth_field,
                bluetooth_default=bluetooth_default,
            ),
            errors=errors,
            description_placeholders={
                "protocol": PROTOCOL_LABELS[self._protocol],
                "transport": TRANSPORT_LABELS[self._transport],
            },
        )

    async def _async_save_paired_gateway(self, gateway: dict[str, Any]):
        """Save a verified Bluetooth gateway through the active options action."""
        gateways = self._gateways()
        if self._pairing_save_action == "add":
            updated = [*gateways, gateway]
        elif self._pairing_save_action == "edit":
            updated = [
                gateway
                if item["gateway_id"] == gateway["gateway_id"]
                else item
                for item in gateways
            ]
        else:
            return self.async_abort(reason="pairing_state_lost")
        self._pairing_save_action = None
        _validate_unique_gateways(updated)
        return self._save(**{CONF_GATEWAYS: updated})

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
                selected = _find_gateway(gateways, user_input[CONF_GATEWAY])
                configured_bond_key = _configured_bond_key(selected)
                owned_bond_key = _owned_bond_key(selected)
                remove_bond = user_input.get(
                    CONF_REMOVE_BLUETOOTH_BOND, False
                )
                if remove_bond and _is_meshtastic_bluetooth(selected):
                    if configured_bond_key is None:
                        # Never claim success when the exact adapter-scoped
                        # target cannot be reconstructed from guided setup.
                        # Keeping the gateway preserves a safe retry path.
                        errors["base"] = "bluetooth_cleanup_failed"
                    else:
                        pairing_manager = _async_pairing_manager(self.hass)
                        adapter, adapter_address, address = configured_bond_key
                        try:
                            await pairing_manager.async_forget_current_bond(
                                address,
                                adapter=adapter,
                                adapter_address=adapter_address,
                                user_confirmed=True,
                            )
                        except PairingError:
                            errors["base"] = "bluetooth_cleanup_failed"
                elif owned_bond_key:
                    pairing_manager = _async_pairing_manager(self.hass)
                    adapter, adapter_address, address = owned_bond_key
                    pairing_manager.release_created(
                        address,
                        adapter=adapter,
                        adapter_address=adapter_address,
                    )
                if not errors:
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
                    vol.Required(
                        CONF_REMOVE_BLUETOOTH_BOND, default=False
                    ): cv.boolean,
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

    async def async_step_maintenance(
        self, user_input: dict[str, Any] | None = None
    ):
        """Configure opt-in, low-traffic Meshtastic network maintenance."""
        options = dict(self.config_entry.options)
        gateway_choices = _maintenance_gateway_choices(self._gateways())
        configured_gateway = options.get(CONF_MAINTENANCE_GATEWAY_ID)
        if (
            not isinstance(configured_gateway, str)
            or configured_gateway not in gateway_choices
        ):
            configured_gateway = None
        defaults = {
            CONF_MAINTENANCE_ENABLED: (
                options.get(
                    CONF_MAINTENANCE_ENABLED,
                    DEFAULT_MAINTENANCE_ENABLED,
                )
                is True
            ),
            CONF_MAINTENANCE_GATEWAY_ID: configured_gateway,
            CONF_MAINTENANCE_INTERVAL: _bounded_int_option(
                options.get(CONF_MAINTENANCE_INTERVAL),
                default=DEFAULT_MAINTENANCE_INTERVAL,
                minimum=MAINTENANCE_MIN_INTERVAL_SECONDS,
                maximum=MAINTENANCE_MAX_INTERVAL_SECONDS,
            ),
            CONF_MAINTENANCE_QUIET_TIME: _bounded_int_option(
                options.get(CONF_MAINTENANCE_QUIET_TIME),
                default=DEFAULT_MAINTENANCE_QUIET_TIME,
                minimum=MAINTENANCE_MIN_QUIET_SECONDS,
                maximum=MAINTENANCE_MAX_QUIET_SECONDS,
            ),
            CONF_MAINTENANCE_MAX_REQUESTS: _bounded_int_option(
                options.get(CONF_MAINTENANCE_MAX_REQUESTS),
                default=DEFAULT_MAINTENANCE_MAX_REQUESTS,
                minimum=MAINTENANCE_MIN_REQUESTS,
                maximum=MAINTENANCE_MAX_REQUESTS,
            ),
        }
        errors: dict[str, str] = {}
        schema = _maintenance_schema(
            gateway_choices=gateway_choices,
            defaults=defaults,
        )
        if user_input is not None:
            validated: dict[str, Any] | None = None
            try:
                validated = schema(user_input)
            except vol.Invalid:
                requested_gateway = user_input.get(
                    CONF_MAINTENANCE_GATEWAY_ID
                )
                if (
                    user_input.get(CONF_MAINTENANCE_ENABLED) is True
                    and requested_gateway not in gateway_choices
                ):
                    errors[
                        CONF_MAINTENANCE_GATEWAY_ID
                        if gateway_choices
                        else "base"
                    ] = "maintenance_gateway_required"
                else:
                    errors["base"] = "invalid_maintenance_settings"
            else:
                selected_gateway = validated.get(
                    CONF_MAINTENANCE_GATEWAY_ID
                )
                if (
                    validated[CONF_MAINTENANCE_ENABLED]
                    and selected_gateway not in gateway_choices
                ):
                    errors[
                        CONF_MAINTENANCE_GATEWAY_ID
                        if gateway_choices
                        else "base"
                    ] = "maintenance_gateway_required"
                else:
                    saved = {
                        CONF_MAINTENANCE_ENABLED: validated[
                            CONF_MAINTENANCE_ENABLED
                        ],
                        CONF_MAINTENANCE_INTERVAL: validated[
                            CONF_MAINTENANCE_INTERVAL
                        ],
                        CONF_MAINTENANCE_QUIET_TIME: validated[
                            CONF_MAINTENANCE_QUIET_TIME
                        ],
                        CONF_MAINTENANCE_MAX_REQUESTS: validated[
                            CONF_MAINTENANCE_MAX_REQUESTS
                        ],
                    }
                    if selected_gateway in gateway_choices:
                        saved[
                            CONF_MAINTENANCE_GATEWAY_ID
                        ] = selected_gateway
                    options.update(saved)
                    if selected_gateway not in gateway_choices:
                        options.pop(CONF_MAINTENANCE_GATEWAY_ID, None)
                    return self.async_create_entry(title="", data=options)
            # Keep only schema-validated values visible after a semantic error.
            if validated is not None:
                defaults.update(
                    {
                        key: value
                        for key, value in validated.items()
                        if key in defaults
                    }
                )
            schema = _maintenance_schema(
                gateway_choices=gateway_choices,
                defaults=defaults,
            )
        return self.async_show_form(
            step_id="maintenance",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "eligible_gateway_count": str(len(gateway_choices))
            },
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
                _require_guided_setup_for_new_bluetooth(
                    self._gateways(), gateways
                )
                gateways = _reconcile_advanced_bluetooth_ownership(
                    self._gateways(), gateways
                )
            except BluetoothGuidedSetupRequiredError:
                errors[CONF_GATEWAYS_JSON] = "bluetooth_requires_gui"
            except BluetoothOwnershipChangeError:
                errors[CONF_GATEWAYS_JSON] = "owned_bond_requires_guided_change"
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


def _async_pairing_manager(hass: Any) -> BluetoothPairingManager:
    """Return one HA-instance manager so pairing is globally serialized."""
    manager = hass.data.get(DATA_BLUETOOTH_PAIRING)
    if isinstance(manager, BluetoothPairingManager):
        return manager
    manager = BluetoothPairingManager(prompt_timeout=50.0)
    hass.data[DATA_BLUETOOTH_PAIRING] = manager
    return manager


def _is_meshtastic_bluetooth(gateway: dict[str, Any]) -> bool:
    return (
        gateway.get(CONF_PROTOCOL) == PROTOCOL_MESHTASTIC
        and gateway.get(CONF_TRANSPORT) == TRANSPORT_BLUETOOTH
    )


def _maintenance_gateway_choices(
    gateways: list[dict[str, Any]],
) -> dict[str, str]:
    """Return exact, unique Meshtastic Bluetooth gateway choices."""
    choices: dict[str, str] = {}
    for gateway in gateways:
        if not isinstance(gateway, dict) or not _is_meshtastic_bluetooth(
            gateway
        ):
            continue
        gateway_id = gateway.get(CONF_GATEWAY_ID)
        if (
            not isinstance(gateway_id, str)
            or not gateway_id
            or gateway_id != gateway_id.strip()
            or len(gateway_id) > 128
            or gateway_id in choices
        ):
            continue
        name = gateway.get(CONF_NAME)
        label = (
            name.strip()
            if isinstance(name, str) and name.strip()
            else "Meshtastic Bluetooth gateway"
        )
        choices[gateway_id] = f"{label} ({gateway_id})"
    return choices


def _has_meshnet_owned_bond(gateway: dict[str, Any]) -> bool:
    return _owned_bond_key(gateway) is not None


def _owned_bond_key(
    gateway: dict[str, Any],
) -> tuple[str, str, str] | None:
    """Return the complete adapter-scoped ownership identity, if valid."""
    options = gateway.get("options") or {}
    if options.get(CONF_BLUETOOTH_BOND_MANAGED) is not True:
        return None
    return _configured_bond_key(gateway)


def _configured_bond_key(
    gateway: dict[str, Any],
) -> tuple[str, str, str] | None:
    """Return the exact guided-setup bond identity, regardless of ownership.

    This weaker identity must only authorize an explicitly confirmed removal
    from the warned options form.  Unattended cleanup continues to require
    ``_owned_bond_key`` so entry deletion, reload, and uninstall cannot remove
    a bond created or later replaced by another Bluetooth client.
    """
    if not _is_meshtastic_bluetooth(gateway):
        return None
    options = gateway.get("options") or {}
    adapter = options.get(CONF_BLUETOOTH_ADAPTER)
    if not (
        isinstance(adapter, str)
        and adapter.startswith("hci")
        and adapter[3:].isdigit()
    ):
        return None
    try:
        adapter_address = normalize_bluetooth_address(
            options.get(CONF_BLUETOOTH_ADAPTER_ADDRESS)
        )
        device_address = normalize_bluetooth_address(gateway.get(CONF_BLE_ADDRESS))
    except ValueError:
        return None
    return adapter, adapter_address, device_address


def _persisted_owned_bond_keys(hass: Any) -> set[tuple[str, str, str]]:
    """Return originally paired bonds represented by committed entry state."""
    config_entries_manager = getattr(hass, "config_entries", None)
    if config_entries_manager is None:
        return set()
    entries = config_entries_manager.async_entries(DOMAIN)
    bond_keys: set[tuple[str, str, str]] = set()
    for entry in entries:
        gateways = entry.options.get(CONF_GATEWAYS)
        if gateways is None:
            gateways = entry.data.get(CONF_GATEWAYS, [])
        for gateway in gateways:
            if not isinstance(gateway, dict):
                continue
            if bond_key := _owned_bond_key(gateway):
                bond_keys.add(bond_key)
    return bond_keys


def _preserve_bluetooth_ownership(
    previous: dict[str, Any], replacement: dict[str, Any]
) -> None:
    """Keep ownership only while editing the same canonical radio address."""
    if previous.get(CONF_BLE_ADDRESS) != replacement.get(CONF_BLE_ADDRESS):
        return
    bond_key = _owned_bond_key(previous)
    if bond_key is None:
        return
    adapter, adapter_address, _device_address = bond_key
    replacement_options = dict(replacement.get("options") or {})
    replacement_options[CONF_BLUETOOTH_BOND_MANAGED] = True
    replacement_options[CONF_BLUETOOTH_ADAPTER] = adapter
    replacement_options[CONF_BLUETOOTH_ADAPTER_ADDRESS] = adapter_address
    replacement["options"] = replacement_options


def _strip_untrusted_bluetooth_ownership(
    gateway: dict[str, Any],
) -> dict[str, Any]:
    """Remove a bond-ownership claim supplied by YAML or another untrusted form."""
    cleaned = deepcopy(gateway)
    options = dict(cleaned.get("options") or {})
    options.pop(CONF_BLUETOOTH_BOND_MANAGED, None)
    options.pop(CONF_BLUETOOTH_ADAPTER, None)
    options.pop(CONF_BLUETOOTH_ADAPTER_ADDRESS, None)
    if options:
        cleaned["options"] = options
    else:
        cleaned.pop("options", None)
    return cleaned


def _require_guided_setup_for_new_bluetooth(
    current: list[dict[str, Any]], proposed: list[dict[str, Any]]
) -> None:
    """Reject new Meshtastic BLE endpoints that bypass protected pairing."""
    existing = {
        (gateway.get("gateway_id"), gateway.get(CONF_BLE_ADDRESS))
        for gateway in current
        if _is_meshtastic_bluetooth(gateway)
    }
    if any(
        _is_meshtastic_bluetooth(gateway)
        and (gateway.get("gateway_id"), gateway.get(CONF_BLE_ADDRESS))
        not in existing
        for gateway in proposed
    ):
        raise BluetoothGuidedSetupRequiredError


def _reconcile_advanced_bluetooth_ownership(
    current: list[dict[str, Any]], proposed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Preserve real ownership while blocking forged or orphaned bond metadata."""
    owned = {
        gateway["gateway_id"]: gateway
        for gateway in current
        if _has_meshnet_owned_bond(gateway)
    }
    matched: set[str] = set()
    reconciled: list[dict[str, Any]] = []
    for gateway in proposed:
        cleaned = deepcopy(gateway)
        options = dict(cleaned.get("options") or {})
        claimed_owned = bool(options.pop(CONF_BLUETOOTH_BOND_MANAGED, False))
        options.pop(CONF_BLUETOOTH_ADAPTER, None)
        options.pop(CONF_BLUETOOTH_ADAPTER_ADDRESS, None)
        previous = owned.get(cleaned.get("gateway_id"))
        same_owned_bond = bool(
            previous
            and _is_meshtastic_bluetooth(cleaned)
            and cleaned.get(CONF_BLE_ADDRESS) == previous.get(CONF_BLE_ADDRESS)
        )
        if claimed_owned and not same_owned_bond:
            raise BluetoothOwnershipChangeError
        if same_owned_bond:
            matched.add(cleaned["gateway_id"])
            options[CONF_BLUETOOTH_BOND_MANAGED] = True
            previous_options = previous.get("options") or {}
            if adapter := previous_options.get(CONF_BLUETOOTH_ADAPTER):
                options[CONF_BLUETOOTH_ADAPTER] = adapter
            if adapter_address := previous_options.get(
                CONF_BLUETOOTH_ADAPTER_ADDRESS
            ):
                options[CONF_BLUETOOTH_ADAPTER_ADDRESS] = adapter_address
        if options:
            cleaned["options"] = options
        else:
            cleaned.pop("options", None)
        reconciled.append(cleaned)
    if matched != set(owned):
        raise BluetoothOwnershipChangeError
    return reconciled


def _pairing_error_key(error: PairingError) -> str:
    """Map internal failures to stable translations without leaking details."""
    if isinstance(error, PairingCleanupIncompleteError):
        return "pairing_cleanup_incomplete"
    if isinstance(error, PairingOwnershipPendingError):
        return "pairing_ownership_pending"
    if isinstance(error, PairingRateLimitedError):
        return "pairing_rate_limited"
    if isinstance(error, (PairingTimeoutError, PinPromptTimeoutError)):
        return "pairing_timeout"
    if isinstance(error, (BluetoothDeviceNotFoundError, AmbiguousBluetoothDeviceError)):
        return "local_adapter_required"
    if isinstance(error, InvalidBluetoothAddressError):
        return "invalid_gateway"
    if isinstance(error, InvalidPinError):
        return "invalid_pin"
    if isinstance(error, NotMeshtasticDeviceError):
        return "not_meshtastic_device"
    if isinstance(error, BluetoothUnavailableError):
        return "bluez_unavailable"
    if isinstance(error, PairingCancelledError):
        return "pairing_cancelled"
    if isinstance(error, (PairingRejectedError, PairingStateError)):
        return "pairing_rejected"
    return "pairing_failed"


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
    bluetooth_field: Any | None = None,
    bluetooth_default: str | None = None,
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
        bluetooth_default = defaults.get(CONF_BLE_ADDRESS) or bluetooth_default
        bluetooth_marker = (
            vol.Required(CONF_BLE_ADDRESS, default=bluetooth_default)
            if bluetooth_default
            else vol.Required(CONF_BLE_ADDRESS)
        )
        fields[
            bluetooth_marker
        ] = bluetooth_field or TextSelector(TextSelectorConfig())
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

    if not (
        protocol == PROTOCOL_MESHTASTIC
        and transport == TRANSPORT_BLUETOOTH
    ):
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


def _maintenance_schema(
    *,
    gateway_choices: dict[str, str],
    defaults: dict[str, Any],
) -> vol.Schema:
    """Build the bounded automatic-maintenance options form."""
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_MAINTENANCE_ENABLED,
            default=defaults[CONF_MAINTENANCE_ENABLED],
        ): cv.boolean,
        vol.Required(
            CONF_MAINTENANCE_INTERVAL,
            default=defaults[CONF_MAINTENANCE_INTERVAL],
        ): _bounded_int_validator(
            minimum=MAINTENANCE_MIN_INTERVAL_SECONDS,
            maximum=MAINTENANCE_MAX_INTERVAL_SECONDS,
        ),
        vol.Required(
            CONF_MAINTENANCE_QUIET_TIME,
            default=defaults[CONF_MAINTENANCE_QUIET_TIME],
        ): _bounded_int_validator(
            minimum=MAINTENANCE_MIN_QUIET_SECONDS,
            maximum=MAINTENANCE_MAX_QUIET_SECONDS,
        ),
        vol.Required(
            CONF_MAINTENANCE_MAX_REQUESTS,
            default=defaults[CONF_MAINTENANCE_MAX_REQUESTS],
        ): _bounded_int_validator(
            minimum=MAINTENANCE_MIN_REQUESTS,
            maximum=MAINTENANCE_MAX_REQUESTS,
        ),
    }
    if gateway_choices:
        configured_gateway = defaults.get(CONF_MAINTENANCE_GATEWAY_ID)
        marker = (
            vol.Optional(
                CONF_MAINTENANCE_GATEWAY_ID,
                default=configured_gateway,
            )
            if configured_gateway in gateway_choices
            else vol.Optional(CONF_MAINTENANCE_GATEWAY_ID)
        )
        fields[marker] = vol.In(gateway_choices)
    return vol.Schema(fields)


def _bounded_int_validator(*, minimum: int, maximum: int):
    """Reject booleans and fractional values before applying integer bounds."""

    def validate(value: Any) -> int:
        if isinstance(value, bool):
            raise vol.Invalid("value must be an integer")
        if isinstance(value, float) and not value.is_integer():
            raise vol.Invalid("value must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as err:
            raise vol.Invalid("value must be an integer") from err
        if not minimum <= parsed <= maximum:
            raise vol.Invalid(
                f"value must be at least {minimum} and at most {maximum}"
            )
        return parsed

    return validate


def _bounded_int_option(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Return one persisted integer only when it remains inside its bounds."""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if isinstance(value, float) and not value.is_integer():
        return default
    return parsed if minimum <= parsed <= maximum else default


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


async def _async_bluetooth_field(
    hass: Any,
    *,
    protocol: str,
    transport: str,
    current: str | None = None,
) -> tuple[Any | None, str | None]:
    """Build a picker from cached Meshtastic advertisements.

    Discovery can include Home Assistant Bluetooth proxies. The BlueZ pairing
    transaction performs a second, authoritative local-adapter check before it
    requests a PIN or changes any bond state.
    """
    if protocol != PROTOCOL_MESHTASTIC or transport != TRANSPORT_BLUETOOTH:
        return None, None
    devices = await async_discover_meshtastic_devices(hass)
    return _bluetooth_field(devices, current=current)


def _bluetooth_field(
    devices: list[BluetoothDevice], *, current: str | None = None
) -> tuple[Any, str | None]:
    """Return a Meshtastic dropdown that still accepts one exact manual MAC."""
    options, default = bluetooth_select_options(devices, current=current)
    if not options:
        return TextSelector(TextSelectorConfig()), default
    return (
        SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(**option) for option in options],
                mode=SelectSelectorMode.DROPDOWN,
                custom_value=True,
            )
        ),
        default,
    )


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
    bluetooth_addresses = [
        gateway.get(CONF_BLE_ADDRESS)
        for gateway in gateways
        if gateway.get(CONF_TRANSPORT) == TRANSPORT_BLUETOOTH
    ]
    if len(bluetooth_addresses) != len(set(bluetooth_addresses)):
        raise ValueError("Bluetooth addresses must be unique")


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
