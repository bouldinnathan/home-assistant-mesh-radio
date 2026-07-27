"""Packet deduplication helpers."""

from __future__ import annotations

from collections import OrderedDict
from time import monotonic

from .models import MeshPacket


class PacketDeduplicator:
    """TTL based packet deduplicator."""

    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 10000) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self.total_packets = 0
        self.duplicate_packets = 0

    @property
    def duplicate_ratio(self) -> float:
        """Return the duplicate packet ratio."""
        if self.total_packets == 0:
            return 0.0
        return self.duplicate_packets / self.total_packets

    def is_duplicate(self, packet: MeshPacket) -> bool:
        """Return True if a packet was recently observed."""
        self.total_packets += 1
        now = monotonic()
        self._expire(now)
        fingerprint = packet.fingerprint()
        if fingerprint in self._seen:
            self.duplicate_packets += 1
            self._seen.move_to_end(fingerprint)
            self._seen[fingerprint] = now
            return True
        self._seen[fingerprint] = now
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return False

    def _expire(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        while self._seen:
            _, first_seen = next(iter(self._seen.items()))
            if first_seen >= cutoff:
                break
            self._seen.popitem(last=False)

    def stats(self) -> dict[str, float | int]:
        """Return diagnostic stats."""
        return {
            "ttl_seconds": self._ttl_seconds,
            "entries": len(self._seen),
            "total_packets": self.total_packets,
            "duplicate_packets": self.duplicate_packets,
            "duplicate_ratio": self.duplicate_ratio,
        }
