"""Repair flows for MeshNet."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


class MeshNetDismissRepairFlow(RepairsFlow):
    """A simple flow that clears a generated MeshNet issue."""

    def __init__(self, hass: HomeAssistant, issue_id: str) -> None:
        self._hass = hass
        self._issue_id = issue_id

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Handle the repair flow."""
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Confirm issue dismissal."""
        if user_input is not None:
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow."""
    return MeshNetDismissRepairFlow(hass, issue_id)
