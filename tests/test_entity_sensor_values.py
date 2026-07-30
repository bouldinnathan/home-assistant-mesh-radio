"""Entity value regressions for valid falsey mesh measurements."""

from __future__ import annotations

import pytest

try:
    from custom_components.meshnet.entities.sensors import STATIC_NODE_SENSORS
    from custom_components.meshnet.models import NodeState
except ImportError:
    pytest.skip("Home Assistant runtime dependencies are unavailable", allow_module_level=True)


def _description(key: str):
    return next(item for item in STATIC_NODE_SENSORS if item.key == key)


def test_static_hop_entities_preserve_valid_zero_measurements() -> None:
    """A direct packet's zero hops and zero hop-limit are real values."""
    node = NodeState(
        node_key="meshtastic:!01020304",
        protocol="meshtastic",
        connectivity={"hops": 0, "hop_limit": 0},
        routing={"hops": 7, "hop_limit": 7},
    )

    assert _description("hops").value_fn(node) == 0
    assert _description("hop_limit").value_fn(node) == 0


def test_static_hop_entities_fall_back_only_when_connectivity_is_missing() -> None:
    """Routing remains a fallback rather than overriding falsey measurements."""
    node = NodeState(
        node_key="meshcore:node",
        protocol="meshcore",
        connectivity={},
        routing={"hops": 2, "hop_limit": 5},
    )

    assert _description("hops").value_fn(node) == 2
    assert _description("hop_limit").value_fn(node) == 5
