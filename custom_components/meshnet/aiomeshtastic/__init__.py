# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
# SPDX-FileCopyrightText: 2025 Hendrik @novag
# SPDX-FileCopyrightText: 2026 MeshNet contributors
#
# SPDX-License-Identifier: MIT

"""Isolated, Bluetooth-only async Meshtastic protocol stack.

The high-level client is imported lazily so transport safety tests do not need
the optional Meshtastic protobuf package installed.
"""

from __future__ import annotations

from typing import Any

from .bluetooth import BluetoothConnection
from .errors import (
    MeshtasticAsyncError,
    MeshtasticCleanupError,
    MeshtasticConfigurationError,
    MeshtasticConnectionError,
    MeshtasticNotConnectedError,
)

__all__ = [
    "BluetoothConnection",
    "MeshtasticAsyncError",
    "MeshtasticBluetoothClient",
    "MeshtasticCleanupError",
    "MeshtasticConfigurationError",
    "MeshtasticConnectionError",
    "MeshtasticNotConnectedError",
]


def __getattr__(name: str) -> Any:
    """Load the protobuf-dependent client only when requested."""
    if name == "MeshtasticBluetoothClient":
        from .client import MeshtasticBluetoothClient

        return MeshtasticBluetoothClient
    raise AttributeError(name)
