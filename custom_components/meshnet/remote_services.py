"""Narrow administrator actions for operator-created remote maintenance."""

from __future__ import annotations

import importlib
import re
from typing import Any

from .const import DOMAIN
from .remote_admin import RemoteAdminError
from .remote_health import approved_firmware


def exact_remote_target(value: Any) -> str:
    """Reject names, broadcasts, coercion and implicit destinations."""
    if not isinstance(value, str) or re.fullmatch(r"![0-9a-f]{8}", value) is None or value in {"!00000000", "!ffffffff"}:
        raise ValueError("target_node must be one exact lowercase !xxxxxxxx node ID")
    return value


def confirmed_reboot(value: Any) -> bool:
    """Scheduling is an explicit authorization to send a reboot at each run."""
    if value is not True:
        raise ValueError("confirm_remote must be true")
    return True


async def _require_admin(hass: Any, call: Any) -> None:
    """Match HA admin action semantics: admin users or system automations."""
    from homeassistant.exceptions import Unauthorized

    context = getattr(call, "context", None)
    if context is None:
        raise Unauthorized()
    if context.user_id is not None:
        user = await hass.auth.async_get_user(context.user_id)
        if user is None or not user.is_admin:
            raise Unauthorized()


def _coordinator_for_gateway(hass: Any, gateway_id: str) -> Any:
    """Resolve an exact configured gateway without first-entry fallback."""
    from homeassistant.exceptions import HomeAssistantError

    matches = [
        coordinator for coordinator in hass.data.get(DOMAIN, {}).values()
        if gateway_id in getattr(coordinator, "gateways", {})
    ]
    if len(matches) != 1:
        raise HomeAssistantError("Select one unique configured MeshNet gateway")
    return matches[0]


def register_remote_services(hass: Any, response_mode: Any) -> None:
    """Expose only health reads and confirmed reboots, never generic admin."""
    import voluptuous as vol

    cv = importlib.import_module("homeassistant.helpers.config_validation")

    common = {
        vol.Required("gateway_id"): cv.string,
        vol.Required("target_node"): exact_remote_target,
    }

    async def check_remote_node(call: Any) -> dict[str, Any] | None:
        await _require_admin(hass, call)
        coordinator = _coordinator_for_gateway(hass, call.data["gateway_id"])
        response = await coordinator.async_check_remote_node(
            gateway_id=call.data["gateway_id"],
            target_node=call.data["target_node"],
            minimum_firmware=call.data.get("minimum_firmware", ""),
        )
        # The caller already supplied both identities. Keep service responses
        # identity-free, including generic HA websocket debug logs.
        return {
            key: response[key] for key in (
                "schema_version", "checked_at", "status", "reason",
                "firmware_version", "minimum_firmware", "firmware_status",
            ) if key in response
        } if call.return_response else None

    async def reboot_remote_node(call: Any) -> dict[str, Any] | None:
        from homeassistant.exceptions import HomeAssistantError

        await _require_admin(hass, call)
        coordinator = _coordinator_for_gateway(hass, call.data["gateway_id"])
        try:
            if not await coordinator.store.async_reserve_remote_reboot(
                call.data["gateway_id"], call.data["target_node"]
            ):
                raise RemoteAdminError("remote_admin_reboot_cooldown")
            response = await coordinator.async_remote_reboot(
                gateway_id=call.data["gateway_id"],
                target_node=call.data["target_node"],
                confirm_remote=call.data["confirm_remote"],
            )
        except RemoteAdminError as err:
            raise HomeAssistantError(err.public_message) from None
        except Exception:
            raise HomeAssistantError("Remote reboot could not be confirmed; do not repeat it blindly") from None
        return {key: response[key] for key in ("schema_version", "status") if key in response} if call.return_response else None

    hass.services.async_register(
        DOMAIN, "check_remote_node", check_remote_node,
        schema=vol.Schema({**common, vol.Optional("minimum_firmware", default=""): approved_firmware}),
        supports_response=response_mode,
    )
    hass.services.async_register(
        DOMAIN, "reboot_remote_node", reboot_remote_node,
        schema=vol.Schema({**common, vol.Required("confirm_remote"): confirmed_reboot}),
        supports_response=response_mode,
    )
