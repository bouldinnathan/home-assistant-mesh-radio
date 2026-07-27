"""Pure allowlists used to keep diagnostics free of provider-supplied PII."""

from __future__ import annotations

import re

_HARDWARE_MODELS = frozenset(
    {
        "DR_DEV",
        "HELTEC_V1",
        "HELTEC_V2_0",
        "HELTEC_V2_1",
        "HELTEC_V3",
        "HELTEC_WIRELESS_TRACKER",
        "HELTEC_WSL_V3",
        "LILYGO_TBEAM_S3_CORE",
        "NANO_G1",
        "NANO_G1_EXPLORER",
        "NRF52840_PCA10059",
        "PICOMPUTER_S3",
        "PORTDUINO",
        "RAK11200",
        "RAK2560",
        "RAK4631",
        "SEEED_XIAO_S3",
        "STATION_G1",
        "T-BEAM",
        "T-DECK",
        "T-ECHO",
        "T-WATCH-S3",
        "TBEAM",
        "T_DECK",
        "T_ECHO",
        "T_WATCH_S3",
        "TLORA_V1",
        "TLORA_V1_1P3",
        "TLORA_V2",
        "TLORA_V2_1_1P6",
        "TLORA_V2_1_1P8",
        "WIO_TRACKER_L1",
        "WIO_WM1110",
        "XIAO_NRF52840",
        "XIAO_S3",
    }
)

_RADIO_TYPES = frozenset(
    {
        "CC1101",
        "LR1110",
        "LR1120",
        "RF95",
        "SX1262",
        "SX1268",
        "SX1276",
        "SX1278",
        "SX1280",
        "SX1281",
    }
)

_ROLES = frozenset(
    {
        "CLIENT",
        "CLIENT_BASE",
        "CLIENT_HIDDEN",
        "CLIENT_MUTE",
        "COMPANION",
        "LOST_AND_FOUND",
        "REPEATER",
        "ROOM_SERVER",
        "ROUTER",
        "ROUTER_CLIENT",
        "ROUTER_LATE",
        "SENSOR",
        "SERVER",
        "TAK",
        "TAK_TRACKER",
        "TRACKER",
    }
)

_FIRMWARE_VERSION_RE = re.compile(
    r"(?i)v?\d{1,4}(?:\.\d{1,4}){1,3}(?:-(?:alpha|beta|rc|dev)\d{0,4})?\Z"
)


def safe_node_metadata(value: str | None, category: str) -> str | None:
    """Return provider metadata only when it matches a strict public allowlist."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if category == "firmware_version":
        return candidate if _FIRMWARE_VERSION_RE.fullmatch(candidate) else None

    normalized = candidate.upper().replace(" ", "_")
    allowed = {
        "hardware_model": _HARDWARE_MODELS,
        "radio_type": _RADIO_TYPES,
        "role": _ROLES,
    }.get(category)
    if allowed is None or normalized not in allowed:
        return None
    return normalized
