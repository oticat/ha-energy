"""Config flow for Oticat Energy.

The integration is added once and holds the service URL. Each inverter is a subentry with its own
site token from dom.oti.cat, where the battery, solar and grid settings are made, along with the
entities the site reads and drives. Here a subentry only asks for that token: everything else is
chosen on the settings page, from the list this side reports, and arrives with each plan.

What is *not* the service's is which entities it may use at all: that is this Home Assistant's, set
in the install flow (all the entities found, or a subset) and changed later in the options. The
service chooses among them; anything else it names is refused before it is read or switched.

The same subentry also drives the site's high-consumption devices — solar surplus first, then the
cheap-grid hours the service marks — which is what replaced the solar_surplus_heater blueprint.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .catalogue import APPROVABLE_DOMAINS, candidate_ids
from .const import (
    CONF_APPROVE,
    CONF_APPROVED,
    CONF_APPROVED_ENTITIES,
    CONF_APPROVED_INVERTERS,
    CONF_INVERTER,
    CONF_INVERTER_PREFIX,
    CONF_SITE_ID,
    CONF_SOC_ENTITY,
    CONF_TOKEN,
    CONF_URL,
    CONF_ZONE,
    DEFAULT_URL,
    DEFAULT_ZONE,
    DOMAIN,
    INVERTER_DEYE,
    SUBENTRY_INVERTER,
)
from .coordinator import PlanRequestError, entities, location, read_soc, request_plan

def _approval_schema(
    approved: list[str], inverters: list[str], approved_inverters: list[str]
) -> vol.Schema:
    """The entities and inverters this Home Assistant allows the service to use.

    Defaults are what is approved now, so opening this form and saving changes nothing; anything
    else has to be ticked. The inverter prefixes are the ones found on this installation plus the
    ones already approved, since a prefix is written to by name rather than read.
    """
    schema: dict[Any, Any] = {
        vol.Optional(CONF_APPROVED_ENTITIES, default=approved): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=list(APPROVABLE_DOMAINS), multiple=True)
        ),
    }
    # No inverter found (and none approved) means no field to show: an empty options list is not a
    # selector.
    if inverters or approved_inverters:
        schema[vol.Optional(CONF_APPROVED_INVERTERS, default=sorted(set(approved_inverters)))] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(options=sorted(set(inverters) | set(approved_inverters)), multiple=True)
            )
        )
    return vol.Schema(schema)


def _inverter_schema(d: dict[str, Any]) -> vol.Schema:
    """The site's token, and nothing else.

    Which sensors the site reads, the inverter control, its prefix, the program power, the devices
    it drives and every threshold are set on dom.oti.cat, from the list this side reports, and
    arrive with the plan. Those keys still live in the subentry — written by the integration as the
    service sends them, and carried over by a reconfigure — which is also what a site that predates
    the service owning them keeps working on.
    """
    return vol.Schema({vol.Required(CONF_TOKEN, default=d.get(CONF_TOKEN, "")): str})


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
            self._url = user_input[CONF_URL]
            return await self.async_step_approve()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str}),
        )

    async def async_step_approve(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The permission: which of this installation's entities the service may use.

        It comes before the entry exists, so the user can see what a site would be able to read and
        drive — and what would leave for each of them — before any token is added. Declining is a
        valid answer: the integration loads, but with nothing allowed it reads nothing, drives
        nothing and offers the service nothing until it is allowed here.
        """
        ids, inverters = candidate_ids(self.hass)
        if user_input is not None:
            if not user_input.get(CONF_APPROVE):
                return self.async_create_entry(
                    title="Oticat Energy",
                    data={CONF_URL: self._url},
                    options={CONF_APPROVED: {CONF_APPROVED_ENTITIES: [], CONF_APPROVED_INVERTERS: []}},
                )
            return await self.async_step_choose()
        return self.async_show_form(
            step_id="approve",
            data_schema=vol.Schema({vol.Required(CONF_APPROVE, default=False): bool}),
            # The noun travels with the number: a description cannot pluralise, and "1 inverters"
            # is what it read before.
            description_placeholders={
                "entities": f"{len(ids)} {'entity' if len(ids) == 1 else 'entities'}",
                "inverters": f"{len(inverters)} {'inverter' if len(inverters) == 1 else 'inverters'}",
            },
        )

    async def async_step_choose(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """All of them, or a subset: the list is pre-filled with everything found."""
        ids, inverters = candidate_ids(self.hass)
        if user_input is not None:
            return self.async_create_entry(
                title="Oticat Energy",
                data={CONF_URL: self._url},
                options={CONF_APPROVED: {
                    CONF_APPROVED_ENTITIES: sorted(set(user_input.get(CONF_APPROVED_ENTITIES, []))),
                    CONF_APPROVED_INVERTERS: sorted(set(user_input.get(CONF_APPROVED_INVERTERS, []))),
                }},
            )
        return self.async_show_form(
            step_id="choose",
            data_schema=_approval_schema(ids, inverters, inverters),
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data={CONF_URL: user_input[CONF_URL]})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_URL, default=entry.data.get(CONF_URL, DEFAULT_URL)): str}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "EnergyOptionsFlow":
        return EnergyOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_INVERTER: InverterSubentryFlow}


class EnergyOptionsFlow(OptionsFlow):
    """The entities this Home Assistant allows the service to use, and nothing else.

    It is the same permission the install flow asks for, opened later: the current approvals are
    the defaults, so saving without touching them changes nothing, and anything new has to be
    ticked. A site that is using an entity unticked here gets it refused on its next plan, with the
    reason shown on its card.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        approved = self.config_entry.options.get(CONF_APPROVED)
        approved = approved if isinstance(approved, dict) else {}
        approved_ids = [e for e in (approved.get(CONF_APPROVED_ENTITIES) or []) if isinstance(e, str)]
        approved_inverters = [p for p in (approved.get(CONF_APPROVED_INVERTERS) or []) if isinstance(p, str)]
        _ids, found = candidate_ids(self.hass)
        if user_input is not None:
            return self.async_create_entry(data={CONF_APPROVED: {
                CONF_APPROVED_ENTITIES: sorted(set(user_input.get(CONF_APPROVED_ENTITIES, []))),
                CONF_APPROVED_INVERTERS: sorted(set(user_input.get(CONF_APPROVED_INVERTERS, []))),
            }})
        return self.async_show_form(
            step_id="init",
            data_schema=_approval_schema(approved_ids, found, approved_inverters),
        )


class InverterSubentryFlow(ConfigSubentryFlow):
    """Adds or reconfigures one inverter, identified by its dom.oti.cat site."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()
        flat = dict(user_input or {})
        if user_input is not None:
            error, site = await _validate(self.hass, entry.data.get(CONF_URL, DEFAULT_URL), flat)
            if error is None:
                data = {**flat, CONF_SITE_ID: site.get("id")}
                title = site.get("name") or "Inverter"
                # A new token for a site already set up here replaces that inverter's
                # settings instead of adding a second copy of the same site.
                for sub in entry.subentries.values():
                    if sub.subentry_type == SUBENTRY_INVERTER and sub.data.get(CONF_SITE_ID) == data[CONF_SITE_ID]:
                        data = {**sub.data, **data}
                        self.hass.config_entries.async_update_subentry(entry, sub, data=data, title=title)
                        return self.async_abort(reason="site_updated")
                return self.async_create_entry(title=title, data=data, unique_id=data[CONF_SITE_ID])
            errors["base"] = error
        return self.async_show_form(step_id="user", data_schema=_inverter_schema(flat), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        flat = dict(user_input or {}) if user_input is not None else dict(subentry.data)
        if user_input is not None:
            error, site = await _validate(self.hass, entry.data.get(CONF_URL, DEFAULT_URL), flat)
            if error is None and site.get("id") != subentry.data.get(CONF_SITE_ID):
                error = "different_site"
            if error is None:
                # The subentry also holds what the service sent — the entities the site reads and
                # the devices it drives — and this form does not know about them, so they are
                # carried over rather than dropped.
                return self.async_update_and_abort(
                    entry, subentry, data={**subentry.data, **flat, CONF_SITE_ID: site["id"]},
                    title=site.get("name") or subentry.title,
                )
            errors["base"] = error
        return self.async_show_form(step_id="reconfigure", data_schema=_inverter_schema(flat), errors=errors)
