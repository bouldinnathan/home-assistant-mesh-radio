"""Binary sensor entities for MeshNet."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from ..entity import MeshNetGatewayEntity, MeshNetNodeEntity
from ..models import NodeState


@dataclass(frozen=True, kw_only=True)
class MeshNetBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor description."""

    value_key: str
    source: str


BINARY_SENSORS: tuple[MeshNetBinarySensorDescription, ...] = (
    MeshNetBinarySensorDescription(
        key="online",
        translation_key="online",
        name="Online",
        value_key="online",
        source="node",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    MeshNetBinarySensorDescription(
        key="charging",
        translation_key="charging",
        name="Charging",
        value_key="charging",
        source="power",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    MeshNetBinarySensorDescription(
        key="motion",
        translation_key="motion",
        name="Motion",
        value_key="motion",
        source="sensors",
        device_class=BinarySensorDeviceClass.MOTION,
    ),
    MeshNetBinarySensorDescription(
        key="door",
        translation_key="door",
        name="Door",
        value_key="door",
        source="sensors",
        device_class=BinarySensorDeviceClass.DOOR,
    ),
    MeshNetBinarySensorDescription(
        key="water",
        translation_key="water",
        name="Water",
        value_key="water",
        source="sensors",
        device_class=BinarySensorDeviceClass.MOISTURE,
    ),
)


GATEWAY_ONLINE_DESCRIPTION = BinarySensorEntityDescription(
    key="online",
    translation_key="gateway_online",
    name="Online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
)


class MeshNetGatewayOnlineBinarySensor(MeshNetGatewayEntity, BinarySensorEntity):
    """Connectivity sensor for a mesh gateway."""

    entity_description = GATEWAY_ONLINE_DESCRIPTION

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id, "online")

    @property
    def is_on(self) -> bool | None:
        """Return if the gateway is connected."""
        gateway = self.gateway
        return gateway.connected if gateway else None


class MeshNetBinarySensor(MeshNetNodeEntity, BinarySensorEntity):
    """Binary sensor for a mesh node."""

    entity_description: MeshNetBinarySensorDescription

    def __init__(self, coordinator, node_key: str, description: MeshNetBinarySensorDescription) -> None:
        super().__init__(coordinator, node_key, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return binary state."""
        node = self.node
        if node is None:
            return None
        value = _value_for(node, self.entity_description.source, self.entity_description.value_key)
        if value is None:
            return None
        return bool(value)


def binary_entities_for_node(coordinator, node_key: str) -> list[BinarySensorEntity]:
    """Build binary sensor entities for a node."""
    node = coordinator.data.nodes[node_key]
    entities: list[BinarySensorEntity] = []
    for description in BINARY_SENSORS:
        value = _value_for(node, description.source, description.value_key)
        if description.key == "online" or value is not None:
            entities.append(MeshNetBinarySensor(coordinator, node_key, description))
    for key, value in sorted(node.sensors.items()):
        if isinstance(value, bool) and key not in {item.value_key for item in BINARY_SENSORS}:
            entities.append(
                MeshNetBinarySensor(
                    coordinator,
                    node_key,
                    MeshNetBinarySensorDescription(
                        key=f"sensor_{key}",
                        name=key.replace("_", " ").title(),
                        value_key=key,
                        source="sensors",
                    ),
                )
            )
    return entities


def gateway_binary_entities_for_gateway(coordinator, gateway_id: str) -> list[BinarySensorEntity]:
    """Build binary sensor entities for a gateway."""
    return [MeshNetGatewayOnlineBinarySensor(coordinator, gateway_id)]


def _value_for(node: NodeState, source: str, key: str) -> Any:
    if source == "node":
        return getattr(node, key)
    return getattr(node, source).get(key)
