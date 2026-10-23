"""Run maintenance blueprint branches in Home Assistant's script engine."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.automation.config import (  # noqa: E402
    AUTOMATION_BLUEPRINT_SCHEMA,
    PLATFORM_SCHEMA,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs  # noqa: E402
from homeassistant.core import Context, HomeAssistant, SupportsResponse  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import script as script_helper  # noqa: E402
from homeassistant.util.yaml import load_yaml  # noqa: E402

ROOT = Path(__file__).parents[1]
RESPONDING = {"status": "responding", "reason": None, "firmware_status": "current", "firmware_version": "2.7.11"}
NO_REPLY = {"status": "no_response", "reason": "remote_admin_no_response", "firmware_status": "unknown"}


def _without_waits(value):
    """Shorten validated delays without depending on HA script internals."""
    if isinstance(value, dict):
        return {key: timedelta(0) if key == "delay" else _without_waits(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_waits(item) for item in value]
    return value


async def _run_blueprint(tmp_path, name, health_results, *, reboot_status="acknowledged"):
    hass = HomeAssistant(str(tmp_path))
    calls = []
    replies = iter(health_results)

    async def check(call):
        calls.append(("check", dict(call.data)))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def reboot(call):
        calls.append(("reboot", dict(call.data)))
        return {"status": reboot_status}

    async def notification(call):
        calls.append((call.service, dict(call.data)))

    hass.services.async_register("meshnet", "check_remote_node", check, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register("meshnet", "reboot_remote_node", reboot, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register("persistent_notification", "create", notification)
    hass.services.async_register("persistent_notification", "dismiss", notification)
    hass.services.async_register("test", "notify", notification)
    path = ROOT / "blueprints/automation/meshnet" / f"{name}.yaml"
    blueprint = Blueprint(load_yaml(str(path)), schema=AUTOMATION_BLUEPRINT_SCHEMA, expected_domain="automation")
    inputs = {
        "gateway_id": "test-gateway",
        "target_node": "!1234abcd",
        "notify_actions": [{"action": "test.notify", "data": {"message": "{{ notification_message }}"}}],
    }
    if name == "remote_node_health_notification":
        inputs["minimum_firmware"] = "2.7.11"
    configured = BlueprintInputs(blueprint, {"use_blueprint": {"path": str(path), "input": inputs}})
    configured.validate()
    config = PLATFORM_SCHEMA(configured.async_substitute())
    script = script_helper.Script(hass, _without_waits(config["actions"]), name, "automation", variables=config["variables"])
    try:
        await script.async_run(context=Context())
    finally:
        await script.async_stop()
        await hass.async_stop(force=True)
    return calls


@pytest.mark.parametrize(("responses", "reboot_status", "reboots", "alerts"), [
    ([RESPONDING, RESPONDING], "acknowledged", 1, 0),
    ([NO_REPLY], "acknowledged", 0, 1),
    ([RESPONDING, NO_REPLY], "acknowledged", 1, 1),
    ([RESPONDING, RESPONDING], "unconfirmed", 1, 1),
])
def test_reboot_blueprint_preflight_one_send_and_postflight(tmp_path, responses, reboot_status, reboots, alerts):
    calls = asyncio.run(_run_blueprint(tmp_path, "scheduled_remote_node_reboot", responses, reboot_status=reboot_status))
    assert sum(name == "reboot" for name, _ in calls) == reboots
    assert sum(name == "create" for name, _ in calls) == alerts
    assert sum(name == "notify" for name, _ in calls) == alerts
    for name, data in calls:
        if name in {"check", "reboot"}:
            assert data["target_node"] == "!1234abcd"
            assert data["gateway_id"] == "test-gateway"
        if name == "reboot":
            assert data["confirm_remote"] is True


@pytest.mark.parametrize(("responses", "checks", "alerts", "title"), [
    ([RESPONDING], 1, 0, None),
    ([NO_REPLY, RESPONDING], 2, 0, None),
    ([NO_REPLY, NO_REPLY], 2, 1, "not responding"),
    ([{**RESPONDING, "firmware_status": "outdated", "firmware_version": "2.6.0"}], 1, 1, "firmware needs maintenance"),
    ([{**RESPONDING, "firmware_status": "unknown", "firmware_version": None}], 1, 1, "firmware unknown"),
    ([{**NO_REPLY, "status": "not_checked", "reason": "remote_admin_controller_unauthorized"}], 1, 1, "check unavailable"),
    ([{**NO_REPLY, "status": "not_checked", "reason": "remote_admin_rate_limited"}], 1, 0, None),
    ([NO_REPLY, HomeAssistantError("Check failed")], 2, 1, "check unavailable"),
])
def test_monitoring_blueprint_distinguishes_health_conditions(tmp_path, responses, checks, alerts, title):
    calls = asyncio.run(_run_blueprint(tmp_path, "remote_node_health_notification", responses))
    assert sum(name == "check" for name, _ in calls) == checks
    assert sum(name == "create" for name, _ in calls) == alerts
    assert sum(name == "notify" for name, _ in calls) == alerts
    assert all(name != "reboot" for name, _ in calls)
    if title:
        message = next(data for name, data in calls if name == "create")
        assert title in message["title"]
        assert "!1234abcd" in message["message"]
