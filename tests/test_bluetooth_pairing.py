"""Security and lifecycle tests for the bounded BlueZ pairing engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.meshnet.bluetooth_pairing import (
    BLUEZ_ADAPTER,
    BLUEZ_DEVICE,
    MESHTASTIC_SERVICE_UUID,
    AmbiguousBluetoothDeviceError,
    BluetoothPairingManager,
    BluetoothUnavailableError,
    BondNotOwnedError,
    InvalidBluetoothAddressError,
    InvalidPinError,
    NotMeshtasticDeviceError,
    PairingCleanupIncompleteError,
    PairingOwnershipPendingError,
    PairingRateLimitedError,
    PairingRejectedError,
    PairingResult,
    PairingStateError,
    PairingTimeoutError,
    PinPromptTimeoutError,
    ProvisionalBond,
    _BlueZPairingBackend,
    _create_agent,
    _load_dbus_api,
    _resolve_device,
    _sender_guard,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
IDENTITY_ADDRESS = "12:34:56:78:9A:BC"
DEVICE_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
ADAPTER_PATH = "/org/bluez/hci0"
ADAPTER_ADDRESS = "00:11:22:33:44:55"
BLUEZ_OWNER = ":1.42"


class _FakeDBusError(Exception):
    pass


class _MessageType:
    METHOD_CALL = 1
    METHOD_RETURN = 2
    ERROR = 3


class _Message:
    def __init__(self, **kwargs: Any) -> None:
        self.message_type = kwargs.pop("message_type", _MessageType.METHOD_CALL)
        self.destination = kwargs.pop("destination", None)
        self.path = kwargs.pop("path", None)
        self.interface = kwargs.pop("interface", None)
        self.member = kwargs.pop("member", None)
        self.signature = kwargs.pop("signature", "")
        self.body = kwargs.pop("body", [])
        self.sender = kwargs.pop("sender", None)
        self.error_name = kwargs.pop("error_name", None)

    @classmethod
    def new_error(cls, message: Any, error_name: str, text: str) -> _Message:
        return cls(
            message_type=_MessageType.ERROR,
            destination=message.sender,
            error_name=error_name,
            signature="s",
            body=[text],
        )


class _ServiceInterface:
    def __init__(self, name: str) -> None:
        self.name = name


def _method(*args: Any, **kwargs: Any):
    del args, kwargs

    def decorator(function):
        return function

    return decorator


_FAKE_API = SimpleNamespace(
    DBusError=_FakeDBusError,
    Message=_Message,
    MessageType=_MessageType,
    ServiceInterface=_ServiceInterface,
    method=_method,
)


def _reply(signature: str = "", body: list[Any] | None = None) -> _Message:
    return _Message(
        message_type=_MessageType.METHOD_RETURN,
        signature=signature,
        body=body or [],
    )


@dataclass
class _Variant:
    value: Any


class _FakeBlueZBus:
    """Small BlueZ state machine that exercises the real backend."""

    def __init__(
        self,
        *,
        paired: bool = False,
        initial_uuids: list[str] | None = None,
        post_uuids: list[str] | None = None,
        pair_mode: str = "prompt",
        identity_address: str | None = None,
        cleanup_hang_members: set[str] | None = None,
        adapter_properties: dict[str, Any] | None = None,
        additional_adapters: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.paired = paired
        self.initial_uuids = (
            [MESHTASTIC_SERVICE_UUID]
            if initial_uuids is None
            else initial_uuids
        )
        self.post_uuids = (
            [MESHTASTIC_SERVICE_UUID] if post_uuids is None else post_uuids
        )
        self.pair_mode = pair_mode
        self.identity_address = identity_address
        self.cleanup_hang_members = cleanup_hang_members or set()
        self.adapter_properties = (
            {
                "Address": _Variant(ADAPTER_ADDRESS),
                "Powered": _Variant(True),
            }
            if adapter_properties is None
            else adapter_properties
        )
        self.additional_adapters = additional_adapters or {}
        self.current_address = ADDRESS
        self.removed = False
        self.agent = None
        self.guard = None
        self.received_passkey = None
        self.actions: list[str] = []

    async def connect(self) -> _FakeBlueZBus:
        self.actions.append("connect")
        return self

    def _objects(self) -> dict[str, Any]:
        objects: dict[str, Any] = {
            ADAPTER_PATH: {BLUEZ_ADAPTER: self.adapter_properties},
            **self.additional_adapters,
        }
        if self.removed:
            return objects
        uuids = self.post_uuids if self.paired else self.initial_uuids
        objects[DEVICE_PATH] = {
            BLUEZ_DEVICE: {
                "Address": _Variant(self.current_address),
                "Paired": _Variant(self.paired),
                "UUIDs": _Variant(uuids),
            }
        }
        return objects

    async def call(self, message: _Message) -> _Message:
        self.actions.append(f"call:{message.member}")
        if message.member in self.cleanup_hang_members:
            await asyncio.Event().wait()
        if message.member == "GetNameOwner":
            return _reply("s", [BLUEZ_OWNER])
        if message.member == "GetManagedObjects":
            return _reply("a{oa{sa{sv}}}", [self._objects()])
        if message.member in ("RegisterAgent", "UnregisterAgent", "CancelPairing"):
            return _reply()
        if message.member == "Pair":
            if self.pair_mode == "hang":
                await asyncio.Event().wait()
            elif self.pair_mode == "external_already_exists":
                # Model another BlueZ client winning the pairing race after
                # our initial Paired=False snapshot.
                self.paired = True
                return _Message(
                    message_type=_MessageType.ERROR,
                    error_name="org.bluez.Error.AlreadyExists",
                    signature="s",
                    body=["external bond"],
                )
            elif self.pair_mode == "wrong_device":
                await self.agent.RequestPasskey(
                    "/org/bluez/hci0/dev_00_00_00_00_00_00"
                )
            elif self.pair_mode == "confirmation":
                self.agent.RequestConfirmation(DEVICE_PATH, 123456)
            elif self.pair_mode == "prompt":
                self.received_passkey = await self.agent.RequestPasskey(DEVICE_PATH)
            elif self.pair_mode != "no_prompt":
                raise AssertionError(f"unknown fake mode: {self.pair_mode}")
            self.paired = True
            if self.identity_address is not None:
                self.current_address = self.identity_address
            return _reply()
        if message.member == "RemoveDevice":
            self.paired = False
            self.removed = True
            return _reply()
        raise AssertionError(f"unexpected D-Bus call: {message.member}")

    def add_message_handler(self, handler) -> None:
        self.actions.append("add_handler")
        self.guard = handler

    def remove_message_handler(self, handler) -> None:
        assert handler is self.guard
        self.actions.append("remove_handler")

    def export(self, path: str, agent: Any) -> None:
        assert path.startswith("/org/meshnet/bluez/agent/a_")
        self.actions.append("export")
        self.agent = agent

    def unexport(self, path: str, agent: Any) -> None:
        assert path.startswith("/org/meshnet/bluez/agent/a_")
        assert agent is self.agent
        self.actions.append("unexport")

    def disconnect(self) -> None:
        self.actions.append("disconnect")


def _manager_for_bus(
    bus: _FakeBlueZBus,
    *,
    overall_timeout: float = 1,
    prompt_timeout: float = 1,
) -> BluetoothPairingManager:
    return BluetoothPairingManager(
        lambda: _BlueZPairingBackend(
            bus_factory=lambda: bus,
            api_loader=lambda: _FAKE_API,
        ),
        overall_timeout=overall_timeout,
        prompt_timeout=prompt_timeout,
    )


def test_real_backend_prompts_verifies_and_cleans_every_registration() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus()
        manager = _manager_for_bus(bus)

        attempt = await manager.async_begin(ADDRESS.lower())
        assert attempt.state == "pin_required"
        assert attempt.prompt_kind == "passkey"

        result = await attempt.async_submit_pin("123456")

        assert result == PairingResult(ADDRESS, "hci0", ADAPTER_ADDRESS, True)
        assert bus.received_passkey == 123456
        assert "call:RequestDefaultAgent" not in bus.actions
        assert "call:RemoveDevice" not in bus.actions
        assert "call:CancelPairing" not in bus.actions
        assert bus.actions.index("call:UnregisterAgent") < bus.actions.index(
            "unexport"
        )
        assert bus.actions.index("unexport") < bus.actions.index("remove_handler")
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_pairing_accepts_bluez_identity_address_change_on_same_device_path() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(identity_address=IDENTITY_ADDRESS)
        attempt = await _manager_for_bus(bus).async_begin(ADDRESS)

        result = await attempt.async_submit_pin("000042")

        assert result.address == IDENTITY_ADDRESS
        assert result.bond_created is True
        assert bus.received_passkey == 42

    asyncio.run(scenario())


def test_paired_identity_address_resolves_after_restart_and_can_be_forgotten() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(paired=True)
        bus.current_address = IDENTITY_ADDRESS
        manager = _manager_for_bus(bus)

        attempt = await manager.async_begin(IDENTITY_ADDRESS)

        assert attempt.result == PairingResult(
            IDENTITY_ADDRESS, "hci0", ADAPTER_ADDRESS, False
        )
        await manager.async_forget_current_bond(
            IDENTITY_ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            user_confirmed=True,
        )
        assert bus.removed is True

    asyncio.run(scenario())


def test_preexisting_verified_bond_is_preserved_without_agent_or_pair_call() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(paired=True)
        attempt = await _manager_for_bus(bus).async_begin(ADDRESS)

        assert attempt.state == "completed"
        assert attempt.result == PairingResult(
            ADDRESS, "hci0", ADAPTER_ADDRESS, False
        )
        assert "export" not in bus.actions
        assert "call:RegisterAgent" not in bus.actions
        assert "call:Pair" not in bus.actions
        assert "call:RemoveDevice" not in bus.actions
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_failed_service_verification_rolls_back_only_new_bond() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(initial_uuids=[], post_uuids=["0000180f-0000-1000-8000-00805f9b34fb"])
        attempt = await _manager_for_bus(bus).async_begin(ADDRESS)

        with pytest.raises(NotMeshtasticDeviceError):
            await attempt.async_submit_pin("123456")

        assert bus.removed is True
        assert "call:CancelPairing" not in bus.actions
        assert bus.actions.index("call:RemoveDevice") < bus.actions.index(
            "call:UnregisterAgent"
        )
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_immediate_rollback_never_crosses_stable_adapter_identity() -> None:
    class HotSwappedAdapterBus(_FakeBlueZBus):
        async def call(self, message: _Message) -> _Message:
            reply = await super().call(message)
            if message.member == "Pair":
                self.adapter_properties["Address"] = _Variant(
                    "00:11:22:33:44:66"
                )
            return reply

    async def scenario() -> None:
        bus = HotSwappedAdapterBus(pair_mode="no_prompt")

        with pytest.raises(PairingCleanupIncompleteError):
            await _manager_for_bus(bus).async_begin(ADDRESS)

        assert bus.paired is True
        assert bus.removed is False
        assert "call:RemoveDevice" not in bus.actions

    asyncio.run(scenario())


def test_pair_never_starts_after_stable_adapter_identity_changes() -> None:
    class HotSwapBeforePairBus(_FakeBlueZBus):
        async def call(self, message: _Message) -> _Message:
            reply = await super().call(message)
            if message.member == "RegisterAgent":
                self.adapter_properties["Address"] = _Variant(
                    "00:11:22:33:44:66"
                )
            return reply

    async def scenario() -> None:
        bus = HotSwapBeforePairBus(pair_mode="no_prompt")

        with pytest.raises(BluetoothUnavailableError, match="identity"):
            await _manager_for_bus(bus).async_begin(ADDRESS)

        assert "call:Pair" not in bus.actions
        assert bus.paired is False
        assert bus.removed is False
        assert "call:UnregisterAgent" in bus.actions

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "post_uuids",
    [["0000180f-0000-1000-8000-00805f9b34fb"], [123]],
)
def test_failed_verification_releases_proof_without_deferred_deletion(
    monkeypatch, post_uuids
) -> None:
    import custom_components.meshnet.bluetooth_pairing as pairing_module

    async def scenario() -> None:
        bus = _FakeBlueZBus(
            initial_uuids=[],
            post_uuids=post_uuids,
            pair_mode="no_prompt",
            cleanup_hang_members={"RemoveDevice"},
        )
        manager = _manager_for_bus(bus)

        with pytest.raises(PairingCleanupIncompleteError) as raised:
            await manager.async_begin(ADDRESS)

        assert raised.value.bond == ProvisionalBond(
            ADDRESS, "hci0", ADAPTER_ADDRESS, DEVICE_PATH
        )
        assert manager._created_bonds == {(ADAPTER_ADDRESS, ADDRESS): "hci0"}
        assert bus.paired is True
        assert bus.removed is False

        assert manager.release_created(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
        )

        assert bus.removed is False
        assert bus.paired is True
        assert manager._created_bonds == {}

    monkeypatch.setattr(pairing_module, "_CLEANUP_TIMEOUT", 0.01)
    asyncio.run(scenario())


def test_provisional_release_after_bluez_owner_restart_never_deletes(
    monkeypatch,
) -> None:
    import custom_components.meshnet.bluetooth_pairing as pairing_module

    class RestartedOwnerBus(_FakeBlueZBus):
        async def call(self, message: _Message) -> _Message:
            if message.member == "GetNameOwner" and self.paired:
                self.actions.append("call:GetNameOwner")
                return _reply("s", [":1.99"])
            return await super().call(message)

    async def scenario() -> None:
        bus = RestartedOwnerBus(
            pair_mode="no_prompt",
            identity_address=IDENTITY_ADDRESS,
            cleanup_hang_members={"RemoveDevice"},
        )
        manager = _manager_for_bus(bus)

        with pytest.raises(PairingCleanupIncompleteError) as raised:
            await manager.async_begin(ADDRESS)

        assert raised.value.bond == ProvisionalBond(
            ADDRESS, "hci0", ADAPTER_ADDRESS, DEVICE_PATH
        )
        assert bus.current_address == IDENTITY_ADDRESS
        assert bus.paired is True

        assert manager.release_created(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
        )

        assert bus.removed is False
        assert manager._created_bonds == {}

    monkeypatch.setattr(pairing_module, "_CLEANUP_TIMEOUT", 0.01)
    asyncio.run(scenario())


def test_provisional_release_preserves_same_owner_identity_address_bond(
    monkeypatch,
) -> None:
    import custom_components.meshnet.bluetooth_pairing as pairing_module

    class PostPairReadFailureBus(_FakeBlueZBus):
        failed_post_pair_read = False

        async def call(self, message: _Message) -> _Message:
            if (
                message.member == "GetManagedObjects"
                and self.paired
                and not self.failed_post_pair_read
            ):
                self.failed_post_pair_read = True
                self.actions.append("call:GetManagedObjects")
                return _Message(
                    message_type=_MessageType.ERROR,
                    error_name="org.bluez.Error.Failed",
                    signature="s",
                    body=["transient read failure"],
                )
            return await super().call(message)

    async def scenario() -> None:
        bus = PostPairReadFailureBus(
            pair_mode="no_prompt",
            identity_address=IDENTITY_ADDRESS,
            cleanup_hang_members={"RemoveDevice"},
        )
        manager = _manager_for_bus(bus)

        with pytest.raises(PairingCleanupIncompleteError) as raised:
            await manager.async_begin(ADDRESS)

        assert raised.value.bond.device_path == DEVICE_PATH
        assert raised.value.bond.bluez_owner == BLUEZ_OWNER
        assert bus.current_address == IDENTITY_ADDRESS
        assert bus.paired is True

        assert manager.release_created(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
        )

        assert bus.removed is False
        assert bus.paired is True
        assert manager._created_bonds == {}

    monkeypatch.setattr(pairing_module, "_CLEANUP_TIMEOUT", 0.01)
    asyncio.run(scenario())


def test_prompt_timeout_runs_full_cleanup_and_never_creates_bond() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus()
        attempt = await _manager_for_bus(bus, prompt_timeout=0.01).async_begin(ADDRESS)

        with pytest.raises(PinPromptTimeoutError):
            await attempt.async_result()

        assert bus.paired is False
        assert "call:UnregisterAgent" in bus.actions
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_cancelled_attempt_runs_full_cleanup() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus()
        attempt = await _manager_for_bus(bus).async_begin(ADDRESS)

        await attempt.async_cancel()

        assert bus.paired is False
        assert "call:UnregisterAgent" in bus.actions
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_cancel_pairing_never_crosses_stable_adapter_identity() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(pair_mode="hang")
        begin_task = asyncio.create_task(_manager_for_bus(bus).async_begin(ADDRESS))

        while "call:Pair" not in bus.actions:
            await asyncio.sleep(0)
        bus.adapter_properties["Address"] = _Variant("00:11:22:33:44:66")
        begin_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await begin_task

        assert "call:CancelPairing" not in bus.actions
        assert bus.removed is False
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_each_cleanup_call_is_bounded_and_later_cleanup_still_runs(monkeypatch) -> None:
    import custom_components.meshnet.bluetooth_pairing as pairing_module

    async def scenario() -> None:
        bus = _FakeBlueZBus(cleanup_hang_members={"UnregisterAgent"})
        attempt = await _manager_for_bus(bus).async_begin(ADDRESS)

        result = await attempt.async_submit_pin("123456")

        assert result.bond_created is True
        assert "call:UnregisterAgent" in bus.actions
        assert "unexport" in bus.actions
        assert bus.actions[-1] == "disconnect"

    monkeypatch.setattr(pairing_module, "_CLEANUP_TIMEOUT", 0.01)
    asyncio.run(scenario())


def test_pair_error_after_external_bond_does_not_remove_or_cancel_it() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(pair_mode="external_already_exists")

        with pytest.raises(PairingRejectedError):
            await _manager_for_bus(bus).async_begin(ADDRESS)

        assert bus.paired is True
        assert bus.removed is False
        assert "call:CancelPairing" not in bus.actions
        assert "call:RemoveDevice" not in bus.actions
        assert "call:UnregisterAgent" in bus.actions
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_timeout_during_success_cleanup_rolls_back_uncommitted_bond(
    monkeypatch,
) -> None:
    import custom_components.meshnet.bluetooth_pairing as pairing_module

    async def scenario() -> None:
        bus = _FakeBlueZBus(
            pair_mode="no_prompt",
            cleanup_hang_members={"UnregisterAgent"},
        )

        with pytest.raises(PairingTimeoutError):
            await _manager_for_bus(bus, overall_timeout=0.005).async_begin(ADDRESS)

        assert bus.paired is False
        assert bus.removed is True
        assert "call:RemoveDevice" in bus.actions
        assert bus.actions[-1] == "disconnect"

    monkeypatch.setattr(pairing_module, "_CLEANUP_TIMEOUT", 0.03)
    asyncio.run(scenario())


def test_failed_cancellation_rollback_commits_result_and_tracks_bond(
    monkeypatch,
) -> None:
    import custom_components.meshnet.bluetooth_pairing as pairing_module

    async def scenario() -> None:
        bus = _FakeBlueZBus(
            pair_mode="no_prompt",
            cleanup_hang_members={"UnregisterAgent", "RemoveDevice"},
        )
        manager = _manager_for_bus(bus, overall_timeout=0.005)

        attempt = await manager.async_begin(ADDRESS)

        assert attempt.result == PairingResult(
            ADDRESS, "hci0", ADAPTER_ADDRESS, True
        )
        assert bus.paired is True
        assert bus.removed is False
        assert (ADAPTER_ADDRESS, ADDRESS) in manager._created_bonds
        assert bus.actions[-1] == "disconnect"

    monkeypatch.setattr(pairing_module, "_CLEANUP_TIMEOUT", 0.03)
    asyncio.run(scenario())


def test_cancelled_begin_delivers_attempt_when_rollback_cannot_be_verified(
    monkeypatch,
) -> None:
    import custom_components.meshnet.bluetooth_pairing as pairing_module

    async def scenario() -> None:
        bus = _FakeBlueZBus(
            pair_mode="no_prompt",
            cleanup_hang_members={"UnregisterAgent", "RemoveDevice"},
        )
        manager = _manager_for_bus(bus, overall_timeout=1)
        begin_task = asyncio.create_task(manager.async_begin(ADDRESS))

        while "call:UnregisterAgent" not in bus.actions:
            await asyncio.sleep(0)
        begin_task.cancel()

        attempt = await begin_task

        assert attempt.result == PairingResult(
            ADDRESS, "hci0", ADAPTER_ADDRESS, True
        )
        assert (ADAPTER_ADDRESS, ADDRESS) in manager._created_bonds
        assert bus.paired is True
        assert bus.removed is False

    monkeypatch.setattr(pairing_module, "_CLEANUP_TIMEOUT", 0.03)
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("adapter_properties", "additional_adapters"),
    [
        ({}, {}),
        (
            {
                "Address": _Variant(ADAPTER_ADDRESS),
                "Powered": _Variant("yes"),
            },
            {},
        ),
        (
            {
                "Address": _Variant(ADAPTER_ADDRESS),
                "Powered": _Variant(False),
            },
            {},
        ),
        (
            {
                "Address": _Variant(ADAPTER_ADDRESS),
                "Powered": _Variant(True),
            },
            {
                "/org/bluez/hci1": {
                    BLUEZ_ADAPTER: {
                        "Address": _Variant("00:11:22:33:44:66"),
                        "Powered": _Variant(True),
                    }
                }
            },
        ),
    ],
)
def test_pairing_fails_closed_before_agent_when_adapter_state_is_unsafe(
    adapter_properties, additional_adapters
) -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(
            adapter_properties=adapter_properties,
            additional_adapters=additional_adapters,
        )

        with pytest.raises(BluetoothUnavailableError, match="adapter"):
            await _manager_for_bus(bus).async_begin(ADDRESS)

        assert "add_handler" not in bus.actions
        assert "export" not in bus.actions
        assert "call:RegisterAgent" not in bus.actions
        assert "call:Pair" not in bus.actions
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_pairing_allows_only_selected_adapter_powered() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(
            additional_adapters={
                "/org/bluez/hci1": {
                    BLUEZ_ADAPTER: {
                        "Address": _Variant("00:11:22:33:44:66"),
                        "Powered": _Variant(False),
                    }
                }
            }
        )
        attempt = await _manager_for_bus(bus).async_begin(ADDRESS)

        assert attempt.requires_pin is True
        await attempt.async_cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("pin", ["", "12345", "1234567", "12a456", 123456])
def test_invalid_pin_is_rejected_without_consuming_prompt(pin: Any) -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus()
        attempt = await _manager_for_bus(bus).async_begin(ADDRESS)

        with pytest.raises(InvalidPinError):
            await attempt.async_submit_pin(pin)
        assert attempt.requires_pin is True

        await attempt.async_cancel()

    asyncio.run(scenario())


def test_agent_rejects_wrong_device_and_confirmation_requests() -> None:
    async def request_pin(kind: str) -> str:
        assert kind == "passkey"
        return "123456"

    def cancel_prompt(error: Exception) -> None:
        raise AssertionError(f"unexpected cancel: {error}")

    agent = _create_agent(_FAKE_API, DEVICE_PATH, request_pin, cancel_prompt)

    with pytest.raises(_FakeDBusError):
        asyncio.run(
            agent.RequestPasskey("/org/bluez/hci0/dev_00_00_00_00_00_00")
        )
    with pytest.raises(_FakeDBusError):
        agent.RequestConfirmation(DEVICE_PATH, 123456)
    with pytest.raises(_FakeDBusError):
        agent.RequestAuthorization(DEVICE_PATH)
    with pytest.raises(_FakeDBusError):
        agent.AuthorizeService(DEVICE_PATH, MESHTASTIC_SERVICE_UUID)


def test_real_dbus_fast_agent_decorators_construct() -> None:
    """Catch decorator/signature incompatibilities in Home Assistant's dbus-fast."""
    pytest.importorskip("dbus_fast")

    agent = _create_agent(
        _load_dbus_api(),
        DEVICE_PATH,
        lambda _kind: None,
        lambda _error: None,
    )

    assert agent.name == "org.bluez.Agent1"


def test_sender_guard_allows_only_captured_bluez_unique_owner() -> None:
    path = "/org/meshnet/bluez/agent/a_test"
    guard = _sender_guard(_FAKE_API, path, BLUEZ_OWNER)

    allowed = _Message(path=path, sender=BLUEZ_OWNER)
    rejected = _Message(path=path, sender=":1.999")

    assert guard(allowed) is None
    reply = guard(rejected)
    assert reply.message_type == _MessageType.ERROR
    assert reply.error_name == "org.freedesktop.DBus.Error.AccessDenied"


def test_dbus_reply_signature_is_validated_before_body_is_used() -> None:
    class WrongSignatureBus(_FakeBlueZBus):
        async def call(self, message: _Message) -> _Message:
            if message.member == "GetNameOwner":
                self.actions.append("call:GetNameOwner")
                return _reply("", [BLUEZ_OWNER])
            return await super().call(message)

    async def scenario() -> None:
        bus = WrongSignatureBus()
        with pytest.raises(BluetoothUnavailableError):
            await _manager_for_bus(bus).async_begin(ADDRESS)
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_resolver_rejects_unsafe_or_ambiguous_device_identity() -> None:
    objects = {
        ADAPTER_PATH: {BLUEZ_ADAPTER: {}},
        "/unsafe/device": {
            BLUEZ_DEVICE: {
                "Address": _Variant(ADDRESS),
                "Paired": _Variant(False),
                "UUIDs": _Variant([MESHTASTIC_SERVICE_UUID]),
            }
        },
    }
    with pytest.raises(BluetoothUnavailableError):
        _resolve_device(objects, ADDRESS)

    objects[DEVICE_PATH] = objects["/unsafe/device"]
    with pytest.raises(AmbiguousBluetoothDeviceError):
        _resolve_device(objects, ADDRESS)


@pytest.mark.parametrize("paired", ["false", 0, 1, None, [], {}])
def test_resolver_rejects_malformed_bluez_paired_state(paired: Any) -> None:
    objects = {
        ADAPTER_PATH: {BLUEZ_ADAPTER: {"Powered": _Variant(True)}},
        DEVICE_PATH: {
            BLUEZ_DEVICE: {
                "Address": _Variant(ADDRESS),
                "Paired": _Variant(paired),
                "UUIDs": _Variant([MESHTASTIC_SERVICE_UUID]),
            }
        },
    }

    with pytest.raises(BluetoothUnavailableError, match="bond state"):
        _resolve_device(objects, ADDRESS)


def test_manager_rejects_non_mac_alias_before_opening_dbus() -> None:
    manager = BluetoothPairingManager(lambda: pytest.fail("backend was opened"))

    with pytest.raises(InvalidBluetoothAddressError):
        asyncio.run(manager.async_begin("meshtastic-radio"))


def test_current_bond_removal_requires_explicit_confirmation() -> None:
    class ForgetBackend:
        def __init__(self) -> None:
            self.forgotten: list[str] = []

        async def async_pair(
            self, address, request_pin, cancel_prompt, check_candidate
        ):
            del request_pin, cancel_prompt
            check_candidate(
                ProvisionalBond(address, "hci0", ADAPTER_ADDRESS)
            )
            return PairingResult(address, "hci0", ADAPTER_ADDRESS, False)

        async def async_forget(
            self, address: str, adapter: str, adapter_address: str
        ) -> None:
            self.forgotten.append((address, adapter, adapter_address))

    async def scenario() -> None:
        backend = ForgetBackend()
        manager = BluetoothPairingManager(lambda: backend)

        with pytest.raises(BondNotOwnedError):
            await manager.async_forget_current_bond(
                ADDRESS,
                adapter="hci0",
                adapter_address=ADAPTER_ADDRESS,
                user_confirmed=False,
            )

        await manager.async_forget_current_bond(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            user_confirmed=True,
        )
        assert backend.forgotten == [(ADDRESS, "hci0", ADAPTER_ADDRESS)]

    asyncio.run(scenario())


def test_manager_refuses_stale_created_bond_promotion_on_retry() -> None:
    class RetryBackend:
        calls = 0

        async def async_pair(
            self, address, request_pin, cancel_prompt, check_candidate
        ):
            del request_pin, cancel_prompt
            check_candidate(
                ProvisionalBond(address, "hci0", ADAPTER_ADDRESS)
            )
            type(self).calls += 1
            return PairingResult(
                address,
                "hci0",
                ADAPTER_ADDRESS,
                type(self).calls == 1,
            )

        async def async_forget(
            self, address, adapter, adapter_address, *, allow_unverified_service=False
        ):
            del address, adapter, adapter_address, allow_unverified_service

    async def scenario() -> None:
        manager = BluetoothPairingManager(RetryBackend)

        first = await manager.async_begin(ADDRESS)
        with pytest.raises(PairingOwnershipPendingError):
            await manager.async_begin(ADDRESS)

        assert first.result is not None and first.result.bond_created is True
        assert RetryBackend.calls == 1

        assert manager.release_created(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
        )
        preexisting = await manager.async_begin(ADDRESS)
        assert preexisting.result is not None
        assert preexisting.result.bond_created is False

    asyncio.run(scenario())


def test_pending_path_match_survives_identity_change_and_hci_renumber() -> None:
    manager = BluetoothPairingManager()
    manager._record_created_bond(
        ProvisionalBond(
            ADDRESS,
            "hci0",
            ADAPTER_ADDRESS,
            DEVICE_PATH,
            BLUEZ_OWNER,
        )
    )

    with pytest.raises(PairingOwnershipPendingError):
        manager._ensure_candidate_unambiguous(
            ProvisionalBond(
                IDENTITY_ADDRESS,
                "hci7",
                ADAPTER_ADDRESS,
                "/org/bluez/hci7/dev_AA_BB_CC_DD_EE_FF",
                BLUEZ_OWNER,
            )
        )


def test_bluez_restart_releases_old_proof_without_deleting_any_bond() -> None:
    manager = BluetoothPairingManager()
    manager._record_created_bond(
        ProvisionalBond(
            ADDRESS,
            "hci0",
            ADAPTER_ADDRESS,
            DEVICE_PATH,
            BLUEZ_OWNER,
        )
    )

    manager._ensure_candidate_unambiguous(
        ProvisionalBond(
            ADDRESS,
            "hci0",
            ADAPTER_ADDRESS,
            DEVICE_PATH,
            ":1.99",
        )
    )

    assert manager._created_bonds == {}


def test_confirmed_forget_is_idempotent_when_bluez_device_is_gone() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(paired=False)
        bus.removed = True
        manager = _manager_for_bus(bus)

        await manager.async_forget_current_bond(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            user_confirmed=True,
        )

        assert "call:RemoveDevice" not in bus.actions
        assert bus.actions[-1] == "disconnect"

    asyncio.run(scenario())


def test_confirmed_forget_preserves_present_unpaired_device_object() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(paired=False)

        await _manager_for_bus(bus).async_forget_current_bond(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            user_confirmed=True,
        )

        assert bus.removed is False
        assert "call:RemoveDevice" not in bus.actions

    asyncio.run(scenario())


def test_confirmed_forget_allows_empty_cached_uuid_list() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(paired=True, post_uuids=[])

        await _manager_for_bus(bus).async_forget_current_bond(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            user_confirmed=True,
        )

        assert bus.removed is True
        assert "call:RemoveDevice" in bus.actions

    asyncio.run(scenario())


def test_confirmed_forget_rejects_nonempty_foreign_uuid_list() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(
            paired=True,
            post_uuids=["0000180f-0000-1000-8000-00805f9b34fb"],
        )

        with pytest.raises(NotMeshtasticDeviceError):
            await _manager_for_bus(bus).async_forget_current_bond(
                ADDRESS,
                adapter="hci0",
                adapter_address=ADAPTER_ADDRESS,
                user_confirmed=True,
            )

        assert bus.removed is False
        assert "call:RemoveDevice" not in bus.actions

    asyncio.run(scenario())


def test_confirmed_forget_rejects_malformed_uuid_list() -> None:
    async def scenario() -> None:
        bus = _FakeBlueZBus(paired=True, post_uuids=[123])

        with pytest.raises(BluetoothUnavailableError, match="service data"):
            await _manager_for_bus(bus).async_forget_current_bond(
                ADDRESS,
                adapter="hci0",
                adapter_address=ADAPTER_ADDRESS,
                user_confirmed=True,
            )

        assert bus.removed is False
        assert "call:RemoveDevice" not in bus.actions

    asyncio.run(scenario())


def test_confirmed_forget_never_removes_same_radio_from_another_adapter() -> None:
    class WrongAdapterBus(_FakeBlueZBus):
        def _objects(self) -> dict[str, Any]:
            return {
                ADAPTER_PATH: {
                    BLUEZ_ADAPTER: {
                        "Address": _Variant(ADAPTER_ADDRESS),
                        "Powered": _Variant(False),
                    }
                },
                "/org/bluez/hci1": {
                    BLUEZ_ADAPTER: {
                        "Address": _Variant("00:11:22:33:44:66"),
                        "Powered": _Variant(True),
                    }
                },
                "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF": {
                    BLUEZ_DEVICE: {
                        "Address": _Variant(ADDRESS),
                        "Paired": _Variant(True),
                        "UUIDs": _Variant([MESHTASTIC_SERVICE_UUID]),
                    }
                },
            }

    async def scenario() -> None:
        bus = WrongAdapterBus(paired=True)

        await _manager_for_bus(bus).async_forget_current_bond(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            user_confirmed=True,
        )

        assert bus.removed is False
        assert "call:RemoveDevice" not in bus.actions

    asyncio.run(scenario())


def test_confirmed_forget_follows_stable_adapter_after_hci_renumber() -> None:
    class RenumberedAdapterBus(_FakeBlueZBus):
        def _objects(self) -> dict[str, Any]:
            objects: dict[str, Any] = {
                "/org/bluez/hci7": {
                    BLUEZ_ADAPTER: {
                        "Address": _Variant(ADAPTER_ADDRESS),
                        "Powered": _Variant(True),
                    }
                }
            }
            if not self.removed:
                objects["/org/bluez/hci7/dev_AA_BB_CC_DD_EE_FF"] = {
                    BLUEZ_DEVICE: {
                        "Address": _Variant(ADDRESS),
                        "Paired": _Variant(True),
                        "UUIDs": _Variant([MESHTASTIC_SERVICE_UUID]),
                    }
                }
            return objects

    async def scenario() -> None:
        bus = RenumberedAdapterBus(paired=True)

        await _manager_for_bus(bus).async_forget_current_bond(
            ADDRESS,
            adapter="hci0",
            adapter_address=ADAPTER_ADDRESS,
            user_confirmed=True,
        )

        assert bus.removed is True
        assert "call:RemoveDevice" in bus.actions

    asyncio.run(scenario())


def test_module_lock_serializes_pairing_across_manager_instances() -> None:
    class Tracker:
        active = 0
        maximum = 0

    class SlowBackend:
        async def async_pair(
            self, address, request_pin, cancel_prompt, check_candidate
        ):
            del request_pin, cancel_prompt
            check_candidate(
                ProvisionalBond(address, "hci0", ADAPTER_ADDRESS)
            )
            Tracker.active += 1
            Tracker.maximum = max(Tracker.maximum, Tracker.active)
            await asyncio.sleep(0.01)
            Tracker.active -= 1
            return PairingResult(address, "hci0", ADAPTER_ADDRESS, False)

        async def async_forget(
            self, address, adapter, adapter_address, *, allow_unverified_service=False
        ):
            del address, adapter, adapter_address, allow_unverified_service

    async def scenario() -> None:
        first = BluetoothPairingManager(SlowBackend)
        second = BluetoothPairingManager(SlowBackend)

        attempts = await asyncio.gather(
            first.async_begin("AA:BB:CC:DD:EE:01"),
            second.async_begin("AA:BB:CC:DD:EE:02"),
        )

        assert all(attempt.state == "completed" for attempt in attempts)
        assert Tracker.maximum == 1

    asyncio.run(scenario())


def test_pairing_attempts_are_rate_limited_per_address_and_window() -> None:
    class ImmediateBackend:
        async def async_pair(
            self, address, request_pin, cancel_prompt, check_candidate
        ):
            del request_pin, cancel_prompt
            check_candidate(
                ProvisionalBond(address, "hci0", ADAPTER_ADDRESS)
            )
            return PairingResult(address, "hci0", ADAPTER_ADDRESS, False)

        async def async_forget(
            self, address, adapter, adapter_address, *, allow_unverified_service=False
        ):
            del address, adapter, adapter_address, allow_unverified_service

    async def scenario() -> None:
        now = [1000.0]
        manager = BluetoothPairingManager(
            ImmediateBackend, monotonic=lambda: now[0]
        )

        for _ in range(3):
            assert (await manager.async_begin(ADDRESS)).state == "completed"
        with pytest.raises(PairingRateLimitedError):
            await manager.async_begin(ADDRESS)

        now[0] += 601
        assert (await manager.async_begin(ADDRESS)).state == "completed"

    asyncio.run(scenario())


def test_submit_pin_requires_an_active_agent_prompt() -> None:
    class ImmediateBackend:
        async def async_pair(
            self, address, request_pin, cancel_prompt, check_candidate
        ):
            del request_pin, cancel_prompt
            check_candidate(
                ProvisionalBond(address, "hci0", ADAPTER_ADDRESS)
            )
            return PairingResult(address, "hci0", ADAPTER_ADDRESS, False)

        async def async_forget(
            self, address, adapter, adapter_address, *, allow_unverified_service=False
        ):
            del address, adapter, adapter_address, allow_unverified_service

    async def scenario() -> None:
        attempt = await BluetoothPairingManager(ImmediateBackend).async_begin(ADDRESS)
        with pytest.raises(PairingStateError):
            await attempt.async_submit_pin("123456")

    asyncio.run(scenario())
