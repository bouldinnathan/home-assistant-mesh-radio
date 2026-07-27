from __future__ import annotations

import logging
import sys
import types

from custom_components.meshnet.const import (
    PROTOCOL_MESHCORE,
    PROTOCOL_MESHTASTIC,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)
from custom_components.meshnet.meshcore_client import MeshCoreClient
from custom_components.meshnet.meshtastic_client import MeshtasticClient
from custom_components.meshnet.models import GatewayConfig


async def _noop(*_args) -> None:
    return None


def test_meshtastic_tcp_uses_configured_port(monkeypatch) -> None:
    class TCPInterface:
        def __init__(self, hostname, **kwargs) -> None:
            self.hostname = hostname
            self.kwargs = kwargs

    meshtastic = types.ModuleType("meshtastic")
    tcp_interface = types.ModuleType("meshtastic.tcp_interface")
    tcp_interface.TCPInterface = TCPInterface
    meshtastic.tcp_interface = tcp_interface
    monkeypatch.setitem(sys.modules, "meshtastic", meshtastic)
    monkeypatch.setitem(sys.modules, "meshtastic.tcp_interface", tcp_interface)

    client = MeshtasticClient(
        None,
        GatewayConfig(
            gateway_id="g1",
            name="Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            host="192.0.2.10",
            port=12345,
        ),
        _noop,
        _noop,
        _noop,
        logging.getLogger(__name__),
    )

    interface = client._make_native_interface()

    assert interface.hostname == "192.0.2.10"
    assert interface.kwargs["portNumber"] == 12345


def test_meshtastic_global_events_require_exact_interface_owner() -> None:
    client = MeshtasticClient(
        None,
        GatewayConfig(
            gateway_id="g1",
            name="Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            host="192.0.2.10",
            port=4403,
        ),
        _noop,
        _noop,
        _noop,
        logging.getLogger(__name__),
    )
    first = object()
    second = object()

    assert not client._owns_interface(first)
    client._interface = first
    assert client._owns_interface(first)
    assert not client._owns_interface(second)
    assert not client._owns_interface(None)


def test_meshtastic_diagnostics_expose_lifecycle_without_endpoint_identity() -> None:
    client = MeshtasticClient(
        None,
        GatewayConfig(
            gateway_id="private-gateway",
            name="Private Gateway",
            protocol=PROTOCOL_MESHTASTIC,
            transport=TRANSPORT_TCP,
            host="private-host.local",
            port=4403,
        ),
        _noop,
        _noop,
        _noop,
        logging.getLogger(__name__),
    )
    client._interface = object()

    diagnostics = client.diagnostic_snapshot()

    assert diagnostics["implementation"] == "MeshtasticClient"
    assert diagnostics["interface_active"] is True
    assert diagnostics["start_task"] == "not_created"
    assert diagnostics["native_executor_operation_count"] == 0
    assert "private-host.local" not in repr(diagnostics)
    assert "private-gateway" not in repr(diagnostics)


def test_meshcore_diagnostics_count_contacts_without_exposing_them() -> None:
    client = MeshCoreClient(
        None,
        GatewayConfig(
            gateway_id="private-gateway",
            name="Private Gateway",
            protocol=PROTOCOL_MESHCORE,
            transport=TRANSPORT_SERIAL,
            serial_path="/dev/serial/by-id/private",
        ),
        _noop,
        _noop,
        _noop,
        logging.getLogger(__name__),
    )
    client._contacts["private-contact"] = {"name": "Private Person"}

    diagnostics = client.diagnostic_snapshot()

    assert diagnostics["implementation"] == "MeshCoreClient"
    assert diagnostics["contact_count"] == 1
    assert diagnostics["background_task_count"] == 0
    assert "private-contact" not in repr(diagnostics)
    assert "Private Person" not in repr(diagnostics)
    assert "/dev/serial" not in repr(diagnostics)
