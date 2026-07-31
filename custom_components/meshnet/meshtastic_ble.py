"""Strict local-controller adapter for the async Meshtastic BLE client."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from .models import coerce_int

_LOCAL_ADAPTER_RE = re.compile(r"hci[0-9]+\Z")
_BLUETOOTH_ADDRESS_RE = re.compile(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}\Z")


class MeshtasticBluetoothTransport:
    """Expose MeshNet's transport contract over one exact local BLE scanner."""

    def __init__(
        self,
        hass: Any,
        *,
        address: str,
        adapter: str,
        adapter_address: str,
        logger: Any,
        phase_callback: Callable[[str], None],
    ) -> None:
        canonical_address = address.upper()
        canonical_adapter_address = adapter_address.upper()
        if _BLUETOOTH_ADDRESS_RE.fullmatch(canonical_address) is None:
            raise ValueError("Meshtastic Bluetooth address is invalid")
        if _LOCAL_ADAPTER_RE.fullmatch(adapter) is None:
            raise ValueError("Local Bluetooth adapter identity is invalid")
        if _BLUETOOTH_ADDRESS_RE.fullmatch(canonical_adapter_address) is None:
            raise ValueError("Local Bluetooth adapter address is invalid")

        self._hass = hass
        self._address = canonical_address
        self._adapter = adapter
        self._adapter_address = canonical_adapter_address
        self._phase_callback = phase_callback
        self._resolution_attempts = 0
        self._resolution_successes = 0
        self._last_resolution_result = "not_started"
        from .aiomeshtastic import MeshtasticBluetoothClient

        self._client = MeshtasticBluetoothClient(
            address=canonical_address,
            device_provider=self._resolve_local_device,
            state_callback=phase_callback,
            logger=logger,
        )

    @property
    def connected(self) -> bool:
        """Return whether the protobuf handshake and GATT link are active."""
        return self._client.connected

    async def async_start(self) -> None:
        """Start the bounded persistent Bluetooth session."""
        await self._client.async_start()

    async def async_stop(self) -> None:
        """Stop all session owners and confirm GATT teardown."""
        await self._client.async_stop()

    async def async_send_text(
        self,
        *,
        target_node: str | None,
        message: str,
        channel: str | None,
        priority: str,
        message_type: str,
    ) -> int:
        """Translate MeshNet's send contract and return the on-air packet ID."""
        del message_type
        channel_index = 0 if channel is None else coerce_int(channel)
        if channel_index is None or not 0 <= channel_index <= 7:
            raise ValueError("Meshtastic Bluetooth channel must be between 0 and 7")
        return await self._client.async_send_text(
            message,
            destination_id=target_node,
            channel_index=channel_index,
            want_ack=priority.casefold() in {"high", "emergency"},
        )

    async def async_node_snapshot(self) -> dict[int, dict[str, Any]]:
        """Return a detached plain-dictionary node snapshot."""
        return await self._client.async_node_snapshot()

    def add_packet_callback(
        self,
        callback: Callable[[dict[str, Any]], Any],
    ) -> Callable[[], None]:
        """Register one transport-local packet callback."""
        return self._client.add_packet_callback(callback)

    def add_connection_callback(
        self,
        callback: Callable[[bool], Any],
    ) -> Callable[[], None]:
        """Register one transport-local link-state callback."""
        return self._client.add_connection_callback(callback)

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return cached endpoint-free lifecycle diagnostics."""
        snapshot = self._client.diagnostic_snapshot()
        snapshot.update(
            {
                "adapter_scoped_resolution": True,
                "resolution_attempts": self._resolution_attempts,
                "resolution_successes": self._resolution_successes,
                "last_resolution_result": self._last_resolution_result,
            }
        )
        return snapshot

    def _resolve_local_device(self) -> Any | None:
        """Resolve exactly one fresh BLEDevice from the verified local scanner.

        Home Assistant's nearest-device helper may choose another controller or
        a network Bluetooth proxy.  Per-scanner candidates are required here;
        a candidate must carry affirmative local-controller evidence, and any
        contradictory BlueZ identity rejects it.
        """
        from homeassistant.components import bluetooth

        self._resolution_attempts += 1
        resolver = getattr(bluetooth, "async_scanner_devices_by_address", None)
        if not callable(resolver):
            self._last_resolution_result = "per_scanner_api_unavailable"
            return None

        candidates = resolver(self._hass, self._address, connectable=True)
        matches: list[Any] = []
        for candidate in candidates:
            scanner = getattr(candidate, "scanner", None)
            device = getattr(candidate, "ble_device", None)
            if scanner is None or device is None:
                continue
            device_address = getattr(device, "address", None)
            if (
                not isinstance(device_address, str)
                or device_address.upper() != self._address
            ):
                continue
            if self._candidate_matches_selected_adapter(scanner, device):
                matches.append(device)

        if len(matches) != 1:
            self._last_resolution_result = (
                "selected_adapter_not_visible"
                if not matches
                else "selected_adapter_ambiguous"
            )
            return None
        self._resolution_successes += 1
        self._last_resolution_result = "matched_verified_local_adapter"
        return matches[0]

    def _candidate_matches_selected_adapter(self, scanner: Any, device: Any) -> bool:
        """Require affirmative identity evidence and reject contradictions."""
        affirmative = False

        scanner_adapter = getattr(scanner, "adapter", None)
        if isinstance(scanner_adapter, str) and scanner_adapter:
            if _LOCAL_ADAPTER_RE.fullmatch(scanner_adapter):
                if scanner_adapter != self._adapter:
                    return False
                affirmative = True

        scanner_source = getattr(scanner, "source", None)
        if isinstance(scanner_source, str) and scanner_source:
            source = scanner_source.upper()
            if _BLUETOOTH_ADDRESS_RE.fullmatch(source):
                if source != self._adapter_address:
                    return False
                affirmative = True

        details = getattr(device, "details", None)
        if isinstance(details, Mapping):
            raw_path = details.get("path")
            if isinstance(raw_path, str) and raw_path:
                path_adapters = set(re.findall(r"(?:^|/)hci[0-9]+(?=/|$)", raw_path))
                normalized = {value.rsplit("/", 1)[-1] for value in path_adapters}
                if normalized:
                    if normalized != {self._adapter}:
                        return False
                    affirmative = True
            raw_source = details.get("source")
            if isinstance(raw_source, str) and raw_source:
                source = raw_source.upper()
                if _BLUETOOTH_ADDRESS_RE.fullmatch(source):
                    if source != self._adapter_address:
                        return False
                    affirmative = True

        return affirmative
