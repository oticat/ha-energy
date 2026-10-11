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
    CONF_GRID_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_HEATER_CONSUMING_STATE,
    CONF_HEATER_FLOW_ENTITY,
    CONF_HEATER_GRID_ENTITY,
    CONF_HEATER_MARGIN,
    CONF_HEATER_MAX_AZIMUTH,
    CONF_HEATER_MAX_IMPORT_W,
    CONF_HEATER_MIN_AZIMUTH,
    CONF_HEATER_MIN_ELEVATION,
    CONF_HEATER_SURPLUS_W,
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
    DEFAULT_HEATER_CONSUMING_STATE,
    DEFAULT_HEATER_HVAC_MODE,
    DEFAULT_HEATER_MARGIN,
    DEFAULT_HEATER_MAX_AZIMUTH,
    DEFAULT_HEATER_MAX_IMPORT_W,
    DEFAULT_HEATER_MIN_AZIMUTH,
    DEFAULT_HEATER_MIN_ELEVATION,
    DEFAULT_HEATER_SURPLUS_W,
    DEFAULT_URL,
    DEFAULT_ZONE,
    DOMAIN,
    INVERTER_DEYE,
    INVERTER_NONE,
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


def _degrees(minimum: float, maximum: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=1, unit_of_measurement="°", mode=selector.NumberSelectorMode.BOX
        )
    )


def _heater_sections(d: dict[str, Any]) -> dict[Any, Any]:
    """The collapsible water-heater sections: shared settings plus one section per heater slot.

    Every field is optional. Leave the heater slots empty to keep the integration inverter-only.
    Priority is the slot order: heater 1 runs before heater 2, and so on.
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
        vol.Optional(
            CONF_HEATER_SURPLUS_W, default=d.get(CONF_HEATER_SURPLUS_W, DEFAULT_HEATER_SURPLUS_W)
        ): _watts(0, 1000, 50),
        vol.Optional(
            CONF_HEATER_MAX_IMPORT_W, default=d.get(CONF_HEATER_MAX_IMPORT_W, DEFAULT_HEATER_MAX_IMPORT_W)
        ): _watts(500, 15000, 100),
        vol.Optional(CONF_HEATER_MARGIN, default=d.get(CONF_HEATER_MARGIN, DEFAULT_HEATER_MARGIN)): _watts(0, 500, 10),
        vol.Optional(
            CONF_HEATER_MIN_ELEVATION, default=d.get(CONF_HEATER_MIN_ELEVATION, DEFAULT_HEATER_MIN_ELEVATION)
        ): _degrees(0, 45),
        vol.Optional(
            CONF_HEATER_MIN_AZIMUTH, default=d.get(CONF_HEATER_MIN_AZIMUTH, DEFAULT_HEATER_MIN_AZIMUTH)
        ): _degrees(0, 179),
        vol.Optional(
            CONF_HEATER_MAX_AZIMUTH, default=d.get(CONF_HEATER_MAX_AZIMUTH, DEFAULT_HEATER_MAX_AZIMUTH)
        ): _degrees(181, 360),
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
                return self.async_update_and_abort(
                    entry, subentry, data={**flat, CONF_SITE_ID: site["id"]}, title=site.get("name") or subentry.title
                )
            errors["base"] = error
        return self.async_show_form(step_id="reconfigure", data_schema=_inverter_schema(flat), errors=errors)
