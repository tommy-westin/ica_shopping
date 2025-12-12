from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import callback
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import selector, BooleanSelector

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


CONF_SESSION_ID = "session_id"
CONF_ICA_LIST_ID = "ica_list_id"
CONF_TODO_ENTITY_ID = "todo_entity_id"
CONF_REMOVE_STRIKED = "remove_striked"


class ICAConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlow:
        return ICAOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            session_id = (user_input.get(CONF_SESSION_ID) or "").strip()
            list_id = (user_input.get(CONF_ICA_LIST_ID) or "").strip()

            if not session_id or not list_id:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(),
                    errors={"base": "required"},
                )

            data = dict(user_input)
            data[CONF_SESSION_ID] = session_id
            data[CONF_ICA_LIST_ID] = list_id
            data[CONF_REMOVE_STRIKED] = bool(user_input.get(CONF_REMOVE_STRIKED, True))

            return self.async_create_entry(title=list_id, data=data)

        return self.async_show_form(step_id="user", data_schema=self._user_schema())

    def _user_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_SESSION_ID): str,
                vol.Required(CONF_ICA_LIST_ID): str,
                vol.Optional(CONF_TODO_ENTITY_ID): selector(
                    {
                        "entity": {
                            "domain": "todo",
                            "multiple": False,
                        }
                    }
                ),
                vol.Optional(CONF_REMOVE_STRIKED, default=True): BooleanSelector(),
            }
        )


class ICAOptionsFlowHandler(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self.config_entry

        current_list_id = entry.options.get(CONF_ICA_LIST_ID, entry.data.get(CONF_ICA_LIST_ID, ""))

        if user_input is not None:
            new_list_id = (user_input.get(CONF_ICA_LIST_ID, current_list_id) or "").strip()
            if new_list_id != current_list_id and current_list_id:
                _LOGGER.warning(
                    "ICA list changed from %s to %s. This may trigger Keep-to-ICA resync.",
                    current_list_id,
                    new_list_id,
                )

            new_options = dict(entry.options)
            new_options[CONF_SESSION_ID] = (user_input.get(CONF_SESSION_ID) or "").strip()
            new_options[CONF_ICA_LIST_ID] = new_list_id
            new_options[CONF_TODO_ENTITY_ID] = user_input.get(CONF_TODO_ENTITY_ID)
            new_options[CONF_REMOVE_STRIKED] = bool(user_input.get(CONF_REMOVE_STRIKED, True))

            return self.async_create_entry(title="", data=new_options)

        schema_dict = {
            vol.Required(
                CONF_SESSION_ID,
                default=entry.options.get(CONF_SESSION_ID, entry.data.get(CONF_SESSION_ID, "")),
            ): str,
            vol.Required(CONF_ICA_LIST_ID, default=current_list_id): str,
            vol.Optional(
                CONF_TODO_ENTITY_ID,
                default=entry.options.get(CONF_TODO_ENTITY_ID, entry.data.get(CONF_TODO_ENTITY_ID, "")),
            ): selector(
                {
                    "entity": {
                        "domain": "todo",
                        "multiple": False,
                    }
                }
            ),
            vol.Optional(
                CONF_REMOVE_STRIKED,
                default=entry.options.get(CONF_REMOVE_STRIKED, True),
            ): BooleanSelector(),
        }

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict))
