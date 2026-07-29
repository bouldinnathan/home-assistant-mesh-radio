from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.meshnet.models import NodeState
from custom_components.meshnet.node_identity import (
    meshtastic_identity_is_valid,
    meshtastic_observation_node_key,
    project_effective_nodes,
)


def _time(hour: int) -> datetime:
    return datetime(2026, 7, 28, hour, tzinfo=UTC)


def test_safe_alias_group_is_collapsed_without_mutating_raw_nodes() -> None:
    named = NodeState(
        node_key="mac:aabbccddeeff",
        protocol="meshtastic",
        node_id="!12345678",
        mac="AA:BB:CC:DD:EE:FF",
        long_name="Hill Repeater",
        short_name="HR",
        hardware_model="RAK4631",
        online=False,
        last_heard=_time(10),
        last_gateway_id="gateway-old",
        gateway_ids={"gateway-old"},
        connectivity={
            "hops": 2,
            "hops_gateway_id": "gateway-old",
            "via_mqtt": False,
        },
        location={"latitude": 40.0, "longitude": -88.0},
    )
    decimal = NodeState(
        node_key="meshtastic:305419896",
        protocol="meshtastic",
        node_id="305419896",
        online=True,
        last_heard=_time(12),
        last_gateway_id="gateway-new",
        gateway_ids={"gateway-new"},
        connectivity={
            "hops": 0,
            "hops_gateway_id": None,
            "via_mqtt": True,
        },
        power={"battery_level": 80},
    )
    hexadecimal = NodeState(
        node_key="meshtastic:!12345678",
        protocol="meshtastic",
        node_id="0x12345678",
        online=False,
        last_heard=_time(11),
        connectivity={"hops": 1, "via_mqtt": False},
    )
    raw = {
        named.node_key: named,
        decimal.node_key: decimal,
        hexadecimal.node_key: hexadecimal,
    }
    before = {key: node.as_dict() for key, node in raw.items()}

    projection = project_effective_nodes(raw)

    assert set(projection.nodes) == {hexadecimal.node_key}
    assert projection.redirects == {
        named.node_key: hexadecimal.node_key,
        decimal.node_key: hexadecimal.node_key,
        hexadecimal.node_key: hexadecimal.node_key,
    }
    node = projection.nodes[hexadecimal.node_key]
    assert node is not hexadecimal
    assert node.node_id == "!12345678"
    assert node.mac == "aabbccddeeff"
    assert node.long_name == "Hill Repeater"
    assert node.short_name == "HR"
    assert node.hardware_model == "RAK4631"
    assert node.online is True
    assert node.last_heard == _time(12)
    assert node.last_gateway_id == "gateway-new"
    assert node.gateway_ids == {"gateway-old", "gateway-new"}
    assert node.connectivity == decimal.connectivity
    assert node.connectivity is not decimal.connectivity
    assert node.location == named.location
    assert node.power == decimal.power
    assert {key: value.as_dict() for key, value in raw.items()} == before
    assert projection.stats == {
        "raw_record_count": 3,
        "effective_node_count": 1,
        "collapsed_alias_record_count": 2,
        "candidate_identity_group_count": 1,
        "resolved_identity_group_count": 1,
        "unresolved_identity_group_count": 0,
        "unresolved_identity_record_count": 0,
        "invalid_identity_record_count": 0,
    }


def test_conflicting_mac_proofs_fail_closed() -> None:
    left = NodeState(
        node_key="mac:111111111111",
        protocol="meshtastic",
        node_id="!22222222",
        mac="11:11:11:11:11:11",
    )
    right = NodeState(
        node_key="meshtastic:!22222222",
        protocol="meshtastic",
        node_id="572662306",
        mac="22:22:22:22:22:22",
    )

    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )

    assert set(projection.nodes) == {left.node_key, right.node_key}
    assert projection.redirects == {
        left.node_key: left.node_key,
        right.node_key: right.node_key,
    }
    assert projection.stats["resolved_identity_group_count"] == 0
    assert projection.stats["unresolved_identity_group_count"] == 1
    assert projection.stats["unresolved_identity_record_count"] == 2
    assert projection.stats["collapsed_alias_record_count"] == 0


def test_conflicting_public_keys_fail_closed() -> None:
    left = NodeState(
        node_key=f"pub:{'11' * 32}",
        protocol="meshtastic",
        node_id="!33333333",
        public_key="11" * 32,
    )
    right = NodeState(
        node_key="meshtastic:!33333333",
        protocol="meshtastic",
        node_id="858993459",
        public_key="22" * 32,
    )

    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )

    assert len(projection.nodes) == 2
    assert projection.stats["unresolved_identity_group_count"] == 1


def test_proof_key_never_ignores_a_malformed_present_public_key() -> None:
    node_id = "!33333334"
    mac = "333333333334"
    node = NodeState(
        node_key=meshtastic_observation_node_key(node_id, mac=mac),
        protocol="meshtastic",
        node_id=node_id,
        mac=mac,
        public_key="not-a-valid-32-byte-public-key",
    )

    projection = project_effective_nodes({node.node_key: node})

    assert not meshtastic_identity_is_valid(node.node_key, node)
    assert projection.unsafe_node_keys == {node.node_key}
    assert projection.stats["invalid_identity_record_count"] == 1


def test_public_key_proof_is_canonical_across_routing_ids() -> None:
    public_upper = "AB" * 32
    public_lower = public_upper.lower()
    left = NodeState(
        node_key=meshtastic_observation_node_key(
            "!33333335", public_key=public_upper
        ),
        protocol="meshtastic",
        node_id="!33333335",
        public_key=public_upper,
    )
    right = NodeState(
        node_key=meshtastic_observation_node_key(
            "!33333336", public_key=public_lower
        ),
        protocol="meshtastic",
        node_id="!33333336",
        public_key=public_lower,
    )

    projection = project_effective_nodes(
        {left.node_key: left, right.node_key: right}
    )

    assert meshtastic_identity_is_valid(left.node_key, left)
    assert meshtastic_identity_is_valid(right.node_key, right)
    assert projection.unsafe_node_keys == {left.node_key, right.node_key}


def test_protocol_comparison_is_whitespace_and_case_normalized() -> None:
    node = NodeState(
        node_key="meshtastic:!33333337",
        protocol="  MeShTaStIc  ",
        node_id="!33333337",
    )

    projection = project_effective_nodes({node.node_key: node})

    assert meshtastic_identity_is_valid(node.node_key, node)
    assert projection.unsafe_node_keys == frozenset()
    assert projection.stats["invalid_identity_record_count"] == 0


def test_complementary_unbound_proofs_fail_closed() -> None:
    """Never manufacture a MAC/public-key association from separate records."""
    node_id = "!34343434"
    mac_only = NodeState(
        node_key=meshtastic_observation_node_key(
            node_id, mac="34:34:34:34:34:34"
        ),
        protocol="meshtastic",
        node_id=node_id,
        mac="343434343434",
    )
    public_only = NodeState(
        node_key=meshtastic_observation_node_key(
            node_id, public_key="34" * 32
        ),
        protocol="meshtastic",
        node_id=node_id,
        public_key="34" * 32,
    )

    projection = project_effective_nodes(
        {
            mac_only.node_key: mac_only,
            public_only.node_key: public_only,
        }
    )

    assert meshtastic_identity_is_valid(mac_only.node_key, mac_only)
    assert meshtastic_identity_is_valid(public_only.node_key, public_only)
    assert set(projection.nodes) == {mac_only.node_key, public_only.node_key}
    assert projection.unsafe_node_keys == {
        mac_only.node_key,
        public_only.node_key,
    }
    assert projection.stats["resolved_identity_group_count"] == 0
    assert projection.stats["unresolved_identity_group_count"] == 1
    assert projection.stats["unresolved_identity_record_count"] == 2
    assert projection.stats["collapsed_alias_record_count"] == 0


def test_id_only_alias_can_merge_with_one_observed_proof_bundle() -> None:
    node_id = "!35353535"
    id_only = NodeState(
        node_key=f"meshtastic:{node_id}",
        protocol="meshtastic",
        node_id=node_id,
    )
    proven = NodeState(
        node_key=meshtastic_observation_node_key(
            node_id,
            mac="35:35:35:35:35:35",
            public_key="35" * 32,
        ),
        protocol="meshtastic",
        node_id=node_id,
        mac="353535353535",
        public_key="35" * 32,
    )

    projection = project_effective_nodes(
        {id_only.node_key: id_only, proven.node_key: proven}
    )

    assert set(projection.nodes) == {id_only.node_key}
    assert projection.redirects[proven.node_key] == id_only.node_key
    merged = projection.nodes[id_only.node_key]
    assert meshtastic_identity_is_valid(merged.node_key, merged)
    assert merged.mac == proven.mac
    assert merged.public_key == proven.public_key


def test_bound_complementary_proofs_use_the_combined_observation_key() -> None:
    node_id = "!36363636"
    mac = "363636363636"
    public_key = "36" * 32
    mac_only = NodeState(
        node_key=meshtastic_observation_node_key(node_id, mac=mac),
        protocol="meshtastic",
        node_id=node_id,
        mac=mac,
    )
    public_only = NodeState(
        node_key=meshtastic_observation_node_key(
            node_id, public_key=public_key
        ),
        protocol="meshtastic",
        node_id=node_id,
        public_key=public_key,
    )
    combined = NodeState(
        node_key=meshtastic_observation_node_key(
            node_id, mac=mac, public_key=public_key
        ),
        protocol="meshtastic",
        node_id=node_id,
        mac=mac,
        public_key=public_key,
    )

    projection = project_effective_nodes(
        {
            mac_only.node_key: mac_only,
            public_only.node_key: public_only,
            combined.node_key: combined,
        }
    )

    assert set(projection.nodes) == {combined.node_key}
    merged = projection.nodes[combined.node_key]
    assert meshtastic_identity_is_valid(merged.node_key, merged)
    assert projection.stats["collapsed_alias_record_count"] == 2
    assert projection.stats["unresolved_identity_group_count"] == 0


@pytest.mark.parametrize(
    ("node_key", "node_id", "mac", "public_key"),
    [
        ("meshtastic:!00000000", "!00000000", None, None),
        ("meshtastic:!ffffffff", "!ffffffff", None, None),
        ("meshtastic:4294967296", "4294967296", None, None),
        ("meshtastic:not-an-id", "not-an-id", None, None),
        ("meshtastic:!44444444", "!55555555", None, None),
        ("mac:111111111111", "!44444444", "22:22:22:22:22:22", None),
        ("pub:key-one", "!44444444", None, "key-two"),
        ("meshcore:!44444444", "!44444444", None, None),
    ],
)
def test_invalid_or_self_inconsistent_identity_is_never_grouped(
    node_key: str,
    node_id: str,
    mac: str | None,
    public_key: str | None,
) -> None:
    invalid = NodeState(
        node_key=node_key,
        protocol="meshtastic",
        node_id=node_id,
        mac=mac,
        public_key=public_key,
    )
    valid = NodeState(
        node_key="meshtastic:!44444444",
        protocol="meshtastic",
        node_id="1145324612",
    )
    raw = {invalid.node_key: invalid}
    if valid.node_key not in raw:
        raw[valid.node_key] = valid

    projection = project_effective_nodes(raw)

    assert set(projection.nodes) == set(raw)
    assert projection.redirects == {key: key for key in raw}
    assert projection.stats["collapsed_alias_record_count"] == 0
    assert projection.stats["invalid_identity_record_count"] == 1


def test_names_are_taken_from_one_coherent_donor() -> None:
    long_only = NodeState(
        node_key="mac:aaaaaaaaaaaa",
        protocol="meshtastic",
        node_id="!66666666",
        mac="aaaaaaaaaaaa",
        long_name="Never Combined",
        last_heard=_time(10),
    )
    short_only = NodeState(
        node_key="meshtastic:!66666666",
        protocol="meshtastic",
        node_id="1717986918",
        short_name="NC",
        last_heard=_time(11),
    )

    node = project_effective_nodes(
        {long_only.node_key: long_only, short_only.node_key: short_only}
    ).nodes[short_only.node_key]

    assert node.long_name is None
    assert node.short_name == "NC"


def test_changed_name_tuple_uses_one_deterministic_complete_donor() -> None:
    older = NodeState(
        node_key="mac:bbbbbbbbbbbb",
        protocol="meshtastic",
        node_id="!77777777",
        mac="bbbbbbbbbbbb",
        long_name="Old Name",
        short_name="OLD",
        last_heard=_time(10),
    )
    newer = NodeState(
        node_key="meshtastic:!77777777",
        protocol="meshtastic",
        node_id="2004318071",
        long_name="New Name",
        short_name="NEW",
        last_heard=_time(12),
    )

    node = project_effective_nodes(
        {older.node_key: older, newer.node_key: newer}
    ).nodes[newer.node_key]

    assert (node.long_name, node.short_name) == ("New Name", "NEW")


def test_projection_is_deterministic_and_does_not_group_other_protocols() -> None:
    meshtastic = [
        NodeState(
            node_key="meshtastic:2863311530",
            protocol="meshtastic",
            node_id="2863311530",
        ),
        NodeState(
            node_key="meshtastic:0xaaaaaaaa",
            protocol="meshtastic",
            node_id="0xaaaaaaaa",
        ),
    ]
    meshcore = NodeState(
        node_key="meshcore:!aaaaaaaa",
        protocol="meshcore",
        node_id="!aaaaaaaa",
    )
    forward = {
        meshtastic[0].node_key: meshtastic[0],
        meshtastic[1].node_key: meshtastic[1],
        meshcore.node_key: meshcore,
    }
    reverse = dict(reversed(list(forward.items())))

    first = project_effective_nodes(forward)
    second = project_effective_nodes(reverse)

    assert first.redirects == second.redirects
    assert first.stats == second.stats
    assert list(first.nodes) == list(second.nodes)
    assert meshcore.node_key in first.nodes
    assert len(first.nodes) == 2


def test_singletons_are_reused_without_mutation_and_keep_existing_keys() -> None:
    node = NodeState(
        node_key="mac:cccccccccccc",
        protocol="meshtastic",
        node_id="!88888888",
        mac="cccccccccccc",
        connectivity={"hops": 1, "via_mqtt": False},
    )

    projection = project_effective_nodes({node.node_key: node})

    projected = projection.nodes[node.node_key]
    assert projected is node
    assert projection.redirects == {node.node_key: node.node_key}
    assert projection.stats["effective_node_count"] == 1
    assert projection.stats["candidate_identity_group_count"] == 0
