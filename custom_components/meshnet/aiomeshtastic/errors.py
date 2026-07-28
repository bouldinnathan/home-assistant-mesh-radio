# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
# SPDX-FileCopyrightText: 2025 Hendrik @novag
# SPDX-FileCopyrightText: 2026 MeshNet contributors
#
# SPDX-License-Identifier: MIT

"""Errors raised by MeshNet's bounded async Meshtastic transport."""


class MeshtasticAsyncError(RuntimeError):
    """Base error for the isolated async Meshtastic stack."""


class MeshtasticConnectionError(MeshtasticAsyncError):
    """A Bluetooth link or GATT operation failed."""


class MeshtasticNotConnectedError(MeshtasticConnectionError):
    """An operation requires an active protocol session."""


class MeshtasticConfigurationError(MeshtasticAsyncError):
    """The radio did not complete the Meshtastic configuration handshake."""


class MeshtasticCleanupError(MeshtasticAsyncError):
    """Bluetooth teardown could not be confirmed within its safety bound."""
