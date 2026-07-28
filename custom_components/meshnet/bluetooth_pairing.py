"""Bounded BlueZ pairing for local Meshtastic Bluetooth radios.

This module deliberately does not use ``from __future__ import annotations``.
dbus-fast 2.x reads the literal D-Bus signature annotations on Agent1 methods
at class creation time.  Keeping those annotations as strings also works with
newer dbus-fast releases.

The public API does not retain a PIN.  A :class:`PairingAttempt` exposes a
short-lived prompt only while BlueZ is waiting for Agent1 to answer.
"""

import asyncio
import inspect
import re
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol
from weakref import WeakKeyDictionary

BLUEZ_SERVICE = "org.bluez"
BLUEZ_AGENT_MANAGER = "org.bluez.AgentManager1"
BLUEZ_AGENT = "org.bluez.Agent1"
BLUEZ_ADAPTER = "org.bluez.Adapter1"
BLUEZ_DEVICE = "org.bluez.Device1"
DBUS_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
MESHTASTIC_SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"

_BLUEZ_ROOT = "/org/bluez"
_DBUS_ROOT = "/org/freedesktop/DBus"
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_DEVICE_PATH_RE = re.compile(
    r"^/org/bluez/(hci[0-9]+)/dev_"
    r"([0-9A-F]{2}(?:_[0-9A-F]{2}){5})$"
)
_ADAPTER_PATH_RE = re.compile(r"^/org/bluez/(hci[0-9]+)$")
_UNIQUE_OWNER_RE = re.compile(r"^:[A-Za-z0-9_.-]+$")
_PIN_RE = re.compile(r"^[0-9]{6}$")
_CLEANUP_TIMEOUT = 5.0
_PAIRING_LOCKS: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()
_ATTEMPT_HISTORY: WeakKeyDictionary[
    Any, OrderedDict[str, deque[float]]
] = WeakKeyDictionary()
_ATTEMPT_LIMIT = 3
_ATTEMPT_WINDOW = 600.0
_ATTEMPT_ADDRESS_CACHE = 128


class PairingError(Exception):
    """Base class for safe, user-displayable pairing failures."""


class BluetoothUnavailableError(PairingError):
    """BlueZ or its Python D-Bus client is unavailable."""


class InvalidBluetoothAddressError(PairingError):
    """The supplied Bluetooth address is not a canonical MAC address."""


class BluetoothDeviceNotFoundError(PairingError):
    """The selected address is not an exact local BlueZ device."""


class AmbiguousBluetoothDeviceError(PairingError):
    """More than one local adapter exposes the selected address."""


class NotMeshtasticDeviceError(PairingError):
    """The selected device does not expose the Meshtastic service."""


class InvalidPinError(PairingError):
    """A PIN must contain exactly six decimal digits."""


class PairingStateError(PairingError):
    """The pairing attempt is not in the requested state."""


class PairingOwnershipPendingError(PairingError):
    """An earlier flow still holds ambiguous same-device ownership proof."""


class PairingTimeoutError(PairingError):
    """The complete BlueZ pairing operation timed out."""


class PairingRateLimitedError(PairingError):
    """Too many pairing transactions targeted the selected address."""


class PinPromptTimeoutError(PairingError):
    """No PIN was supplied while BlueZ was waiting for one."""


class PairingRejectedError(PairingError):
    """BlueZ rejected or could not complete pairing."""


class PairingCancelledError(PairingError):
    """BlueZ cancelled the active Agent1 prompt."""


class PairingCleanupIncompleteError(PairingError):
    """A new bond exists but its verification/rollback did not finish safely."""

    def __init__(
        self,
        bond: "ProvisionalBond | tuple[ProvisionalBond, ...]",
    ) -> None:
        bonds = (bond,) if isinstance(bond, ProvisionalBond) else tuple(bond)
        if not bonds:
            raise ValueError("At least one provisional bond is required")
        self.bonds = bonds
        # Keep the original singular attribute for callers that only need the
        # first cleanup handle.
        self.bond = bonds[0]
        super().__init__(
            "MeshNet created a Bluetooth bond but could not verify or remove it"
        )


class BondNotOwnedError(PairingError):
    """MeshNet did not create the bond in this manager's lifetime."""


class _BlueZCallError(PairingRejectedError):
    """A sanitized D-Bus error from BlueZ."""

    def __init__(self, error_name: str = "org.bluez.Error.Failed") -> None:
        self.error_name = error_name
        super().__init__("BlueZ could not complete the pairing operation")


@dataclass(frozen=True, slots=True)
class PairingResult:
    """Verified outcome of a local BlueZ pairing operation."""

    address: str
    adapter: str
    adapter_address: str
    bond_created: bool
    # These are same-process safety evidence, not config-entry data.  Exclude
    # them from equality/repr so they cannot accidentally become UI metadata.
    device_path: str | None = field(default=None, compare=False, repr=False)
    bluez_owner: str | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ProvisionalBond:
    """Deletion authority retained after our successful Device1.Pair call."""

    address: str
    adapter: str
    adapter_address: str
    device_path: str | None = None
    bluez_owner: str | None = field(default=None, compare=False, repr=False)


@dataclass(slots=True)
class _RollbackState:
    """Mutable result shared with shielded cleanup."""

    attempted: bool = False
    succeeded: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedDevice:
    """Validated Device1 state read from ObjectManager."""

    address: str
    path: str
    adapter: str
    adapter_path: str
    adapter_address: str
    paired: bool
    uuids: frozenset[str]


class _PairingBackend(Protocol):
    """Injectable backend used by :class:`BluetoothPairingManager`."""

    async def async_pair(
        self,
        address: str,
        request_pin: Callable[[str], Awaitable[str]],
        cancel_prompt: Callable[[PairingError], None],
        check_candidate: Callable[[ProvisionalBond], None],
    ) -> PairingResult:
        """Pair and verify one exact local device."""

    async def async_forget(
        self,
        address: str,
        adapter: str,
        adapter_address: str,
    ) -> None:
        """Remove the explicitly confirmed current local bond."""


def _normalize_address(address: str) -> str:
    """Validate and normalize a Bluetooth MAC without accepting aliases."""
    if not isinstance(address, str) or _MAC_RE.fullmatch(address) is None:
        raise InvalidBluetoothAddressError(
            "Enter a Bluetooth address as six hexadecimal octets"
        )
    return address.upper()


def _normalize_adapter(adapter: str) -> str:
    """Validate the persisted BlueZ controller identity without guessing."""
    if not isinstance(adapter, str) or re.fullmatch(r"hci[0-9]+", adapter) is None:
        raise BluetoothUnavailableError("The stored Bluetooth adapter is invalid")
    return adapter


def _normalize_adapter_address(adapter_address: str) -> str:
    """Validate the stable controller address stored with bond ownership."""
    normalized = _normalize_address(adapter_address)
    if normalized in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}:
        raise BluetoothUnavailableError("The stored Bluetooth adapter is invalid")
    return normalized


def _value(value: Any) -> Any:
    """Unwrap dbus-fast Variant values while keeping tests dependency-free."""
    return getattr(value, "value", value)


def _pairing_lock() -> asyncio.Lock:
    """Share one pairing lock across managers on each Home Assistant loop."""
    loop = asyncio.get_running_loop()
    lock = _PAIRING_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _PAIRING_LOCKS[loop] = lock
    return lock


def _consume_attempt(address: str, now: float) -> None:
    """Apply a bounded, per-loop and per-address brute-force throttle."""
    loop = asyncio.get_running_loop()
    address_history = _ATTEMPT_HISTORY.get(loop)
    if address_history is None:
        address_history = OrderedDict()
        _ATTEMPT_HISTORY[loop] = address_history

    attempts = address_history.get(address)
    if attempts is None:
        attempts = deque()
        address_history[address] = attempts
    cutoff = now - _ATTEMPT_WINDOW
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
    if len(attempts) >= _ATTEMPT_LIMIT:
        raise PairingRateLimitedError(
            "Too many pairing attempts; wait before trying this radio again"
        )
    attempts.append(now)
    address_history.move_to_end(address)
    while len(address_history) > _ATTEMPT_ADDRESS_CACHE:
        address_history.popitem(last=False)


def _device_at_path(
    objects: Any,
    path: str,
    *,
    initial_address: str | None = None,
    validate_uuids: bool = True,
) -> _ResolvedDevice:
    """Validate one exact Device1 path and read its current identity address."""
    if not isinstance(objects, dict):
        raise BluetoothUnavailableError("BlueZ returned invalid device data")

    path_match = _DEVICE_PATH_RE.fullmatch(path)
    if path_match is None:
        raise BluetoothUnavailableError("BlueZ returned an unsafe device path")
    interfaces = objects.get(path)
    if not isinstance(interfaces, dict):
        raise BluetoothDeviceNotFoundError(
            "The selected Bluetooth device is not available locally"
        )
    properties = interfaces.get(BLUEZ_DEVICE)
    if not isinstance(properties, dict):
        raise BluetoothDeviceNotFoundError(
            "The selected Bluetooth device is not available locally"
        )
    raw_address = _value(properties.get("Address"))
    if not isinstance(raw_address, str) or _MAC_RE.fullmatch(raw_address) is None:
        raise BluetoothUnavailableError("BlueZ returned an invalid device identity")
    current_address = raw_address.upper()
    raw_paired = _value(properties.get("Paired", False))
    if not isinstance(raw_paired, bool):
        raise BluetoothUnavailableError("BlueZ returned invalid bond state")
    paired = raw_paired

    # Before Pair(), the object path and Address must identify the exact user
    # selection.  BlueZ may replace Address with the identity address after a
    # successful LE pairing while keeping the same Device1 object path.
    if initial_address is not None:
        path_address = path_match.group(2).replace("_", ":")
        if current_address != initial_address:
            raise BluetoothUnavailableError("BlueZ device identity changed")
        if path_address != initial_address and not paired:
            raise BluetoothUnavailableError("BlueZ device identity changed")

    adapter = path_match.group(1)
    adapter_path = f"{_BLUEZ_ROOT}/{adapter}"
    adapter_interfaces = objects.get(adapter_path)
    if not isinstance(adapter_interfaces, dict):
        raise BluetoothUnavailableError("BlueZ device has no local adapter")
    adapter_properties = adapter_interfaces.get(BLUEZ_ADAPTER)
    if not isinstance(adapter_properties, dict):
        raise BluetoothUnavailableError("BlueZ device has no local adapter")
    raw_adapter_address = _value(adapter_properties.get("Address"))
    if (
        not isinstance(raw_adapter_address, str)
        or _MAC_RE.fullmatch(raw_adapter_address) is None
        or raw_adapter_address.upper()
        in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}
    ):
        raise BluetoothUnavailableError("BlueZ returned an invalid adapter identity")
    adapter_address = raw_adapter_address.upper()

    raw_uuids = _value(properties.get("UUIDs", []))
    if not isinstance(raw_uuids, (list, tuple)) or any(
        not isinstance(item, str) for item in raw_uuids
    ):
        if validate_uuids:
            raise BluetoothUnavailableError("BlueZ returned invalid service data")
        uuids = frozenset()
    else:
        uuids = frozenset(item.lower() for item in raw_uuids)
    return _ResolvedDevice(
        address=current_address,
        path=path,
        adapter=adapter,
        adapter_path=adapter_path,
        adapter_address=adapter_address,
        paired=paired,
        uuids=uuids,
    )


def _resolve_device(objects: Any, address: str) -> _ResolvedDevice:
    """Resolve exactly one Device1 by its Address property and validate path."""
    if not isinstance(objects, dict):
        raise BluetoothUnavailableError("BlueZ returned invalid device data")

    paths = []
    for path, interfaces in objects.items():
        if not isinstance(path, str) or not isinstance(interfaces, dict):
            continue
        properties = interfaces.get(BLUEZ_DEVICE)
        if not isinstance(properties, dict):
            continue
        raw_address = _value(properties.get("Address"))
        if (
            isinstance(raw_address, str)
            and _MAC_RE.fullmatch(raw_address) is not None
            and raw_address.upper() == address
        ):
            paths.append(path)

    if not paths:
        raise BluetoothDeviceNotFoundError(
            "The selected Bluetooth device is not available locally"
        )
    if len(paths) != 1:
        raise AmbiguousBluetoothDeviceError(
            "The selected device appears on more than one local adapter"
        )
    return _device_at_path(objects, paths[0], initial_address=address)


def _resolve_device_on_adapter(
    objects: Any,
    address: str,
    adapter: str,
    *,
    validate_uuids: bool = True,
) -> _ResolvedDevice:
    """Resolve an address only on its persisted adapter ownership boundary."""
    if not isinstance(objects, dict):
        raise BluetoothUnavailableError("BlueZ returned invalid device data")

    paths: list[str] = []
    for path, interfaces in objects.items():
        if not isinstance(path, str) or not isinstance(interfaces, dict):
            continue
        properties = interfaces.get(BLUEZ_DEVICE)
        if not isinstance(properties, dict):
            continue
        raw_address = _value(properties.get("Address"))
        if not (
            isinstance(raw_address, str)
            and _MAC_RE.fullmatch(raw_address) is not None
            and raw_address.upper() == address
        ):
            continue
        path_match = _DEVICE_PATH_RE.fullmatch(path)
        if path_match is None:
            raise BluetoothUnavailableError("BlueZ returned an unsafe device path")
        if path_match.group(1) == adapter:
            paths.append(path)

    if not paths:
        raise BluetoothDeviceNotFoundError(
            "The owned Bluetooth device is no longer present on its adapter"
        )
    if len(paths) != 1:
        raise AmbiguousBluetoothDeviceError(
            "The owned Bluetooth device is ambiguous on its adapter"
        )
    return _device_at_path(
        objects,
        paths[0],
        initial_address=address,
        validate_uuids=validate_uuids,
    )


def _resolve_adapter_identity(objects: Any, adapter_address: str) -> str:
    """Resolve one current hci name from its stable controller address."""
    if not isinstance(objects, dict):
        raise BluetoothUnavailableError("BlueZ returned invalid adapter data")

    matches: list[str] = []
    for path, interfaces in objects.items():
        if not isinstance(interfaces, dict) or BLUEZ_ADAPTER not in interfaces:
            continue
        path_match = _ADAPTER_PATH_RE.fullmatch(path) if isinstance(path, str) else None
        properties = interfaces.get(BLUEZ_ADAPTER)
        if path_match is None or not isinstance(properties, dict):
            raise BluetoothUnavailableError("BlueZ returned invalid adapter data")
        raw_address = _value(properties.get("Address"))
        if not isinstance(raw_address, str) or _MAC_RE.fullmatch(raw_address) is None:
            raise BluetoothUnavailableError("BlueZ returned invalid adapter identity")
        if raw_address.upper() == adapter_address:
            matches.append(path_match.group(1))

    if not matches:
        raise BluetoothUnavailableError(
            "The Bluetooth adapter that owns this bond is unavailable"
        )
    if len(matches) != 1:
        raise BluetoothUnavailableError("The Bluetooth adapter identity is ambiguous")
    return matches[0]


def _require_selected_powered_adapter(
    objects: Any, device: _ResolvedDevice
) -> None:
    """Require the exact selected controller to remain valid and powered.

    Pairing is pinned to one Device1 object beneath one local Adapter1 path.
    Other local controllers are irrelevant to that ownership boundary, but the
    selected controller must still have the stable identity captured when the
    device was resolved and an explicit, valid ``Powered=True`` state.
    """
    if not isinstance(objects, dict):
        raise BluetoothUnavailableError("BlueZ returned invalid adapter data")

    path_match = _ADAPTER_PATH_RE.fullmatch(device.adapter_path)
    if path_match is None or path_match.group(1) != device.adapter:
        raise BluetoothUnavailableError("BlueZ returned invalid adapter data")

    interfaces = objects.get(device.adapter_path)
    if not isinstance(interfaces, dict):
        raise BluetoothUnavailableError(
            "The selected Bluetooth adapter is unavailable"
        )
    properties = interfaces.get(BLUEZ_ADAPTER)
    if not isinstance(properties, dict):
        raise BluetoothUnavailableError(
            "The selected Bluetooth adapter is unavailable"
        )

    missing = object()
    powered = _value(properties.get("Powered", missing))
    if not isinstance(powered, bool):
        raise BluetoothUnavailableError("BlueZ returned invalid adapter power state")
    if not powered:
        raise BluetoothUnavailableError(
            "The selected Bluetooth adapter is not powered"
        )

    raw_adapter_address = _value(properties.get("Address", missing))
    if (
        not isinstance(raw_adapter_address, str)
        or _MAC_RE.fullmatch(raw_adapter_address) is None
        or raw_adapter_address.upper()
        in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}
    ):
        raise BluetoothUnavailableError("BlueZ returned invalid adapter identity")
    if raw_adapter_address.upper() != device.adapter_address:
        raise BluetoothUnavailableError("BlueZ adapter identity changed")


def _load_dbus_api() -> Any:
    """Load stable dbus-fast APIs only when the real backend is used."""
    try:
        from dbus_fast import DBusError, Message
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.service import ServiceInterface, method
    except ImportError as exc:
        raise BluetoothUnavailableError(
            "The Home Assistant D-Bus Bluetooth dependency is unavailable"
        ) from exc
    return SimpleNamespace(
        BusType=BusType,
        DBusError=DBusError,
        Message=Message,
        MessageBus=MessageBus,
        MessageType=MessageType,
        ServiceInterface=ServiceInterface,
        method=method,
    )


def _new_message(api: Any, **kwargs: Any) -> Any:
    """Create a method-call message using the pinned daemon destination."""
    return api.Message(**kwargs)


async def _checked_call(
    bus: Any,
    api: Any,
    message: Any,
    *,
    expected_signature: str,
) -> Any:
    """Call D-Bus and discard potentially sensitive daemon error text."""
    reply = await bus.call(message)
    if reply is None:
        raise BluetoothUnavailableError("BlueZ returned no D-Bus reply")
    if reply.message_type == api.MessageType.ERROR:
        error_name = getattr(reply, "error_name", None)
        if not isinstance(error_name, str):
            error_name = "org.bluez.Error.Failed"
        raise _BlueZCallError(error_name)
    if reply.message_type != api.MessageType.METHOD_RETURN:
        raise BluetoothUnavailableError("BlueZ returned an invalid D-Bus reply")
    if getattr(reply, "signature", None) != expected_signature:
        raise BluetoothUnavailableError("BlueZ returned an invalid D-Bus signature")
    if not isinstance(getattr(reply, "body", None), list):
        raise BluetoothUnavailableError("BlueZ returned an invalid D-Bus body")
    return reply


def _create_agent(
    api: Any,
    device_path: str,
    request_pin: Callable[[str], Awaitable[str]],
    cancel_prompt: Callable[[PairingError], None],
) -> Any:
    """Build an Agent1 using literal signature annotations for dbus-fast."""
    ServiceInterface = api.ServiceInterface
    method = api.method
    DBusError = api.DBusError

    class MeshNetPairingAgent(ServiceInterface):
        """One-device, one-attempt BlueZ Agent1."""

        def __init__(self) -> None:
            super().__init__(BLUEZ_AGENT)
            self.failure = None

        def _reject(self) -> None:
            raise DBusError("org.bluez.Error.Rejected", "Pairing request rejected")

        def _check_device(self, device: str) -> None:
            if device != device_path:
                self._reject()

        async def _get_pin(self, kind: str) -> str:
            try:
                pin = await request_pin(kind)
            except PairingError as exc:
                self.failure = exc
                self._reject()
            except asyncio.CancelledError:
                self.failure = PairingCancelledError("Pairing was cancelled")
                self._reject()
            if not isinstance(pin, str) or _PIN_RE.fullmatch(pin) is None:
                self.failure = InvalidPinError(
                    "Enter the six-digit PIN shown by the radio"
                )
                self._reject()
            return pin

        @method()
        def Release(self):
            cancel_prompt(PairingCancelledError("BlueZ released the pairing agent"))
            return None

        @method()
        async def RequestPinCode(self, device: "o") -> "s":
            self._check_device(device)
            return await self._get_pin("pin_code")

        @method()
        async def RequestPasskey(self, device: "o") -> "u":
            self._check_device(device)
            return int(await self._get_pin("passkey"))

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):
            self._check_device(device)
            self._reject()

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
            self._check_device(device)
            self._reject()

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):
            self._check_device(device)
            self._reject()

        @method()
        def RequestAuthorization(self, device: "o"):
            self._check_device(device)
            self._reject()

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):
            self._check_device(device)
            self._reject()

        @method()
        def Cancel(self):
            self.failure = PairingCancelledError("Pairing was cancelled by BlueZ")
            cancel_prompt(self.failure)
            return None

    return MeshNetPairingAgent()


def _sender_guard(api: Any, agent_path: str, bluez_owner: str) -> Callable[[Any], Any]:
    """Reject every call to the temporary agent from a non-BlueZ sender."""

    def guard(message: Any) -> Any:
        if (
            message.message_type == api.MessageType.METHOD_CALL
            and getattr(message, "path", None) == agent_path
            and getattr(message, "sender", None) != bluez_owner
        ):
            return api.Message.new_error(
                message,
                "org.freedesktop.DBus.Error.AccessDenied",
                "Temporary pairing agent access denied",
            )
        return None

    return guard


class _BlueZPairingBackend:
    """A transactional pairing backend using one dedicated system bus."""

    def __init__(
        self,
        bus_factory: Callable[[], Any] | None = None,
        api_loader: Callable[[], Any] = _load_dbus_api,
    ) -> None:
        self._bus_factory = bus_factory
        self._api_loader = api_loader

    async def _open_bus(self, api: Any) -> Any:
        factory = self._bus_factory
        if factory is None:
            bus = api.MessageBus(bus_type=api.BusType.SYSTEM)
        else:
            bus = factory()
            if inspect.isawaitable(bus):
                bus = await bus
        connect = getattr(bus, "connect", None)
        if connect is None:
            raise BluetoothUnavailableError("The system D-Bus connection is unavailable")
        connected = connect()
        if inspect.isawaitable(connected):
            connected = await connected
        return connected if connected is not None else bus

    async def _owner(self, bus: Any, api: Any) -> str:
        reply = await _checked_call(
            bus,
            api,
            _new_message(
                api,
                destination="org.freedesktop.DBus",
                path=_DBUS_ROOT,
                interface="org.freedesktop.DBus",
                member="GetNameOwner",
                signature="s",
                body=[BLUEZ_SERVICE],
            ),
            expected_signature="s",
        )
        owner = reply.body[0] if len(reply.body) == 1 else None
        if not isinstance(owner, str) or _UNIQUE_OWNER_RE.fullmatch(owner) is None:
            raise BluetoothUnavailableError("BlueZ has no valid D-Bus owner")
        return owner

    async def _objects(self, bus: Any, api: Any, owner: str) -> dict[str, Any]:
        reply = await _checked_call(
            bus,
            api,
            _new_message(
                api,
                destination=owner,
                path="/",
                interface=DBUS_OBJECT_MANAGER,
                member="GetManagedObjects",
            ),
            expected_signature="a{oa{sa{sv}}}",
        )
        objects = reply.body[0] if len(reply.body) == 1 else None
        if not isinstance(objects, dict):
            raise BluetoothUnavailableError("BlueZ returned invalid device data")
        return objects

    async def _call_bluez(
        self,
        bus: Any,
        api: Any,
        owner: str,
        *,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
    ) -> Any:
        return await _checked_call(
            bus,
            api,
            _new_message(
                api,
                destination=owner,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            ),
            expected_signature="",
        )

    async def _safe_bluez_call(self, *args: Any, **kwargs: Any) -> bool:
        try:
            async with asyncio.timeout(_CLEANUP_TIMEOUT):
                await self._call_bluez(*args, **kwargs)
        except BaseException:
            return False
        return True

    async def _cleanup_pair_attempt(
        self,
        *,
        bus: Any,
        api: Any,
        owner: str | None,
        device: _ResolvedDevice | None,
        agent: Any,
        agent_path: str | None,
        guard: Callable[[Any], Any] | None,
        registered: bool,
        exported: bool,
        rollback: bool,
        cancel_pairing: bool,
        rollback_state: _RollbackState,
        rollback_requested: asyncio.Event | None = None,
        commit_irrevocable: asyncio.Event | None = None,
    ) -> None:
        """Run all bounded cleanup steps on a separately shielded task."""
        async def rollback_created_bond() -> bool:
            rollback_state.attempted = True
            if owner is None or device is None:
                return False
            try:
                async with asyncio.timeout(_CLEANUP_TIMEOUT):
                    current = _device_at_path(
                        await self._objects(bus, api, owner), device.path
                    )
            except BluetoothDeviceNotFoundError:
                rollback_state.succeeded = True
                return True
            except BaseException:
                return False
            if current.adapter_address != device.adapter_address:
                # The hci path may have been reused by a hot-swapped
                # controller while bluetoothd kept the same D-Bus owner.
                # Never redirect rollback across stable adapter identities.
                return False
            if not current.paired:
                rollback_state.succeeded = True
                return True
            removed = await self._safe_bluez_call(
                bus,
                api,
                owner,
                path=device.adapter_path,
                interface=BLUEZ_ADAPTER,
                member="RemoveDevice",
                signature="o",
                body=[device.path],
            )
            if not removed:
                return False

            try:
                async with asyncio.timeout(_CLEANUP_TIMEOUT):
                    remaining = _device_at_path(
                        await self._objects(bus, api, owner), device.path
                    )
            except BluetoothDeviceNotFoundError:
                rollback_state.succeeded = True
                return True
            except BaseException:
                return False
            rollback_state.succeeded = not remaining.paired
            return rollback_state.succeeded

        if owner is not None and device is not None and cancel_pairing:
            same_controller = False
            try:
                async with asyncio.timeout(_CLEANUP_TIMEOUT):
                    current = _device_at_path(
                        await self._objects(bus, api, owner), device.path
                    )
                same_controller = (
                    current.adapter_address == device.adapter_address
                )
            except BaseException:
                pass
            if same_controller:
                await self._safe_bluez_call(
                    bus,
                    api,
                    owner,
                    path=device.path,
                    interface=BLUEZ_DEVICE,
                    member="CancelPairing",
                )

        if rollback or (
            rollback_requested is not None and rollback_requested.is_set()
        ):
            await rollback_created_bond()

        if owner is not None and registered and agent_path:
            await self._safe_bluez_call(
                bus,
                api,
                owner,
                path=_BLUEZ_ROOT,
                interface=BLUEZ_AGENT_MANAGER,
                member="UnregisterAgent",
                signature="o",
                body=[agent_path],
            )
        if exported and agent_path:
            with suppress(BaseException):
                bus.unexport(agent_path, agent)
        if guard is not None:
            with suppress(BaseException):
                bus.remove_message_handler(guard)

        # Cancellation can arrive while the agent is being unregistered.  Do
        # one final ownership decision while the D-Bus connection is usable.
        # There is deliberately no await between this check and the commit
        # marker, so cancellation cannot slip through that handoff boundary.
        if (
            not rollback_state.attempted
            and rollback_requested is not None
            and rollback_requested.is_set()
        ):
            await rollback_created_bond()
        if (
            commit_irrevocable is not None
            and (
                (
                    not rollback
                    and (
                        rollback_requested is None
                        or not rollback_requested.is_set()
                        or (
                            rollback_state.attempted
                            and not rollback_state.succeeded
                        )
                    )
                )
                or (
                    rollback
                    and rollback_state.attempted
                    and not rollback_state.succeeded
                )
            )
        ):
            # If a cancellation rollback cannot be verified, preserve the
            # successful result and hand ownership to the manager.  Losing
            # both the result and the bond marker would make safe removal
            # impossible on the next lifecycle transition.
            commit_irrevocable.set()
        await self._disconnect_bus(bus)

    async def _disconnect_bus(self, bus: Any) -> None:
        """Bound an asynchronous disconnect implementation, if provided."""
        with suppress(BaseException):
            disconnected = bus.disconnect()
            if inspect.isawaitable(disconnected):
                async with asyncio.timeout(_CLEANUP_TIMEOUT):
                    await disconnected

    async def _run_shielded_cleanup(
        self,
        cleanup: Awaitable[None],
        *,
        rollback_requested: asyncio.Event | None = None,
        commit_irrevocable: asyncio.Event | None = None,
    ) -> None:
        """Do not let cancellation strand the exported temporary agent."""
        task = asyncio.create_task(cleanup)
        cancelled_before_commit = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A verified new bond has a small cleanup window before its
                # PairingResult reaches the manager.  Request rollback if that
                # transaction is cancelled before the explicit commit point.
                # After the commit point, defer cancellation so the result is
                # delivered and ownership is recorded instead of being lost.
                if commit_irrevocable is not None and commit_irrevocable.is_set():
                    continue
                if rollback_requested is not None:
                    rollback_requested.set()
                cancelled_before_commit = True

        # Propagate an unexpected cleanup error, if one escaped its bounded
        # best-effort steps.  The task is already done, so use result() without
        # another cancellation point after the rollback/commit decision.
        task.result()
        if cancelled_before_commit and not (
            commit_irrevocable is not None and commit_irrevocable.is_set()
        ):
            raise asyncio.CancelledError

    async def async_pair(
        self,
        address: str,
        request_pin: Callable[[str], Awaitable[str]],
        cancel_prompt: Callable[[PairingError], None],
        check_candidate: Callable[[ProvisionalBond], None],
    ) -> PairingResult:
        api = self._api_loader()
        bus = None
        owner = None
        device = None
        agent = None
        agent_path = None
        guard = None
        registered = False
        exported = False
        result_ready = False
        preexisting = False
        pair_call_succeeded = False
        cancel_pairing = False
        provisional_bond = None

        try:
            bus = await self._open_bus(api)
            owner = await self._owner(bus, api)
            initial_objects = await self._objects(bus, api, owner)
            device = _resolve_device(initial_objects, address)
            _require_selected_powered_adapter(initial_objects, device)
            preexisting = device.paired

            if device.uuids and MESHTASTIC_SERVICE_UUID not in device.uuids:
                raise NotMeshtasticDeviceError(
                    "The selected Bluetooth device is not a Meshtastic radio"
                )

            if preexisting:
                if MESHTASTIC_SERVICE_UUID not in device.uuids:
                    raise NotMeshtasticDeviceError(
                        "The paired device does not expose the Meshtastic service"
                    )
                owner_after = await self._owner(bus, api)
                if owner_after != owner:
                    raise BluetoothUnavailableError("BlueZ restarted during verification")
                verified_objects = await self._objects(bus, api, owner)
                verified = _device_at_path(
                    verified_objects,
                    device.path,
                    initial_address=address,
                )
                _require_selected_powered_adapter(verified_objects, verified)
                if verified.adapter_address != device.adapter_address:
                    raise BluetoothUnavailableError(
                        "BlueZ adapter identity changed during verification"
                    )
                if not verified.paired or verified.path != device.path:
                    raise PairingRejectedError("The Bluetooth bond could not be verified")
                check_candidate(
                    ProvisionalBond(
                        address=verified.address,
                        adapter=verified.adapter,
                        adapter_address=verified.adapter_address,
                        device_path=verified.path,
                        bluez_owner=owner,
                    )
                )
                result_ready = True
                return PairingResult(
                    address=verified.address,
                    adapter=verified.adapter,
                    adapter_address=verified.adapter_address,
                    bond_created=False,
                    device_path=verified.path,
                    bluez_owner=owner,
                )

            # Stop before Pair() when an earlier transaction still controls
            # this exact controller/address/object.  Never transfer that
            # cleanup authority to the new flow.
            check_candidate(
                ProvisionalBond(
                    address=device.address,
                    adapter=device.adapter,
                    adapter_address=device.adapter_address,
                    device_path=device.path,
                    bluez_owner=owner,
                )
            )
            agent_path = f"/org/meshnet/bluez/agent/a_{secrets.token_hex(12)}"
            agent = _create_agent(api, device.path, request_pin, cancel_prompt)
            guard = _sender_guard(api, agent_path, owner)
            bus.add_message_handler(guard)
            bus.export(agent_path, agent)
            exported = True

            await self._call_bluez(
                bus,
                api,
                owner,
                path=_BLUEZ_ROOT,
                interface=BLUEZ_AGENT_MANAGER,
                member="RegisterAgent",
                signature="os",
                body=[agent_path, "KeyboardOnly"],
            )
            registered = True

            owner_before_pair = await self._owner(bus, api)
            if owner_before_pair != owner:
                raise BluetoothUnavailableError(
                    "BlueZ restarted before pairing"
                )
            pre_pair_objects = await self._objects(bus, api, owner)
            pre_pair_device = _device_at_path(
                pre_pair_objects,
                device.path,
                initial_address=address,
            )
            _require_selected_powered_adapter(pre_pair_objects, pre_pair_device)
            if pre_pair_device.adapter_address != device.adapter_address:
                raise BluetoothUnavailableError(
                    "BlueZ adapter identity changed before pairing"
                )
            if pre_pair_device.paired:
                raise PairingRejectedError(
                    "The Bluetooth bond changed before pairing"
                )
            if (
                pre_pair_device.uuids
                and MESHTASTIC_SERVICE_UUID not in pre_pair_device.uuids
            ):
                raise NotMeshtasticDeviceError(
                    "The selected Bluetooth device is not a Meshtastic radio"
                )

            try:
                await self._call_bluez(
                    bus,
                    api,
                    owner,
                    path=device.path,
                    interface=BLUEZ_DEVICE,
                    member="Pair",
                )
            except asyncio.CancelledError:
                cancel_pairing = True
                raise
            except BaseException as exc:
                if agent.failure is not None:
                    raise agent.failure from exc
                raise
            else:
                # This successful method return is the ownership boundary.  A
                # later verification failure may roll back this bond; a Pair
                # error must never remove a bond another client may have made.
                pair_call_succeeded = True
                provisional_bond = ProvisionalBond(
                    address=device.address,
                    adapter=device.adapter,
                    adapter_address=device.adapter_address,
                    device_path=device.path,
                    bluez_owner=owner,
                )

            owner_after = await self._owner(bus, api)
            if owner_after != owner:
                raise BluetoothUnavailableError("BlueZ restarted during pairing")
            verified_objects = await self._objects(bus, api, owner)
            verified = _device_at_path(verified_objects, device.path)
            _require_selected_powered_adapter(verified_objects, verified)
            if verified.adapter_address != device.adapter_address:
                raise BluetoothUnavailableError(
                    "BlueZ adapter identity changed during pairing"
                )
            if verified.path != device.path:
                raise PairingRejectedError("The Bluetooth device identity changed")
            provisional_bond = ProvisionalBond(
                address=verified.address,
                adapter=device.adapter,
                adapter_address=device.adapter_address,
                device_path=device.path,
                bluez_owner=owner,
            )
            if not verified.paired:
                raise PairingRejectedError("BlueZ did not create a Bluetooth bond")
            if MESHTASTIC_SERVICE_UUID not in verified.uuids:
                raise NotMeshtasticDeviceError(
                    "The paired device does not expose the Meshtastic service"
                )

            result_ready = True
            return PairingResult(
                address=verified.address,
                adapter=verified.adapter,
                adapter_address=verified.adapter_address,
                bond_created=True,
                device_path=verified.path,
                bluez_owner=owner,
            )
        except PairingError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise BluetoothUnavailableError(
                "The local BlueZ pairing service failed"
            ) from exc
        finally:
            if bus is not None:
                owns_new_bond = pair_call_succeeded and not preexisting
                rollback_state = _RollbackState()
                rollback_requested = (
                    asyncio.Event() if owns_new_bond else None
                )
                commit_irrevocable = (
                    asyncio.Event() if owns_new_bond else None
                )
                await self._run_shielded_cleanup(
                    self._cleanup_pair_attempt(
                        bus=bus,
                        api=api,
                        owner=owner,
                        device=device,
                        agent=agent,
                        agent_path=agent_path,
                        guard=guard,
                        registered=registered,
                        exported=exported,
                        rollback=owns_new_bond and not result_ready,
                        cancel_pairing=cancel_pairing,
                        rollback_state=rollback_state,
                        rollback_requested=rollback_requested,
                        commit_irrevocable=commit_irrevocable,
                    ),
                    rollback_requested=rollback_requested,
                    commit_irrevocable=commit_irrevocable,
                )
                if (
                    owns_new_bond
                    and not result_ready
                    and not rollback_state.succeeded
                    and provisional_bond is not None
                ):
                    # Pair() succeeded, so this bond is ours.  If verification
                    # and rollback both failed, replace the original error with
                    # a sanitized ownership handle instead of orphaning it.
                    raise PairingCleanupIncompleteError(provisional_bond)

    async def async_forget(
        self,
        address: str,
        adapter: str,
        adapter_address: str,
    ) -> None:
        api = self._api_loader()
        bus = None
        _normalize_adapter(adapter)
        adapter_address = _normalize_adapter_address(adapter_address)
        try:
            bus = await self._open_bus(api)
            owner = await self._owner(bus, api)
            objects = await self._objects(bus, api, owner)
            current_adapter = _resolve_adapter_identity(objects, adapter_address)
            try:
                device = _resolve_device_on_adapter(
                    objects,
                    address,
                    current_adapter,
                    validate_uuids=True,
                )
            except BluetoothDeviceNotFoundError:
                return
            if not device.paired:
                # RemoveDevice deletes the whole Device1 object, not merely a
                # bond.  Do nothing when BlueZ reports there is no bond.
                return
            if device.uuids and MESHTASTIC_SERVICE_UUID not in device.uuids:
                raise NotMeshtasticDeviceError(
                    "The selected bond is not a verified Meshtastic device"
                )
            await self._call_bluez(
                bus,
                api,
                owner,
                path=device.adapter_path,
                interface=BLUEZ_ADAPTER,
                member="RemoveDevice",
                signature="o",
                body=[device.path],
            )

            verified_objects = await self._objects(bus, api, owner)
            with suppress(BluetoothDeviceNotFoundError):
                remaining = _resolve_device_on_adapter(
                    verified_objects,
                    address,
                    current_adapter,
                    validate_uuids=True,
                )
                if remaining.paired:
                    raise PairingRejectedError(
                        "BlueZ did not remove the Bluetooth bond"
                    )
        except PairingError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise BluetoothUnavailableError(
                "The local BlueZ pairing service failed"
            ) from exc
        finally:
            if bus is not None:
                await self._run_shielded_cleanup(self._disconnect_bus(bus))


class PairingAttempt:
    """One background Pair() call and its optional transient PIN prompt."""

    def __init__(
        self,
        manager: "BluetoothPairingManager",
        address: str,
        backend: _PairingBackend,
        overall_timeout: float,
        prompt_timeout: float,
    ) -> None:
        self.address = address
        self._manager = manager
        self._backend = backend
        self._overall_timeout = overall_timeout
        self._prompt_timeout = prompt_timeout
        self._ready = asyncio.Event()
        self._prompt_kind = None
        self._pin_future = None
        self._prompt_used = False
        self._provisional_bonds: tuple[ProvisionalBond, ...] = ()
        self._retired_bonds: tuple[ProvisionalBond, ...] = ()
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[PairingResult]) -> None:
        self._ready.set()
        if self._pin_future is not None and not self._pin_future.done():
            self._pin_future.cancel()
        with suppress(BaseException):
            task.exception()

    def _accept_result(self, result: PairingResult) -> PairingResult:
        """Record or reject a result before releasing the process-wide lock."""
        if result.bond_created:
            self._retired_bonds = self._manager._record_created_bond(
                ProvisionalBond(
                    address=result.address,
                    adapter=result.adapter,
                    adapter_address=result.adapter_address,
                    device_path=result.device_path,
                    bluez_owner=result.bluez_owner,
                )
            )
            return result

        pending = self._manager._matching_created_bonds(
            ProvisionalBond(
                address=result.address,
                adapter=result.adapter,
                adapter_address=result.adapter_address,
                device_path=result.device_path,
                bluez_owner=result.bluez_owner,
            )
        )
        if pending:
            # Never turn a backend-confirmed pre-existing bond into an owned
            # one from stale process memory, and never hand the older flow's
            # cleanup authority to this attempt.
            raise PairingOwnershipPendingError(
                "An earlier pairing flow still controls this radio"
            )
        return result

    async def _run(self) -> PairingResult:
        try:
            async with asyncio.timeout(self._overall_timeout):
                async with _pairing_lock():
                    result = await self._backend.async_pair(
                        self.address,
                        self._request_pin,
                        self._cancel_prompt,
                        self._manager._ensure_candidate_unambiguous,
                    )
                    return self._accept_result(result)
        except PairingCleanupIncompleteError as exc:
            self._provisional_bonds = exc.bonds
            for bond in exc.bonds:
                if not self._manager._contains_created_bond(bond):
                    self._manager._record_created_bond(bond)
            raise
        except TimeoutError as exc:
            raise PairingTimeoutError("Bluetooth pairing timed out") from exc
        finally:
            if self._pin_future is not None and not self._pin_future.done():
                self._pin_future.cancel()

    async def _request_pin(self, kind: str) -> str:
        if kind not in ("passkey", "pin_code") or self._prompt_used:
            raise PairingStateError("BlueZ requested an unexpected authentication method")
        self._prompt_used = True
        self._prompt_kind = kind
        self._pin_future = asyncio.get_running_loop().create_future()
        self._ready.set()
        try:
            async with asyncio.timeout(self._prompt_timeout):
                return await self._pin_future
        except TimeoutError as exc:
            raise PinPromptTimeoutError("The Bluetooth PIN prompt timed out") from exc
        finally:
            self._pin_future = None
            self._prompt_kind = None

    def _cancel_prompt(self, error: PairingError) -> None:
        """Fail the transient prompt when Agent1 reports Cancel or Release."""
        if self._pin_future is not None and not self._pin_future.done():
            self._pin_future.set_exception(error)

    async def _wait_ready(self) -> None:
        try:
            await self._ready.wait()
            if self._task.done():
                await self._task
        except asyncio.CancelledError:
            await self.async_cancel()
            # The backend suppresses cancellation only when a verified new
            # bond could not be rolled back.  Deliver that completed attempt
            # so the flow can record or explicitly remove its ownership marker
            # instead of losing the only safe cleanup handle.
            if self.state == "completed":
                return
            if self.provisional_bond is not None:
                return
            raise
        except BaseException:
            await self.async_cancel()
            raise

    @property
    def state(self) -> str:
        """Return pairing, pin_required, completed, failed, or cancelled."""
        if self._task.cancelled():
            return "cancelled"
        if self._task.done():
            return "failed" if self._task.exception() is not None else "completed"
        if self._prompt_kind is not None:
            return "pin_required"
        return "pairing"

    @property
    def requires_pin(self) -> bool:
        """Whether Agent1 is currently waiting for the GUI PIN."""
        return self.state == "pin_required"

    @property
    def prompt_kind(self) -> str | None:
        """The BlueZ prompt kind, without any PIN data."""
        return self._prompt_kind if self.requires_pin else None

    @property
    def result(self) -> PairingResult | None:
        """Return the verified result only after successful completion."""
        if self.state != "completed":
            return None
        return self._task.result()

    @property
    def provisional_bond(self) -> ProvisionalBond | None:
        """Return retained ownership when verification and rollback both failed."""
        return self._provisional_bonds[0] if self._provisional_bonds else None

    @property
    def provisional_bonds(self) -> tuple[ProvisionalBond, ...]:
        """Return every ambiguous cleanup handle retained by this attempt."""
        return self._provisional_bonds

    @property
    def retired_bonds(self) -> tuple[ProvisionalBond, ...]:
        """Return superseded aliases replaced by this proven new bond."""
        return self._retired_bonds

    async def async_result(self) -> PairingResult:
        """Wait for the verified final result."""
        return await self._task

    async def async_submit_pin(self, pin: str) -> PairingResult:
        """Submit one transient six-digit PIN and wait for verification."""
        if not isinstance(pin, str) or _PIN_RE.fullmatch(pin) is None:
            raise InvalidPinError("Enter the six-digit PIN shown by the radio")
        pin_future = self._pin_future
        if not self.requires_pin or pin_future is None or pin_future.done():
            raise PairingStateError("This pairing attempt is not waiting for a PIN")
        pin_future.set_result(pin)
        del pin, pin_future
        return await self._task

    async def async_cancel(self) -> None:
        """Cancel the attempt and wait until its backend cleanup has run."""
        if self._pin_future is not None and not self._pin_future.done():
            self._pin_future.cancel()
        if not self._task.done():
            self._task.cancel()
        with suppress(BaseException):
            await self._task


class BluetoothPairingManager:
    """Serialize bounded pairing attempts and track MeshNet-created bonds."""

    def __init__(
        self,
        backend_factory: Callable[[], _PairingBackend] | None = None,
        *,
        overall_timeout: float = 75.0,
        prompt_timeout: float = 50.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if overall_timeout <= 0 or prompt_timeout <= 0:
            raise ValueError("Pairing timeouts must be positive")
        self._backend_factory = backend_factory or _BlueZPairingBackend
        self._overall_timeout = overall_timeout
        self._prompt_timeout = prompt_timeout
        self._monotonic = monotonic
        self._created_bonds: dict[tuple[str, str], str] = {}
        self._created_bond_paths: dict[tuple[str, str], str] = {}
        self._created_bond_owners: dict[tuple[str, str], str] = {}

    @staticmethod
    def _normalized_bond(bond: ProvisionalBond) -> ProvisionalBond:
        """Validate internal ownership evidence before retaining or comparing it."""
        address = _normalize_address(bond.address)
        adapter = _normalize_adapter(bond.adapter)
        adapter_address = _normalize_adapter_address(bond.adapter_address)
        device_path = bond.device_path
        if device_path is not None:
            path_match = (
                _DEVICE_PATH_RE.fullmatch(device_path)
                if isinstance(device_path, str)
                else None
            )
            if path_match is None or path_match.group(1) != adapter:
                raise BluetoothUnavailableError(
                    "The provisional Bluetooth device identity is invalid"
                )
        bluez_owner = bond.bluez_owner
        if bluez_owner is not None and (
            not isinstance(bluez_owner, str)
            or _UNIQUE_OWNER_RE.fullmatch(bluez_owner) is None
        ):
            raise BluetoothUnavailableError(
                "The provisional BlueZ owner identity is invalid"
            )
        return ProvisionalBond(
            address=address,
            adapter=adapter,
            adapter_address=adapter_address,
            device_path=device_path,
            bluez_owner=bluez_owner,
        )

    def _bond_for_key(self, key: tuple[str, str]) -> ProvisionalBond:
        """Rebuild one validated cleanup handle from the private maps."""
        return ProvisionalBond(
            address=key[1],
            adapter=self._created_bonds[key],
            adapter_address=key[0],
            device_path=self._created_bond_paths.get(key),
            bluez_owner=self._created_bond_owners.get(key),
        )

    def _matching_created_bonds(
        self, candidate: ProvisionalBond
    ) -> tuple[ProvisionalBond, ...]:
        """Match pending proof by stable controller plus address or object path."""
        normalized = self._normalized_bond(candidate)
        matches: list[ProvisionalBond] = []
        for key in sorted(self._created_bonds):
            if key[0] != normalized.adapter_address:
                continue
            same_address = key[1] == normalized.address
            stored_path = self._created_bond_paths.get(key)
            stored_path_match = (
                _DEVICE_PATH_RE.fullmatch(stored_path)
                if stored_path is not None
                else None
            )
            candidate_path_match = (
                _DEVICE_PATH_RE.fullmatch(normalized.device_path)
                if normalized.device_path is not None
                else None
            )
            same_path = bool(
                stored_path_match is not None
                and candidate_path_match is not None
                # Stable controller identity was already matched above, so
                # compare the Device1 suffix independently of hci renumbering.
                and stored_path_match.group(2) == candidate_path_match.group(2)
            )
            if same_address or same_path:
                matches.append(self._bond_for_key(key))
        return tuple(matches)

    def _contains_created_bond(self, bond: ProvisionalBond) -> bool:
        """Return whether this exact process proof is already retained."""
        normalized = self._normalized_bond(bond)
        key = (normalized.adapter_address, normalized.address)
        return (
            self._created_bonds.get(key) == normalized.adapter
            and self._created_bond_paths.get(key) == normalized.device_path
            and self._created_bond_owners.get(key) == normalized.bluez_owner
        )

    def _release_key(self, key: tuple[str, str]) -> None:
        """Discard process-local proof without changing BlueZ state."""
        self._created_bonds.pop(key, None)
        self._created_bond_paths.pop(key, None)
        self._created_bond_owners.pop(key, None)

    def _record_created_bond(
        self, bond: ProvisionalBond
    ) -> tuple[ProvisionalBond, ...]:
        """Retain proof and retire every same-device alias it supersedes."""
        normalized = self._normalized_bond(bond)
        retired = self._matching_created_bonds(normalized)
        for old in retired:
            self._release_key((old.adapter_address, old.address))
        key = (normalized.adapter_address, normalized.address)
        self._created_bonds[key] = normalized.adapter
        if normalized.device_path is not None:
            self._created_bond_paths[key] = normalized.device_path
        if normalized.bluez_owner is not None:
            self._created_bond_owners[key] = normalized.bluez_owner
        return retired

    def _ensure_candidate_unambiguous(self, candidate: ProvisionalBond) -> None:
        """Refuse Pair() while older same-device ownership is unresolved."""
        pending = self._matching_created_bonds(candidate)
        if not pending:
            return
        normalized = self._normalized_bond(candidate)
        # A BlueZ owner restart terminates every call associated with the old
        # daemon generation.  It is therefore safe to release, but not delete,
        # old process proof and treat any current bond as pre-existing.
        if normalized.bluez_owner is not None and all(
            bond.bluez_owner is not None
            and bond.bluez_owner != normalized.bluez_owner
            for bond in pending
        ):
            for bond in pending:
                self._release_key((bond.adapter_address, bond.address))
            return
        raise PairingOwnershipPendingError(
            "An earlier pairing flow still controls this radio"
        )

    def release_created(
        self,
        address: str,
        *,
        adapter: str,
        adapter_address: str,
    ) -> bool:
        """Release exact process proof after HA commits or explicitly keeps it."""
        normalized = _normalize_address(address)
        normalized_adapter = _normalize_adapter(adapter)
        normalized_adapter_address = _normalize_adapter_address(adapter_address)
        key = (normalized_adapter_address, normalized)
        if self._created_bonds.get(key) != normalized_adapter:
            return False
        self._release_key(key)
        return True

    async def async_begin(self, address: str) -> PairingAttempt:
        """Start pairing and return at completion or the first PIN prompt."""
        normalized = _normalize_address(address)
        _consume_attempt(normalized, self._monotonic())
        attempt = PairingAttempt(
            self,
            normalized,
            self._backend_factory(),
            self._overall_timeout,
            self._prompt_timeout,
        )
        await attempt._wait_ready()
        return attempt

    async def async_forget_current_bond(
        self,
        address: str,
        *,
        adapter: str,
        adapter_address: str,
        user_confirmed: bool,
    ) -> None:
        """Remove the current address-scoped bond after explicit user consent.

        BlueZ does not expose bond generations.  The stored marker proves only
        that MeshNet originally paired this radio; it cannot prove that the
        current bond was not later recreated by another client.  This method
        is therefore reserved for the warned Configure -> Remove gateway form
        and must never be called by unattended entry/HACS teardown.
        """
        if user_confirmed is not True:
            raise BondNotOwnedError(
                "Current Bluetooth bond removal was not explicitly confirmed"
            )
        normalized = _normalize_address(address)
        normalized_adapter = _normalize_adapter(adapter)
        normalized_adapter_address = _normalize_adapter_address(adapter_address)
        await self._async_forget_address(
            normalized,
            adapter=normalized_adapter,
            adapter_address=normalized_adapter_address,
        )
        self._release_key((normalized_adapter_address, normalized))

    async def _async_forget_address(
        self,
        address: str,
        *,
        adapter: str,
        adapter_address: str,
    ) -> None:
        """Serialize and bound one explicitly confirmed bond removal."""
        try:
            async with _pairing_lock():
                async with asyncio.timeout(self._overall_timeout):
                    await self._backend_factory().async_forget(
                        address, adapter, adapter_address
                    )
        except TimeoutError as exc:
            raise PairingTimeoutError("Bluetooth bond removal timed out") from exc


# Concise alias for callers that do not need the Bluetooth qualifier.
PairingManager = BluetoothPairingManager


__all__ = [
    "AmbiguousBluetoothDeviceError",
    "BluetoothDeviceNotFoundError",
    "BluetoothPairingManager",
    "BluetoothUnavailableError",
    "BondNotOwnedError",
    "InvalidBluetoothAddressError",
    "InvalidPinError",
    "MESHTASTIC_SERVICE_UUID",
    "NotMeshtasticDeviceError",
    "PairingAttempt",
    "PairingCancelledError",
    "PairingCleanupIncompleteError",
    "PairingError",
    "PairingManager",
    "PairingOwnershipPendingError",
    "PairingRateLimitedError",
    "PairingRejectedError",
    "PairingResult",
    "PairingStateError",
    "PairingTimeoutError",
    "PinPromptTimeoutError",
    "ProvisionalBond",
]
