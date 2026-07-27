from __future__ import annotations

from custom_components.meshnet.dedupe import PacketDeduplicator
from custom_components.meshnet.models import MeshPacket


def test_packet_dedupe_by_packet_id() -> None:
    dedupe = PacketDeduplicator()
    packet = MeshPacket(protocol="meshtastic", gateway_id="g1", packet_id="abc")
    duplicate = MeshPacket(protocol="meshtastic", gateway_id="g2", packet_id="abc")

    assert dedupe.is_duplicate(packet) is False
    assert dedupe.is_duplicate(duplicate) is True
    assert dedupe.stats()["duplicate_packets"] == 1
