"""Fetches battery plans from the Oticat Energy service and works out what applies now.

The site's battery, solar and grid settings live in the service, tied to the token. This
side sends only where the house is, its SOC and its recent hourly consumption.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GRID_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
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
    PLAN_PATH,
)
from .inverter import DeyeWriter

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=15)
HISTORY_REFRESH = timedelta(hours=1)
FALLBACK_MIN_SOC = 10
FALLBACK_SEGMENTS = 6


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
        conf = self.conf
        self.writer: DeyeWriter | None = None
        if conf.get(CONF_INVERTER) == INVERTER_DEYE:
            self.writer = DeyeWriter(hass, conf.get(CONF_INVERTER_PREFIX, "deye"), int(conf.get(CONF_PROGRAM_POWER, 5000)))

    @property
    def conf(self) -> dict[str, Any]:
        """The inverter's settings plus the service URL from the parent entry."""
        return {CONF_URL: self.config_entry.data.get(CONF_URL, DEFAULT_URL), **self.subentry.data}

    async def async_setup(self) -> None:
        """Recompute the current view at every hour change, between plan fetches."""
        # Reported with each plan so dom.oti.cat can show which version runs and offer updates.
        self._version = str((await async_get_integration(self.hass, DOMAIN)).version)

        @callback
        def _hour_changed(_now: datetime) -> None:
            self.async_set_updated_data(self.view())

        self.config_entry.async_on_unload(
            async_track_time_change(self.hass, _hour_changed, minute=0, second=5)
        )
        if self.writer:
            self.config_entry.async_on_unload(self.async_add_listener(self._write_inverter))

    @callback
    def _write_inverter(self) -> None:
        if self.writer and self.data:
            self.config_entry.async_create_background_task(
                self.hass, self.writer.async_apply(self.data["segments"]), f"oticat_energy_inverter_{self.subentry.subentry_id}"
            )

    async def _entity_history(self, entity_id: str | None) -> list[dict[str, Any]]:
        """The last 14 days of hourly `change` for one energy sensor, refreshed hourly.

        The grid sensor may be signed (negative while exporting); `change` keeps the sign, so
        the service can turn each hour into a grid cost or a little export revenue.
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
        soc = read_soc(self.hass, conf[CONF_SOC_ENTITY])
        if soc is None:
            self.error = f"{conf[CONF_SOC_ENTITY]} has no value"
            return self.view()
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
                histories[key] = await self._entity_history(entity_id)
        except Exception as err:  # noqa: BLE001 - a recorder problem must not stop planning
            _LOGGER.warning("Could not read history: %s", err)
            for key, entity_id in wanted.items():
                histories.setdefault(key, self._history.get(entity_id or "", (None, []))[1])
        where = location(self.hass, conf.get(CONF_ZONE))
        body = {
            "location": where,
            "state": {"soc": soc},
            **histories,
            "entities": entities(conf),
            "client": {"integration": self._version, "ha": HA_VERSION},
        }
        try:
            self.plan = await request_plan(self.hass, conf, body)
            self.error = None if "zone" in where else f"{conf.get(CONF_ZONE) or DEFAULT_ZONE} not found, using Home Assistant's location"
        except PlanRequestError as err:
            # Keep the last plan: it covers until its horizon_end, usually the end of tomorrow.
            self.error = str(err)
            _LOGGER.warning("Plan request failed: %s", err)
        return self.view()

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
                "segments": fallback_segments(min_soc, count),
                "plan": plan,
            }
        return {
            "status": "stale" if self.error else "ok",
            "error": self.error,
            "action": current["action"],
            "grid_charge": current["grid_charge"],
            "soc_target": current["soc_target"],
            "segments": plan["segments"],
            "plan": plan,
        }
