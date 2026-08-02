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


class MeshtasticNeighborInfoError(MeshtasticConfigurationError):
    """A NeighborInfo request failed with a fixed, identity-free code."""

    _CODES = frozenset(
        {
            "neighbor_info_disconnected",
            "neighbor_info_lifecycle_changed",
            "neighbor_info_rejected",
            "neighbor_info_send_failed",
            "neighbor_info_timeout",
            "neighbor_info_unsupported",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if code in self._CODES else "neighbor_info_send_failed"
        super().__init__(self.code)


class MeshtasticRemoteAdminError(MeshtasticConfigurationError):
    """A remote-admin request failed with a stable, non-secret category."""

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


class MeshtasticCleanupError(MeshtasticAsyncError):
    """Bluetooth teardown could not be confirmed within its safety bound."""
