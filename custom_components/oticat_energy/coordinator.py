"""Fetches battery plans from the Oticat Energy service and works out what applies now.

The site's battery, solar and grid settings live in the service, tied to the token. This
side sends only where the house is, its SOC and its recent hourly consumption.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
from typing import Any

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GRID_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_HEATER_GRID_ENTITY,
    CONF_INVERTER,
    CONF_INVERTER_PREFIX,
    CONF_LOAD_ENTITY,
    CONF_PROGRAM_POWER,
    CONF_SOLAR_ENTITY,
    CONF_SOC_ENTITY,
    CONF_TOKEN,
    CONF_URL,
    CONF_ZONE,
    DEFAULT_URL,
    DEFAULT_ZONE,
    DOMAIN,
    HISTORY_DAYS,
    INVERTER_DEYE,
    INVERTER_NONE,
    PLAN_PATH,
)
from .catalogue import build_catalogue
from .inverter import DeyeWriter
from .heater import HeaterController, thresholds

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=15)
HISTORY_REFRESH = timedelta(hours=1)
FALLBACK_MIN_SOC = 10
FALLBACK_SEGMENTS = 6
# Bumped only if the shape of the stored configuration changes.
APPLIED_STORE_VERSION = 1


class PlanRequestError(Exception):
    """The service refused the request or could not be reached."""

    def __init__(self, message: str, unauthorized: bool = False) -> None:
        super().__init__(message)
        self.unauthorized = unauthorized


def read_soc(hass: HomeAssistant, entity_id: str) -> float | None:
    """Current SOC in percent, or None when the entity has no usable value."""
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def location(hass: HomeAssistant, zone: str | None = None) -> dict[str, Any]:
    """Where the inverter is: the chosen zone, by default the home zone.

    A zone that no longer exists falls back to Home Assistant's own location, without a
    zone name, and the coordinator reports it.
    """
    state = hass.states.get(zone or DEFAULT_ZONE)
    lat = state.attributes.get("latitude") if state else None
    lon = state.attributes.get("longitude") if state else None
    if lat is None or lon is None:
        return {"latitude": hass.config.latitude, "longitude": hass.config.longitude, "timezone": hass.config.time_zone}
    return {"latitude": lat, "longitude": lon, "timezone": hass.config.time_zone, "zone": state.name}


def entities(conf: dict[str, Any]) -> dict[str, Any]:
    """This inverter's local entity choices, sent with each plan.

    dom.oti.cat can't read a subentry's settings, so it echoes them back on the site's card and
    uses them to preselect the fields in Change setup.
    """
    return {
        "soc": conf.get(CONF_SOC_ENTITY),
        "load": conf.get(CONF_LOAD_ENTITY),
        "grid": conf.get(CONF_GRID_ENTITY),
        "grid_export": conf.get(CONF_GRID_EXPORT_ENTITY),
        "solar": conf.get(CONF_SOLAR_ENTITY),
        "zone": conf.get(CONF_ZONE),
        "inverter": conf.get(CONF_INVERTER),
        "inverter_prefix": conf.get(CONF_INVERTER_PREFIX),
        "program_power": conf.get(CONF_PROGRAM_POWER),
    }


async def request_plan(hass: HomeAssistant, conf: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """POST one plan request. Used by the coordinator and by the config flow's token check."""
    url = conf.get(CONF_URL, DEFAULT_URL).rstrip("/") + PLAN_PATH
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {conf[CONF_TOKEN]}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 401:
                raise PlanRequestError("Token rejected", unauthorized=True)
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise PlanRequestError(f"HTTP {resp.status}: {data.get('error') if isinstance(data, dict) else data}")
            return data
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise PlanRequestError(f"{type(err).__name__}: {err}") from err


def fallback_segments(min_soc: int, count: int) -> list[dict[str, Any]]:
    """Plain self-consumption on every program, evenly spread over the day."""
    step = 24 * 60 // count
    return [
        {"start": f"{(k * step) // 60:02d}:{(k * step) % 60:02d}", "grid_charge": False, "soc": min_soc}
        for k in range(count)
    ]


class EnergyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps the latest plan, the view of it that applies right now, and drives the inverter."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {subentry.title}", update_interval=UPDATE_INTERVAL
        )
        self.subentry = subentry
        self.plan: dict[str, Any] | None = None
        self.error: str | None = None
        self._history: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
        self._version: str | None = None
        # The configuration the service last sent, kept here and in a small store so it survives
        # a restart: dom is the source of truth, and a service that cannot be reached must not
        # change how the site already behaves.
        self._store = Store(hass, APPLIED_STORE_VERSION, f"{DOMAIN}.{subentry.subentry_id}")
        self.applied: dict[str, Any] | None = None
        self.apply_error: str | None = None
        # Set when the service asks for the entities it can offer, and kept until it stops
        # asking, which is how the settings page fills its pickers on a private Home Assistant.
        self._send_catalogue = False
        conf = self.conf
        self.writer: DeyeWriter | None = None
        if conf.get(CONF_INVERTER) == INVERTER_DEYE:
            self.writer = DeyeWriter(hass, conf.get(CONF_INVERTER_PREFIX, "deye"), int(conf.get(CONF_PROGRAM_POWER, 5000)))
        # Water heaters run on the same plan. Real-time, so the controller listens to the grid
        # power itself instead of waiting for the 15-minute plan refresh.
        self.heater = HeaterController(hass, self)

    @property
    def conf(self) -> dict[str, Any]:
        """The inverter's settings plus the service URL from the parent entry."""
        return {CONF_URL: self.config_entry.data.get(CONF_URL, DEFAULT_URL), **self.subentry.data}

    def applied_heaters(self) -> dict[str, Any] | None:
        """The device thresholds the service last sent, or None when it has not sent any."""
        heaters = (self.applied or {}).get("heaters")
        return heaters if isinstance(heaters, dict) else None

    def applied_config(self) -> dict[str, Any]:
        """The part of the configuration this side is running, echoed with each plan.

        Only what the integration actually applies is echoed, so the service can tell what is in
        sync from what is still waiting; everything else in the service's configuration is none of
        our business.
        """
        applied = self.applied or {}
        out: dict[str, Any] = {}
        if isinstance(applied.get("heaters"), dict):
            out["heaters"] = applied["heaters"]
        if isinstance(applied.get("program_power"), (int, float)):
            out["program_power"] = applied["program_power"]
        if isinstance(applied.get("entities"), dict):
            out["entities"] = applied["entities"]
        return out

    def _entity_problem(self, changed: dict[str, Any]) -> str | None:
        """Which of the entities the service sent this Home Assistant cannot accept, or None.

        An entity that does not exist is almost always a rename here that the site has not caught
        up with, so the whole change is refused rather than half applied, and the error travels
        back in `applied` for the settings page to show.
        """
        for conf_key, value in changed.items():
            if conf_key == CONF_INVERTER:
                if value not in (INVERTER_DEYE, INVERTER_NONE):
                    return f"{value} is not an inverter control"
            elif conf_key == CONF_INVERTER_PREFIX:
                if not re.fullmatch(r"[a-z0-9_]{1,40}", str(value)):
                    return f"{value} is not an inverter prefix"
            elif self.hass.states.get(value) is None:
                return f"{value} does not exist"
        # A Deye needs its programs to write to.
        inverter = (self.subentry.data or {}).get(CONF_INVERTER)
        inverter = changed.get(CONF_INVERTER, inverter)
        if inverter == INVERTER_DEYE:
            prefix = changed.get(CONF_INVERTER_PREFIX) or (self.subentry.data or {}).get(CONF_INVERTER_PREFIX)
            if not any(self.hass.states.get(f"{d}.{prefix}_program_1_soc") for d in ("number", "input_number")):
                return f"{prefix}_program_1_soc does not exist"
        return None

    def _apply_entities(self, entities: Any) -> None:
        """Write the entities the service sent into the subentry, which is where HA keeps them.

        Everything else is applied in place; the entities cannot be, because the sensors, the
        writer and the controller are all built from them when the entry is set up. Updating the
        subentry reloads the entry, which rebuilds them from the new choices.
        """
        if not isinstance(entities, dict) or not entities:
            return
        wanted: dict[str, Any] = {}
        for conf_key, value in (
            (CONF_SOC_ENTITY, entities.get("soc")),
            (CONF_LOAD_ENTITY, entities.get("load")),
            (CONF_GRID_ENTITY, entities.get("grid")),
            (CONF_GRID_EXPORT_ENTITY, entities.get("grid_export")),
            (CONF_SOLAR_ENTITY, entities.get("solar")),
            (CONF_ZONE, entities.get("zone")),
            (CONF_INVERTER, entities.get("inverter")),
            (CONF_INVERTER_PREFIX, entities.get("inverter_prefix")),
        ):
            if isinstance(value, str) and value:
                wanted[conf_key] = value
        changed = {k: v for k, v in wanted.items() if self.subentry.data.get(k) != v}
        if not changed:
            return
        problem = self._entity_problem(changed)
        if problem:
            raise ValueError(problem)
        _LOGGER.warning("Applying the site entities from dom.oti.cat: %s", ", ".join(sorted(changed)))
        if self.hass.config_entries.async_update_subentry(
            self.config_entry, self.subentry, data={**self.subentry.data, **changed}, title=self.subentry.title
        ):
            # The sensors, the writer and the controller were all built from the old choices, so
            # the entry has to be set up again. Scheduled rather than awaited: this runs in the
            # middle of an update, and Home Assistant's own advice is not to await a reload from
            # inside the integration that asked for it.
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    @callback
    def _apply_config(self) -> None:
        """Hand the configuration the service sent to the parts that use it."""
        if self.writer:
            power = (self.applied or {}).get("program_power")
            if isinstance(power, (int, float)) and power > 0:
                self.writer.program_power = int(power)
        self.heater.refresh_config()

    async def async_setup(self) -> None:
        """Recompute the current view at every hour change, between plan fetches."""
        # Reported with each plan so dom.oti.cat can show which version runs and offer updates.
        self._version = str((await async_get_integration(self.hass, DOMAIN)).version)
        stored = await self._store.async_load() or {}
        self.applied = stored.get("config") if isinstance(stored.get("config"), dict) else None
        self._apply_config()

        @callback
        def _hour_changed(_now: datetime) -> None:
            self.async_set_updated_data(self.view())

        self.config_entry.async_on_unload(
            async_track_time_change(self.hass, _hour_changed, minute=0, second=5)
        )
        if self.writer:
            self.config_entry.async_on_unload(self.async_add_listener(self._write_inverter))
        if self.heater.active:
            await self.heater.async_setup()
            self.config_entry.async_on_unload(self.heater.async_stop)

    @callback
    def _write_inverter(self) -> None:
        if self.writer and self.data:
            self.config_entry.async_create_background_task(
                self.hass, self.writer.async_apply(self.data["segments"]), f"oticat_energy_inverter_{self.subentry.subentry_id}"
            )

    async def entity_history(self, entity_id: str | None) -> list[dict[str, Any]]:
        """The last 14 days of hourly `change` for one energy sensor, refreshed hourly.

        The grid sensor may be signed (negative while exporting); `change` keeps the sign, so
        the service can turn each hour into a grid cost or a little export revenue. The water
        heater control reuses it to count the days without surplus.
        """
        if not entity_id:
            return []
        now = dt_util.utcnow()
        cached = self._history.get(entity_id)
        if cached and now - cached[0] < HISTORY_REFRESH:
            return cached[1]
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(days=HISTORY_DAYS)
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            None,
            {entity_id},
            "hour",
            {"energy": "kWh"},
            {"change"},
        )
        rows = [
            {"start": dt_util.utc_from_timestamp(row["start"]).isoformat(), "kwh": round(row["change"], 4)}
            for row in stats.get(entity_id, [])
            if row.get("change") is not None
        ]
        self._history[entity_id] = (now, rows)
        return rows

    async def _async_update_data(self) -> dict[str, Any]:
        conf = self.conf
        soc = read_soc(self.hass, conf.get(CONF_SOC_ENTITY, ""))
        if soc is None:
            # No SOC means no plan, but not no request: the configuration and the catalogue are
            # still worth having. A fresh site has no sensors picked yet, and a battery sensor
            # that is down must not leave the site stuck on an old configuration.
            self.error = f"{conf.get(CONF_SOC_ENTITY) or 'the SOC entity'} has no value"
        # Each measured sensor becomes an hourly history: consumption, grid import, grid export
        # and solar production. A recorder problem keeps the last copy and must not stop planning.
        wanted = {
            "load_history": conf.get(CONF_LOAD_ENTITY),
            "grid_history": conf.get(CONF_GRID_ENTITY),
            "grid_export_history": conf.get(CONF_GRID_EXPORT_ENTITY),
            "solar_history": conf.get(CONF_SOLAR_ENTITY),
        }
        histories: dict[str, list[dict[str, Any]]] = {}
        try:
            for key, entity_id in wanted.items():
                histories[key] = await self.entity_history(entity_id)
        except Exception as err:  # noqa: BLE001 - a recorder problem must not stop planning
            _LOGGER.warning("Could not read history: %s", err)
            for key, entity_id in wanted.items():
                histories.setdefault(key, self._history.get(entity_id or "", (None, []))[1])
        where = location(self.hass, conf.get(CONF_ZONE))
        body = {
            "location": where,
            # Without a SOC the service answers with the configuration only, which is what a site
            # needs before it has a sensor to read.
            "state": {} if soc is None else {"soc": soc},
            **histories,
            "entities": entities(conf),
            # What this side is running, so the service can tell what is in sync from what is
            # still waiting and what failed.
            "applied": {
                "ok": self.apply_error is None,
                "error": self.apply_error,
                "config": self.applied_config(),
            },
            "client": {"integration": self._version, "ha": HA_VERSION},
        }
        # Until the service has sent a configuration, report the numbers the subentry holds so it
        # can adopt the ones the site has never expressed itself. Without this, a site that had
        # tuned them here would silently drop to the service's defaults, which it did once.
        if self.applied is None and conf.get(CONF_HEATER_GRID_ENTITY):
            body["legacy"] = {"heaters": thresholds(conf)}

        sent_catalogue = self._send_catalogue
        if sent_catalogue:
            body["catalogue"] = build_catalogue(self.hass)
        try:
            answer = await request_plan(self.hass, conf, body)
            # An answer with no hours is a configuration, not a plan: keep covering now with the
            # plan we already had.
            if answer.get("hours"):
                self.plan = answer
                self.error = None if "zone" in where else f"{conf.get(CONF_ZONE) or DEFAULT_ZONE} not found, using Home Assistant's location"
            await self._take_config(answer.get("config"))
            # The service asks until it has one; once it stops, the copy that just went out was
            # good enough and there is no reason to keep sending it.
            if answer.get("want_catalogue"):
                if not self._send_catalogue:
                    self._send_catalogue = True
                    # The catalogue rides on the next request. Asking is usually the settings page
                    # waiting for the list, so go again now instead of in fifteen minutes.
                    self.config_entry.async_create_background_task(
                        self.hass, self.async_request_refresh(), f"oticat_energy_catalogue_{self.subentry.subentry_id}"
                    )
            elif sent_catalogue:
                self._send_catalogue = False
        except PlanRequestError as err:
            # Keep the last plan: it covers until its horizon_end, usually the end of tomorrow.
            self.error = str(err)
            _LOGGER.warning("Plan request failed: %s", err)
        return self.view()

    async def _take_config(self, config: Any) -> None:
        """Apply the configuration the service sent, and keep it for a restart.

        dom is the source of truth, so a new configuration is applied as it arrives. A failure
        leaves the previous one in place and is reported back with the next plan.
        """
        if isinstance(config, dict) and config != self.applied:
            previous = self.applied
            try:
                self.applied = config
                self.apply_error = None
                self._apply_config()
                self._apply_entities(config.get("entities"))
            except Exception as err:  # noqa: BLE001 - a bad configuration must not stop planning
                self.applied = previous
                self.apply_error = str(err)
                _LOGGER.warning("Applying the site configuration failed: %s", err)
                return
            await self._store.async_save({"config": config})
            return
        # The entities are checked even when nothing has changed: reconfiguring the inverter in
        # Home Assistant, or a hand edit, can drop what we wrote into the subentry, and the site
        # would then read nothing while believing it is up to date.
        try:
            self._apply_entities((self.applied or {}).get("entities"))
            self.apply_error = None
        except Exception as err:  # noqa: BLE001 - a bad entity must not stop planning
            self.apply_error = str(err)
            _LOGGER.warning("Applying the site entities failed: %s", err)

    def view(self) -> dict[str, Any]:
        """What applies now, from the last plan, or self-consumption once it has run out."""
        plan = self.plan
        site = (plan or {}).get("site", {})
        min_soc = int(site.get("min_soc", FALLBACK_MIN_SOC))
        count = int(site.get("max_segments", FALLBACK_SEGMENTS))
        now = dt_util.utcnow()
        current = None
        if plan and dt_util.parse_datetime(plan["horizon_end"]) > now:
            for hour in plan["hours"]:
                start = dt_util.parse_datetime(hour["start"])
                if start <= now < start + timedelta(hours=1):
                    current = hour
                    break
        if current is None:
            return {
                "status": "fallback",
                "error": self.error,
                "action": "self_consume",
                "grid_charge": False,
                "soc_target": min_soc,
                "preheat": False,
                "price": None,
                "segments": fallback_segments(min_soc, count),
                "plan": plan,
            }
        return {
            "status": "stale" if self.error else "ok",
            "error": self.error,
            "action": current["action"],
            "grid_charge": current["grid_charge"],
            "soc_target": current["soc_target"],
            # This hour's buy price is cheap enough to heat from the grid without surplus.
            "preheat": bool(current.get("preheat", False)),
            # This hour's PVPC buy price, for the water heater's cheap-hour decision.
            "price": current.get("price"),
            "segments": plan["segments"],
            "plan": plan,
        }
