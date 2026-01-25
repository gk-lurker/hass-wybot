"""Options flow for WyBot."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    OPT_DP0_DELAY_SECONDS,
    OPT_TS_OFFSET_SECONDS,
    DEFAULT_DP0_DELAY_SECONDS,
    DEFAULT_TS_OFFSET_SECONDS,
)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle WyBot options.

    IMPORTANT:
    Do not override __init__. Home Assistant's OptionsFlow base class
    handles config_entry wiring; overriding it can cause 500s.
    """

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Manage the WyBot options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        dp0_delay_default = float(
            self.config_entry.options.get(OPT_DP0_DELAY_SECONDS, DEFAULT_DP0_DELAY_SECONDS)
        )
        ts_offset_default = int(
            self.config_entry.options.get(OPT_TS_OFFSET_SECONDS, DEFAULT_TS_OFFSET_SECONDS)
        )

        schema = vol.Schema(
            {
                vol.Required(OPT_DP0_DELAY_SECONDS, default=dp0_delay_default): vol.All(
                    vol.Coerce(float),
                    vol.Clamp(min=0.0, max=60.0),
                ),
                vol.Required(OPT_TS_OFFSET_SECONDS, default=ts_offset_default): vol.Coerce(int),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
