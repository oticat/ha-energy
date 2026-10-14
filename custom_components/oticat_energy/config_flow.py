"""Config flow for Oticat Energy.

The integration is added once and holds the service URL. Each inverter is a subentry with
its own site token from dom.oti.cat, where the battery, solar and grid settings are made.
Here each inverter only gets its token and its local entities.

The same subentry can also drive the site's water heaters: solar surplus first, and the
cheap-grid hours the service marks, replacing the solar_surplus_heater blueprint.
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
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    CONF_HEATER_CONSUMING_STATE,
    CONF_HEATER_FLOW_ENTITY,
    CONF_HEATER_GRID_ENTITY,
    CONF_INVERTER,
    CONF_INVERTER_PREFIX,
    CONF_SITE_ID,
    CONF_SOC_ENTITY,
    CONF_TOKEN,
    CONF_URL,
    CONF_ZONE,
    DEFAULT_HEATER_CONSUMING_STATE,
    DEFAULT_HEATER_HVAC_MODE,
    DEFAULT_URL,
    DEFAULT_ZONE,
    DOMAIN,
    INVERTER_DEYE,
    MAX_HEATERS,
    SUBENTRY_INVERTER,
    heater_key,
)
from .coordinator import PlanRequestError, entities, location, read_soc, request_plan

SECTION_WATER_HEATERS = "water_heaters"
HVAC_MODES = ["cool", "heat", "fan_only", "dry", "auto"]


def _watts(minimum: float, maximum: float, step: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=step, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX
        )
    )


def _heater_sections(d: dict[str, Any]) -> dict[Any, Any]:
    """The collapsible water-heater sections: shared entities plus one section per heater slot.

    Every field is optional. Leave the heater slots empty to keep the integration inverter-only.
    Priority is the slot order: heater 1 runs before heater 2, and so on.

    Only what is local to this house is here. The thresholds the controller runs on — the surplus,
    the import ceiling, the sun window, the dry-spell limits — are the site's energy policy and are
    set on dom.oti.cat, arriving with each plan.
    """
    shared = {
        vol.Optional(
            CONF_HEATER_GRID_ENTITY, description={"suggested_value": d.get(CONF_HEATER_GRID_ENTITY)}
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_HEATER_FLOW_ENTITY, description={"suggested_value": d.get(CONF_HEATER_FLOW_ENTITY)}
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_HEATER_CONSUMING_STATE, default=d.get(CONF_HEATER_CONSUMING_STATE, DEFAULT_HEATER_CONSUMING_STATE)
        ): str,
    }
    out: dict[Any, Any] = {vol.Optional(SECTION_WATER_HEATERS): section(vol.Schema(shared), {"collapsed": True})}
    for i in range(1, MAX_HEATERS + 1):
        out[vol.Optional(f"heater_{i}")] = section(
            vol.Schema({
                vol.Optional(
                    heater_key(i, "entity"), description={"suggested_value": d.get(heater_key(i, "entity"))}
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["switch", "climate", "input_boolean"])
                ),
                vol.Optional(
                    heater_key(i, "power"), description={"suggested_value": d.get(heater_key(i, "power"))}
                ): _watts(0, 15000, 50),
                vol.Optional(
                    heater_key(i, "hvac_mode"), default=d.get(heater_key(i, "hvac_mode"), DEFAULT_HEATER_HVAC_MODE)
                ): selector.SelectSelector(selector.SelectSelectorConfig(options=HVAC_MODES)),
            }),
            {"collapsed": True},
        )
    return out


def _inverter_schema(d: dict[str, Any]) -> vol.Schema:
    """The site's token, and what is local to this house.

    Which sensors the site reads, the inverter control, its prefix and the program power are set on
    dom.oti.cat, from the list this side reports, and arrive with the plan — so they are not asked
    here. The keys still live in the subentry, written by the integration as the service sends
    them, which is also what a site that predates the service owning them keeps working on.
    """
    return vol.Schema({
        vol.Required(CONF_TOKEN, default=d.get(CONF_TOKEN, "")): str,
        **_heater_sections(d),
    })


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    """Lift the fields out of their collapsible sections so the subentry data is one flat dict."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def _heater_errors(d: dict[str, Any]) -> str | None:
    """Consistency of the water-heater settings, or None."""
    active = [d.get(heater_key(i, "entity")) for i in range(1, MAX_HEATERS + 1)]
    if not any(active):
        return None
    if not d.get(CONF_HEATER_GRID_ENTITY):
        return "heater_grid_required"
    for i in range(1, MAX_HEATERS + 1):
        if d.get(heater_key(i, "entity")) and not d.get(heater_key(i, "power")):
            return "heater_power_required"
    return None


async def _validate(hass: HomeAssistant, url: str, conf: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Ask the service for a real plan. Returns (error key, site info from the service).

    A site that has no entities yet is fine: the request goes out without a SOC and the service
    answers with the configuration and the entity catalogue, which is what the settings page
    needs to be able to offer the right ones.
    """
    soc = read_soc(hass, conf[CONF_SOC_ENTITY]) if conf.get(CONF_SOC_ENTITY) else None
    if conf.get(CONF_SOC_ENTITY) and soc is None:
        return "soc_unavailable", {}
    if conf.get(CONF_INVERTER) == INVERTER_DEYE and conf.get(CONF_INVERTER_PREFIX):
        prefix = conf[CONF_INVERTER_PREFIX]
        if not any(hass.states.get(f"{d}.{prefix}_program_1_soc") for d in ("number", "input_number")):
            return "inverter_not_found", {}
    try:
        body = {
            "location": location(hass, conf.get(CONF_ZONE)),
            "state": {} if soc is None else {"soc": soc},
            "entities": entities(conf),
        }
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
        flat = _flatten(user_input) if user_input else {}
        if user_input is not None:
            error = _heater_errors(flat)
            if error is None:
                error, site = await _validate(self.hass, entry.data.get(CONF_URL, DEFAULT_URL), flat)
            if error is None:
                data = {**flat, CONF_SITE_ID: site.get("id")}
                title = site.get("name") or "Inverter"
                # A new token for a site already set up here replaces that inverter's
                # settings instead of adding a second copy of the same site.
                for sub in entry.subentries.values():
                    if sub.subentry_type == SUBENTRY_INVERTER and sub.data.get(CONF_SITE_ID) == data[CONF_SITE_ID]:
                        self.hass.config_entries.async_update_subentry(entry, sub, data=data, title=title)
                        return self.async_abort(reason="site_updated")
                return self.async_create_entry(title=title, data=data, unique_id=data[CONF_SITE_ID])
            errors["base"] = error
        return self.async_show_form(step_id="user", data_schema=_inverter_schema(flat), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        flat = _flatten(user_input) if user_input else dict(subentry.data)
        if user_input is not None:
            error = _heater_errors(flat)
            site: dict[str, Any] = {}
            if error is None:
                error, site = await _validate(self.hass, entry.data.get(CONF_URL, DEFAULT_URL), flat)
            if error is None and site.get("id") != subentry.data.get(CONF_SITE_ID):
                error = "different_site"
            if error is None:
                # The subentry also holds what the service sent — the entities it reads and drives — and
                # this form does not know about them, so they are carried over rather than dropped.
                return self.async_update_and_abort(
                    entry, subentry, data={**subentry.data, **flat, CONF_SITE_ID: site["id"]},
                    title=site.get("name") or subentry.title,
                )
            errors["base"] = error
        return self.async_show_form(step_id="reconfigure", data_schema=_inverter_schema(flat), errors=errors)
