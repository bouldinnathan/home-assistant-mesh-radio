from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.meshnet.const import (
    CONF_GATEWAYS,
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_TCP,
)
from custom_components.meshnet.models import (
    GatewayConfig,
    GatewayStatus,
    MeshPacket,
    MeshSnapshot,
    MessageRecord,
    NodeState,
)
from custom_components.meshnet.node_identity import (
    meshtastic_observation_node_key,
)
from custom_components.meshnet.panel_telemetry import PanelTelemetry


def _load_coordinator_without_home_assistant(monkeypatch):
    """Load the coordinator with minimal HA shims in the lightweight test env."""
    try:
        coordinator_class = importlib.import_module(
            "custom_components.meshnet.coordinator"
        ).MeshNetCoordinator

        async def async_shutdown(self) -> None:
            self._super_shutdown_called = True

        monkeypatch.setattr(
            coordinator_class.__mro__[1], "async_shutdown", async_shutdown
        )
        return coordinator_class
    except ModuleNotFoundError as err:
        if err.name != "homeassistant":
            raise

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")
    exceptions = types.ModuleType("homeassistant.exceptions")
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        async def async_shutdown(self) -> None:
            self._super_shutdown_called = True

    class HomeAssistantError(Exception):
        pass

    config_entries.ConfigEntry = object
    core.HomeAssistant = object
    exceptions.HomeAssistantError = HomeAssistantError
    issue_registry.IssueSeverity = SimpleNamespace(ERROR="error", WARNING="warning")
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = RuntimeError
    helpers.issue_registry = issue_registry

    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.issue_registry": issue_registry,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    module = importlib.import_module("custom_components.meshnet.coordinator")
    sys.modules.pop(module.__name__, None)
    return module.MeshNetCoordinator


def test_coordinator_first_refresh_does_not_start_radio_transports(
    monkeypatch,
) -> None:
    """Keep blocking radio SDK constructors out of config-entry setup."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

        class Store:
            async def async_open(self) -> None:
                return None

            async def async_load_snapshot(self, *, recent_limit: int):
                assert recent_limit == 100
                return MeshSnapshot()

        coordinator = object.__new__(coordinator_class)
        coordinator.store = Store()
        coordinator.snapshot = MeshSnapshot()
        coordinator._rebuild_gateways = AsyncMock()
        coordinator._start_gateways = AsyncMock(
            side_effect=AssertionError("radio SDK started during first refresh")
        )

        await asyncio.wait_for(coordinator._async_setup(), timeout=0.1)

        coordinator._rebuild_gateways.assert_awaited_once_with()
        coordinator._start_gateways.assert_not_awaited()

    asyncio.run(run())


def test_cached_nodes_remain_cached_only_until_live_callback(
    monkeypatch,
) -> None:
    """Loading SQLite state must not masquerade as a current-session sighting."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        cached_node = NodeState(
            node_key="private-cached-key",
            protocol=PROTOCOL_MESHTASTIC,
        )

        class Store:
            async def async_open(self) -> None:
                return None

            async def async_load_snapshot(self, *, recent_limit: int):
                assert recent_limit == 100
                return MeshSnapshot(nodes={cached_node.node_key: cached_node})

            async def async_upsert_node(self, node: NodeState) -> None:
                assert node.node_key == "private-live-key"

        coordinator = object.__new__(coordinator_class)
        coordinator.store = Store()
        coordinator.snapshot = MeshSnapshot()
        coordinator._session_observed_node_keys = set()
        coordinator._rebuild_gateways = AsyncMock()
        coordinator._gateway_generation = 1
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator.async_set_updated_data = lambda _snapshot: None

        await coordinator._async_setup()

        assert coordinator.node_observed_this_session(cached_node.node_key) is False
        assert coordinator.node_observability_diagnostics()["node_counts"] == {
            "total": 1,
            "observed_this_session": 0,
            "cached_only": 1,
            "online": 0,
            "offline": 1,
            "located": 0,
            "located_offline": 0,
            "stored_total": 1,
            "analyzed": 1,
            "analysis_omitted": 0,
            "analysis_truncated": False,
        }

        live_node = NodeState(
            node_key="private-live-key",
            protocol=PROTOCOL_MESHTASTIC,
            online=True,
        )
        await coordinator._handle_node(live_node, gateway_generation=1)

        assert coordinator.node_observed_this_session(cached_node.node_key) is False
        assert coordinator.node_observed_this_session(live_node.node_key) is True
        counts = coordinator.node_observability_diagnostics()["node_counts"]
        assert counts["total"] == 2
        assert counts["observed_this_session"] == 1
        assert counts["cached_only"] == 1

    asyncio.run(run())


def test_cached_meshtastic_aliases_publish_once_without_deleting_raw_records(
    monkeypatch,
) -> None:
    """Exact non-conflicting IDs become one effective node, reversibly."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        raw_nodes = {
            "mac:aabbccddeeff": NodeState(
                node_key="mac:aabbccddeeff",
                protocol=PROTOCOL_MESHTASTIC,
                node_id="!12345678",
                mac="aabbccddeeff",
                long_name="Hill Repeater",
            ),
            "meshtastic:305419896": NodeState(
                node_key="meshtastic:305419896",
                protocol=PROTOCOL_MESHTASTIC,
                node_id="305419896",
            ),
            "meshtastic:!12345678": NodeState(
                node_key="meshtastic:!12345678",
                protocol=PROTOCOL_MESHTASTIC,
                node_id="0x12345678",
            ),
        }

        class Store:
            async def async_open(self) -> None:
                return None

            async def async_load_snapshot(self, *, recent_limit: int):
                assert recent_limit == 100
                return MeshSnapshot(nodes=raw_nodes)

        coordinator = object.__new__(coordinator_class)
        coordinator.store = Store()
        coordinator.snapshot = MeshSnapshot()
        coordinator._session_observed_node_keys = set()
        coordinator._rebuild_gateways = AsyncMock()

        await coordinator._async_setup()

        assert set(coordinator._raw_nodes) == set(raw_nodes)
        assert set(coordinator.snapshot.nodes) == {
            "meshtastic:!12345678"
        }
        effective = coordinator.snapshot.nodes["meshtastic:!12345678"]
        assert effective.long_name == "Hill Repeater"
        assert effective.mac == "aabbccddeeff"
        assert coordinator.node_alias_keys(effective.node_key) == (
            "mac:aabbccddeeff",
            "meshtastic:!12345678",
            "meshtastic:305419896",
        )
        assert coordinator.node_observability_diagnostics()[
            "identity_projection"
        ] == {
            "raw_record_count": 3,
            "effective_node_count": 1,
            "collapsed_alias_record_count": 2,
            "candidate_identity_group_count": 1,
            "resolved_identity_group_count": 1,
            "unresolved_identity_group_count": 0,
            "unresolved_identity_record_count": 0,
            "invalid_identity_record_count": 0,
        }

    asyncio.run(run())


def test_retained_alias_resolves_to_canonical_meshtastic_send_target(
    monkeypatch,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(
        nodes={
            "meshtastic:!12345678": NodeState(
                node_key="meshtastic:!12345678",
                protocol=PROTOCOL_MESHTASTIC,
                node_id="!12345678",
                mac="aabbccddeeff",
                long_name="Exact Long Name",
                short_name="ELN",
            )
        }
    )
    coordinator._node_alias_redirects = {
        "mac:aabbccddeeff": "meshtastic:!12345678",
        "meshtastic:!12345678": "meshtastic:!12345678",
    }

    assert (
        coordinator._provider_message_target("mac:aabbccddeeff")
        == "!12345678"
    )
    assert (
        coordinator._provider_message_target("meshtastic:!12345678")
        == "!12345678"
    )
    assert (
        coordinator._provider_message_target(" exact long name ")
        == "!12345678"
    )
    assert coordinator._provider_message_target("eln") == "!12345678"
    assert (
        coordinator._provider_message_target("MAC:AA:BB:CC:DD:EE:FF")
        == "!12345678"
    )


def test_unresolved_meshtastic_id_collision_cannot_be_direct_messaged(
    monkeypatch,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(
        nodes={
            "mac:111111111111": NodeState(
                node_key="mac:111111111111",
                protocol=PROTOCOL_MESHTASTIC,
                node_id="!12345678",
                mac="111111111111",
                long_name="Alpha relay",
                short_name="AR",
            ),
            "mac:222222222222": NodeState(
                node_key="mac:222222222222",
                protocol=PROTOCOL_MESHTASTIC,
                node_id="305419896",
                mac="222222222222",
                long_name="Beta relay",
                short_name="BR",
            ),
        }
    )
    coordinator._node_alias_redirects = {
        key: key for key in coordinator.snapshot.nodes
    }
    error_class = coordinator_class._provider_message_target.__globals__[
        "HomeAssistantError"
    ]

    for target in (
        "mac:111111111111",
        "mac:222222222222",
        "!12345678",
        "meshtastic:!12345678",
        "meshtastic:0x12345678",
        "meshtastic:305419896",
        "0x12345678",
        "305419896",
        "MAC:11:11:11:11:11:11",
        "mac:11-11-11-11-11-11",
        " alpha relay ",
        "BR",
    ):
        with pytest.raises(error_class, match="identity is ambiguous"):
            coordinator._provider_message_target(target)

    assert coordinator._provider_message_target(None) is None


def test_duplicate_meshtastic_name_cannot_be_direct_messaged(
    monkeypatch,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    nodes = {
        "meshtastic:!11111111": NodeState(
            node_key="meshtastic:!11111111",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!11111111",
            long_name="Shared Relay",
            short_name="Relay:West",
        ),
        "meshtastic:!22222222": NodeState(
            node_key="meshtastic:!22222222",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!22222222",
            short_name="shared relay",
            long_name="relay:west",
        ),
    }
    coordinator.snapshot = MeshSnapshot(nodes=nodes)
    coordinator._node_alias_redirects = {key: key for key in nodes}
    error_class = coordinator_class._provider_message_target.__globals__[
        "HomeAssistantError"
    ]

    with pytest.raises(error_class, match="identity is ambiguous"):
        coordinator._provider_message_target("  SHARED RELAY ")
    with pytest.raises(error_class, match="identity is ambiguous"):
        coordinator._provider_message_target("RELAY:WEST")


@pytest.mark.parametrize("shared_proof", ["mac", "public_key"])
def test_cross_id_shared_proof_blocks_every_direct_alias(
    monkeypatch,
    shared_proof: str,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    shared_mac = "111111111111" if shared_proof == "mac" else None
    shared_public = "1" * 64 if shared_proof == "public_key" else None
    left = NodeState(
        node_key=meshtastic_observation_node_key(
            "!11111111", shared_mac, shared_public
        ),
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!11111111",
        mac=shared_mac,
        public_key=shared_public,
    )
    right = NodeState(
        node_key=meshtastic_observation_node_key(
            "!22222222", shared_mac, shared_public
        ),
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!22222222",
        mac=shared_mac,
        public_key=shared_public,
    )
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(
        nodes={left.node_key: left, right.node_key: right}
    )
    coordinator._node_alias_redirects = {
        left.node_key: left.node_key,
        right.node_key: right.node_key,
    }
    error_class = coordinator_class._provider_message_target.__globals__[
        "HomeAssistantError"
    ]
    targets = [left.node_key, right.node_key, left.node_id, right.node_id]
    if shared_mac is not None:
        targets.append(f"MAC:{':'.join(['11'] * 6)}")
    if shared_public is not None:
        targets.append(shared_public)

    for target in targets:
        with pytest.raises(error_class, match="identity is ambiguous"):
            coordinator._provider_message_target(target)


def test_invalid_meshtastic_identity_cannot_be_direct_messaged(
    monkeypatch,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    invalid = NodeState(
        node_key=f"meshtastic-proof:{'1' * 64}",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!12345678",
        mac="111111111111",
        public_key="2" * 64,
    )
    coordinator.snapshot = MeshSnapshot(nodes={invalid.node_key: invalid})
    coordinator._node_alias_redirects = {invalid.node_key: invalid.node_key}
    error_class = coordinator_class._provider_message_target.__globals__[
        "HomeAssistantError"
    ]

    for target in (
        invalid.node_key,
        "!12345678",
        "meshtastic:0x12345678",
        "305419896",
    ):
        with pytest.raises(
            error_class, match="malformed or internally inconsistent"
        ):
            coordinator._provider_message_target(target)

    missing_id = NodeState(
        node_key="mac:111111111111",
        protocol=PROTOCOL_MESHTASTIC,
        node_id=None,
        mac="111111111111",
    )
    coordinator.snapshot = MeshSnapshot(
        nodes={missing_id.node_key: missing_id}
    )
    coordinator._node_alias_redirects = {
        missing_id.node_key: missing_id.node_key
    }
    with pytest.raises(
        error_class, match="malformed or internally inconsistent"
    ):
        coordinator._provider_message_target(missing_id.node_key)


def test_identity_grammars_cannot_fall_through_to_wrong_recipient_names(
    monkeypatch,
) -> None:
    """ID-, MAC-, public-key-, and reserved-looking names fail closed."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    public_key = "a1" * 32
    proof_name = f"meshtastic-proof:{'b2' * 32}"
    node = NodeState(
        node_key="meshtastic:!11111111",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!11111111",
        user_name=proof_name,
        long_name="305419896",
        short_name="Relay:West",
    )
    mac_name = NodeState(
        node_key="meshtastic:!22222222",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!22222222",
        long_name="aa:bb:cc:dd:ee:ff",
        short_name=public_key,
    )
    pub_name = NodeState(
        node_key="meshtastic:!33333333",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!33333333",
        long_name=f"pub:{public_key}",
    )
    malformed_names = NodeState(
        node_key="meshtastic:!44444444",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!44444444",
        short_name="!12345g78",
        long_name="0xzzzz",
    )
    malformed_mac_names = NodeState(
        node_key="meshtastic:!55555555",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!55555555",
        short_name="aa:bb:cc:dd:ee:gg",
        long_name="aabbccddeezz",
    )
    malformed_number_and_key_names = NodeState(
        node_key="meshtastic:!66666666",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!66666666",
        short_name="12345678901234567890",
        long_name=f"{'ab' * 31}ag",
    )
    compatibility_identity_names = NodeState(
        node_key="meshtastic:!77777777",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!77777777",
        short_name="！12345g78",
        long_name="０xzzzz",
    )
    compatibility_prefix_names = NodeState(
        node_key="meshtastic:!88888888",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!88888888",
        short_name="ｍａｃ:AA:BB:CC:DD:EE:FF",
        long_name=f"ｐｕｂ:{public_key}",
    )
    compatibility_protocol_name = NodeState(
        node_key="meshtastic:!99999999",
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!99999999",
        long_name="ｍｅｓｈｔａｓｔｉｃ:!12345678",
    )
    nodes = {
        candidate.node_key: candidate
        for candidate in (
            node,
            mac_name,
            pub_name,
            malformed_names,
            malformed_mac_names,
            malformed_number_and_key_names,
            compatibility_identity_names,
            compatibility_prefix_names,
            compatibility_protocol_name,
        )
    }
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(nodes=nodes)
    coordinator._node_alias_redirects = {key: key for key in nodes}
    error_class = coordinator_class._provider_message_target.__globals__[
        "HomeAssistantError"
    ]

    # The decimal grammar identifies !12345678 even though a different node
    # advertises that exact text as its long name.
    assert coordinator._provider_message_target("305419896") == "!12345678"
    for identity_shaped_name in (
        "aa:bb:cc:dd:ee:ff",
        public_key,
        f"pub:{public_key}",
        proof_name,
        "!12345g78",
        "0xzzzz",
        "aa:bb:cc:dd:ee:gg",
        "aabbccddeezz",
        "12345678901234567890",
        f"{'ab' * 31}ag",
        "！12345g78",
        "０xzzzz",
        "ｍａｃ:AA:BB:CC:DD:EE:FF",
        f"ｐｕｂ:{public_key}",
    ):
        with pytest.raises(
            error_class, match="malformed or internally inconsistent"
        ):
            coordinator._provider_message_target(identity_shaped_name)

    # A compatibility-spelled reserved protocol prefix is still identity
    # syntax and must never select the node advertising that same-looking name.
    assert (
        coordinator._provider_message_target(
            "ｍｅｓｈｔａｓｔｉｃ:!12345678"
        )
        == "!12345678"
    )

    # A colon remains legal in a genuine name when its prefix is not reserved.
    assert coordinator._provider_message_target("relay:west") == "!11111111"


def test_strong_identity_inputs_resolve_safely_with_legacy_protocol_case(
    monkeypatch,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    public_key = "ab" * 32
    mac = "aabbccddeeff"
    node = NodeState(
        node_key=meshtastic_observation_node_key(
            "!12345678", mac, public_key
        ),
        protocol="Meshtastic",
        node_id="!12345678",
        mac=mac,
        public_key=public_key,
    )
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(nodes={node.node_key: node})
    coordinator._node_alias_redirects = {node.node_key: node.node_key}

    for target in (
        node.node_key.upper(),
        "MAC:AA:BB:CC:DD:EE:FF",
        "AA-BB-CC-DD-EE-FF",
        f"pub:{public_key.upper()}",
        public_key.upper(),
    ):
        assert coordinator._provider_message_target(target) == "!12345678"


def test_public_identity_shared_across_protocols_is_ambiguous(monkeypatch) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    public_key = "cd" * 32
    meshtastic = NodeState(
        node_key=meshtastic_observation_node_key(
            "!12345678", None, public_key
        ),
        protocol=PROTOCOL_MESHTASTIC,
        node_id="!12345678",
        public_key=public_key,
    )
    meshcore = NodeState(
        node_key=f"pub:{public_key}",
        protocol=PROTOCOL_MESHCORE,
        public_key=public_key,
    )
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(
        nodes={
            meshtastic.node_key: meshtastic,
            meshcore.node_key: meshcore,
        }
    )
    coordinator._node_alias_redirects = {
        key: key for key in coordinator.snapshot.nodes
    }
    error_class = coordinator_class._provider_message_target.__globals__[
        "HomeAssistantError"
    ]

    for target in (public_key, f"pub:{public_key}"):
        with pytest.raises(error_class, match="identity is ambiguous"):
            coordinator._provider_message_target(target)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_node": None},
        {"target_node": "!12345678", "message_type": "broadcast"},
        {"target_node": "!12345678", "message_type": "group"},
        {"target_node": "!12345678", "message_type": "emergency"},
        {"message_type": "DIRECT"},
        {"message_type": "unsupported"},
        {"target_node": "   "},
        {"target_node": "x" * 129},
        {"message": 123},
        {"message": "   "},
        {"message": "x" * 238},
        {"message": "😀" * 60},
        {"message": "\ud800"},
        {"channel": True},
        {"channel": 1.5},
        {"channel": " 1"},
        {"channel": "00"},
        {"channel": "9" * 5000},
        {"channel": "8"},
        {"priority": "Normal"},
        {"priority": "urgent"},
        {"priority": []},
        {"gateway_id": ""},
        {"gateway_id": " gateway"},
        {"gateway_id": "g" * 129},
        {"gateway_id": []},
    ],
)
def test_message_envelope_rejects_unsafe_fields_before_routing(
    monkeypatch,
    overrides: dict,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    validator = coordinator_class._async_send_message.__globals__[
        "validated_message_envelope"
    ]
    error_class = coordinator_class._async_send_message.__globals__[
        "HomeAssistantError"
    ]
    fields = {
        "target_node": "!12345678",
        "message": "safe",
        "channel": None,
        "priority": "normal",
        "message_type": "direct",
        "gateway_id": None,
    }
    fields.update(overrides)

    with pytest.raises(error_class):
        validator(**fields)


def test_message_envelope_normalizes_valid_channel_and_utf8_boundary(
    monkeypatch,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    validator = coordinator_class._async_send_message.__globals__[
        "validated_message_envelope"
    ]

    envelope = validator(
        target_node=" !12345678 ",
        message="😀" * 59,
        channel=7.0,
        priority="high",
        message_type="direct",
        gateway_id="gateway-1",
    )

    assert envelope.target_node == "!12345678"
    assert envelope.channel == "7"
    assert envelope.message == "😀" * 59
    assert envelope.gateway_id == "gateway-1"


def test_message_api_projection_ignores_unhashable_provider_metadata(
    monkeypatch,
) -> None:
    """Malformed provider raw values cannot break event serialization."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    project = coordinator_class._handle_packet.__globals__["message_api_dict"]
    message = MessageRecord(
        message_id="malformed-provider-raw",
        protocol=PROTOCOL_MESHTASTIC,
        gateway_id="gateway-1",
        sender="radio",
        receiver=None,
        channel=None,
        text="safe content",
        reply_to_message_id=["meshtastic:1"],  # type: ignore[arg-type]
        reaction="<script>",
        raw={"status": [], "last_error_code": {}},
    )

    projected = project(message)
    assert projected["raw"] == {}
    assert "reply_to_message_id" not in projected
    assert "reaction" not in projected

    outbound = MessageRecord(
        message_id="home-assistant-submission",
        protocol=PROTOCOL_MESHTASTIC,
        gateway_id="gateway-1",
        sender="homeassistant",
        receiver="!ffffffff",
        channel="0",
        text="react to this",
        direction="tx",
        raw={"status": "sent", "provider_id": "123456789"},
    )
    assert project(outbound)["mesh_packet_id"] == "meshtastic:123456789"


def test_unknown_gateway_is_rejected_before_message_persistence(monkeypatch) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot()
        coordinator.gateways = {}
        coordinator.store = SimpleNamespace(async_add_message=AsyncMock())
        error_class = coordinator_class._async_send_message.__globals__[
            "HomeAssistantError"
        ]

        with pytest.raises(error_class, match="Unknown MeshNet gateway"):
            await coordinator._async_send_message(
                target_node=None,
                message="safe",
                channel=None,
                priority="normal",
                message_type="broadcast",
                gateway_id="missing-gateway",
            )

        coordinator.store.async_add_message.assert_not_awaited()

    asyncio.run(run())


def test_node_observability_is_aggregate_only_and_privacy_safe(monkeypatch) -> None:
    """Provenance diagnostics expose counts, never compared identity aliases."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    private_alias = "AA:BB:CC:DD:EE:FF"
    nodes = {
        f"private-node-key-{index}": NodeState(
            node_key=f"private-node-key-{index}",
            protocol=PROTOCOL_MESHTASTIC,
            node_id=f"private-node-id-{index}",
            mac=private_alias if index in {0, 1} else None,
            public_key=f"private-public-key-{index}",
            online=index in {0, 2},
            last_heard=(
                None
                if index == 6
                else now
                - (
                    timedelta(minutes=5),
                    timedelta(minutes=30),
                    timedelta(hours=2),
                    timedelta(hours=12),
                    timedelta(days=2),
                    timedelta(minutes=10),
                )[index]
            ),
            connectivity={
                "via_mqtt": (True, False, None, True, False, None, None)[index],
                "hops": (0, 1, 2, 3, 5, 8, None)[index],
            },
            location=(
                {"latitude": 1.0, "longitude": 1.0}
                if index in {0, 1, 4}
                else {}
            ),
        )
        for index in range(7)
    }
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(nodes=nodes)
    coordinator._session_observed_node_keys = {
        "private-node-key-0",
        "private-node-key-2",
    }

    result = coordinator.node_observability_diagnostics(now=now)

    assert result["node_counts"] == {
        "total": 7,
        "observed_this_session": 2,
        "cached_only": 5,
        "online": 2,
        "offline": 5,
        "located": 3,
        "located_offline": 2,
        "stored_total": 7,
        "analyzed": 7,
        "analysis_omitted": 0,
        "analysis_truncated": False,
    }
    assert result["via_mqtt_counts"] == {
        "true": 2,
        "false": 2,
        "unknown": 3,
    }
    assert result["hop_counts"] == {
        "0": 1,
        "1": 1,
        "2": 1,
        "3": 1,
        "4-7": 1,
        ">=8": 1,
        "unknown": 1,
    }
    assert result["age_counts"] == {
        "<15m": 2,
        "15m-1h": 1,
        "1-6h": 1,
        "6-24h": 1,
        ">=1d": 1,
        "unknown": 1,
    }
    assert result["identity_alias_collisions"] == {
        "group_count": 1,
        "node_count": 2,
        "shared_alias_count": 1,
    }
    assert result["location_provenance"] == {
        "independent_timestamp_available": False,
        "source_available": False,
        "freshness_basis": "node_last_heard_proxy",
        "cached_location_may_be_older": True,
        "warning": (
            "MeshNet does not currently retain an independent location "
            "observation timestamp or source; node last-heard age is only a "
            "freshness proxy and a cached location may be older."
        ),
    }
    assert coordinator.panel_node_provenance() == {
        "total_node_count": 7,
        "retained_node_record_count": 7,
        "collapsed_alias_record_count": 0,
        "resolved_identity_group_count": 0,
        "unresolved_identity_group_count": 1,
        "unresolved_identity_node_count": 2,
        "invalid_identity_record_count": 0,
        "analyzed_node_count": 7,
        "omitted_node_count": 0,
        "current_session_node_count": 2,
        "cached_only_node_count": 5,
        "online_node_count": 2,
        "located_node_count": 3,
        "located_offline_node_count": 2,
        "mqtt_node_count": 2,
        "mqtt_unknown_node_count": 3,
        "identity_collision_group_count": 1,
        "identity_collision_node_count": 2,
    }
    serialized = repr(result)
    assert private_alias not in serialized
    for node in nodes.values():
        assert node.node_key not in serialized
        assert node.node_id not in serialized
        assert node.public_key not in serialized


def test_node_observability_caps_recurring_analysis_without_deleting_nodes(
    monkeypatch,
) -> None:
    """An oversized retained database must not create unbounded panel work."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    monkeypatch.setitem(
        coordinator_class.node_observability_diagnostics.__globals__,
        "MAX_PANEL_NODES",
        2,
    )
    nodes = {
        f"private-node-{index}": NodeState(
            node_key=f"private-node-{index}",
            protocol=PROTOCOL_MESHTASTIC,
            online=True,
        )
        for index in range(3)
    }
    coordinator = object.__new__(coordinator_class)
    coordinator.snapshot = MeshSnapshot(nodes=nodes)
    coordinator._session_observed_node_keys = set(nodes)

    counts = coordinator.node_observability_diagnostics()["node_counts"]

    assert counts == {
        "total": 2,
        "observed_this_session": 2,
        "cached_only": 0,
        "online": 2,
        "offline": 0,
        "located": 0,
        "located_offline": 0,
        "stored_total": 3,
        "analyzed": 2,
        "analysis_omitted": 1,
        "analysis_truncated": True,
    }
    assert coordinator.panel_node_provenance()["total_node_count"] == 3
    assert coordinator.panel_node_provenance()["omitted_node_count"] == 1
    assert len(coordinator.snapshot.nodes) == 3


def test_start_gateways_is_noop_after_shutdown_begins(monkeypatch) -> None:
    """Late background callbacks must not start transports during unload."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = True
        coordinator.gateways = {"gateway-1": object()}
        coordinator._start_gateways = AsyncMock()
        coordinator._flush_outbox = AsyncMock()

        await coordinator.async_start_gateways()

        coordinator._start_gateways.assert_not_awaited()
        coordinator._flush_outbox.assert_not_awaited()

    asyncio.run(run())


def test_shutdown_cancels_queued_startup_before_stopping_gateway(
    monkeypatch,
) -> None:
    """A retained startup that has not run is canceled and drained first."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        order: list[str] = []

        async def queued_startup() -> None:
            order.append("startup-ran")

        startup_task = asyncio.create_task(queued_startup())
        coordinator._gateway_startup_task = startup_task

        class Gateway:
            async def async_stop(self) -> None:
                assert startup_task.done()
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()

        # Do not yield between task creation and shutdown: this represents an
        # entry-owned startup queued by setup while unload begins immediately.
        await coordinator.async_shutdown()

        assert startup_task.cancelled()
        assert coordinator._gateway_startup_task is None
        assert coordinator._super_shutdown_called is True
        assert order == ["stop", "close"]

    asyncio.run(run())


def test_shutdown_drains_active_startup_before_gateway_stop(monkeypatch) -> None:
    """Startup cancellation must finish before transport stop can run."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        order: list[str] = []
        startup_active = asyncio.Event()
        gateway_stopped = False

        async def active_startup() -> None:
            order.append("startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                order.append("startup-cancelled")
                # Cancellation cleanup is allowed to yield. Shutdown must
                # still drain it before it stops any gateway.
                await asyncio.sleep(0)
                assert gateway_stopped is False
                order.append("startup-drained")
                raise

        startup_task = asyncio.create_task(active_startup())
        coordinator._gateway_startup_task = startup_task

        class Gateway:
            async def async_stop(self) -> None:
                nonlocal gateway_stopped
                gateway_stopped = True
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()

        await startup_active.wait()
        await coordinator.async_shutdown()

        assert startup_task.cancelled()
        assert order == [
            "startup-active",
            "startup-cancelled",
            "startup-drained",
            "stop",
            "close",
        ]

    asyncio.run(run())


def test_stubborn_startup_cancel_is_bounded_and_retained(monkeypatch) -> None:
    """A provider that suppresses cancellation cannot hang the coordinator."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_gateway_startup_task.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._gateway_startup_task = None
        startup_active = asyncio.Event()
        cancellation_ignored = asyncio.Event()
        release_startup = asyncio.Event()

        async def stubborn_startup() -> None:
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_ignored.set()
                await release_startup.wait()

        coordinator.async_start_gateways = stubborn_startup
        coordinator._async_create_background_task = (
            lambda target, _name: asyncio.create_task(target)
        )
        coordinator.async_start_gateways_background()
        startup_task = coordinator._gateway_startup_task
        assert startup_task is not None

        await startup_active.wait()
        assert await coordinator._cancel_gateway_startup_task() is False

        assert cancellation_ignored.is_set()
        assert coordinator._gateway_startup_task is startup_task
        assert not startup_task.done()

        release_startup.set()
        await startup_task
        await asyncio.sleep(0)

        assert coordinator._gateway_startup_task is None

    asyncio.run(run())


def test_stubborn_reconnect_cancel_is_bounded_retained_and_not_repeated(
    monkeypatch,
) -> None:
    """A cancellation-suppressing reconnect stays owned without cancel spam."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_reconnect_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._reconnect_tasks = {}
        reconnect_active = asyncio.Event()
        cancellation_ignored = asyncio.Event()
        release_reconnect = asyncio.Event()

        async def stubborn_reconnect() -> None:
            reconnect_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_ignored.set()
                await release_reconnect.wait()

        reconnect_task = asyncio.create_task(stubborn_reconnect())
        coordinator._reconnect_tasks["gateway-1"] = reconnect_task

        def clear_reconnect(done_task: asyncio.Task[None]) -> None:
            if coordinator._reconnect_tasks.get("gateway-1") is done_task:
                coordinator._reconnect_tasks.pop("gateway-1", None)

        reconnect_task.add_done_callback(clear_reconnect)

        await reconnect_active.wait()
        assert await coordinator._cancel_reconnect_tasks() is False
        first_cancel_count = reconnect_task.cancelling()

        assert cancellation_ignored.is_set()
        assert first_cancel_count == 1
        assert coordinator._reconnect_tasks == {"gateway-1": reconnect_task}
        assert await coordinator._cancel_reconnect_tasks() is False
        assert reconnect_task.cancelling() == first_cancel_count

        release_reconnect.set()
        await reconnect_task
        await asyncio.sleep(0)

        assert coordinator._reconnect_tasks == {}

    asyncio.run(run())


def test_shutdown_continues_when_startup_ignores_cancellation(monkeypatch) -> None:
    """A stuck startup cannot hold unload open or flush after transport stop."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_gateway_startup_task.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        coordinator._gateway_startup_task = None
        order: list[str] = []
        startup_active = asyncio.Event()
        release_startup = asyncio.Event()

        async def stubborn_gateway_start() -> None:
            order.append("startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_startup.wait()
            order.append("late-start-finished")

        coordinator._start_gateways = stubborn_gateway_start
        coordinator._flush_outbox = AsyncMock()
        coordinator._async_create_background_task = (
            lambda target, _name: asyncio.create_task(target)
        )

        class Gateway:
            config = SimpleNamespace(gateway_id="gateway-1")

            async def async_stop(self) -> None:
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()
        coordinator.async_start_gateways_background()
        startup_task = coordinator._gateway_startup_task
        assert startup_task is not None

        await startup_active.wait()
        await asyncio.wait_for(coordinator.async_shutdown(), timeout=0.1)

        assert coordinator._gateway_startup_task is startup_task
        assert order == ["startup-active", "stop", "close"]
        coordinator._flush_outbox.assert_not_awaited()

        release_startup.set()
        await startup_task
        await asyncio.sleep(0)

        assert order == [
            "startup-active",
            "stop",
            "close",
            "late-start-finished",
        ]
        assert coordinator._gateway_startup_task is None
        coordinator._flush_outbox.assert_not_awaited()

    asyncio.run(run())


def test_shutdown_continues_when_reconnect_ignores_cancellation(
    monkeypatch,
) -> None:
    """A stuck reconnect cannot hold gateway stop or store close open."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_reconnect_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_startup_task = None
        coordinator._reconnect_tasks = {}
        order: list[str] = []
        reconnect_active = asyncio.Event()
        release_reconnect = asyncio.Event()

        async def stubborn_reconnect() -> None:
            order.append("reconnect-active")
            reconnect_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_reconnect.wait()
            order.append("late-reconnect-finished")

        reconnect_task = asyncio.create_task(stubborn_reconnect())
        coordinator._reconnect_tasks["gateway-1"] = reconnect_task

        def clear_reconnect(done_task: asyncio.Task[None]) -> None:
            if coordinator._reconnect_tasks.get("gateway-1") is done_task:
                coordinator._reconnect_tasks.pop("gateway-1", None)

        reconnect_task.add_done_callback(clear_reconnect)

        class Gateway:
            config = SimpleNamespace(gateway_id="gateway-1")

            async def async_stop(self) -> None:
                order.append("stop")

        class Store:
            async def async_close(self) -> None:
                order.append("close")

        coordinator.gateways = {"gateway-1": Gateway()}
        coordinator.store = Store()

        await reconnect_active.wait()
        await asyncio.wait_for(coordinator.async_shutdown(), timeout=0.1)

        assert coordinator._reconnect_tasks == {"gateway-1": reconnect_task}
        assert order == ["reconnect-active", "stop", "close"]

        release_reconnect.set()
        await reconnect_task
        await asyncio.sleep(0)

        assert order == [
            "reconnect-active",
            "stop",
            "close",
            "late-reconnect-finished",
        ]
        assert coordinator._reconnect_tasks == {}

    asyncio.run(run())


def test_reload_cancels_old_startup_before_stop_and_rebuild(monkeypatch) -> None:
    """Reload cannot let an old startup race the replacement gateways."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        coordinator.entry = object()
        order: list[str] = []
        startup_active = asyncio.Event()

        async def old_startup() -> None:
            order.append("old-startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                order.append("old-startup-drained")
                raise

        startup_task = asyncio.create_task(old_startup())
        coordinator._gateway_startup_task = startup_task

        class Gateway:
            async def async_stop(self) -> None:
                assert startup_task.done()
                order.append("old-stop")

        coordinator.gateways = {"old-gateway": Gateway()}

        def load_gateway_configs(entry):
            assert entry is coordinator.entry
            order.append("load")
            return ["replacement-config"]

        async def rebuild_gateways() -> None:
            assert coordinator._gateway_configs == ["replacement-config"]
            order.append("rebuild")

        def start_gateways_background() -> None:
            assert coordinator._reconnect_suspended is False
            order.append("new-startup")

        coordinator._load_gateway_configs = load_gateway_configs
        coordinator._rebuild_gateways = rebuild_gateways
        coordinator.async_start_gateways_background = start_gateways_background

        await startup_active.wait()
        await coordinator.async_reload_gateways()

        assert startup_task.cancelled()
        assert coordinator._gateway_startup_task is None
        assert order == [
            "old-startup-active",
            "old-startup-drained",
            "old-stop",
            "load",
            "rebuild",
            "new-startup",
        ]

    asyncio.run(run())


def test_reload_defers_until_stubborn_startup_finishes(monkeypatch) -> None:
    """A timed-out old startup cannot overlap replacement gateway objects."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_gateway_startup_task.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_tasks = {}
        coordinator.entry = object()
        coordinator._gateway_startup_task = None
        order: list[str] = []
        startup_active = asyncio.Event()
        release_startup = asyncio.Event()

        async def stubborn_startup() -> None:
            order.append("old-startup-active")
            startup_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_startup.wait()
            order.append("old-startup-finished")

        coordinator.async_start_gateways = stubborn_startup
        coordinator._async_create_background_task = (
            lambda target, _name: asyncio.create_task(target)
        )

        class Gateway:
            async def async_stop(self) -> None:
                order.append("old-stop")

        coordinator.gateways = {"old-gateway": Gateway()}

        def load_gateway_configs(_entry):
            order.append("load")
            return ["replacement-config"]

        async def rebuild_gateways() -> None:
            order.append("rebuild")

        replacement_starts = 0

        def start_replacement() -> None:
            nonlocal replacement_starts
            replacement_starts += 1
            order.append("new-startup")

        coordinator._load_gateway_configs = load_gateway_configs
        coordinator._rebuild_gateways = rebuild_gateways
        coordinator.async_start_gateways_background()
        startup_task = coordinator._gateway_startup_task
        assert startup_task is not None
        coordinator.async_start_gateways_background = start_replacement

        await startup_active.wait()
        await asyncio.wait_for(coordinator.async_reload_gateways(), timeout=0.1)

        assert coordinator._reconnect_suspended is False
        assert coordinator._radio_operations_accepting is False
        assert coordinator._gateway_startup_task is startup_task
        assert replacement_starts == 0
        assert order == ["old-startup-active"]

        release_startup.set()
        await startup_task
        await asyncio.sleep(0)
        assert coordinator._gateway_startup_task is None

        await coordinator.async_reload_gateways()

        assert coordinator._reconnect_suspended is False
        assert coordinator._radio_operations_accepting is True
        assert replacement_starts == 1
        assert order == [
            "old-startup-active",
            "old-startup-finished",
            "old-stop",
            "load",
            "rebuild",
            "new-startup",
        ]

    asyncio.run(run())


def test_reload_defers_until_stubborn_reconnect_finishes(monkeypatch) -> None:
    """A timed-out reconnect cannot race stopped or replacement gateways."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_reconnect_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_startup_task = None
        coordinator._reconnect_tasks = {}
        coordinator.entry = object()
        order: list[str] = []
        reconnect_active = asyncio.Event()
        release_reconnect = asyncio.Event()

        async def stubborn_reconnect() -> None:
            order.append("old-reconnect-active")
            reconnect_active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_reconnect.wait()
            order.append("old-reconnect-finished")

        reconnect_task = asyncio.create_task(stubborn_reconnect())
        coordinator._reconnect_tasks["old-gateway"] = reconnect_task

        def clear_reconnect(done_task: asyncio.Task[None]) -> None:
            if coordinator._reconnect_tasks.get("old-gateway") is done_task:
                coordinator._reconnect_tasks.pop("old-gateway", None)

        reconnect_task.add_done_callback(clear_reconnect)

        class Gateway:
            async def async_stop(self) -> None:
                order.append("old-stop")

        coordinator.gateways = {"old-gateway": Gateway()}

        def load_gateway_configs(_entry):
            order.append("load")
            return ["replacement-config"]

        async def rebuild_gateways() -> None:
            order.append("rebuild")

        replacement_starts = 0

        def start_replacement() -> None:
            nonlocal replacement_starts
            replacement_starts += 1
            order.append("new-startup")

        coordinator._load_gateway_configs = load_gateway_configs
        coordinator._rebuild_gateways = rebuild_gateways
        coordinator.async_start_gateways_background = start_replacement

        await reconnect_active.wait()
        await asyncio.wait_for(coordinator.async_reload_gateways(), timeout=0.1)

        assert coordinator._reconnect_suspended is False
        assert coordinator._reconnect_tasks == {
            "old-gateway": reconnect_task
        }
        assert replacement_starts == 0
        assert order == ["old-reconnect-active"]

        release_reconnect.set()
        await reconnect_task
        await asyncio.sleep(0)
        assert coordinator._reconnect_tasks == {}

        await coordinator.async_reload_gateways()

        assert coordinator._reconnect_suspended is False
        assert replacement_starts == 1
        assert order == [
            "old-reconnect-active",
            "old-reconnect-finished",
            "old-stop",
            "load",
            "rebuild",
            "new-startup",
        ]

    asyncio.run(run())


def test_flush_outbox_ignores_reentrant_gateway_status_flush(monkeypatch) -> None:
    """A status emitted while sending must not reacquire the outbox lock."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        record = MessageRecord(
            message_id="queued-message",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-1",
            sender="homeassistant",
            receiver=None,
            channel=None,
            text="hello",
            direction="tx",
            raw={"status": "queued", "gateway_id": "gateway-1"},
        )

        class Store:
            async def async_pending_outbox(
                self,
                *,
                limit: int,
                after: tuple[str, str] | None = None,
            ) -> list[MessageRecord]:
                assert limit == 100
                assert after is None
                return [record] if record.raw["status"] == "queued" else []

            async def async_add_message(self, message: MessageRecord) -> None:
                assert message is record

            async def async_recent_messages(self, limit: int) -> list[MessageRecord]:
                assert limit == 100
                return [record]

        class Limiter:
            async def acquire(self) -> None:
                return None

        class Bus:
            def __init__(self) -> None:
                self.events: list[tuple[str, dict]] = []

            def async_fire(self, event: str, data: dict) -> None:
                self.events.append((event, data))

        coordinator = object.__new__(coordinator_class)
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._reconnect_tasks = {}
        coordinator._shutting_down = False
        coordinator.store = Store()
        coordinator.tx_limiter = Limiter()
        coordinator.snapshot = MeshSnapshot()
        coordinator.hass = SimpleNamespace(bus=Bus())
        coordinator.async_set_updated_data = lambda _snapshot: None

        class Gateway:
            def __init__(self) -> None:
                self.config = GatewayConfig(
                    gateway_id="gateway-1",
                    name="Gateway",
                    protocol=PROTOCOL_MESHTASTIC,
                    transport=TRANSPORT_TCP,
                )
                self.status = GatewayStatus(
                    gateway_id="gateway-1",
                    name="Gateway",
                    protocol=PROTOCOL_MESHTASTIC,
                    transport=TRANSPORT_TCP,
                    connected=True,
                )
                self.send_count = 0

            async def async_send_message(self, **_kwargs) -> str:
                self.send_count += 1
                await coordinator._handle_gateway_status(self.status)
                return "provider-message"

        gateway = Gateway()
        coordinator.gateways = {gateway.config.gateway_id: gateway}

        await asyncio.wait_for(coordinator._flush_outbox(), timeout=0.25)

        assert gateway.send_count == 1
        assert record.raw == {
            "status": "sent",
            "gateway_id": "gateway-1",
            "provider_id": "provider-message",
        }

    asyncio.run(run())


def test_meshtastic_target_never_falls_back_to_connected_meshcore(
    monkeypatch,
) -> None:
    """An offline target protocol queues instead of crossing providers."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )

        class Gateway:
            def __init__(
                self, gateway_id: str, protocol: str, connected: bool
            ) -> None:
                self.config = GatewayConfig(
                    gateway_id=gateway_id,
                    name=gateway_id,
                    protocol=protocol,
                    transport=TRANSPORT_TCP,
                )
                self.status = GatewayStatus(
                    gateway_id=gateway_id,
                    name=gateway_id,
                    protocol=protocol,
                    transport=TRANSPORT_TCP,
                    connected=connected,
                )
                self.async_send_message = AsyncMock()

        meshtastic = Gateway("meshtastic-offline", PROTOCOL_MESHTASTIC, False)
        meshcore = Gateway("meshcore-online", PROTOCOL_MESHCORE, True)
        node = NodeState(
            node_key="meshtastic:!12345678",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot(nodes={node.node_key: node})
        coordinator._node_alias_redirects = {node.node_key: node.node_key}
        coordinator.gateways = {
            meshtastic.config.gateway_id: meshtastic,
            meshcore.config.gateway_id: meshcore,
        }
        coordinator.store = SimpleNamespace(
            async_add_message=AsyncMock(),
            async_recent_messages=AsyncMock(return_value=[]),
        )
        coordinator.async_set_updated_data = Mock()

        await coordinator._async_send_message(
            target_node=node.node_key,
            message="queue locally",
            channel=None,
            priority="normal",
            message_type="direct",
            gateway_id=None,
        )

        meshcore.async_send_message.assert_not_awaited()
        queued = coordinator.store.async_add_message.await_args.args[0]
        assert queued.protocol == PROTOCOL_MESHTASTIC
        assert queued.gateway_id == "queued"
        assert {
            key: value
            for key, value in queued.raw.items()
            if key != "target_binding"
        } == {
            "status": "queued",
            "target_node": "!12345678",
            "gateway_id": None,
        }
        assert queued.raw["target_binding"].startswith("node:")

    asyncio.run(run())


def test_explicit_cross_protocol_gateway_is_rejected_before_persistence(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        gateway = SimpleNamespace(
            config=GatewayConfig(
                gateway_id="meshcore-online",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
            ),
            status=GatewayStatus(
                gateway_id="meshcore-online",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
                connected=True,
            ),
            async_send_message=AsyncMock(),
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot()
        coordinator._node_alias_redirects = {}
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.store = SimpleNamespace(async_add_message=AsyncMock())
        error_class = coordinator_class._async_send_message.__globals__[
            "HomeAssistantError"
        ]

        with pytest.raises(error_class, match="different mesh protocols"):
            await coordinator._async_send_message(
                target_node="!12345678",
                message="must not cross",
                channel=None,
                priority="normal",
                message_type="direct",
                gateway_id=gateway.config.gateway_id,
            )

        coordinator.store.async_add_message.assert_not_awaited()
        gateway.async_send_message.assert_not_awaited()

    asyncio.run(run())


def test_known_meshcore_public_key_routes_only_to_meshcore(monkeypatch) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        public_key = "ef" * 32
        node = NodeState(
            node_key=f"pub:{public_key}",
            protocol=PROTOCOL_MESHCORE,
            public_key=public_key,
        )
        gateway = SimpleNamespace(
            config=GatewayConfig(
                gateway_id="meshcore-online",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
            ),
            status=GatewayStatus(
                gateway_id="meshcore-online",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
                connected=True,
            ),
            async_send_message=AsyncMock(return_value="provider-id"),
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot(nodes={node.node_key: node})
        coordinator._node_alias_redirects = {node.node_key: node.node_key}
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.store = SimpleNamespace(
            async_add_message=AsyncMock(),
            async_recent_messages=AsyncMock(return_value=[]),
        )
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()
        await coordinator._async_send_message(
            target_node=node.node_key,
            message="meshcore only",
            channel=None,
            priority="normal",
            message_type="direct",
            gateway_id=None,
        )

        gateway.async_send_message.assert_awaited_once_with(
            target_node=node.node_key,
            message="meshcore only",
            channel=None,
            priority="normal",
            message_type="direct",
        )

    asyncio.run(run())


def test_ambiguous_outbox_record_does_not_starve_following_broadcast(
    monkeypatch,
) -> None:
    """An unsafe direct queue item is blocked once while later work continues."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        direct = MessageRecord(
            message_id="ambiguous-direct",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-1",
            sender="homeassistant",
            receiver="!12345678",
            channel=None,
            text="do not send",
            message_type="direct",
            direction="tx",
            raw={"status": "queued", "gateway_id": "gateway-1"},
        )
        broadcast = MessageRecord(
            message_id="safe-broadcast",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-1",
            sender="homeassistant",
            receiver=None,
            channel=None,
            text="safe",
            direction="tx",
            raw={"status": "queued", "gateway_id": "gateway-1"},
        )
        persisted: list[tuple[str, dict]] = []

        class Store:
            async def async_pending_outbox(
                self,
                *,
                limit: int,
                after: tuple[str, str] | None = None,
            ) -> list[MessageRecord]:
                assert limit == 100
                assert after is None
                return [direct, broadcast]

            async def async_add_message(
                self, message: MessageRecord
            ) -> None:
                persisted.append((message.message_id, dict(message.raw)))

            async def async_recent_messages(
                self, limit: int
            ) -> list[MessageRecord]:
                assert limit == 100
                return [direct, broadcast]

        class Limiter:
            calls = 0

            async def acquire(self) -> None:
                self.calls += 1

        class Gateway:
            config = GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            )
            status = GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            )

            def __init__(self) -> None:
                self.calls: list[dict] = []

            async def async_send_message(self, **kwargs) -> str:
                self.calls.append(kwargs)
                return "provider-broadcast"

        left = NodeState(
            node_key="mac:111111111111",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
            mac="111111111111",
        )
        right = NodeState(
            node_key="mac:222222222222",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="305419896",
            mac="222222222222",
        )
        gateway = Gateway()
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._active_send_message_ids = set()
        coordinator._node_alias_redirects = {
            left.node_key: left.node_key,
            right.node_key: right.node_key,
        }
        coordinator.snapshot = MeshSnapshot(
            nodes={left.node_key: left, right.node_key: right}
        )
        coordinator.store = Store()
        coordinator.tx_limiter = Limiter()
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()
        await coordinator._flush_outbox()

        assert direct.raw == {
            "status": "blocked",
            "gateway_id": "gateway-1",
            "last_error_code": "unsafe_identity",
        }
        assert gateway.calls == [
            {
                "target_node": None,
                "message": "safe",
                "channel": None,
                "priority": "normal",
                "message_type": "broadcast",
            }
        ]
        assert coordinator.tx_limiter.calls == 1
        assert persisted[0] == ("ambiguous-direct", dict(direct.raw))
        assert persisted[-1][0] == "safe-broadcast"
        assert broadcast.raw["status"] == "sent"
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.snapshot
        )

    asyncio.run(run())


def test_outbox_waits_for_matching_protocol_instead_of_crossing_callback(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        node = NodeState(
            node_key="meshtastic:!12345678",
            protocol="Meshtastic",
            node_id="!12345678",
        )
        record = MessageRecord(
            message_id="queued-direct",
            protocol="Meshtastic",
            gateway_id="queued",
            sender="homeassistant",
            receiver="!12345678",
            channel=None,
            text="wait for the right radio",
            message_type="direct",
            direction="tx",
            raw={"status": "queued", "gateway_id": None},
        )

        class Store:
            def __init__(self) -> None:
                self.saved: list[MessageRecord] = []

            async def async_pending_outbox(
                self,
                *,
                limit: int,
                after: tuple[str, str] | None = None,
            ) -> list[MessageRecord]:
                assert limit == 100
                assert after is None
                return [record] if record.raw["status"] == "queued" else []

            async def async_add_message(self, message: MessageRecord) -> None:
                self.saved.append(message)

            async def async_recent_messages(
                self, limit: int
            ) -> list[MessageRecord]:
                assert limit == 100
                return [record]

        class Gateway:
            def __init__(
                self, gateway_id: str, protocol: str, connected: bool
            ) -> None:
                self.config = GatewayConfig(
                    gateway_id=gateway_id,
                    name=gateway_id,
                    protocol=protocol,
                    transport=TRANSPORT_TCP,
                )
                self.status = GatewayStatus(
                    gateway_id=gateway_id,
                    name=gateway_id,
                    protocol=protocol,
                    transport=TRANSPORT_TCP,
                    connected=connected,
                )
                self.async_send_message = AsyncMock(
                    return_value=f"{gateway_id}-provider"
                )

        meshcore = Gateway("meshcore", PROTOCOL_MESHCORE, True)
        meshtastic = Gateway("meshtastic", "Meshtastic", False)
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot(nodes={node.node_key: node})
        coordinator._node_alias_redirects = {node.node_key: node.node_key}
        coordinator.gateways = {
            meshcore.config.gateway_id: meshcore,
            meshtastic.config.gateway_id: meshtastic,
        }
        coordinator.store = Store()
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()
        record.raw["target_binding"] = coordinator._resolve_message_target(
            node.node_key
        ).binding

        await coordinator._flush_outbox(gateway_id="meshcore")

        assert {
            key: value
            for key, value in record.raw.items()
            if key != "target_binding"
        } == {"status": "queued", "gateway_id": None}
        assert record.raw["target_binding"].startswith("node:")
        meshcore.async_send_message.assert_not_awaited()
        meshtastic.async_send_message.assert_not_awaited()

        meshtastic.status.connected = True
        await coordinator._flush_outbox(gateway_id="meshtastic")

        meshtastic.async_send_message.assert_awaited_once()
        meshcore.async_send_message.assert_not_awaited()
        assert record.raw["status"] == "sent"

    asyncio.run(run())


def test_outbox_honors_persisted_gateway_and_blocks_protocol_mismatch(
    monkeypatch,
) -> None:
    """A None raw gateway cannot erase the resolved record gateway."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        mismatched = MessageRecord(
            message_id="wrong-protocol",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="meshcore",
            sender="homeassistant",
            receiver="!12345678",
            channel=None,
            text="must block",
            message_type="direct",
            direction="tx",
            raw={"status": "queued", "gateway_id": None},
        )
        following = MessageRecord(
            message_id="following",
            protocol=PROTOCOL_MESHCORE,
            gateway_id="meshcore",
            sender="homeassistant",
            receiver=None,
            channel="0",
            text="may send",
            message_type="broadcast",
            direction="tx",
            raw={"status": "queued", "gateway_id": None},
        )

        class Store:
            async def async_pending_outbox(
                self,
                *,
                limit: int,
                after: tuple[str, str] | None = None,
            ) -> list[MessageRecord]:
                assert limit == 100
                assert after is None
                return [mismatched, following]

            async def async_add_message(self, _message: MessageRecord) -> None:
                return None

            async def async_recent_messages(
                self, limit: int
            ) -> list[MessageRecord]:
                assert limit == 100
                return [mismatched, following]

        gateway = SimpleNamespace(
            config=GatewayConfig(
                gateway_id="meshcore",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
            ),
            status=GatewayStatus(
                gateway_id="meshcore",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
                connected=True,
            ),
            async_send_message=AsyncMock(return_value="provider"),
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot()
        coordinator._node_alias_redirects = {}
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.store = Store()
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()
        mismatched.raw["target_binding"] = (
            coordinator._canonical_target_binding(
                "!12345678", PROTOCOL_MESHTASTIC
            )
        )

        await coordinator._flush_outbox(gateway_id="meshcore")

        assert {
            key: value
            for key, value in mismatched.raw.items()
            if key != "target_binding"
        } == {
            "status": "blocked",
            "gateway_id": None,
            "last_error_code": "unsafe_route",
        }
        gateway.async_send_message.assert_awaited_once_with(
            target_node=None,
            message="may send",
            channel="0",
            priority="normal",
            message_type="broadcast",
        )
        assert following.raw["status"] == "sent"

    asyncio.run(run())


def test_invalid_legacy_outbox_envelopes_block_without_starving_valid_work(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        invalid = [
            MessageRecord(
                message_id="direct-without-target",
                protocol=PROTOCOL_MESHCORE,
                gateway_id="meshcore",
                sender="homeassistant",
                receiver=None,
                channel=None,
                text="invalid",
                message_type="direct",
                direction="tx",
                raw={"status": "queued", "gateway_id": "meshcore"},
            ),
            MessageRecord(
                message_id="broadcast-with-target",
                protocol=PROTOCOL_MESHCORE,
                gateway_id="meshcore",
                sender="homeassistant",
                receiver="pub:deadbeef",
                channel=None,
                text="invalid",
                message_type="broadcast",
                direction="tx",
                raw={"status": "queued", "gateway_id": "meshcore"},
            ),
            MessageRecord(
                message_id="bad-channel",
                protocol=PROTOCOL_MESHCORE,
                gateway_id="meshcore",
                sender="homeassistant",
                receiver=None,
                channel="9",
                text="invalid",
                direction="tx",
                raw={"status": "queued", "gateway_id": "meshcore"},
            ),
            MessageRecord(
                message_id="poison-gateway",
                protocol=PROTOCOL_MESHCORE,
                gateway_id="meshcore",
                sender="homeassistant",
                receiver=None,
                channel=None,
                text="invalid",
                direction="tx",
                raw={"status": "queued", "gateway_id": []},
            ),
        ]
        valid = MessageRecord(
            message_id="valid",
            protocol=PROTOCOL_MESHCORE,
            gateway_id="meshcore",
            sender="homeassistant",
            receiver=None,
            channel=0,
            text="valid",
            direction="tx",
            raw={"status": "queued", "gateway_id": "meshcore"},
        )

        class Store:
            async def async_pending_outbox(
                self,
                *,
                limit: int,
                after: tuple[str, str] | None = None,
            ) -> list[MessageRecord]:
                assert limit == 100
                assert after is None
                return [*invalid, valid]

            async def async_add_message(self, _message: MessageRecord) -> None:
                return None

            async def async_recent_messages(
                self, limit: int
            ) -> list[MessageRecord]:
                assert limit == 100
                return [*invalid, valid]

        gateway = SimpleNamespace(
            config=GatewayConfig(
                gateway_id="meshcore",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
            ),
            status=GatewayStatus(
                gateway_id="meshcore",
                name="MeshCore",
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_TCP,
                connected=True,
            ),
            async_send_message=AsyncMock(return_value="provider"),
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot()
        coordinator._node_alias_redirects = {}
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.store = Store()
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()

        await coordinator._flush_outbox()

        assert all(record.raw["status"] == "blocked" for record in invalid)
        assert all(
            record.raw["last_error_code"] == "invalid_message"
            for record in invalid
        )
        gateway.async_send_message.assert_awaited_once()
        assert valid.channel == "0"
        assert valid.raw["status"] == "sent"

    asyncio.run(run())


def test_outbox_keyset_scan_passes_nonterminal_oldest_page(monkeypatch) -> None:
    """One disconnected head row cannot hide work after the first 100."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        base = datetime(2026, 7, 29, tzinfo=UTC)
        stuck = MessageRecord(
            message_id="000-stuck",
            protocol=PROTOCOL_MESHCORE,
            gateway_id="offline",
            sender="homeassistant",
            receiver=None,
            channel=None,
            text="wait",
            timestamp=base,
            direction="tx",
            raw={"status": "queued", "gateway_id": "offline"},
        )
        invalid = [
            MessageRecord(
                message_id=f"{index:03d}-invalid",
                protocol=PROTOCOL_MESHCORE,
                gateway_id="online",
                sender="homeassistant",
                receiver=None,
                channel=None,
                text="invalid",
                message_type="direct",
                timestamp=base + timedelta(seconds=index),
                direction="tx",
                raw={"status": "queued", "gateway_id": "online"},
            )
            for index in range(1, 100)
        ]
        later = MessageRecord(
            message_id="100-later",
            protocol=PROTOCOL_MESHCORE,
            gateway_id="online",
            sender="homeassistant",
            receiver=None,
            channel=None,
            text="deliver later page",
            timestamp=base + timedelta(seconds=100),
            direction="tx",
            raw={"status": "queued", "gateway_id": "online"},
        )
        first_page = [stuck, *invalid]
        expected_cursor = (
            first_page[-1].timestamp.isoformat(),
            first_page[-1].message_id,
        )

        class Store:
            def __init__(self) -> None:
                self.cursors: list[tuple[str, str] | None] = []

            async def async_pending_outbox(
                self,
                *,
                limit: int,
                after: tuple[str, str] | None = None,
            ) -> list[MessageRecord]:
                assert limit == 100
                self.cursors.append(after)
                if after is None:
                    return first_page
                if after == expected_cursor:
                    return [later]
                return []

            async def async_add_message(self, _message: MessageRecord) -> None:
                return None

            async def async_recent_messages(
                self, limit: int
            ) -> list[MessageRecord]:
                assert limit == 100
                return [later]

        def gateway(gateway_id: str, connected: bool) -> SimpleNamespace:
            return SimpleNamespace(
                config=GatewayConfig(
                    gateway_id=gateway_id,
                    name=gateway_id,
                    protocol=PROTOCOL_MESHCORE,
                    transport=TRANSPORT_TCP,
                ),
                status=GatewayStatus(
                    gateway_id=gateway_id,
                    name=gateway_id,
                    protocol=PROTOCOL_MESHCORE,
                    transport=TRANSPORT_TCP,
                    connected=connected,
                ),
                async_send_message=AsyncMock(return_value="provider"),
            )

        offline = gateway("offline", False)
        online = gateway("online", True)
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot()
        coordinator._node_alias_redirects = {}
        coordinator.gateways = {"offline": offline, "online": online}
        coordinator.store = Store()
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()

        await coordinator._flush_outbox()

        assert coordinator.store.cursors == [None, expected_cursor]
        assert stuck.raw["status"] == "queued"
        assert all(record.raw["status"] == "blocked" for record in invalid)
        assert later.raw["status"] == "sent"
        online.async_send_message.assert_awaited_once()
        offline.async_send_message.assert_not_awaited()

    asyncio.run(run())


def test_direct_send_revalidates_identity_after_rate_limiter(
    monkeypatch,
) -> None:
    """A conflict arriving while throttled must block the provider send."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        left = NodeState(
            node_key="mac:111111111111",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
            mac="111111111111",
        )
        right = NodeState(
            node_key="mac:222222222222",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
            mac="222222222222",
        )
        persisted: list[dict] = []

        class Store:
            async def async_add_message(
                self, message: MessageRecord
            ) -> None:
                persisted.append(dict(message.raw))

            async def async_recent_messages(
                self, limit: int
            ) -> list[MessageRecord]:
                assert limit == 100
                return []

        class Limiter:
            async def acquire(self) -> None:
                coordinator.snapshot.nodes[right.node_key] = right

        class Gateway:
            config = GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            )
            status = GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            )

            def __init__(self) -> None:
                self.send_count = 0

            async def async_send_message(self, **_kwargs) -> str:
                self.send_count += 1
                return "must-not-send"

        gateway = Gateway()
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator._node_alias_redirects = {
            left.node_key: left.node_key
        }
        coordinator.snapshot = MeshSnapshot(nodes={left.node_key: left})
        coordinator.store = Store()
        coordinator.tx_limiter = Limiter()
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()
        error_class = coordinator_class._provider_message_target.__globals__[
            "HomeAssistantError"
        ]

        with pytest.raises(error_class, match="identity is ambiguous"):
            await coordinator._async_send_message(
                target_node=left.node_key,
                message="do not send",
                channel=None,
                priority="normal",
                message_type="direct",
                gateway_id="gateway-1",
            )

        assert gateway.send_count == 0
        assert persisted[0]["status"] == "queued"
        assert {
            key: value
            for key, value in persisted[-1].items()
            if key != "target_binding"
        } == {
            "status": "blocked",
            "target_node": "!12345678",
            "gateway_id": "gateway-1",
            "last_error_code": "unsafe_identity",
        }
        assert persisted[-1]["target_binding"].startswith("node:")
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.snapshot
        )

    asyncio.run(run())


def test_known_proof_target_replacement_is_blocked_after_rate_limiter(
    monkeypatch,
) -> None:
    """A proof/name selection cannot degrade to an ID-only replacement."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        original = NodeState(
            node_key="meshtastic:!12345678",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
            mac="aabbccddeeff",
            long_name="Original relay",
        )
        replacement = NodeState(
            node_key="meshtastic:!12345678",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
        )
        saved: list[dict] = []

        class Limiter:
            async def acquire(self) -> None:
                coordinator.snapshot.nodes = {
                    replacement.node_key: replacement
                }

        gateway = SimpleNamespace(
            config=GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            ),
            status=GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            ),
            async_send_message=AsyncMock(),
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator.snapshot = MeshSnapshot(
            nodes={original.node_key: original}
        )
        coordinator._node_alias_redirects = {
            original.node_key: original.node_key
        }
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.store = SimpleNamespace(
            async_add_message=lambda record: _append_async(
                saved, dict(record.raw)
            ),
            async_recent_messages=AsyncMock(return_value=[]),
        )
        coordinator.tx_limiter = Limiter()
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()
        error_class = coordinator_class._async_send_message.__globals__[
            "HomeAssistantError"
        ]

        with pytest.raises(error_class, match="identity changed"):
            await coordinator._async_send_message(
                target_node=original.node_key,
                message="do not redirect",
                channel=None,
                priority="normal",
                message_type="direct",
                gateway_id=gateway.config.gateway_id,
            )

        gateway.async_send_message.assert_not_awaited()
        assert saved[-1]["status"] == "blocked"
        assert saved[-1]["last_error_code"] == "unsafe_identity"
        assert saved[-1]["target_binding"].startswith("node:")

    async def _append_async(items: list[dict], item: dict) -> None:
        items.append(item)

    asyncio.run(run())


def test_queued_proof_binding_blocks_id_only_replacement_after_restart(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        original = NodeState(
            node_key="meshtastic:!12345678",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
            public_key="ab" * 32,
            short_name="OLD",
        )
        replacement = NodeState(
            node_key="meshtastic:!12345678",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!12345678",
        )
        coordinator = object.__new__(coordinator_class)
        coordinator.snapshot = MeshSnapshot(
            nodes={original.node_key: original}
        )
        coordinator._node_alias_redirects = {
            original.node_key: original.node_key
        }
        binding = coordinator._resolve_message_target(
            original.node_key
        ).binding
        record = MessageRecord(
            message_id="queued-bound-direct",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-1",
            sender="homeassistant",
            receiver="!12345678",
            channel=None,
            text="do not redirect",
            message_type="direct",
            direction="tx",
            raw={
                "status": "queued",
                "gateway_id": "gateway-1",
                "target_binding": binding,
            },
        )

        class Store:
            async def async_pending_outbox(
                self,
                *,
                limit: int,
                after: tuple[str, str] | None = None,
            ) -> list[MessageRecord]:
                assert limit == 100
                assert after is None
                return [record]

            async def async_add_message(self, _record: MessageRecord) -> None:
                return None

            async def async_recent_messages(
                self, limit: int
            ) -> list[MessageRecord]:
                assert limit == 100
                return [record]

        gateway = SimpleNamespace(
            config=GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            ),
            status=GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            ),
            async_send_message=AsyncMock(),
        )
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator._active_send_message_ids = set()
        coordinator.snapshot.nodes = {replacement.node_key: replacement}
        coordinator.gateways = {gateway.config.gateway_id: gateway}
        coordinator.store = Store()
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()

        await coordinator._flush_outbox()

        gateway.async_send_message.assert_not_awaited()
        assert record.raw["status"] == "blocked"
        assert record.raw["last_error_code"] == "unsafe_identity"

    asyncio.run(run())


def test_meshtastic_gateway_rejects_unknown_provider_alias_before_queue(
    monkeypatch,
) -> None:
    """Unknown names cannot fall through to a provider's private node cache."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )

        class Gateway:
            config = GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            )
            status = GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            )
            async_send_message = AsyncMock()

        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator._node_alias_redirects = {}
        coordinator.snapshot = MeshSnapshot()
        coordinator.store = SimpleNamespace(async_add_message=AsyncMock())
        coordinator.gateways = {"gateway-1": Gateway()}
        error_class = coordinator_class._provider_message_target.__globals__[
            "HomeAssistantError"
        ]

        with pytest.raises(error_class, match="not a known node"):
            await coordinator._async_send_message(
                target_node="unknown cached name",
                message="do not queue",
                channel=None,
                priority="normal",
                message_type="direct",
                gateway_id="gateway-1",
            )

        coordinator.store.async_add_message.assert_not_awaited()
        coordinator.gateways[
            "gateway-1"
        ].async_send_message.assert_not_awaited()

    asyncio.run(run())


def test_late_provider_callbacks_are_ignored_during_shutdown(monkeypatch) -> None:
    """No late radio callback may touch state or storage after unload begins."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = True
        coordinator._reconnect_suspended = True
        coordinator._gateway_generation = 4
        coordinator.snapshot = MeshSnapshot()
        coordinator.store = SimpleNamespace(
            async_add_packet=AsyncMock(),
            async_add_message=AsyncMock(),
            async_recent_messages=AsyncMock(),
            async_upsert_node=AsyncMock(),
        )
        coordinator._flush_outbox = AsyncMock()
        coordinator._schedule_reconnect = AsyncMock()
        status = GatewayStatus(
            gateway_id="gateway-1",
            name="Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            connected=True,
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(status=status)
        }

        await coordinator._handle_packet(
            MeshPacket(protocol=PROTOCOL_MESHTASTIC, gateway_id="gateway-1"),
            gateway_generation=4,
        )
        await coordinator._handle_node(
            NodeState(
                node_key="meshtastic:1",
                protocol=PROTOCOL_MESHTASTIC,
                last_gateway_id="gateway-1",
            ),
            gateway_generation=4,
        )
        await coordinator._handle_gateway_status(
            status, gateway_generation=4
        )

        coordinator.store.async_add_packet.assert_not_awaited()
        coordinator.store.async_upsert_node.assert_not_awaited()
        coordinator._flush_outbox.assert_not_awaited()
        coordinator._schedule_reconnect.assert_not_awaited()
        assert coordinator.snapshot == MeshSnapshot()

    asyncio.run(run())


def test_inbound_meshtastic_reaction_persists_exact_message_relationship(
    monkeypatch,
) -> None:
    """A reaction remains a record but points to its exact on-air message ID."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator.snapshot = MeshSnapshot()
        coordinator.gateways = {}
        coordinator.deduplicator = SimpleNamespace(is_duplicate=lambda _packet: False)
        stored: list[MessageRecord] = []

        class Store:
            async def async_add_packet(self, _packet) -> bool:
                return True

            async def async_add_message(self, message: MessageRecord) -> None:
                stored.append(message)

            async def async_recent_messages(self, _limit: int) -> list[MessageRecord]:
                return list(stored)

        coordinator.store = Store()
        coordinator.async_set_updated_data = Mock()

        await coordinator._handle_packet(
            MeshPacket(
                protocol=PROTOCOL_MESHTASTIC,
                gateway_id="gateway-1",
                packet_id="222",
                sender="!12345678",
                receiver="!ffffffff",
                channel="0",
                portnum="TEXT_MESSAGE_APP",
                text="👍",
                reply_to_message_id="meshtastic:111",
                reaction="👍",
            ),
            gateway_generation=1,
        )

        assert len(stored) == 1
        assert stored[0].message_id == "meshtastic:222"
        assert stored[0].reply_to_message_id == "meshtastic:111"
        assert stored[0].reaction == "👍"
        assert coordinator.snapshot.recent_messages == stored

    asyncio.run(run())


def test_stale_same_id_gateway_status_cannot_mutate_replacement(
    monkeypatch,
) -> None:
    """A callback from an old gateway object cannot target its replacement."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 2
        coordinator._reconnect_tasks = {}
        coordinator.snapshot = MeshSnapshot()
        coordinator._flush_outbox = AsyncMock()
        old_status = GatewayStatus(
            gateway_id="gateway-1",
            name="Old",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            connected=True,
        )
        replacement_status = GatewayStatus(
            gateway_id="gateway-1",
            name="Replacement",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(status=replacement_status)
        }

        await coordinator._handle_gateway_status(
            old_status, gateway_generation=2
        )

        coordinator._flush_outbox.assert_not_awaited()
        assert coordinator.snapshot.gateways == {}

    asyncio.run(run())


def test_outbox_does_not_write_after_shutdown_begins_mid_send(
    monkeypatch,
) -> None:
    """A radio send completing late cannot write into a closing store."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator._outbox_lock = asyncio.Lock()
        coordinator._outbox_flush_owner = None
        coordinator.snapshot = MeshSnapshot()
        record = MessageRecord(
            message_id="queued-message",
            protocol=PROTOCOL_MESHTASTIC,
            gateway_id="gateway-1",
            sender="homeassistant",
            receiver=None,
            channel=None,
            text="hello",
            direction="tx",
            raw={"status": "queued", "gateway_id": "gateway-1"},
        )
        coordinator.store = SimpleNamespace(
            async_pending_outbox=AsyncMock(return_value=[record]),
            async_add_message=AsyncMock(),
            async_recent_messages=AsyncMock(),
        )
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )

        class Gateway:
            config = GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            )
            status = GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            )

            async def async_send_message(self, **_kwargs) -> str:
                coordinator._shutting_down = True
                return "provider-message"

        coordinator.gateways = {"gateway-1": Gateway()}

        await coordinator._flush_outbox(gateway_generation=1)

        coordinator.store.async_add_message.assert_not_awaited()
        coordinator.store.async_recent_messages.assert_not_awaited()
        assert record.raw == {"status": "queued", "gateway_id": "gateway-1"}
        assert coordinator._outbox_flush_owner is None

    asyncio.run(run())


def test_outbox_owner_cancellation_is_bounded_and_retained(monkeypatch) -> None:
    """A send that suppresses cancellation cannot hold unload open."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_outbox_flush_owner.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        release = asyncio.Event()
        active = asyncio.Event()

        async def stubborn_flush() -> None:
            active.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(stubborn_flush())
        coordinator._outbox_flush_owner = task
        await active.wait()

        assert await coordinator._cancel_outbox_flush_owner() is False
        assert coordinator._outbox_flush_owner is task

        release.set()
        await task
        assert await coordinator._cancel_outbox_flush_owner() is True
        assert coordinator._outbox_flush_owner is None

    asyncio.run(run())


def test_status_does_not_publish_after_lifecycle_changes_during_flush(
    monkeypatch,
) -> None:
    """A status callback must recheck shutdown after awaiting the outbox."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator._reconnect_tasks = {}
        coordinator.snapshot = MeshSnapshot()
        updates: list[MeshSnapshot] = []
        coordinator.async_set_updated_data = updates.append
        status = GatewayStatus(
            gateway_id="gateway-1",
            name="Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            connected=True,
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(status=status)
        }

        async def begin_shutdown(**_kwargs) -> None:
            coordinator._shutting_down = True

        coordinator._flush_outbox = begin_shutdown

        await coordinator._handle_gateway_status(
            status, gateway_generation=1
        )

        assert updates == []

    asyncio.run(run())


def test_direct_send_cannot_write_after_bounded_shutdown(monkeypatch) -> None:
    """A cancellation-suppressing provider send cannot outlive storage safely."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        monkeypatch.setitem(
            coordinator_class._cancel_send_tasks.__globals__,
            "_GATEWAY_TASK_CANCEL_TIMEOUT",
            0.01,
        )
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator._gateway_startup_task = None
        coordinator._reconnect_tasks = {}
        coordinator._outbox_flush_owner = None
        coordinator._send_tasks = set()
        coordinator.snapshot = MeshSnapshot()
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        sent_events: list[tuple[str, dict]] = []
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(
                async_fire=lambda event, data: sent_events.append((event, data))
            )
        )
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()

        class Store:
            def __init__(self) -> None:
                self.closed = False
                self.added: list[MessageRecord] = []
                self.recent_calls = 0

            async def async_add_message(self, record: MessageRecord) -> None:
                assert self.closed is False
                self.added.append(record)

            async def async_recent_messages(self, _limit: int):
                assert self.closed is False
                self.recent_calls += 1
                return []

            async def async_close(self) -> None:
                self.closed = True

        store = Store()
        coordinator.store = store

        class Gateway:
            config = GatewayConfig(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
            )
            status = GatewayStatus(
                gateway_id="gateway-1",
                name="Gateway",
                protocol=PROTOCOL_MESHTASTIC,
                transport=TRANSPORT_TCP,
                connected=True,
            )

            async def async_send_message(self, **_kwargs) -> str:
                provider_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    await release_provider.wait()
                return "provider-message"

            async def async_stop(self) -> None:
                return None

        coordinator.gateways = {"gateway-1": Gateway()}
        send_task = asyncio.create_task(
            coordinator.async_send_message(
                target_node=None,
                message="hello",
                gateway_id="gateway-1",
            )
        )
        await provider_started.wait()

        await asyncio.wait_for(coordinator.async_shutdown(), timeout=0.2)

        assert store.closed is True
        assert len(store.added) == 1
        assert store.added[0].raw["status"] == "queued"
        assert coordinator._send_tasks == {send_task}

        release_provider.set()
        await send_task

        assert coordinator._send_tasks == set()
        assert len(store.added) == 1
        assert store.recent_calls == 0
        assert sent_events == []

    asyncio.run(run())


def test_diagnostics_exclude_gateway_identity_errors_and_mesh_content(monkeypatch) -> None:
    """Downloaded diagnostics must be useful without exposing private mesh data."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

        class Store:
            async def async_diagnostics(self) -> dict[str, int]:
                return {"node_count": 1, "message_count": 1, "packet_count": 1}

        coordinator = object.__new__(coordinator_class)
        coordinator.node_timeout = 900
        coordinator.history_days = 30
        coordinator.store = Store()
        coordinator.deduplicator = SimpleNamespace(
            stats=lambda: {"entries": 1, "total_packets": 1}
        )
        coordinator.tx_limiter = SimpleNamespace(
            snapshot=lambda: {"rate": 0.5, "capacity": 5.0, "tokens": 4.0}
        )
        coordinator.panel_telemetry = PanelTelemetry()
        coordinator.panel_telemetry.record_failure(
            "private-node-id",
            category="token=private",
            error_type="PrivateOwnerError",
            error_code="192.0.2.99",
        )
        coordinator.snapshot = MeshSnapshot(
            nodes={
                "private-node-id": NodeState(
                    node_key="private-node-id",
                    protocol=PROTOCOL_MESHTASTIC,
                    hardware_model="Private Owner's Bedroom Radio",
                    firmware_version="Private Owner Firmware",
                    radio_type="Private Owner Radio Type",
                    role="Private Owner Bedroom Role",
                )
            },
            recent_messages=[
                MessageRecord(
                    message_id="private-message-id",
                    protocol=PROTOCOL_MESHTASTIC,
                    gateway_id="private-gateway-id",
                    sender="private-sender",
                    receiver=None,
                    channel="private-channel",
                    text="private message text",
                )
            ],
        )
        coordinator.gateways = {
            "private-gateway-id": SimpleNamespace(
                status=GatewayStatus(
                    gateway_id="private-gateway-id",
                    name="Private Home Gateway",
                    protocol=PROTOCOL_MESHTASTIC,
                    transport=TRANSPORT_TCP,
                    connected=False,
                    errors=["connection failed at 192.0.2.99 with token=private"],
                    detail={"host": "192.0.2.99"},
                )
            )
        }

        diagnostics = await coordinator.async_diagnostics()
        serialized = repr(diagnostics)

        assert diagnostics["configuration"]["gateway_count"] == 1
        assert diagnostics["gateways"][0]["error_count"] == 1
        assert diagnostics["panel"]["failure_event_count"] == 1
        assert diagnostics["panel"]["failure_events"][0] == {
            "sequence": 1,
            "recorded_at": diagnostics["panel"]["failure_events"][0][
                "recorded_at"
            ],
            "operation": "reporting",
            "category": "unknown",
            "error_type": "other_error",
            "error_code": "unexpected_error",
            "occurrence": 1,
            "consecutive": 1,
            "duration_bucket": "unknown",
        }
        assert diagnostics["node_observability"]["node_counts"] == {
            "total": 1,
            "observed_this_session": 0,
            "cached_only": 1,
            "online": 0,
            "offline": 1,
            "located": 0,
            "located_offline": 0,
            "stored_total": 1,
            "analyzed": 1,
            "analysis_omitted": 0,
            "analysis_truncated": False,
        }
        for private_value in (
            "private-node-id",
            "private-message-id",
            "private-gateway-id",
            "Private Home Gateway",
            "private message text",
            "private-sender",
            "private-channel",
            "192.0.2.99",
                "token=private",
                "Private Owner's Bedroom Radio",
                "Private Owner Firmware",
                "Private Owner Radio Type",
                "Private Owner Bedroom Role",
            ):
            assert private_value not in serialized

    asyncio.run(run())


def test_gateway_issue_ids_do_not_expose_gateway_identity(monkeypatch) -> None:
    """HA appends issue IDs outside integration-controlled diagnostic redaction."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    coordinator._gateway_configs = [
        GatewayConfig(
            gateway_id="private-gateway-id",
            name="Private Home Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
    ]
    coordinator.gateways = {}

    issue_id = coordinator._gateway_issue_id(
        "gateway_start", "private-gateway-id"
    )

    assert issue_id == "gateway_start_gateway_001"
    assert "private-gateway-id" not in issue_id


def test_gateway_start_repair_placeholder_redacts_identity_and_raw_error(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        private_error = (
            "connection to AA:BB:CC:DD:EE:FF at /dev/tty-private failed"
        )
        config = GatewayConfig(
            gateway_id="private-kitchen-radio",
            name="Private Kitchen Radio",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        gateway = SimpleNamespace(
            config=config,
            async_start=AsyncMock(side_effect=RuntimeError(private_error)),
        )
        coordinator = object.__new__(coordinator_class)
        coordinator.hass = object()
        coordinator._gateway_configs = [config]
        coordinator.gateways = {config.gateway_id: gateway}
        coordinator._schedule_reconnect = Mock()
        issue_registry_module = coordinator_class._start_gateways.__globals__[
            "ir"
        ]
        created: list[dict] = []
        monkeypatch.setattr(
            issue_registry_module,
            "async_delete_issue",
            lambda *_args, **_kwargs: None,
            raising=False,
        )
        monkeypatch.setattr(
            issue_registry_module,
            "async_create_issue",
            lambda *_args, **kwargs: created.append(kwargs),
            raising=False,
        )

        await coordinator._start_gateways()

        placeholder = created[0]["translation_placeholders"]["message"]
        assert "Gateway adapter 001" in placeholder
        assert "connection" in placeholder
        assert "RuntimeError" in placeholder
        for private in (
            config.gateway_id,
            config.name,
            "AA:BB:CC:DD:EE:FF",
            "/dev/tty-private",
        ):
            assert private not in placeholder

    asyncio.run(run())


def test_send_failure_record_and_repair_do_not_retain_raw_error(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        private_error = (
            "connection to AA:BB:CC:DD:EE:FF at /dev/tty-private failed"
        )
        config = GatewayConfig(
            gateway_id="private-kitchen-radio",
            name="Private Kitchen Radio",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        gateway = SimpleNamespace(
            config=config,
            status=GatewayStatus(
                gateway_id=config.gateway_id,
                name=config.name,
                protocol=config.protocol,
                transport=config.transport,
                connected=True,
            ),
            async_send_message=AsyncMock(
                side_effect=RuntimeError(private_error)
            ),
        )
        saved: list[MessageRecord] = []

        async def save(record: MessageRecord) -> None:
            saved.append(record)

        coordinator = object.__new__(coordinator_class)
        coordinator._gateway_generation = 0
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._active_send_message_ids = set()
        coordinator._gateway_configs = [config]
        coordinator.snapshot = MeshSnapshot()
        coordinator._node_alias_redirects = {}
        coordinator.gateways = {config.gateway_id: gateway}
        coordinator.store = SimpleNamespace(
            async_add_message=save,
            async_recent_messages=AsyncMock(return_value=[]),
        )
        coordinator.tx_limiter = SimpleNamespace(acquire=AsyncMock())
        coordinator.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args: None)
        )
        coordinator.async_set_updated_data = Mock()
        issue_registry_module = coordinator_class._async_send_message.__globals__[
            "ir"
        ]
        created: list[dict] = []
        monkeypatch.setattr(
            issue_registry_module,
            "async_create_issue",
            lambda *_args, **kwargs: created.append(kwargs),
            raising=False,
        )

        await coordinator._async_send_message(
            target_node=None,
            message="safe visible message",
            channel=None,
            priority="normal",
            message_type="broadcast",
            gateway_id=config.gateway_id,
        )

        assert saved[-1].raw == {
            "status": "queued",
            "target_node": None,
            "gateway_id": config.gateway_id,
            "last_error_code": "send_failed",
        }
        assert private_error not in repr(saved[-1].raw)
        placeholder = created[0]["translation_placeholders"]["message"]
        for private in (
            config.gateway_id,
            config.name,
            "AA:BB:CC:DD:EE:FF",
            "/dev/tty-private",
        ):
            assert private not in placeholder

    asyncio.run(run())


def test_gateway_stop_debug_log_uses_only_safe_ordinal_and_categories(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(
            monkeypatch
        )
        private_error = (
            "connection to AA:BB:CC:DD:EE:FF at /dev/tty-private failed"
        )
        config = GatewayConfig(
            gateway_id="private-kitchen-radio",
            name="Private Kitchen Radio",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )

        async def fail_stop() -> None:
            raise RuntimeError(private_error)

        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_startup_task = None
        coordinator._reconnect_tasks = {}
        coordinator._outbox_flush_owner = None
        coordinator._send_tasks = set()
        coordinator._gateway_configs = [config]
        coordinator.gateways = {
            config.gateway_id: SimpleNamespace(
                config=config, async_stop=fail_stop
            )
        }
        coordinator.store = SimpleNamespace(async_close=AsyncMock())

        with caplog.at_level(
            logging.DEBUG, logger="custom_components.meshnet.coordinator"
        ):
            await coordinator.async_shutdown()

        rendered = caplog.text
        assert "gateway adapter 001" in rendered
        assert "connection" in rendered
        assert "RuntimeError" in rendered
        for private in (
            config.gateway_id,
            config.name,
            "AA:BB:CC:DD:EE:FF",
            "/dev/tty-private",
        ):
            assert private not in rendered

    asyncio.run(run())


def test_gateway_start_repairs_follow_current_outcomes(monkeypatch) -> None:
    """Clear resolved indexed repairs while preserving the failed gateway."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator.hass = object()
        first_config = GatewayConfig(
            gateway_id="private-first-gateway",
            name="Private First Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        second_config = GatewayConfig(
            gateway_id="private-second-gateway",
            name="Private Second Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        first = SimpleNamespace(
            config=first_config,
            async_start=AsyncMock(),
        )
        second = SimpleNamespace(
            config=second_config,
            async_start=AsyncMock(side_effect=RuntimeError("start failed")),
        )
        coordinator._gateway_configs = [first_config, second_config]
        coordinator.gateways = {
            first_config.gateway_id: first,
            second_config.gateway_id: second,
        }
        coordinator._schedule_reconnect = Mock()
        issue_registry_module = coordinator_class._start_gateways.__globals__["ir"]
        deleted: list[tuple[str, str]] = []
        created: list[tuple[str, str]] = []
        monkeypatch.setattr(
            issue_registry_module,
            "async_delete_issue",
            lambda _hass, domain, issue_id: deleted.append((domain, issue_id)),
            raising=False,
        )
        monkeypatch.setattr(
            issue_registry_module,
            "async_create_issue",
            lambda _hass, domain, issue_id, **_kwargs: created.append(
                (domain, issue_id)
            ),
            raising=False,
        )

        await coordinator._start_gateways()

        assert deleted == [
            ("meshnet", "no_gateways"),
            ("meshnet", "gateway_start_gateway_001"),
        ]
        assert created == [("meshnet", "gateway_start_gateway_002")]
        coordinator._schedule_reconnect.assert_called_once_with(
            second_config.gateway_id
        )
        assert all("private" not in issue_id for _domain, issue_id in deleted)
        assert all("private" not in issue_id for _domain, issue_id in created)

    asyncio.run(run())


def test_connected_gateway_clears_failed_start_repair(monkeypatch) -> None:
    """A background recovery must remove the warning from its failed start."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._gateway_generation = 1
        coordinator._reconnect_tasks = {}
        coordinator.snapshot = MeshSnapshot()
        coordinator.async_set_updated_data = Mock()
        coordinator._flush_outbox = AsyncMock()
        coordinator._delete_resolved_issue = Mock(return_value=True)
        config = GatewayConfig(
            gateway_id="private-gateway",
            name="Private Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        status = GatewayStatus(
            gateway_id=config.gateway_id,
            name=config.name,
            protocol=config.protocol,
            transport=config.transport,
            connected=True,
        )
        coordinator._gateway_configs = [config]
        coordinator.gateways = {
            config.gateway_id: SimpleNamespace(config=config, status=status)
        }

        await coordinator._handle_gateway_status(
            status,
            gateway_generation=1,
        )

        coordinator._delete_resolved_issue.assert_called_once_with(
            "gateway_start_gateway_001"
        )
        coordinator._flush_outbox.assert_awaited_once_with(
            gateway_id=config.gateway_id,
            gateway_generation=1,
        )

    asyncio.run(run())


def test_resolved_issue_delete_failure_cannot_break_gateway_start(
    monkeypatch,
) -> None:
    """A read-only repair registry cannot turn a healthy transport into failure."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator.hass = object()
        config = GatewayConfig(
            gateway_id="private-gateway",
            name="Private Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
        )
        start = AsyncMock()
        coordinator._gateway_configs = [config]
        coordinator.gateways = {
            config.gateway_id: SimpleNamespace(config=config, async_start=start)
        }
        issue_registry_module = coordinator_class._start_gateways.__globals__["ir"]
        attempted: list[str] = []

        def fail_delete(_hass, _domain: str, issue_id: str) -> None:
            attempted.append(issue_id)
            raise RuntimeError("registry is temporarily read-only")

        monkeypatch.setattr(
            issue_registry_module,
            "async_delete_issue",
            fail_delete,
            raising=False,
        )

        await coordinator._start_gateways()

        start.assert_awaited_once_with()
        assert attempted == ["no_gateways", "gateway_start_gateway_001"]

    asyncio.run(run())


def test_no_gateway_repair_is_preserved_until_a_gateway_exists(
    monkeypatch,
) -> None:
    """An empty configuration still creates its repair and clears nothing."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator.hass = object()
        coordinator._gateway_configs = []
        coordinator.gateways = {}
        issue_registry_module = coordinator_class._start_gateways.__globals__["ir"]
        deleted: list[str] = []
        created: list[str] = []
        monkeypatch.setattr(
            issue_registry_module,
            "async_delete_issue",
            lambda _hass, _domain, issue_id: deleted.append(issue_id),
            raising=False,
        )
        monkeypatch.setattr(
            issue_registry_module,
            "async_create_issue",
            lambda _hass, _domain, issue_id, **_kwargs: created.append(issue_id),
            raising=False,
        )

        await coordinator._start_gateways()

        assert created == ["no_gateways"]
        assert deleted == []

    asyncio.run(run())


def test_legacy_gateway_issues_are_deleted_without_touching_safe_keys(
    monkeypatch,
) -> None:
    """Upgrade cleanup removes old name-derived IDs from HA diagnostics."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    coordinator.hass = object()
    issue_registry_module = (
        coordinator_class._delete_legacy_gateway_issues.__globals__["ir"]
    )
    legacy_ids = {
        "gateway_start_private-home-radio_12345678",
        "send_failed_private-home-radio_12345678",
        "unsupported_protocol_private-home-radio_12345678",
    }
    safe_id = "gateway_start_gateway_001"
    unrelated_id = "no_gateways"
    registry = SimpleNamespace(
        issues={
            **{
                ("meshnet", issue_id): SimpleNamespace(active=False)
                for issue_id in legacy_ids
            },
            ("meshnet", safe_id): SimpleNamespace(active=True),
            ("meshnet", unrelated_id): SimpleNamespace(active=True),
            ("other_domain", "gateway_start_private"): SimpleNamespace(
                active=True
            ),
        }
    )
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        issue_registry_module,
        "async_get",
        lambda _hass: registry,
        raising=False,
    )
    monkeypatch.setattr(
        issue_registry_module,
        "async_delete_issue",
        lambda _hass, domain, issue_id: deleted.append((domain, issue_id)),
        raising=False,
    )

    count = coordinator._delete_legacy_gateway_issues()

    assert count == len(legacy_ids)
    assert set(deleted) == {("meshnet", issue_id) for issue_id in legacy_ids}
    assert ("meshnet", safe_id) not in deleted
    assert ("meshnet", unrelated_id) not in deleted


def test_legacy_issue_cleanup_failure_cannot_block_setup(monkeypatch) -> None:
    """A registry anomaly is diagnostic-only and never a setup dependency."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    coordinator.hass = object()
    issue_registry_module = (
        coordinator_class._delete_legacy_gateway_issues.__globals__["ir"]
    )
    registry = SimpleNamespace(
        issues={
            (
                "meshnet",
                "gateway_start_private-home-radio_12345678",
            ): SimpleNamespace(active=False)
        }
    )
    monkeypatch.setattr(
        issue_registry_module,
        "async_get",
        lambda _hass: registry,
        raising=False,
    )

    def fail_delete(*_args) -> None:
        raise RuntimeError("registry is temporarily read-only")

    monkeypatch.setattr(
        issue_registry_module,
        "async_delete_issue",
        fail_delete,
        raising=False,
    )

    assert coordinator._delete_legacy_gateway_issues() == 0


def test_update_cycle_tracks_real_success_timestamp_and_duration(monkeypatch) -> None:
    """Diagnostics must not depend on a coordinator attribute HA never sets."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator.history_days = 30
        coordinator.snapshot = MeshSnapshot()
        coordinator._last_update_attempt_at = None
        coordinator._last_update_success_at = None
        coordinator._last_update_duration_seconds = None
        coordinator._last_update_error_category = None

        class Store:
            async def async_prune(self, history_days: int) -> None:
                assert history_days == 30

            async def async_recent_messages(self, limit: int):
                assert limit == 100
                return []

            async def async_messages_since(self, _timestamp) -> int:
                return 0

        coordinator.store = Store()
        coordinator._mark_stale_nodes = lambda: None
        coordinator._mesh_health_score = lambda: 100.0

        result = await coordinator._async_update_data()

        assert result is coordinator.snapshot
        assert coordinator._last_update_attempt_at is not None
        assert coordinator._last_update_success_at is not None
        assert coordinator._last_update_duration_seconds is not None
        assert coordinator._last_update_duration_seconds >= 0
        assert coordinator._last_update_error_category is None

    asyncio.run(run())


def test_reconnect_loop_retries_with_increasing_backoff(monkeypatch) -> None:
    """A failed reconnect remains single-flight and retries until connected."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False

        delays = []
        original_sleep = asyncio.sleep

        async def fast_sleep(delay: float) -> None:
            delays.append(delay)
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        coordinator._reconnect_delay = lambda attempt: float(30 * (2**attempt))

        class Gateway:
            def __init__(self) -> None:
                self.status = SimpleNamespace(connected=False)
                self.start_pending = False
                self.start_calls = 0
                self.stop_calls = 0
                self.errors = []

            async def async_stop(self) -> None:
                self.stop_calls += 1

            async def async_start(self) -> None:
                self.start_calls += 1
                if self.start_calls < 3:
                    raise RuntimeError(f"failure {self.start_calls}")
                self.status.connected = True

            async def _emit_error(self, error: str) -> None:
                self.errors.append(error)

        gateway = Gateway()
        coordinator.gateways = {"gateway-1": gateway}

        await coordinator._delayed_reconnect("gateway-1")

        assert delays == [30.0, 60.0, 120.0]
        assert gateway.stop_calls == 3
        assert gateway.start_calls == 3
        assert gateway.errors == [
            "Reconnect failed: failure 1",
            "Reconnect failed: failure 2",
        ]

    asyncio.run(run())


def test_reconnect_joins_pending_start_before_retry(monkeypatch) -> None:
    """Reconnect must not stop or duplicate an in-flight BLE constructor."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_delay = lambda _attempt: 0.0

        order = []
        pending_joined = asyncio.Event()
        release_pending = asyncio.Event()
        original_sleep = asyncio.sleep

        async def fast_sleep(_delay: float) -> None:
            order.append("sleep")
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        class Gateway:
            def __init__(self) -> None:
                self.status = SimpleNamespace(connected=False)
                self.start_pending = True

            async def async_start(self) -> None:
                if self.start_pending:
                    order.append("join")
                    pending_joined.set()
                    await release_pending.wait()
                    self.start_pending = False
                    raise RuntimeError("original start failed")
                order.append("retry")
                self.status.connected = True

            async def async_stop(self) -> None:
                order.append("stop")

            async def _emit_error(self, _error: str) -> None:
                return None

        coordinator.gateways = {"gateway-1": Gateway()}
        reconnect_task = asyncio.create_task(coordinator._delayed_reconnect("gateway-1"))

        await pending_joined.wait()
        assert order == ["join"]
        release_pending.set()
        await reconnect_task

        assert order == ["join", "sleep", "stop", "retry"]

    asyncio.run(run())


def test_reconnect_backoff_is_exponential_jittered_and_capped(monkeypatch) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

    first = coordinator_class._reconnect_delay(0)
    second = coordinator_class._reconnect_delay(1)
    capped = coordinator_class._reconnect_delay(100)

    assert 24.0 <= first <= 36.0
    assert 48.0 <= second <= 72.0
    assert second > first
    assert 240.0 <= capped <= 300.0


def test_shutdown_cancels_reconnect_before_transport_restart(monkeypatch) -> None:
    """A reconnect sleeping during unload cannot restart its gateway."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator._shutting_down = False
        coordinator._reconnect_suspended = False
        coordinator._reconnect_delay = lambda _attempt: 30.0

        sleep_started = asyncio.Event()

        async def blocking_sleep(_delay: float) -> None:
            sleep_started.set()
            await asyncio.Future()

        monkeypatch.setattr(asyncio, "sleep", blocking_sleep)

        class Gateway:
            def __init__(self) -> None:
                self.status = SimpleNamespace(connected=False)
                self.start_pending = False
                self.calls = []

            async def async_stop(self) -> None:
                self.calls.append("stop")

            async def async_start(self) -> None:
                self.calls.append("start")

            async def _emit_error(self, _error: str) -> None:
                return None

        gateway = Gateway()
        coordinator.gateways = {"gateway-1": gateway}
        reconnect_task = asyncio.create_task(coordinator._delayed_reconnect("gateway-1"))
        coordinator._reconnect_tasks = {"gateway-1": reconnect_task}

        await sleep_started.wait()
        coordinator._shutting_down = True
        coordinator._reconnect_suspended = True
        await coordinator._cancel_reconnect_tasks()

        assert reconnect_task.cancelled()
        assert coordinator._reconnect_tasks == {}
        assert gateway.calls == []

    asyncio.run(run())


def test_verified_meshcore_pin_update_is_saved_without_losing_entry_options(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

        def assert_marker_armed(_entry, *, options):
            assert coordinator._connection_update_reload_options == options
            asyncio.get_running_loop().call_soon(
                coordinator.consume_connection_update_reload,
                options,
            )
            return True

        update_entry = Mock(side_effect=assert_marker_armed)
        gateway_config = GatewayConfig(
            gateway_id="gateway-1",
            name="Local radio",
            protocol=PROTOCOL_MESHCORE,
            transport=TRANSPORT_BLUETOOTH,
            options={"existing": "preserved"},
        )
        coordinator = object.__new__(coordinator_class)
        coordinator.entry = SimpleNamespace(
            options={
                "unrelated": 7,
                CONF_GATEWAYS: [
                    {
                        "gateway_id": "gateway-1",
                        "name": "Local radio",
                        "protocol": PROTOCOL_MESHCORE,
                        "transport": TRANSPORT_BLUETOOTH,
                        "options": {"existing": "preserved", "pin": "123456"},
                    },
                    {
                        "gateway_id": "gateway-2",
                        "protocol": PROTOCOL_MESHTASTIC,
                        "transport": TRANSPORT_TCP,
                    },
                ],
            },
            data={"untouched": True},
        )
        coordinator.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update_entry)
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(config=gateway_config)
        }

        await coordinator.async_persist_gateway_connection_updates(
            "gateway-1", {"pin": "654321"}
        )

        update_entry.assert_called_once()
        args, kwargs = update_entry.call_args
        assert args == (coordinator.entry,)
        assert kwargs["options"]["unrelated"] == 7
        saved = kwargs["options"][CONF_GATEWAYS]
        assert saved[0]["options"] == {
            "existing": "preserved",
            "pin": "654321",
        }
        assert saved[1]["gateway_id"] == "gateway-2"
        assert gateway_config.options == {
            "existing": "preserved",
            "pin": "654321",
        }
        assert coordinator._connection_update_reload_options is None
        assert coordinator._connection_update_reload_waiter is None
        assert not coordinator.consume_connection_update_reload(
            kwargs["options"]
        )

    asyncio.run(run())


def test_meshcore_pin_clear_uses_data_fallback_and_rejects_bad_updates(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        update_entry = Mock(return_value=False)
        gateway_config = GatewayConfig(
            gateway_id="gateway-1",
            name="Local radio",
            protocol=PROTOCOL_MESHCORE,
            transport=TRANSPORT_BLUETOOTH,
            options={"pin": "123456"},
        )
        coordinator = object.__new__(coordinator_class)
        coordinator.entry = SimpleNamespace(
            options={"unrelated": True},
            data={
                CONF_GATEWAYS: [
                    {
                        "gateway_id": "gateway-1",
                        "protocol": PROTOCOL_MESHCORE,
                        "transport": TRANSPORT_BLUETOOTH,
                        "options": {"pin": "123456"},
                    }
                ]
            },
        )
        coordinator.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update_entry)
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(config=gateway_config)
        }

        await coordinator.async_persist_gateway_connection_updates(
            "gateway-1", {"pin": None}
        )
        saved = update_entry.call_args.kwargs["options"]
        assert saved["unrelated"] is True
        assert "options" not in saved[CONF_GATEWAYS][0]
        assert "pin" not in gateway_config.options
        assert coordinator._connection_update_reload_options is None

        for invalid in ({"pin": "12345"}, {"password": "654321"}):
            try:
                await coordinator.async_persist_gateway_connection_updates(
                    "gateway-1", invalid
                )
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe connection update was accepted")
        assert update_entry.call_count == 1

    asyncio.run(run())


def test_connection_update_reload_marker_is_invalidated_by_mismatch(
    monkeypatch,
) -> None:
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    coordinator._connection_update_reload_options = {
        "unrelated": 7,
        CONF_GATEWAYS: [{"gateway_id": "gateway-1"}],
    }

    assert not coordinator.consume_connection_update_reload(
        {
            "unrelated": 8,
            CONF_GATEWAYS: [{"gateway_id": "gateway-1"}],
        }
    )
    assert coordinator._connection_update_reload_options is None
    assert not coordinator.consume_connection_update_reload(
        {
            "unrelated": 7,
            CONF_GATEWAYS: [{"gateway_id": "gateway-1"}],
        }
    )


def test_concurrent_connection_updates_wait_for_each_listener_ack(
    monkeypatch,
) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        gateways = [
            {
                "gateway_id": gateway_id,
                "protocol": PROTOCOL_MESHCORE,
                "transport": TRANSPORT_BLUETOOTH,
                "options": {"pin": "123456"},
            }
            for gateway_id in ("gateway-1", "gateway-2")
        ]
        coordinator.entry = SimpleNamespace(
            options={CONF_GATEWAYS: gateways},
            data={},
        )
        configs = {
            gateway_id: GatewayConfig(
                gateway_id=gateway_id,
                name=gateway_id,
                protocol=PROTOCOL_MESHCORE,
                transport=TRANSPORT_BLUETOOTH,
                options={"pin": "123456"},
            )
            for gateway_id in ("gateway-1", "gateway-2")
        }
        coordinator.gateways = {
            key: SimpleNamespace(config=value) for key, value in configs.items()
        }
        pending_options: list[dict] = []
        second_update = asyncio.Event()

        def update_entry(entry, *, options):
            entry.options = options
            pending_options.append(options)
            if len(pending_options) == 2:
                second_update.set()
            return True

        coordinator.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update_entry)
        )

        first = asyncio.create_task(
            coordinator.async_persist_gateway_connection_updates(
                "gateway-1", {"pin": "654321"}
            )
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            coordinator.async_persist_gateway_connection_updates(
                "gateway-2", {"pin": "765432"}
            )
        )
        await asyncio.sleep(0)
        assert len(pending_options) == 1

        assert coordinator.consume_connection_update_reload(
            pending_options[0]
        )
        await asyncio.wait_for(second_update.wait(), timeout=1)
        assert len(pending_options) == 2
        assert coordinator.consume_connection_update_reload(
            pending_options[1]
        )
        await asyncio.gather(first, second)

        saved = {
            item["gateway_id"]: item["options"]["pin"]
            for item in coordinator.entry.options[CONF_GATEWAYS]
        }
        assert saved == {
            "gateway-1": "654321",
            "gateway-2": "765432",
        }
        assert configs["gateway-1"].options["pin"] == "654321"
        assert configs["gateway-2"].options["pin"] == "765432"

    asyncio.run(run())


def test_connection_update_failure_clears_reload_marker(monkeypatch) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

        def fail_update_entry(_entry, **_values):
            raise RuntimeError("config entry update failed")

        gateway_config = GatewayConfig(
            gateway_id="gateway-1",
            name="Local radio",
            protocol=PROTOCOL_MESHCORE,
            transport=TRANSPORT_BLUETOOTH,
            options={"pin": "123456"},
        )
        coordinator = object.__new__(coordinator_class)
        coordinator.entry = SimpleNamespace(
            options={
                CONF_GATEWAYS: [
                    {
                        "gateway_id": "gateway-1",
                        "protocol": PROTOCOL_MESHCORE,
                        "transport": TRANSPORT_BLUETOOTH,
                        "options": {"pin": "123456"},
                    }
                ]
            },
            data={},
        )
        coordinator.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=fail_update_entry
            )
        )
        coordinator.gateways = {
            "gateway-1": SimpleNamespace(config=gateway_config)
        }

        try:
            await coordinator.async_persist_gateway_connection_updates(
                "gateway-1", {"pin": "654321"}
            )
        except RuntimeError as err:
            assert str(err) == "config entry update failed"
        else:
            raise AssertionError("config entry failure was suppressed")

        assert coordinator._connection_update_reload_options is None
        assert coordinator._connection_update_reload_waiter is None
        assert gateway_config.options["pin"] == "123456"

    asyncio.run(run())


def _window_store():
    class Store:
        def __init__(self) -> None:
            self.deleted: list[list[str]] = []
            self.cached: dict[str, NodeState] = {}

        async def async_open(self) -> None:
            return None

        async def async_load_snapshot(self, *, recent_limit: int):
            assert recent_limit == 100
            return MeshSnapshot(nodes=dict(self.cached))

        async def async_upsert_node(self, node: NodeState) -> None:
            return None

        async def async_delete_nodes(self, node_keys) -> int:
            keys = list(node_keys)
            self.deleted.append(keys)
            return len(keys)

    return Store()


def _window_coordinator(coordinator_class, store):
    coordinator = object.__new__(coordinator_class)
    coordinator.store = store
    coordinator.snapshot = MeshSnapshot()
    coordinator.node_limit = 25
    coordinator._session_observed_node_keys = set()
    coordinator._rebuild_gateways = AsyncMock()
    coordinator._gateway_generation = 1
    coordinator._shutting_down = False
    coordinator._reconnect_suspended = False
    coordinator.async_set_updated_data = lambda _snapshot: None
    coordinator.gateways = {}
    return coordinator


def _heard_node(index: int, heard: datetime | None) -> NodeState:
    return NodeState(
        node_key=f"meshtastic:!000000{index:02x}",
        protocol=PROTOCOL_MESHTASTIC,
        node_id=f"!000000{index:02x}",
        last_heard=heard,
        online=heard is not None,
    )


def _coordinator_module(monkeypatch):
    """Return the coordinator module under either the real or shimmed HA."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    return importlib.import_module(coordinator_class.__module__)


def test_bounded_node_limit_clamps_to_supported_range(monkeypatch) -> None:
    module = _coordinator_module(monkeypatch)
    assert module.bounded_node_limit(None) == 250
    assert module.bounded_node_limit("abc") == 250
    assert module.bounded_node_limit(True) == 250
    assert module.bounded_node_limit(5) == 25
    assert module.bounded_node_limit("400") == 400
    assert module.bounded_node_limit(10_000) == 1000


def test_rolling_node_window_evicts_least_recent_and_protects_gateway_node(
    monkeypatch,
) -> None:
    """Keep the cache bounded without ever dropping the radio's own record."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        store = _window_store()
        coordinator = _window_coordinator(coordinator_class, store)
        coordinator.gateways = {"g1": SimpleNamespace(local_node_id="!000000ff")}
        removed: list[set[str]] = []
        unsubscribe = coordinator.async_add_node_removed_listener(
            lambda keys: removed.append(set(keys))
        )
        await coordinator._async_setup()
        now = datetime.now(UTC)

        for index in range(1, 26):
            await coordinator._handle_node(
                _heard_node(index, now - timedelta(minutes=index)), gateway_generation=1
            )
        assert len(coordinator._raw_nodes) == 25
        assert store.deleted == []

        own_radio = NodeState(
            node_key="meshtastic:!000000ff",
            protocol=PROTOCOL_MESHTASTIC,
            node_id="!000000ff",
            last_heard=now - timedelta(days=30),
        )
        await coordinator._handle_node(own_radio, gateway_generation=1)
        # The oldest ordinary node leaves; the gateway's own very old record stays.
        assert store.deleted == [["meshtastic:!00000019"]]
        assert "meshtastic:!000000ff" in coordinator._raw_nodes
        assert removed == [{"meshtastic:!00000019"}]

        never_heard = _heard_node(0x40, None)
        await coordinator._handle_node(never_heard, gateway_generation=1)
        # The record being updated is protected, so the next-oldest heard goes.
        assert store.deleted[-1] == ["meshtastic:!00000018"]

        await coordinator._handle_node(_heard_node(0x41, now), gateway_generation=1)
        # A never-heard record is evicted before any heard record.
        assert store.deleted[-1] == ["meshtastic:!00000040"]
        assert "meshtastic:!00000040" not in coordinator._session_observed_node_keys
        assert len(coordinator._raw_nodes) == 25
        assert len(coordinator.snapshot.nodes) == 25

        diagnostics = coordinator.node_window_diagnostics()
        assert diagnostics["node_limit"] == 25
        assert diagnostics["evicted"] == 3
        assert diagnostics["retained_raw_records"] == 25

        unsubscribe()
        await coordinator._handle_node(_heard_node(0x42, now), gateway_generation=1)
        assert len(removed) == 3

    asyncio.run(run())


def test_rolling_node_window_trims_cached_records_on_startup(monkeypatch) -> None:
    """A previously unbounded cache is cut back before any entity is created."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        store = _window_store()
        now = datetime.now(UTC)
        for index in range(1, 31):
            node = _heard_node(index, now - timedelta(hours=index))
            store.cached[node.node_key] = node
        coordinator = _window_coordinator(coordinator_class, store)
        removed: list[set[str]] = []
        coordinator.async_add_node_removed_listener(lambda keys: removed.append(set(keys)))

        await coordinator._async_setup()

        assert len(coordinator._raw_nodes) == 25
        assert len(coordinator.snapshot.nodes) == 25
        expected = [f"meshtastic:!000000{index:02x}" for index in range(26, 31)]
        assert store.deleted == [sorted(expected, key=expected.index)] or set(store.deleted[0]) == set(expected)
        assert removed == [set(expected)]
        assert coordinator.node_window_diagnostics()["startup_trimmed"] == 5
        assert coordinator.node_observability_diagnostics()["node_counts"]["total"] == 25

    asyncio.run(run())


def test_message_conversation_key_matches_panel_grouping(monkeypatch) -> None:
    module = _coordinator_module(monkeypatch)
    key = module.message_conversation_key

    def record(**overrides):
        base = {
            "message_id": "m",
            "protocol": PROTOCOL_MESHTASTIC,
            "gateway_id": "g1",
            "sender": "!aaaa0001",
            "receiver": "^all",
            "channel": "0",
            "text": "x",
        }
        base.update(overrides)
        return MessageRecord(**base)

    assert key(record()) == "broadcast:0"
    assert key(record(channel="3")) == "channel:3"
    assert key(record(receiver="!aaaa0003")) == "direct:meshtastic:!aaaa0001"
    assert key(record(receiver="!aaaa0002", direction="tx")) == "direct:meshtastic:!aaaa0002"
    # An unsupported channel index with a broadcast receiver is still shown as
    # the primary broadcast thread by the panel, so the server agrees.
    assert key(record(channel="9")) == "broadcast:0"
    assert key(record(receiver="somebody")) == "unknown"
    assert module.conversation_key_is_valid("broadcast:0")
    assert module.conversation_key_is_valid("channel:7")
    assert module.conversation_key_is_valid("direct:meshtastic:!aaaa0001")
    assert module.conversation_key_is_valid("unknown")
    assert not module.conversation_key_is_valid("channel:0")
    assert not module.conversation_key_is_valid("direct:")
    assert not module.conversation_key_is_valid("direct:has space")
    assert not module.conversation_key_is_valid(None)


def test_delete_message_history_scopes_and_keeps_in_flight_sends(monkeypatch) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)

        def record(message_id: str, **overrides):
            base = {
                "message_id": message_id,
                "protocol": PROTOCOL_MESHTASTIC,
                "gateway_id": "g1",
                "sender": "!aaaa0001",
                "receiver": "^all",
                "channel": "0",
                "text": "x",
            }
            base.update(overrides)
            return MessageRecord(**base)

        class Store:
            def __init__(self) -> None:
                self.messages = [
                    record("broadcast-1"),
                    record("direct-rx", sender="!aaaa0002", receiver="!aaaa0003"),
                    record("direct-tx", sender=None, receiver="!aaaa0002", direction="tx"),
                    record("active", sender=None, direction="tx"),
                ]
                self.predicates: list[object] = []

            async def async_delete_message_history(self, predicate) -> int:
                self.predicates.append(predicate)
                if predicate is None:
                    count = len(self.messages)
                    self.messages.clear()
                    return count
                kept = [message for message in self.messages if not predicate(message)]
                deleted = len(self.messages) - len(kept)
                self.messages[:] = kept
                return deleted

            async def async_recent_messages(self, limit: int):
                return list(self.messages)[-limit:]

            async def async_messages_since(self, _when) -> int:
                return len(self.messages)

        store = Store()
        coordinator = object.__new__(coordinator_class)
        coordinator.store = store
        coordinator.snapshot = MeshSnapshot()
        coordinator._active_send_message_ids = {"active"}
        updates: list[object] = []
        coordinator.async_set_updated_data = lambda snapshot: updates.append(snapshot)

        result = await coordinator.async_delete_message_history(
            scope="conversation", conversation_key="direct:meshtastic:!aaaa0002"
        )
        assert result == {
            "schema_version": 1,
            "scope": "conversation",
            "conversation_key": "direct:meshtastic:!aaaa0002",
            "deleted_messages": 2,
            "retained_active": 0,
        }
        assert [message.message_id for message in store.messages] == ["broadcast-1", "active"]
        assert [message.message_id for message in coordinator.snapshot.recent_messages] == [
            "broadcast-1",
            "active",
        ]
        assert coordinator.snapshot.messages_today == 2

        result = await coordinator.async_delete_message_history(scope="all", conversation_key="ignored")
        assert result["scope"] == "all"
        assert result["conversation_key"] is None
        assert result["deleted_messages"] == 1
        assert result["retained_active"] == 1
        assert [message.message_id for message in store.messages] == ["active"]
        assert store.predicates[-1] is not None

        coordinator._active_send_message_ids = set()
        result = await coordinator.async_delete_message_history(scope="all")
        assert result["deleted_messages"] == 1
        assert store.predicates[-1] is None
        assert len(updates) == 3

        with pytest.raises(ValueError):
            await coordinator.async_delete_message_history(scope="everything")
        with pytest.raises(ValueError):
            await coordinator.async_delete_message_history(scope="conversation")
        with pytest.raises(ValueError):
            await coordinator.async_delete_message_history(
                scope="conversation", conversation_key="direct:"
            )

    asyncio.run(run())


def _budget_node(index: int, heard: datetime | None) -> NodeState:
    return NodeState(
        node_key=f"meshtastic:!{index:08x}",
        protocol=PROTOCOL_MESHTASTIC,
        node_id=f"!{index:08x}",
        last_heard=heard,
        online=heard is not None,
    )


def test_entity_budget_math_scales_cached_nodes_to_the_remaining_budget(monkeypatch) -> None:
    module = _coordinator_module(monkeypatch)
    assert module.entity_cap() == 10_000
    assert module.entity_budget(10_000) == 9_000
    assert module.entity_budget(1_500) == 750
    ceiling = module.entity_budget_node_ceiling
    assert ceiling(9_100, 350, budget=9_000) == 346
    # One more node still fits comfortably, so the configured window applies.
    assert ceiling(6_500, 250, budget=9_000) is None
    assert ceiling(39, 1, budget=9_000) is None
    # The next node would not fit: hold the window at its current size.
    assert ceiling(8_990, 345, budget=9_000) == 345
    assert ceiling(0, 250, budget=9_000) is None
    assert ceiling(100, 0, budget=9_000) is None
    assert ceiling(True, 10, budget=9_000) is None
    assert ceiling("9000", 10, budget=9_000) is None


def test_entity_budget_evicts_nodes_before_home_assistant_stops_adding_entities(
    monkeypatch, caplog
) -> None:
    """Hold the window below node_limit when the registry nears the entity cap."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        module = importlib.import_module(coordinator_class.__module__)
        issues: dict[str, dict] = {}
        monkeypatch.setattr(
            module.ir,
            "async_create_issue",
            lambda hass, domain, issue_id, **kwargs: issues.__setitem__(issue_id, kwargs),
            raising=False,
        )
        monkeypatch.setattr(
            module.ir,
            "async_delete_issue",
            lambda hass, domain, issue_id: issues.pop(issue_id, None),
            raising=False,
        )
        store = _window_store()
        coordinator = _window_coordinator(coordinator_class, store)
        coordinator.node_limit = 1000
        coordinator.hass = SimpleNamespace()
        cost = {"per_node": 26}
        # Simulates Home Assistant's registry counter: fixed summary/gateway
        # entities plus a per-node cost for every cached record.
        coordinator._enabled_entity_count = lambda: 13 + cost["per_node"] * len(coordinator._raw_nodes)
        removed: list[set[str]] = []
        coordinator.async_add_node_removed_listener(lambda keys: removed.append(set(keys)))
        await coordinator._async_setup()
        assert coordinator._effective_node_limit() == 1000
        now = datetime.now(UTC)

        for index in range(1, 400):
            await coordinator._handle_node(
                _budget_node(index, now - timedelta(seconds=1000 - index)), gateway_generation=1
            )
            if index == 50:
                # Far from the cap nothing binds and no repair is raised.
                assert coordinator._effective_node_limit() == 1000
                assert issues == {}
                assert coordinator.node_window_diagnostics()["entity_budget"]["node_ceiling"] is None

        retained = len(coordinator._raw_nodes)
        assert retained < 399
        assert retained == coordinator._effective_node_limit()
        assert coordinator._enabled_entity_count() <= 9_000 + cost["per_node"]
        assert coordinator._enabled_entity_count() >= 9_000 - 2 * cost["per_node"]
        # The newest records survive; the oldest were evicted.
        assert _budget_node(399, now).node_key in coordinator._raw_nodes
        assert _budget_node(1, now).node_key not in coordinator._raw_nodes
        assert removed and all(len(keys) == 1 for keys in removed)

        diagnostics = coordinator.node_window_diagnostics()
        assert diagnostics["node_limit"] == 1000
        assert diagnostics["effective_node_limit"] == retained
        assert diagnostics["evicted"] == diagnostics["budget_evicted"] > 0
        assert diagnostics["cleanup_pending"] is False
        budget = diagnostics["entity_budget"]
        assert budget["available"] is True
        assert budget["binding"] is True
        assert budget["entity_cap"] == 10_000
        assert budget["budget"] == 9_000
        assert budget["node_ceiling"] == retained
        assert budget["checks"] == 400
        assert budget["last_checked_at"]
        issue = issues["entity_budget"]
        assert issue["translation_key"] == "entity_budget"
        assert issue["is_fixable"] is False
        placeholders = issue["translation_placeholders"]
        assert placeholders["effective_limit"] == str(retained)
        assert placeholders["configured_limit"] == "1000"
        assert placeholders["entity_cap"] == "10000"
        assert int(placeholders["enabled_entities"]) > 8_000
        # The guard stays quiet while the mesh is small.
        assert not any("instead of the configured" in record.message for record in caplog.records[:1])

        # While evicted devices are still being removed the registry count is
        # stale, so the budget is not re-measured and nothing extra is evicted.
        coordinator._node_cleanup_pending = 1
        assert coordinator.node_cleanup_pending() is True
        assert coordinator._refresh_entity_budget() is False
        assert await coordinator._async_enforce_entity_budget() == 0
        assert coordinator.node_window_diagnostics()["entity_budget"]["skipped_during_cleanup"] == 2
        coordinator._node_cleanup_pending = 0

        # Telemetry growth without any new node is caught by the refresh check.
        cost["per_node"] = 40
        evicted = await coordinator._async_enforce_entity_budget()
        assert evicted > 0
        assert len(coordinator._raw_nodes) == coordinator._effective_node_limit() < retained
        assert coordinator._enabled_entity_count() <= 9_000 + cost["per_node"]
        assert store.deleted[-1] and len(store.deleted[-1]) == evicted
        assert len(coordinator.snapshot.nodes) == len(coordinator._raw_nodes)

        # Fewer enabled entities per node lets the configured window apply again.
        cost["per_node"] = 5
        await coordinator._handle_node(_budget_node(900, now), gateway_generation=1)
        assert coordinator._effective_node_limit() == 1000
        assert "entity_budget" not in issues
        assert coordinator.node_window_diagnostics()["entity_budget"]["binding"] is False

        # A registry flooded by stale entries never collapses the window below
        # the smallest supported node_limit.
        cost["per_node"] = 5_000
        await coordinator._handle_node(_budget_node(901, now), gateway_generation=1)
        assert coordinator._effective_node_limit() == 25
        assert len(coordinator._raw_nodes) == 25
        assert issues["entity_budget"]["translation_placeholders"]["effective_limit"] == "25"

    asyncio.run(run())


def test_entity_budget_is_inactive_without_a_registry_count(monkeypatch) -> None:
    """Without Home Assistant's counter only the configured window applies."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        store = _window_store()
        coordinator = _window_coordinator(coordinator_class, store)
        await coordinator._async_setup()
        now = datetime.now(UTC)
        for index in range(1, 40):
            await coordinator._handle_node(_budget_node(index, now), gateway_generation=1)
        assert len(coordinator._raw_nodes) == 25
        assert await coordinator._async_enforce_entity_budget() == 0
        budget = coordinator.node_window_diagnostics()["entity_budget"]
        assert budget["available"] is False
        assert budget["binding"] is False
        assert budget["node_ceiling"] is None
        assert budget["enabled_entities"] is None
        assert coordinator.node_window_diagnostics()["budget_evicted"] == 0

    asyncio.run(run())


def _reboot_gateway(gateway_id: str, *, transport: str = "bluetooth", connected: bool = True, protocol: str = "meshtastic"):
    from custom_components.meshnet.models import GatewayConfig, GatewayStatus

    return SimpleNamespace(
        config=GatewayConfig(gateway_id=gateway_id, name="Radio", protocol=protocol, transport=transport),
        status=GatewayStatus(gateway_id=gateway_id, name="Radio", protocol=protocol, transport=transport, connected=connected),
        async_reboot_radio=AsyncMock(return_value={"status": "acknowledged", "delay_seconds": 5, "scope": "local"}),
    )


def test_reboot_gateway_radio_targets_one_connected_bluetooth_gateway(monkeypatch) -> None:
    """The service path reboots only the gateway's own radio and counts outcomes."""

    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        module = importlib.import_module(coordinator_class.__module__)
        coordinator = object.__new__(coordinator_class)
        radio = _reboot_gateway("ble-1")
        coordinator.gateways = {"ble-1": radio}

        result = await coordinator.async_reboot_gateway_radio()
        assert result == {"schema_version": 1, "gateway_id": "ble-1", "status": "acknowledged", "delay_seconds": 5}
        radio.async_reboot_radio.assert_awaited_once_with(delay_seconds=5)

        result = await coordinator.async_reboot_gateway_radio("ble-1", delay_seconds=30)
        assert result["delay_seconds"] == 30
        assert radio.async_reboot_radio.await_args.kwargs == {"delay_seconds": 30}

        radio.async_reboot_radio = AsyncMock(return_value={"status": "unconfirmed"})
        assert (await coordinator.async_reboot_gateway_radio())["status"] == "unconfirmed"

        for bad in (0, 301, True, "5"):
            with pytest.raises(module.HomeAssistantError):
                await coordinator.async_reboot_gateway_radio(delay_seconds=bad)  # type: ignore[arg-type]
        with pytest.raises(module.HomeAssistantError):
            await coordinator.async_reboot_gateway_radio("missing")

        coordinator.gateways["serial-1"] = _reboot_gateway("serial-1", transport="serial")
        assert (await coordinator.async_reboot_gateway_radio())["gateway_id"] == "ble-1"
        with pytest.raises(module.HomeAssistantError):
            await coordinator.async_reboot_gateway_radio("serial-1")

        coordinator.gateways["ble-2"] = _reboot_gateway("ble-2", connected=False)
        with pytest.raises(module.HomeAssistantError):
            await coordinator.async_reboot_gateway_radio()
        with pytest.raises(module.HomeAssistantError):
            await coordinator.async_reboot_gateway_radio("ble-2")

        coordinator.gateways.pop("ble-2")
        radio.async_reboot_radio = AsyncMock(side_effect=RuntimeError("bluetooth write failed at AA:BB"))
        with pytest.raises(module.HomeAssistantError) as failure:
            await coordinator.async_reboot_gateway_radio()
        assert "AA:BB" not in str(failure.value)

        coordinator._radio_operations_accepting = False
        with pytest.raises(module.HomeAssistantError):
            await coordinator.async_reboot_gateway_radio()

        diagnostics = coordinator.radio_reboot_diagnostics()
        assert diagnostics["requests"] == 14
        assert diagnostics["acknowledged"] == 2
        assert diagnostics["unconfirmed"] == 2
        assert diagnostics["failed"] == 1
        assert diagnostics["rejected"] == 9
        assert diagnostics["remote_requests"] == 0

    asyncio.run(run())


def test_remote_reboot_counts_outcomes_and_delegates_to_the_manager(monkeypatch) -> None:
    async def run() -> None:
        coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
        coordinator = object.__new__(coordinator_class)
        coordinator.remote_admin = SimpleNamespace(
            async_reboot=AsyncMock(return_value={"schema_version": 1, "status": "acknowledged"})
        )
        result = await coordinator.async_remote_reboot(gateway_id="g", target_node="!1234abcd", confirm_remote=True)
        assert result["status"] == "acknowledged"
        coordinator.remote_admin.async_reboot.assert_awaited_once_with("g", "!1234abcd", confirm_remote=True)
        coordinator.remote_admin.async_reboot = AsyncMock(side_effect=RuntimeError("nope"))
        with pytest.raises(RuntimeError):
            await coordinator.async_remote_reboot(gateway_id="g", target_node="!1234abcd", confirm_remote=True)
        diagnostics = coordinator.radio_reboot_diagnostics()
        assert diagnostics["remote_requests"] == 2
        assert diagnostics["remote_acknowledged"] == 1
        assert diagnostics["remote_failed"] == 1

    asyncio.run(run())


def test_gateway_start_repair_text_tells_the_user_what_to_do(monkeypatch) -> None:
    """The repair separates an unreachable radio from one that refuses connections."""
    coordinator_class = _load_coordinator_without_home_assistant(monkeypatch)
    coordinator = object.__new__(coordinator_class)
    coordinator.gateways = {}
    coordinator._gateway_configs = []

    def gateway(context):
        return SimpleNamespace(
            config=SimpleNamespace(gateway_id="secret-gateway-id"),
            start_failure_context=lambda: context,
        )

    error = RuntimeError("Bluetooth connect to AA:BB:CC timed out")
    refusing = coordinator._gateway_start_failure_message(
        gateway(
            {
                "transport": "bluetooth",
                "phase": "bluetooth_connecting",
                "error_subtype": "TimeoutError",
                "resolution_result": "matched_verified_local_adapter",
                "adapter_validation": "passed",
            }
        ),
        error,
    )
    assert refusing.startswith("Gateway adapter unknown failed to start (bluetooth; RuntimeError).")
    assert "advertising, so it is powered and in range" in refusing
    assert "reset button or power-cycle it" in refusing
    assert "secret-gateway-id" not in refusing
    assert "AA:BB:CC" not in refusing

    absent = coordinator._gateway_start_failure_message(
        gateway({"transport": "bluetooth", "phase": "bluetooth_resolving_device", "adapter_validation": "passed"}),
        error,
    )
    assert "not advertising" in absent
    assert "powered off, out of battery, out of range" in absent

    stale = coordinator._gateway_start_failure_message(
        gateway({"transport": "bluetooth", "phase": "bluetooth_requesting_configuration", "adapter_validation": "passed"}),
        error,
    )
    assert "never finished its configuration exchange" in stale
    assert "Remove gateway with bond removal" in stale

    host = coordinator._gateway_start_failure_message(
        gateway({"transport": "bluetooth", "phase": "validating_bluetooth_adapter", "adapter_validation": "failed"}),
        error,
    )
    assert "saved Bluetooth adapter on the Home Assistant host" in host

    serial = coordinator._gateway_start_failure_message(gateway({"transport": "serial", "phase": "constructing_interface"}), error)
    assert "/dev/serial/by-id" in serial

    plain = coordinator._gateway_start_failure_message(
        SimpleNamespace(config=SimpleNamespace(gateway_id="meshcore")), error
    )
    assert plain == "Gateway adapter unknown failed to start (bluetooth; RuntimeError)."

    broken = coordinator._gateway_start_failure_message(
        SimpleNamespace(config=SimpleNamespace(gateway_id="x"), start_failure_context=lambda: 1 / 0), error
    )
    assert broken == plain
