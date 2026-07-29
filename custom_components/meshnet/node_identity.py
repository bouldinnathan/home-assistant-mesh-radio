"""Conservative, side-effect-free projection of distinct mesh nodes."""

from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .const import PROTOCOL_MESHTASTIC
from .models import NodeState, canonical_node_key

_MESHTASTIC_BROADCAST_NODE = 0xFFFFFFFF
_NAME_FIELDS = ("long_name", "user_name", "short_name")
_STATIC_FIELDS = (
    "hardware_model",
    "firmware_version",
    "radio_type",
    "role",
)
_STATE_MAPPING_FIELDS = ("power", "radio", "location", "routing", "sensors")


@dataclass(frozen=True, slots=True)
class EffectiveNodeProjection:
    """An effective node view plus redirects and identity-free counts."""

    nodes: dict[str, NodeState]
    redirects: dict[str, str]
    stats: dict[str, int]
    unsafe_node_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class _IdentityEvidence:
    """Validated identity evidence for one retained node record."""

    applicable: bool
    valid: bool
    canonical_node_id: str | None = None
    mac: str | None = None
    public_key: str | None = None


@dataclass(frozen=True, slots=True)
class _NodeRecord:
    """One mapping entry and its validated identity evidence."""

    raw_key: str
    node: NodeState
    identity: _IdentityEvidence


def project_effective_nodes(
    nodes: Mapping[str, NodeState],
) -> EffectiveNodeProjection:
    """Return a distinct-node view without modifying or deleting raw records.

    Meshtastic records are eligible for projection only when they carry one
    valid, non-broadcast 32-bit routing ID and their key, MAC, and public-key
    evidence are internally consistent. Records sharing that exact routing ID
    are collapsed only when every non-empty strong proof agrees. Any malformed
    record or conflicting proof remains independently visible.
    """
    records = [
        _NodeRecord(
            raw_key=raw_key,
            node=node,
            identity=_identity_evidence(raw_key, node),
        )
        for raw_key, node in sorted(nodes.items(), key=lambda item: item[0])
    ]
    unsafe_node_keys = meshtastic_unsafe_identity_keys(nodes)
    # Singleton and fail-closed records can be shared safely because this
    # projection never mutates them. Only collapsed groups need detached merged
    # state. Avoid deep-copying an entire large radio database on every update.
    effective = {record.raw_key: record.node for record in records}
    redirects = {record.raw_key: record.raw_key for record in records}
    identity_groups: dict[str, list[_NodeRecord]] = defaultdict(list)
    invalid_identity_records = 0

    for record in records:
        evidence = record.identity
        if not evidence.applicable:
            continue
        if not evidence.valid or evidence.canonical_node_id is None:
            invalid_identity_records += 1
            continue
        identity_groups[evidence.canonical_node_id].append(record)

    candidate_groups = 0
    resolved_groups = 0
    unresolved_groups = 0
    unresolved_records = 0
    collapsed_records = 0

    for canonical_node_id in sorted(identity_groups):
        members = identity_groups[canonical_node_id]
        if len(members) > 1:
            candidate_groups += 1
        if any(member.raw_key in unsafe_node_keys for member in members):
            unresolved_groups += 1
            unresolved_records += len(members)
            continue
        if len(members) < 2:
            continue

        group_mac, group_public_key = _group_strong_proofs(members)
        representative = min(
            members,
            key=lambda record: _representative_key(
                record,
                canonical_node_id,
                group_mac=group_mac,
                group_public_key=group_public_key,
            ),
        )
        merged = _merge_group(
            members,
            representative=representative,
            canonical_node_id=canonical_node_id,
        )
        if not meshtastic_identity_is_valid(merged.node_key, merged):
            # A projected node must never claim proof that its retained key
            # does not authenticate. Keep the raw observations reversible and
            # fail closed if a future legacy key shape violates that invariant.
            unsafe_node_keys.update(member.raw_key for member in members)
            unresolved_groups += 1
            unresolved_records += len(members)
            continue
        for member in members:
            effective.pop(member.raw_key, None)
            redirects[member.raw_key] = representative.raw_key
        effective[representative.raw_key] = merged
        resolved_groups += 1
        collapsed_records += len(members) - 1

    stats = {
        "raw_record_count": len(records),
        "effective_node_count": len(effective),
        "collapsed_alias_record_count": collapsed_records,
        "candidate_identity_group_count": candidate_groups,
        "resolved_identity_group_count": resolved_groups,
        "unresolved_identity_group_count": unresolved_groups,
        "unresolved_identity_record_count": unresolved_records,
        "invalid_identity_record_count": invalid_identity_records,
    }
    return EffectiveNodeProjection(
        nodes=effective,
        redirects=redirects,
        stats=stats,
        unsafe_node_keys=frozenset(unsafe_node_keys),
    )


def _identity_evidence(raw_key: str, node: NodeState) -> _IdentityEvidence:
    if str(node.protocol).strip().casefold() != PROTOCOL_MESHTASTIC:
        return _IdentityEvidence(applicable=False, valid=False)
    if raw_key != node.node_key or not isinstance(node.node_key, str):
        return _IdentityEvidence(applicable=True, valid=False)

    explicit_id_present = node.node_id is not None
    explicit_id = (
        canonical_meshtastic_node_id(node.node_id)
        if explicit_id_present
        else None
    )
    if explicit_id_present and explicit_id is None:
        return _IdentityEvidence(applicable=True, valid=False)

    key = node.node_key.strip()
    if not key or key != node.node_key:
        return _IdentityEvidence(applicable=True, valid=False)
    prefix, separator, suffix = key.partition(":")
    if not separator:
        return _IdentityEvidence(applicable=True, valid=False)
    prefix = prefix.casefold()

    key_id: str | None = None
    key_mac: str | None = None
    key_public: str | None = None
    key_proof_digest: str | None = None
    if prefix == PROTOCOL_MESHTASTIC:
        key_id = canonical_meshtastic_node_id(suffix)
        if key_id is None:
            return _IdentityEvidence(applicable=True, valid=False)
    elif prefix == "mac":
        key_mac = _strict_mac(suffix)
        if key_mac is None:
            return _IdentityEvidence(applicable=True, valid=False)
    elif prefix == "pub":
        key_public = _strict_public_key(suffix)
        if key_public is None:
            return _IdentityEvidence(applicable=True, valid=False)
    elif prefix == "meshtastic-proof":
        key_proof_digest = suffix.casefold()
        if len(key_proof_digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in key_proof_digest
        ):
            return _IdentityEvidence(applicable=True, valid=False)
    else:
        return _IdentityEvidence(applicable=True, valid=False)

    if explicit_id is not None and key_id is not None and explicit_id != key_id:
        return _IdentityEvidence(applicable=True, valid=False)
    canonical_node_id = explicit_id or key_id
    if canonical_node_id is None:
        return _IdentityEvidence(applicable=True, valid=False)

    explicit_mac_present = node.mac is not None
    explicit_mac = _strict_mac(node.mac) if explicit_mac_present else None
    if explicit_mac_present and explicit_mac is None:
        return _IdentityEvidence(applicable=True, valid=False)
    if key_mac is not None and explicit_mac is not None and key_mac != explicit_mac:
        return _IdentityEvidence(applicable=True, valid=False)

    explicit_public_present = node.public_key is not None
    explicit_public = (
        _strict_public_key(node.public_key)
        if explicit_public_present
        else None
    )
    if explicit_public_present and explicit_public is None:
        return _IdentityEvidence(applicable=True, valid=False)
    if (
        key_public is not None
        and explicit_public is not None
        and key_public.casefold() != explicit_public.casefold()
    ):
        return _IdentityEvidence(applicable=True, valid=False)
    if key_proof_digest is not None:
        expected_key = meshtastic_observation_node_key(
            canonical_node_id,
            explicit_mac,
            explicit_public,
        )
        if expected_key != f"meshtastic-proof:{key_proof_digest}":
            return _IdentityEvidence(applicable=True, valid=False)

    return _IdentityEvidence(
        applicable=True,
        valid=True,
        canonical_node_id=canonical_node_id,
        mac=explicit_mac or key_mac,
        # Meshtastic public keys are exact 32-byte values represented by their
        # canonical lowercase hex encoding.
        public_key=explicit_public or key_public,
    )


def canonical_meshtastic_node_id(value: Any) -> str | None:
    """Return one safe ``!xxxxxxxx`` Meshtastic routing identifier."""
    if isinstance(value, bool) or value is None:
        return None
    number: int
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            if text.startswith("!") and 1 <= len(text[1:]) <= 8:
                number = int(text[1:], 16)
            elif text.casefold().startswith("0x") and 1 <= len(text[2:]) <= 8:
                number = int(text, 16)
            elif text.isascii() and text.isdecimal() and len(text) <= 10:
                number = int(text, 10)
            else:
                return None
        except ValueError:
            return None
    else:
        return None
    if not 0 < number < _MESHTASTIC_BROADCAST_NODE:
        return None
    return f"!{number:08x}"


def meshtastic_identity_is_valid(raw_key: str, node: NodeState) -> bool:
    """Return whether one Meshtastic record has self-consistent identity proof."""
    evidence = _identity_evidence(raw_key, node)
    return evidence.applicable and evidence.valid


def meshtastic_unsafe_identity_keys(
    nodes: Mapping[str, NodeState],
) -> set[str]:
    """Return records that are malformed or conflict with known strong proof."""
    records = [
        _NodeRecord(
            raw_key=raw_key,
            node=node,
            identity=_identity_evidence(raw_key, node),
        )
        for raw_key, node in nodes.items()
        if str(node.protocol).strip().casefold() == PROTOCOL_MESHTASTIC
    ]
    unsafe = {
        record.raw_key for record in records if not record.identity.valid
    }
    valid = [record for record in records if record.identity.valid]

    by_node_id: dict[str, list[_NodeRecord]] = defaultdict(list)
    proof_groups: dict[tuple[str, str], list[_NodeRecord]] = defaultdict(list)
    for record in valid:
        identity = record.identity
        if identity.canonical_node_id is None:
            unsafe.add(record.raw_key)
            continue
        by_node_id[identity.canonical_node_id].append(record)
        if identity.mac is not None:
            proof_groups[("mac", identity.mac)].append(record)
        if identity.public_key is not None:
            proof_groups[("public_key", identity.public_key)].append(record)

    for members in by_node_id.values():
        if _has_unmergeable_strong_proofs(members):
            unsafe.update(member.raw_key for member in members)
    for members in proof_groups.values():
        node_ids = {
            member.identity.canonical_node_id for member in members
        }
        if len(node_ids) > 1:
            unsafe.update(member.raw_key for member in members)
    return unsafe


def canonical_meshtastic_mac(value: Any) -> str | None:
    """Return one strict lowercase 12-hex Meshtastic MAC identifier."""
    return _strict_mac(value)


def meshtastic_observation_node_key(
    node_id: Any,
    mac: Any = None,
    public_key: Any = None,
) -> str:
    """Return a stable key without discarding simultaneous strong proofs.

    An ID-only record keeps the readable ``meshtastic:`` key. Whenever a valid
    MAC and/or public key is present, a one-way digest binds the canonical
    routing ID to every available proof. Therefore a later conflicting proof
    cannot overwrite an earlier raw record before the conservative projector
    sees it.
    """
    canonical_id = canonical_meshtastic_node_id(node_id)
    canonical_mac = _strict_mac(mac)
    canonical_public = _strict_public_key(public_key)
    if canonical_id is not None and (
        canonical_mac is not None or canonical_public is not None
    ):
        material = "\0".join(
            (
                "meshnet-meshtastic-proof-v1",
                canonical_id,
                canonical_mac or "",
                canonical_public or "",
            )
        ).encode()
        return f"meshtastic-proof:{hashlib.sha256(material).hexdigest()}"
    return canonical_node_key(
        PROTOCOL_MESHTASTIC,
        node_id=canonical_id if canonical_id is not None else node_id,
        mac=mac,
        public_key=public_key,
    )


def _strict_mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    compact = text.replace(":", "").replace("-", "")
    if len(compact) != 12 or any(character not in "0123456789abcdef" for character in compact):
        return None
    if text != compact:
        colon = ":".join(compact[index : index + 2] for index in range(0, 12, 2))
        hyphen = colon.replace(":", "-")
        if text not in {colon, hyphen}:
            return None
    return compact


def _strict_public_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        return None
    return text


def _has_unmergeable_strong_proofs(members: Sequence[_NodeRecord]) -> bool:
    """Reject conflicting or merely complementary proof observations.

    A MAC-only record and a public-key-only record do not prove that the two
    identifiers belong together, even when they claim the same routing ID.
    Collapsing them would synthesize a proof tuple that no packet or node-info
    record actually carried. ID-only records add no strong proof and may still
    be merged with one consistently observed proof tuple.
    """
    proof_bundles = {
        (member.identity.mac, member.identity.public_key)
        for member in members
        if member.identity.mac is not None
        or member.identity.public_key is not None
    }
    macs = {mac for mac, _public_key in proof_bundles if mac is not None}
    public_keys = {
        public_key
        for _mac, public_key in proof_bundles
        if public_key is not None
    }
    if len(macs) > 1 or len(public_keys) > 1:
        return True
    if macs and public_keys:
        # The separate values become one identity only when a source record
        # actually observed them together.
        return (next(iter(macs)), next(iter(public_keys))) not in proof_bundles
    return False


def _group_strong_proofs(
    members: Sequence[_NodeRecord],
) -> tuple[str | None, str | None]:
    """Return the single proof values after mergeability was established."""
    macs = {
        member.identity.mac
        for member in members
        if member.identity.mac is not None
    }
    public_keys = {
        member.identity.public_key
        for member in members
        if member.identity.public_key is not None
    }
    return (
        next(iter(macs)) if macs else None,
        next(iter(public_keys)) if public_keys else None,
    )


def _representative_key(
    record: _NodeRecord,
    canonical_node_id: str,
    *,
    group_mac: str | None,
    group_public_key: str | None,
) -> tuple[int, str, str]:
    key = record.raw_key
    folded = key.casefold()
    if folded == f"{PROTOCOL_MESHTASTIC}:{canonical_node_id}":
        rank = 0
    elif folded.startswith(f"{PROTOCOL_MESHTASTIC}:"):
        rank = 1
    elif (
        record.identity.mac == group_mac
        and record.identity.public_key == group_public_key
        and folded.startswith("mac:")
    ):
        rank = 2
    elif (
        record.identity.mac == group_mac
        and record.identity.public_key == group_public_key
        and folded.startswith("pub:")
    ):
        rank = 3
    elif (
        record.identity.mac == group_mac
        and record.identity.public_key == group_public_key
        and folded.startswith("meshtastic-proof:")
    ):
        rank = 4
    else:
        rank = 5
    return rank, folded, key


def _merge_group(
    members: Sequence[_NodeRecord],
    *,
    representative: _NodeRecord,
    canonical_node_id: str,
) -> NodeState:
    merged = copy.deepcopy(representative.node)
    merged.node_key = representative.raw_key
    merged.node_id = canonical_node_id

    macs = sorted(
        {
            member.identity.mac
            for member in members
            if member.identity.mac is not None
        }
    )
    public_keys = sorted(
        {
            member.identity.public_key
            for member in members
            if member.identity.public_key is not None
        }
    )
    merged.mac = macs[0] if macs else None
    merged.public_key = public_keys[0] if public_keys else None

    observation = _freshest_record(members)
    merged.online = observation.node.online
    merged.last_heard = observation.node.last_heard
    merged.last_gateway_id = observation.node.last_gateway_id
    # The complete dictionary is copied from one observation so hops,
    # observing gateway, and MQTT/RF provenance cannot be cross-combined.
    merged.connectivity = copy.deepcopy(observation.node.connectivity)
    merged.raw = copy.deepcopy(observation.node.raw)

    gateway_ids = {
        gateway_id
        for member in members
        for gateway_id in member.node.gateway_ids
        if gateway_id
    }
    gateway_ids.update(
        member.node.last_gateway_id
        for member in members
        if member.node.last_gateway_id
    )
    merged.gateway_ids = gateway_ids

    name_donor = _coherent_name_donor(members)
    for field in _NAME_FIELDS:
        setattr(
            merged,
            field,
            _clean_text(getattr(name_donor.node, field))
            if name_donor is not None
            else None,
        )

    for field in _STATIC_FIELDS:
        donor = _freshest_record_with_value(members, field)
        if donor is not None:
            setattr(merged, field, copy.deepcopy(getattr(donor.node, field)))

    for field in _STATE_MAPPING_FIELDS:
        donor = _freshest_record_with_mapping(members, field)
        setattr(
            merged,
            field,
            copy.deepcopy(getattr(donor.node, field)) if donor is not None else {},
        )
    return merged


def _freshest_record(members: Sequence[_NodeRecord]) -> _NodeRecord:
    return min(
        members,
        key=lambda member: (
            -_timestamp(member.node.last_heard),
            member.raw_key.casefold(),
            member.raw_key,
        ),
    )


def _freshest_record_with_value(
    members: Sequence[_NodeRecord], field: str
) -> _NodeRecord | None:
    candidates = [
        member
        for member in members
        if getattr(member.node, field) not in (None, "")
    ]
    return _freshest_record(candidates) if candidates else None


def _freshest_record_with_mapping(
    members: Sequence[_NodeRecord], field: str
) -> _NodeRecord | None:
    candidates = [member for member in members if getattr(member.node, field)]
    return _freshest_record(candidates) if candidates else None


def _coherent_name_donor(
    members: Sequence[_NodeRecord],
) -> _NodeRecord | None:
    named = [
        member
        for member in members
        if any(_clean_text(getattr(member.node, field)) for field in _NAME_FIELDS)
    ]
    if not named:
        return None

    unique_values: dict[str, str] = {}
    fields_are_unambiguous = True
    for field in _NAME_FIELDS:
        values = {
            _name_sort_key(value)
            for member in named
            if (value := _clean_text(getattr(member.node, field))) is not None
        }
        if len(values) > 1:
            fields_are_unambiguous = False
        elif values:
            unique_values[field] = next(iter(values))

    if fields_are_unambiguous:
        complete_donors = [
            member
            for member in named
            if all(
                (
                    value := _clean_text(getattr(member.node, field))
                )
                is not None
                and _name_sort_key(value) == expected
                for field, expected in unique_values.items()
            )
        ]
        if complete_donors:
            return _best_name_donor(complete_donors)

    # Never construct a synthetic long/user/short tuple from separate records.
    # When names changed or only split evidence exists, use one best donor's
    # complete tuple as-is.
    return _best_name_donor(named)


def _best_name_donor(members: Sequence[_NodeRecord]) -> _NodeRecord:
    return min(
        members,
        key=lambda member: (
            -sum(
                _clean_text(getattr(member.node, field)) is not None
                for field in _NAME_FIELDS
            ),
            -_timestamp(member.node.last_heard),
            member.raw_key.casefold(),
            member.raw_key,
        ),
    )


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _name_sort_key(value: str) -> str:
    return value.casefold()


def _timestamp(value: datetime | None) -> float:
    if not isinstance(value, datetime):
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return float("-inf")


__all__ = [
    "EffectiveNodeProjection",
    "canonical_meshtastic_mac",
    "canonical_meshtastic_node_id",
    "meshtastic_identity_is_valid",
    "meshtastic_observation_node_key",
    "meshtastic_unsafe_identity_keys",
    "project_effective_nodes",
]
