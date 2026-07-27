"""Base Home Assistant entities for MeshNet."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MeshNetCoordinator
from .models import GatewayStatus, NodeState


class MeshNetCoordinatorEntity(CoordinatorEntity[MeshNetCoordinator]):
    """Base coordinator entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MeshNetCoordinator, unique_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return hub device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name=self.coordinator.entry.title,
            manufacturer="MeshNet",
        )


class MeshNetNodeEntity(MeshNetCoordinatorEntity):
    """Base entity bound to a mesh node."""

    def __init__(self, coordinator: MeshNetCoordinator, node_key: str, unique_suffix: str) -> None:
        super().__init__(coordinator, f"{node_key}_{unique_suffix}")
        self.node_key = node_key

    @property
    def node(self) -> NodeState | None:
        """Return the current node state."""
        return self.coordinator.data.nodes.get(self.node_key) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        """Return if the node exists."""
        return self.node is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return node device info."""
        node = self.node
        if node is None:
            return DeviceInfo(identifiers={(DOMAIN, self.node_key)})
        return DeviceInfo(
            identifiers={(DOMAIN, node.node_key)},
            name=node.display_name,
            manufacturer=node.protocol.title(),
            model=node.hardware_model or node.radio_type,
            sw_version=node.firmware_version,
            via_device=(
                (DOMAIN, node.last_gateway_id)
                if node.last_gateway_id
                else (DOMAIN, self.coordinator.entry.entry_id)
            ),
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return common node attributes."""
        node = self.node
        if node is None:
            return {}
        return {
            "node_key": node.node_key,
            "node_id": node.node_id,
            "mac": node.mac,
            "public_key": node.public_key,
            "gateway_ids": sorted(node.gateway_ids),
            "last_gateway_id": node.last_gateway_id,
            "role": node.role,
            "protocol": node.protocol,
        }


class MeshNetGatewayEntity(MeshNetCoordinatorEntity):
    """Base entity bound to a mesh gateway."""

    def __init__(self, coordinator: MeshNetCoordinator, gateway_id: str, unique_suffix: str) -> None:
        super().__init__(coordinator, f"{gateway_id}_{unique_suffix}")
        self.gateway_id = gateway_id

    @property
    def gateway(self) -> GatewayStatus | None:
        """Return the current gateway status."""
        return self.coordinator.data.gateways.get(self.gateway_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        """Return if the gateway exists."""
        return self.gateway is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return gateway device info."""
        gateway = self.gateway
        if gateway is None:
            return DeviceInfo(identifiers={(DOMAIN, self.gateway_id)})
        return DeviceInfo(
            identifiers={(DOMAIN, gateway.gateway_id)},
            name=gateway.name,
            manufacturer="MeshNet",
            model=f"{gateway.protocol.title()} {gateway.transport.title()} Gateway",
            via_device=(DOMAIN, self.coordinator.entry.entry_id),
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return common gateway attributes."""
        gateway = self.gateway
        if gateway is None:
            return {}
        return {
            "gateway_id": gateway.gateway_id,
            "protocol": gateway.protocol,
            "transport": gateway.transport,
            "last_connected": gateway.last_connected.isoformat() if gateway.last_connected else None,
            "last_packet": gateway.last_packet.isoformat() if gateway.last_packet else None,
        }
