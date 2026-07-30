"""Sensor entities for MeshNet."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant import const as ha_const
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTemperature,
)

from ..compat import percentage_unit
from ..entity import MeshNetCoordinatorEntity, MeshNetGatewayEntity, MeshNetNodeEntity
from ..models import GatewayStatus, MeshSnapshot, NodeState

PERCENTAGE_UNIT = percentage_unit(ha_const)


@dataclass(frozen=True, kw_only=True)
class MeshNetSummarySensorDescription(SensorEntityDescription):
    """Summary sensor description."""

    value_fn: Callable[[MeshSnapshot], Any]


@dataclass(frozen=True, kw_only=True)
class MeshNetNodeSensorDescription(SensorEntityDescription):
    """Node sensor description."""

    value_fn: Callable[[NodeState], Any]


@dataclass(frozen=True, kw_only=True)
class MeshNetGatewaySensorDescription(SensorEntityDescription):
    """Gateway sensor description."""

    value_fn: Callable[[GatewayStatus], Any]


SUMMARY_SENSORS: tuple[MeshNetSummarySensorDescription, ...] = (
    MeshNetSummarySensorDescription(
        key="total_nodes",
        translation_key="total_nodes",
        name="Total nodes",
        icon="mdi:access-point-network",
        value_fn=lambda data: len(data.nodes),
    ),
    MeshNetSummarySensorDescription(
        key="active_nodes",
        translation_key="active_nodes",
        name="Active nodes",
        icon="mdi:radio-tower",
        value_fn=lambda data: sum(1 for node in data.nodes.values() if node.online),
    ),
    MeshNetSummarySensorDescription(
        key="offline_nodes",
        translation_key="offline_nodes",
        name="Offline nodes",
        icon="mdi:radio-off",
        value_fn=lambda data: sum(1 for node in data.nodes.values() if not node.online),
    ),
    MeshNetSummarySensorDescription(
        key="average_battery",
        translation_key="average_battery",
        name="Average battery",
        native_unit_of_measurement=PERCENTAGE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _average_battery(data.nodes.values()),
    ),
    MeshNetSummarySensorDescription(
        key="mesh_health_score",
        translation_key="mesh_health_score",
        name="Mesh health score",
        native_unit_of_measurement=PERCENTAGE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.mesh_health_score,
    ),
    MeshNetSummarySensorDescription(
        key="messages_today",
        translation_key="messages_today",
        name="Messages today",
        icon="mdi:message-text-clock",
        value_fn=lambda data: data.messages_today,
    ),
)

GATEWAY_SENSORS: tuple[MeshNetGatewaySensorDescription, ...] = (
    MeshNetGatewaySensorDescription(
        key="last_connected",
        translation_key="gateway_last_connected",
        name="Last connected",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda gateway: gateway.last_connected,
    ),
    MeshNetGatewaySensorDescription(
        key="last_packet",
        translation_key="gateway_last_packet",
        name="Last packet",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda gateway: gateway.last_packet,
    ),
    MeshNetGatewaySensorDescription(
        key="packets_received",
        translation_key="gateway_packets_received",
        name="Packets received",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda gateway: gateway.packets_received,
    ),
    MeshNetGatewaySensorDescription(
        key="packets_sent",
        translation_key="gateway_packets_sent",
        name="Packets sent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda gateway: gateway.packets_sent,
    ),
    MeshNetGatewaySensorDescription(
        key="duplicate_packets",
        translation_key="gateway_duplicate_packets",
        name="Duplicate packets",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda gateway: gateway.duplicate_packets,
    ),
    MeshNetGatewaySensorDescription(
        key="error_count",
        translation_key="gateway_error_count",
        name="Error count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda gateway: len(gateway.errors),
    ),
    MeshNetGatewaySensorDescription(
        key="failure_count",
        translation_key="gateway_failure_count",
        name="Failure count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda gateway: gateway.failure_count,
    ),
)

STATIC_NODE_SENSORS: tuple[MeshNetNodeSensorDescription, ...] = (
    MeshNetNodeSensorDescription(
        key="last_heard",
        translation_key="last_heard",
        name="Last heard",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda node: node.last_heard,
    ),
    MeshNetNodeSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        name="Battery",
        native_unit_of_measurement=PERCENTAGE_UNIT,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.power.get("battery_level"),
    ),
    MeshNetNodeSensorDescription(
        key="voltage",
        translation_key="voltage",
        name="Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.power.get("voltage"),
    ),
    MeshNetNodeSensorDescription(
        key="rssi",
        translation_key="rssi",
        name="RSSI",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.connectivity.get("rssi"),
    ),
    MeshNetNodeSensorDescription(
        key="snr",
        translation_key="snr",
        name="SNR",
        native_unit_of_measurement="dB",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.connectivity.get("snr"),
    ),
    MeshNetNodeSensorDescription(
        key="channel_utilization",
        translation_key="channel_utilization",
        name="Channel utilization",
        native_unit_of_measurement=PERCENTAGE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.connectivity.get("channel_utilization"),
    ),
    MeshNetNodeSensorDescription(
        key="air_utilization",
        translation_key="air_utilization",
        name="Air utilization",
        native_unit_of_measurement=PERCENTAGE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.connectivity.get("air_utilization"),
    ),
    MeshNetNodeSensorDescription(
        key="hops",
        translation_key="hops",
        name="Hops",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: (
            node.connectivity["hops"]
            if node.connectivity.get("hops") is not None
            else node.routing.get("hops")
        ),
    ),
    MeshNetNodeSensorDescription(
        key="hop_limit",
        translation_key="hop_limit",
        name="Hop limit",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: (
            node.connectivity["hop_limit"]
            if node.connectivity.get("hop_limit") is not None
            else node.routing.get("hop_limit")
        ),
    ),
    MeshNetNodeSensorDescription(
        key="latitude",
        translation_key="latitude",
        name="Latitude",
        value_fn=lambda node: node.location.get("latitude"),
    ),
    MeshNetNodeSensorDescription(
        key="longitude",
        translation_key="longitude",
        name="Longitude",
        value_fn=lambda node: node.location.get("longitude"),
    ),
    MeshNetNodeSensorDescription(
        key="altitude",
        translation_key="altitude",
        name="Altitude",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.location.get("altitude"),
    ),
    MeshNetNodeSensorDescription(
        key="speed",
        translation_key="speed",
        name="Speed",
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.location.get("speed"),
    ),
    MeshNetNodeSensorDescription(
        key="heading",
        translation_key="heading",
        name="Heading",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.location.get("heading"),
    ),
    MeshNetNodeSensorDescription(
        key="frequency",
        translation_key="frequency",
        name="Frequency",
        native_unit_of_measurement="MHz",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.radio.get("frequency"),
    ),
    MeshNetNodeSensorDescription(
        key="tx_power",
        translation_key="tx_power",
        name="TX power",
        native_unit_of_measurement="dBm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.radio.get("tx_power"),
    ),
    MeshNetNodeSensorDescription(
        key="temperature",
        translation_key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.sensors.get("temperature"),
    ),
    MeshNetNodeSensorDescription(
        key="humidity",
        translation_key="humidity",
        name="Humidity",
        native_unit_of_measurement=PERCENTAGE_UNIT,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.sensors.get("humidity"),
    ),
    MeshNetNodeSensorDescription(
        key="pressure",
        translation_key="pressure",
        name="Pressure",
        native_unit_of_measurement="hPa",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.sensors.get("pressure"),
    ),
    MeshNetNodeSensorDescription(
        key="co2",
        translation_key="co2",
        name="CO2",
        native_unit_of_measurement="ppm",
        device_class=SensorDeviceClass.CO2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda node: node.sensors.get("co2"),
    ),
)


class MeshNetSummarySensor(MeshNetCoordinatorEntity, SensorEntity):
    """Summary sensor for the mesh network."""

    entity_description: MeshNetSummarySensorDescription

    def __init__(self, coordinator, description: MeshNetSummarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)


class MeshNetNodeSensor(MeshNetNodeEntity, SensorEntity):
    """Sensor for a mesh node."""

    entity_description: MeshNetNodeSensorDescription

    def __init__(self, coordinator, node_key: str, description: MeshNetNodeSensorDescription) -> None:
        super().__init__(coordinator, node_key, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the node sensor value."""
        node = self.node
        if node is None:
            return None
        value = self.entity_description.value_fn(node)
        if isinstance(value, datetime):
            return value
        return value


class MeshNetGatewaySensor(MeshNetGatewayEntity, SensorEntity):
    """Sensor for a mesh gateway."""

    entity_description: MeshNetGatewaySensorDescription

    def __init__(self, coordinator, gateway_id: str, description: MeshNetGatewaySensorDescription) -> None:
        super().__init__(coordinator, gateway_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the gateway sensor value."""
        gateway = self.gateway
        if gateway is None:
            return None
        return self.entity_description.value_fn(gateway)


class MeshNetDynamicSensor(MeshNetNodeEntity, SensorEntity):
    """Dynamically discovered mesh sensor."""

    def __init__(self, coordinator, node_key: str, sensor_key: str) -> None:
        super().__init__(coordinator, node_key, f"sensor_{sensor_key}")
        self.sensor_key = sensor_key
        self._attr_name = sensor_key.replace("_", " ").title()
        self._attr_icon = "mdi:gauge"

    @property
    def native_value(self) -> Any:
        """Return the dynamic sensor value."""
        node = self.node
        if node is None:
            return None
        return node.sensors.get(self.sensor_key)


def gateway_sensor_entities_for_gateway(coordinator, gateway_id: str) -> list[SensorEntity]:
    """Build sensor entities for a gateway."""
    return [
        MeshNetGatewaySensor(coordinator, gateway_id, description)
        for description in GATEWAY_SENSORS
    ]


def sensor_entities_for_node(coordinator, node_key: str) -> list[SensorEntity]:
    """Build static sensor entities for a node."""
    entities: list[SensorEntity] = []
    node = coordinator.data.nodes[node_key]
    for description in STATIC_NODE_SENSORS:
        if description.value_fn(node) is not None:
            entities.append(MeshNetNodeSensor(coordinator, node_key, description))
    for key, value in sorted(node.sensors.items()):
        if value is not None and not isinstance(value, bool) and key not in {sensor.key for sensor in STATIC_NODE_SENSORS}:
            entities.append(MeshNetDynamicSensor(coordinator, node_key, key))
    return entities


def _average_battery(nodes) -> float | None:
    values = [
        float(node.power["battery_level"])
        for node in nodes
        if isinstance(node.power.get("battery_level"), (int, float))
    ]
    if not values:
        return None
    return round(sum(values) / len(values), 1)
