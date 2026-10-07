"""Config flow for Oticat Energy.

The integration is added once and holds the service URL. Each inverter is a subentry with
its own site token from dom.oti.cat, where the battery, solar and grid settings are made.
Here each inverter only gets its token and its local entities.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_GRID_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_INVERTER,
    CONF_INVERTER_PREFIX,
    CONF_LOAD_ENTITY,
    CONF_PROGRAM_POWER,
    CONF_SOLAR_ENTITY,
    CONF_SITE_ID,
    CONF_SOC_ENTITY,
    CONF_TOKEN,
    CONF_URL,
    CONF_ZONE,
    DEFAULT_URL,
    DEFAULT_ZONE,
    DOMAIN,
    INVERTER_DEYE,
    INVERTER_NONE,
    SUBENTRY_INVERTER,
)
from .coordinator import PlanRequestError, entities, location, read_soc, request_plan


def _inverter_schema(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_TOKEN, default=d.get(CONF_TOKEN, "")): str,
        vol.Required(CONF_SOC_ENTITY, description={"suggested_value": d.get(CONF_SOC_ENTITY)}): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor", "input_number", "number"])
        ),
        vol.Optional(CONF_LOAD_ENTITY, description={"suggested_value": d.get(CONF_LOAD_ENTITY)}): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        ),
        vol.Optional(CONF_GRID_ENTITY, description={"suggested_value": d.get(CONF_GRID_ENTITY)}): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        ),
        vol.Optional(CONF_GRID_EXPORT_ENTITY, description={"suggested_value": d.get(CONF_GRID_EXPORT_ENTITY)}): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        ),
        vol.Optional(CONF_SOLAR_ENTITY, description={"suggested_value": d.get(CONF_SOLAR_ENTITY)}): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        ),
        vol.Required(CONF_ZONE, default=d.get(CONF_ZONE, DEFAULT_ZONE)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="zone")
        ),
        vol.Required(CONF_INVERTER, default=d.get(CONF_INVERTER, INVERTER_DEYE)): selector.SelectSelector(
            selector.SelectSelectorConfig(options=[INVERTER_DEYE, INVERTER_NONE], translation_key="inverter")
        ),
        vol.Required(CONF_INVERTER_PREFIX, default=d.get(CONF_INVERTER_PREFIX, "deye")): str,
        vol.Required(CONF_PROGRAM_POWER, default=d.get(CONF_PROGRAM_POWER, 5000)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=12000, step=100, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX)
        ),
    })


async def _validate(hass: HomeAssistant, url: str, conf: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Ask the service for a real plan. Returns (error key, site info from the service)."""
    soc = read_soc(hass, conf[CONF_SOC_ENTITY])
    if soc is None:
        return "soc_unavailable", {}
    if conf[CONF_INVERTER] == INVERTER_DEYE:
        prefix = conf[CONF_INVERTER_PREFIX]
        if not any(hass.states.get(f"{d}.{prefix}_program_1_soc") for d in ("number", "input_number")):
            return "inverter_not_found", {}
    try:
        body = {"location": location(hass, conf.get(CONF_ZONE)), "state": {"soc": soc}, "entities": entities(conf)}
        plan = await request_plan(hass, {CONF_URL: url, **conf}, body)
    except PlanRequestError as err:
        return ("invalid_auth" if err.unauthorized else "cannot_connect"), {}
    return None, plan.get("site", {})


class EnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Oticat Energy", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str}),
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data={CONF_URL: user_input[CONF_URL]})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_URL, default=entry.data.get(CONF_URL, DEFAULT_URL)): str}),
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_INVERTER: InverterSubentryFlow}


class InverterSubentryFlow(ConfigSubentryFlow):
    """Adds or reconfigures one inverter, identified by its dom.oti.cat site."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()
        if user_input is not None:
            error, site = await _validate(self.hass, entry.data.get(CONF_URL, DEFAULT_URL), user_input)
            if error is None:
                data = {**user_input, CONF_SITE_ID: site.get("id")}
                title = site.get("name") or "Inverter"
                # A new token for a site already set up here replaces that inverter's
                # settings instead of adding a second copy of the same site.
                for sub in entry.subentries.values():
                    if sub.subentry_type == SUBENTRY_INVERTER and sub.data.get(CONF_SITE_ID) == data[CONF_SITE_ID]:
                        self.hass.config_entries.async_update_subentry(entry, sub, data=data, title=title)
                        return self.async_abort(reason="site_updated")
                return self.async_create_entry(title=title, data=data, unique_id=data[CONF_SITE_ID])
            errors["base"] = error
        return self.async_show_form(step_id="user", data_schema=_inverter_schema(user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            error, site = await _validate(self.hass, entry.data.get(CONF_URL, DEFAULT_URL), user_input)
            if error is None and site.get("id") != subentry.data.get(CONF_SITE_ID):
                error = "different_site"
            if error is None:
                return self.async_update_and_abort(
                    entry, subentry, data={**user_input, CONF_SITE_ID: site["id"]}, title=site.get("name") or subentry.title
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure", data_schema=_inverter_schema(user_input or dict(subentry.data)), errors=errors
        )
