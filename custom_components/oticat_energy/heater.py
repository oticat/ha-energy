"""Drives water heaters (and other dump loads) straight from the plan.

The service decides when the grid is cheap enough to heat from (`preheat`); this controller
adds the real-time dynamics: run on solar surplus, or on cheap grid within an import ceiling,
chained by priority, with anti-flapping, a sun window and overload cut-off.

It is the `solar_surplus_heater` blueprint ported into the integration, so the plan, the prices
and the heaters are configured in one place. `decide()` is pure and holds the whole policy.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from collections.abc import Collection
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HEATER_CHEAP_PRICE,
    CONF_HEATER_CONSUMING_STATE,
    CONF_HEATER_DRY_DAYS,
    CONF_HEATER_FLOW_ENTITY,
    CONF_HEATER_GRID_ENTITY,
    CONF_HEATER_MARGIN,
    CONF_HEATER_MAX_AZIMUTH,
    CONF_HEATER_MAX_IMPORT_W,
    CONF_HEATER_MIN_AZIMUTH,
    CONF_HEATER_MIN_ELEVATION,
    CONF_HEATER_MIN_HEAT_KWH,
    CONF_HEATER_SURPLUS_W,
    DEFAULT_HEATER_CHEAP_PRICE,
    DEFAULT_HEATER_CONSUMING_STATE,
    DEFAULT_HEATER_DRY_DAYS,
    DEFAULT_HEATER_HVAC_MODE,
    DEFAULT_HEATER_MARGIN,
    DEFAULT_HEATER_MAX_AZIMUTH,
    DEFAULT_HEATER_MAX_IMPORT_W,
    DEFAULT_HEATER_MIN_AZIMUTH,
    DEFAULT_HEATER_MIN_ELEVATION,
    DEFAULT_HEATER_MIN_HEAT_KWH,
    DEFAULT_HEATER_SURPLUS_W,
    HEAT_HISTORY_DAYS,
    HEATER_DEVICE_DOMAINS,
    MAX_HEATERS,
    heater_key,
)

_LOGGER = logging.getLogger(__name__)

# A higher-priority device has to have been on (or off) this long before the next one reacts,
# so the priority chain settles instead of starting every device at once.
HP_STABLE_SECONDS = 40
# Re-check this often even without a state change: catches missed edges and the cheap hour passing.
TICK = timedelta(seconds=30)

_CLIMATE_OFF = ("off", STATE_UNAVAILABLE, STATE_UNKNOWN)
# A climate that is not off may still be idle, with its thermostat satisfied: it commands heat but
# draws nothing. Only that one state is left out of the heat a day is credited with.
_CLIMATE_IDLE = ("idle",)
_INVALID = (STATE_UNAVAILABLE, STATE_UNKNOWN)


@dataclass
class Heater:
    """One controlled device. Priority is the order in the list, first is the highest."""

    entity: str
    power: float
    hvac_mode: str = DEFAULT_HEATER_HVAC_MODE

    @property
    def is_climate(self) -> bool:
        return self.entity.startswith("climate.")


def drivable(entity_id: str) -> bool:
    """Whether this integration may call a service on that entity.

    A device is driven through a switch, an input_boolean or a climate, and nothing else: the
    service sends the entity ids, and a value it should not have sent must never reach a service
    call that could turn on a light or unlock a door.
    """
    return entity_id.split(".")[0] in HEATER_DEVICE_DOMAINS


@dataclass
class HeaterConfig:
    heaters: list[Heater]
    grid_entity: str
    flow_entity: str | None
    consuming: str
    surplus_w: float
    max_import_w: float
    margin: float
    min_elevation: float
    min_azimuth: float
    max_azimuth: float
    # The site's on/off switch for this block, which Home Assistant also shows as an entity. Off is
    # a pause: the controller turns off what it was running and then leaves the devices alone.
    enabled: bool = True
    # Dry-spell top-up: after this many full days heating less than `min_heat_kwh`, the heaters may
    # also heat from the grid in hours at or below cheap_price (the always-cheap hours come from the
    # service's preheat flag). The heat of a day is what its devices drew, measured from their state
    # history: rated power times the hours they ran.
    dry_days: int = DEFAULT_HEATER_DRY_DAYS
    min_heat_kwh: float = DEFAULT_HEATER_MIN_HEAT_KWH
    cheap_price: float = DEFAULT_HEATER_CHEAP_PRICE


@dataclass
class Snapshot:
    """Everything decide() needs, so the policy is testable without Home Assistant."""

    grid_power: float | None            # signed W (import +, export −); None when invalid
    sun_elevation: float
    sun_azimuth: float
    preheat: bool                       # the current hour's buy price is cheap
    on: list[bool]
    on_seconds: list[float | None]      # seconds the device has been on, None when off
    off_seconds: list[float | None]     # seconds the device has been off, None when on


def effective_margin(config: HeaterConfig) -> float:
    """Hysteresis actually used: never more than 25 W below the surplus threshold."""
    return max(0.0, min(config.margin, config.surplus_w - 25))


def decide(config: HeaterConfig, s: Snapshot) -> list[bool]:
    """The desired on/off state of every heater, from the current snapshot.

    Turn a device on when it is off and either:
      * the sun is up, in the window, and the grid already shows enough surplus to absorb its
        full draw (solar surplus mode), or
      * this is a cheap-grid hour and the house stays under the import ceiling (preheat mode).
    A higher-priority device must have been on long enough first.
    Turn it off when its higher-priority device has been off long enough, or when neither mode
    holds — but the lowest active device takes the overload first, exactly like the blueprint.
    """
    margin = effective_margin(config)
    grid = s.grid_power
    valid = grid is not None
    window_ok = (
        s.sun_elevation > config.min_elevation
        and config.min_azimuth <= s.sun_azimuth <= config.max_azimuth
    )
    grid_forces_off = (not valid) or grid > config.surplus_w + margin
    cheap_import_high = s.preheat and valid and grid > config.max_import_w + margin
    cheap_keep = s.preheat and not cheap_import_high

    want = list(s.on)
    count = len(config.heaters)
    for i, heater in enumerate(config.heaters):
        hp = i - 1 if i > 0 else None
        lp = i + 1 if i + 1 < count else None

        hp_on_stable = hp is None or (s.on[hp] and (s.on_seconds[hp] or 0.0) > HP_STABLE_SECONDS)
        hp_off_cascade = hp is not None and not s.on[hp] and (s.off_seconds[hp] or 0.0) > HP_STABLE_SECONDS
        lp_is_on = lp is not None and s.on[lp]

        surplus_ok = valid and grid < config.surplus_w - margin - heater.power
        cheap_ok = s.preheat and valid and grid + heater.power <= config.max_import_w - margin

        should_be_on = hp_on_stable and ((window_ok and surplus_ok) or cheap_ok)
        should_be_off = hp_off_cascade or (
            (not window_ok or (grid_forces_off and not lp_is_on)) and not cheap_keep
        )

        if not s.on[i] and should_be_on:
            want[i] = True
        elif s.on[i] and should_be_off:
            want[i] = False
    return want


# The controller's numbers, under the names the service uses: (service key, subentry key, default).
THRESHOLDS: tuple[tuple[str, str, float], ...] = (
    ("surplus_w", CONF_HEATER_SURPLUS_W, DEFAULT_HEATER_SURPLUS_W),
    ("max_import_w", CONF_HEATER_MAX_IMPORT_W, DEFAULT_HEATER_MAX_IMPORT_W),
    ("margin", CONF_HEATER_MARGIN, DEFAULT_HEATER_MARGIN),
    ("min_elevation", CONF_HEATER_MIN_ELEVATION, DEFAULT_HEATER_MIN_ELEVATION),
    ("min_azimuth", CONF_HEATER_MIN_AZIMUTH, DEFAULT_HEATER_MIN_AZIMUTH),
    ("max_azimuth", CONF_HEATER_MAX_AZIMUTH, DEFAULT_HEATER_MAX_AZIMUTH),
    ("dry_days", CONF_HEATER_DRY_DAYS, DEFAULT_HEATER_DRY_DAYS),
    ("min_heat_kwh", CONF_HEATER_MIN_HEAT_KWH, DEFAULT_HEATER_MIN_HEAT_KWH),
    ("cheap_price", CONF_HEATER_CHEAP_PRICE, DEFAULT_HEATER_CHEAP_PRICE),
)


def thresholds(conf: dict[str, Any], applied: dict[str, Any] | None = None) -> dict[str, float]:
    """The controller's numbers.

    What the service has sent wins, because it is the source of truth now; whatever it has not
    sent comes from the subentry, which is what lets a site keep running on its own when the
    service cannot be reached.
    """
    from_service = applied or {}
    out: dict[str, float] = {}
    for key, conf_key, default in THRESHOLDS:
        value = from_service.get(key)
        out[key] = float(value) if isinstance(value, (int, float)) else float(conf.get(conf_key, default))
    return out


def heater_devices(
    conf: dict[str, Any], applied: dict[str, Any] | None = None, approved: Collection[str] | None = None
) -> list[Heater]:
    """The devices to drive, highest priority first.

    What the service sent wins, a list which may be empty when the site has decided it drives
    none. Until it sends one, the subentry's slots are used, which is what a site that predates
    the service owning them keeps running on.
    """
    sent = (applied or {}).get("devices")
    if isinstance(sent, list):
        out: list[Heater] = []
        for item in sent[:MAX_HEATERS]:
            if not isinstance(item, dict):
                continue
            entity = item.get("entity")
            if not isinstance(entity, str) or not entity:
                continue
            if not drivable(entity):
                _LOGGER.warning("Not driving %s: devices must be a switch, an input_boolean or a climate", entity)
                continue
            if approved is not None and entity not in approved:
                continue   # not allowed in Home Assistant any more
            power = item.get("power_w")
            mode = item.get("hvac_mode")
            out.append(
                Heater(
                    entity=entity,
                    power=float(power) if isinstance(power, (int, float)) else 0.0,
                    hvac_mode=mode if isinstance(mode, str) and mode else DEFAULT_HEATER_HVAC_MODE,
                )
            )
        return out
    out = []
    for i in range(1, MAX_HEATERS + 1):
        entity = conf.get(heater_key(i, "entity"))
        if not entity:
            continue
        if not drivable(entity):
            _LOGGER.warning("Not driving %s: devices must be a switch, an input_boolean or a climate", entity)
            continue
        if approved is not None and entity not in approved:
            continue   # not allowed in Home Assistant
        out.append(
            Heater(
                entity=entity,
                power=float(conf.get(heater_key(i, "power")) or 0),
                hvac_mode=conf.get(heater_key(i, "hvac_mode")) or DEFAULT_HEATER_HVAC_MODE,
            )
        )
    return out


def _sent(applied: dict[str, Any] | None, key: str) -> str | None:
    """One of the service's settings, when it is a string it means anything by."""
    value = (applied or {}).get(key)
    return value if isinstance(value, str) and value else None


def subentry_heaters(conf: dict[str, Any], approved: Collection[str] | None = None) -> dict[str, Any]:
    """This side's own heater settings, for the service to adopt as its starting point.

    Sent until the service has applied a configuration: a site that had tuned these in Home
    Assistant must not drop to the service's defaults, which it once did.
    """
    grid = conf.get(CONF_HEATER_GRID_ENTITY) or ""
    flow = conf.get(CONF_HEATER_FLOW_ENTITY) or ""
    if approved is not None:
        grid = grid if grid in approved else ""
        flow = flow if flow in approved else ""
    return {
        **thresholds(conf),
        "grid_entity": grid,
        "flow_entity": flow,
        "consuming_state": conf.get(CONF_HEATER_CONSUMING_STATE) or DEFAULT_HEATER_CONSUMING_STATE,
        "devices": [
            {"entity": h.entity, "power_w": h.power, "hvac_mode": h.hvac_mode}
            for h in heater_devices(conf, approved=approved)
        ],
    }


def config_from(
    conf: dict[str, Any], applied: dict[str, Any] | None = None, approved: Collection[str] | None = None
) -> HeaterConfig | None:
    """Build the heater config from what the site drives, or None when it drives nothing.

    What the service sent comes first, since the choices are its now; what it has not sent comes
    from the subentry, which is what the site keeps running on when the service cannot be reached.
    """
    grid_entity = _sent(applied, "grid_entity") or conf.get(CONF_HEATER_GRID_ENTITY)
    if approved is not None and grid_entity not in approved:
        return None
    if not grid_entity:
        return None
    heaters = heater_devices(conf, applied, approved)
    if not heaters:
        return None
    t = thresholds(conf, applied)
    # The switch is on unless the service has said otherwise. It lives in the applied configuration
    # (and its store), so an unreachable service leaves the site doing what it was doing.
    raw_enabled = (applied or {}).get("enabled")
    return HeaterConfig(
        heaters=heaters,
        enabled=raw_enabled if isinstance(raw_enabled, bool) else True,
        grid_entity=grid_entity,
        flow_entity=(
            (_sent(applied, "flow_entity") or conf.get(CONF_HEATER_FLOW_ENTITY) or None)
            if approved is None
            or (_sent(applied, "flow_entity") or conf.get(CONF_HEATER_FLOW_ENTITY)) in approved
            else None
        ),
        consuming=_sent(applied, "consuming_state") or conf.get(CONF_HEATER_CONSUMING_STATE) or DEFAULT_HEATER_CONSUMING_STATE,
        surplus_w=t["surplus_w"],
        max_import_w=t["max_import_w"],
        margin=t["margin"],
        min_elevation=t["min_elevation"],
        min_azimuth=t["min_azimuth"],
        max_azimuth=t["max_azimuth"],
        dry_days=int(t["dry_days"]),
        min_heat_kwh=t["min_heat_kwh"],
        cheap_price=t["cheap_price"],
    )


def _is_on(state: Any, is_climate: bool) -> bool:
    if state is None:
        return False
    return state.state not in _CLIMATE_OFF if is_climate else state.state == "on"


def draws_power(state: str, is_climate: bool) -> bool:
    """Whether a state means the device is taking energy right now.

    A switch is on or it is not. A climate that is not off may still be idle with its thermostat
    satisfied, which draws nothing, so that one state does not count as heat.
    """
    if is_climate:
        return state not in _CLIMATE_OFF and state not in _CLIMATE_IDLE
    return state == "on"


def hours_drawing(series: list[tuple[datetime, str]], start: datetime, end: datetime, is_climate: bool) -> float:
    """Hours an entity spent drawing power between two instants.

    `series` is its state changes, oldest first, with the state in force at or before `start`
    first: the recorder is asked for the start-time state, so a device already on when the window
    opens is counted from its beginning. Changes before `start` or after `end` are clipped.
    """
    total = 0.0
    for i, (when, state) in enumerate(series):
        following = series[i + 1][0] if i + 1 < len(series) else end
        a, b = max(when, start), min(following, end)
        if b > a and draws_power(state, is_climate):
            total += (b - a).total_seconds() / 3600
    return total


def kwh_per_hour(
    series: dict[str, list[tuple[datetime, str]]],
    heaters: list[Heater],
    start: datetime,
    hours: int,
    end: datetime | None = None,
) -> list[float]:
    """kWh the devices drew in each hour from `start`, one bucket per hour.

    It is the same heat the dry spell counts, only hour by hour, so the day chart on dom can show
    what the tank took next to what the grid and the panels did. `end` cuts the last bucket at the
    present, so the hour that is still running is not credited with the time it has left.
    """
    out: list[float] = []
    for hour in range(hours):
        a = start + timedelta(hours=hour)
        b = a + timedelta(hours=1)
        if end is not None:
            b = min(b, end)
        total = 0.0
        if b > a:
            for heater in heaters:
                rows = series.get(heater.entity) or []
                if rows:
                    total += hours_drawing(rows, a, b, heater.is_climate) * heater.power / 1000
        out.append(round(total, 3))
    return out


@dataclass
class _HeaterView:
    entity: str
    power: float
    on: bool
    want: bool


class HeaterController:
    """Keeps the heaters matching the plan and the live grid, evaluated on every change."""

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.config = config_from(coordinator.conf, coordinator.applied_heaters(), coordinator.approved)
        # Whether a switch-off has already let the devices go, and whether the controller has had a
        # say at all since it started. A restart while the control is off must not switch off what
        # the household turned on by hand, and it cannot tell that apart from a switch flipped while
        # it was running, so it waits for the first pass that actually drives something.
        self._disabled = False
        self._evaluated = False
        self.status = "disabled" if self.config is not None and not self.config.enabled else "idle"
        self.devices: list[_HeaterView] = []
        self.days_without_heat = 0
        # Yesterday's heat, so the rule can be read without digging into the recorder: the kWh the
        # devices drew and the hours they were on, per device.
        self.heat_kwh = 0.0
        self.heat_hours = 0.0
        self.heat_devices: dict[str, dict[str, float]] = {}
        self.price: float | None = None
        # Where the cheap-grid permission comes from, kept apart so the sensors can say it and an
        # automation can tell "the heaters have heated little for days" from "the plan says cheap
        # hour". Reacting to cheap_grid instead of preheat is what makes the dry spell notifiable.
        self.preheat_flag = False
        self.dry_spell = False
        self.cheap_grid = False
        self._heat_cache: tuple[date, float, int, float, float, dict[str, dict[str, float]]] | None = None
        self._unsubs: list[Any] = []
        self._listeners: list[Any] = []
        self._lock = asyncio.Lock()
        self._pending = False

    @property
    def active(self) -> bool:
        return self.config is not None

    @property
    def enabled(self) -> bool:
        """Whether the controller is driving the devices, as the switch shows it."""
        return bool(self.config.enabled) if self.config is not None else False

    @callback
    def refresh_config(self) -> None:
        """Take what a new plan brought. A change of devices or sensors arrives through the
        subentry instead, which reloads the entry and sets this up again from scratch; here it is
        the numbers, which the next evaluation reads."""
        if self.config is not None:
            self.config = config_from(
                self.coordinator.conf, self.coordinator.applied_heaters(), self.coordinator.approved
            )

    def async_add_listener(self, update_callback: Any) -> Any:
        """Entities that show the heaters register here to be written as soon as they change."""
        self._listeners.append(update_callback)

        @callback
        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove

    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    async def async_setup(self) -> None:
        """Start listening. Does nothing when the site has no heaters configured."""
        config = self.config
        if config is None:
            return
        watched = [config.grid_entity, *[h.entity for h in config.heaters], "sun.sun"]
        if config.flow_entity:
            watched.append(config.flow_entity)
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._changed))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, TICK))
        self._unsubs.append(self.coordinator.async_add_listener(self._changed))
        # First evaluation as a task, never inside setup: a blocking service call in the setup
        # call stack can wedge a config-entry reload.
        self._changed()

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _changed(self, *_args: Any) -> None:
        if self._lock.locked():
            self._pending = True
            return
        self.coordinator.config_entry.async_create_background_task(
            self.hass, self._evaluate(), f"oticat_energy_heaters_{self.coordinator.subentry.subentry_id}"
        )

    @callback
    def _tick(self, _now: Any) -> None:
        self._changed()

    async def _evaluate(self) -> None:
        # One run at a time; a change arriving mid-run sets the dirty flag for one more pass.
        async with self._lock:
            while True:
                self._pending = False
                try:
                    await self._apply()
                except Exception as err:  # noqa: BLE001 - a heater error must not stop planning
                    _LOGGER.warning("Driving the water heaters failed: %s", err)
                    self.status = f"error: {err}"
                if not self._pending:
                    break

    def view(self) -> dict[str, Any]:
        """The heaters' state, for the Water heaters sensor's attributes."""
        return {
            "status": self.status,
            "days_without_heat": self.days_without_heat,
            "heat_kwh": round(self.heat_kwh, 2),
            "heat_hours": round(self.heat_hours, 2),
            "heat_devices": {
                entity: {"hours": round(values["hours"], 2), "kwh": round(values["kwh"], 2)}
                for entity, values in self.heat_devices.items()
            },
            "price": self.price,
            "devices": [
                {"entity": d.entity, "power": d.power, "on": d.on, "want": d.want} for d in self.devices
            ],
        }

    # --- snapshot -------------------------------------------------------------------------

    def _num(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _INVALID:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _unavailable(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is None or state.state in _INVALID

    def _grid_power(self, config: HeaterConfig) -> float | None:
        power = self._num(config.grid_entity)
        if power is None:
            return None
        if config.flow_entity:
            flow = self.hass.states.get(config.flow_entity)
            if flow is None or flow.state in _INVALID:
                return None
            return power if flow.state == config.consuming else -power
        return power

    def _snapshot_devices(self, config: HeaterConfig) -> tuple[list[bool], list[float | None], list[float | None]]:
        now = dt_util.utcnow()
        on: list[bool] = []
        on_seconds: list[float | None] = []
        off_seconds: list[float | None] = []
        for heater in config.heaters:
            state = self.hass.states.get(heater.entity)
            is_on = _is_on(state, heater.is_climate)
            elapsed = (now - state.last_changed).total_seconds() if state is not None else None
            on.append(is_on)
            on_seconds.append(elapsed if is_on else None)
            off_seconds.append(elapsed if not is_on else None)
        return on, on_seconds, off_seconds

    def _current_price(self) -> float | None:
        price = (self.coordinator.data or {}).get("price")
        try:
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    async def heat_history(self, hours: int = 48) -> list[dict[str, Any]]:
        """The kWh the devices drew in each of the last `hours` hours, oldest first.

        The day chart on dom shows it next to the grid and the panels, so the heat the tank took
        is visible hour by hour. Buckets are local hours, sent as their UTC start like the other
        measured series. Empty when the site drives no devices.
        """
        config = self.config
        if config is None or not config.heaters:
            return []
        try:
            zone = dt_util.DEFAULT_TIME_ZONE
            top = dt_util.as_local(dt_util.utcnow()).replace(minute=0, second=0, microsecond=0)
            first = top - timedelta(hours=hours - 1)
            series = await self.coordinator.state_history(
                [h.entity for h in config.heaters], dt_util.as_utc(first), dt_util.as_utc(top + timedelta(hours=1))
            )
            kwh = kwh_per_hour(series, config.heaters, first, hours, dt_util.utcnow())
            return [
                {"start": dt_util.as_utc(first + timedelta(hours=i)).isoformat(), "kwh": value}
                for i, value in enumerate(kwh)
            ]
        except Exception as err:  # noqa: BLE001 - a recorder problem must not stop planning
            _LOGGER.warning("Reading the heaters' history failed: %s", err)
            return []

    async def _heat_days(self, config: HeaterConfig) -> int:
        """How many full local days in a row the devices heated less than the site's minimum.

        The tank has no sensor, so its heat is what the devices drew: their state history over the
        day, in hours on, times the rated power each one was configured with. That is the quantity
        the tank needs, and it does not care where the energy came from, so a day that heated on
        surplus counts exactly like one that heated on the grid.

        It replaces an export test, which lied in both directions: one sunny hour can export more
        than the tank ever took, and a week of low heating can sit under a modest export. It is
        optimistic in only one direction — a device left on with its thermostat open is credited
        with power it never drew — and that one errs on the side of not topping up.

        Only days the recorder has count: a day with no history stops the count, so a fresh site
        waits instead of topping up every night for a reason it cannot see.

        Cached for an hour and keyed on the local date as well as its age, because the window ends
        at midnight: a count taken at 23:26 stops at yesterday, and reused after midnight it is
        missing the day that just closed. That is how the heaters of Gelida ran 00:00-00:26 on
        2026-10-21, on a spell that had ended with the previous day's heat.
        """
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date()
        if (
            self._heat_cache is not None
            and self._heat_cache[0] == today
            and now.timestamp() - self._heat_cache[1] < 3600
        ):
            return self._heat_cache[2]
        count = 0
        kwh_yesterday = 0.0
        hours_yesterday = 0.0
        devices_yesterday: dict[str, dict[str, float]] = {}
        try:
            days_back = min(max(config.dry_days, 1) + 1, HEAT_HISTORY_DAYS)
            zone = dt_util.DEFAULT_TIME_ZONE
            first = dt_util.as_utc(datetime.combine(today - timedelta(days=days_back), time.min, tzinfo=zone))
            last = dt_util.as_utc(datetime.combine(today, time.min, tzinfo=zone))
            series = await self.coordinator.state_history([h.entity for h in config.heaters], first, last)
            heat: dict[date, float] = {}
            day = today - timedelta(days=days_back)
            while day < today:
                start = datetime.combine(day, time.min, tzinfo=zone)
                end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
                total = 0.0
                hours = 0.0
                devices: dict[str, dict[str, float]] = {}
                seen = False
                for heater in config.heaters:
                    rows = series.get(heater.entity) or []
                    if not rows:
                        continue
                    seen = True
                    ran = hours_drawing(rows, start, end, heater.is_climate)
                    hours += ran
                    devices[heater.entity] = {"hours": ran, "kwh": ran * heater.power / 1000}
                    total += devices[heater.entity]["kwh"]
                if seen:
                    heat[day] = total
                if day == today - timedelta(days=1):
                    kwh_yesterday, hours_yesterday, devices_yesterday = total, hours, devices
                day += timedelta(days=1)
            walk = today - timedelta(days=1)
            while walk in heat and heat[walk] < config.min_heat_kwh:
                count += 1
                walk -= timedelta(days=1)
        except Exception as err:  # noqa: BLE001 - a recorder problem must not stop the heaters
            _LOGGER.warning("Counting the days without heating failed: %s", err)
            count = 0
            kwh_yesterday = hours_yesterday = 0.0
            devices_yesterday = {}
        self._heat_cache = (today, now.timestamp(), count, kwh_yesterday, hours_yesterday, devices_yesterday)
        return count

    async def _snapshot(self, config: HeaterConfig) -> Snapshot:
        on, on_seconds, off_seconds = self._snapshot_devices(config)
        self.price = self._current_price()
        self.days_without_heat = await self._heat_days(config)
        cached = self._heat_cache
        if cached is not None:
            self.heat_kwh, self.heat_hours, self.heat_devices = cached[3], cached[4], cached[5]
        # The service's preheat flag is the always-cheap tier (price at or below the site's max).
        # On top of it, a dry spell adds the cheap hours: the heaters get topped up from the grid
        # a few days a year, when they have heated less than the site's minimum for dry_days.
        always = bool((self.coordinator.data or {}).get("preheat"))
        cheap = (
            self.days_without_heat >= config.dry_days
            and self.price is not None
            and self.price <= config.cheap_price
        )
        self.preheat_flag = always
        self.dry_spell = cheap
        self.cheap_grid = always or cheap
        return Snapshot(
            grid_power=self._grid_power(config),
            sun_elevation=_float_attr(self.hass, "sun.sun", "elevation", -90.0),
            sun_azimuth=_float_attr(self.hass, "sun.sun", "azimuth", 180.0),
            preheat=self.cheap_grid,
            on=on,
            on_seconds=on_seconds,
            off_seconds=off_seconds,
        )

    def _info(self) -> tuple[Any, ...]:
        """What the sensors report, compared before and after a pass to decide whether to write."""
        return (
            self.days_without_heat,
            round(self.heat_kwh, 2),
            self.price,
            self.preheat_flag,
            self.dry_spell,
            self.cheap_grid,
        )

    # --- apply ----------------------------------------------------------------------------

    async def _apply(self) -> None:
        config = self.config
        assert config is not None
        if not config.enabled:
            await self._release(config)
            return
        self._disabled = False
        self._evaluated = True
        before = self._info()
        snapshot = await self._snapshot(config)
        want = decide(config, snapshot)
        changed: list[str] = []
        devices: list[_HeaterView] = []
        for heater, is_on, should in zip(config.heaters, snapshot.on, want):
            # A device that is unavailable is left alone; it is evaluated again when it returns.
            if not self._unavailable(heater.entity) and is_on != should:
                await self._set(heater, should)
                changed.append(f"{heater.entity} {'on' if should else 'off'}")
                is_on = should
            devices.append(_HeaterView(entity=heater.entity, power=heater.power, on=is_on, want=should))
        # The status is set before the listeners are written, so what they report is this pass and
        # not the one before it, and a pass that only changes the status is written too.
        previous = self.status
        self.status = "; ".join(changed) if changed else "idle"
        if devices != self.devices or self.status != previous or self._info() != before:
            self.devices = devices
            self._notify()
        if changed:
            _LOGGER.debug("Water heaters: %s", self.status)

    async def _release(self, config: HeaterConfig) -> None:
        """The switch is off: let the devices go, switching off what was running, once.

        Turning them off once is the whole job — after that they belong to the household again, so
        a device switched on by hand while the control is paused stays on. The views are still
        refreshed so the Water heaters sensor keeps telling the truth.
        """
        changed: list[str] = []
        devices: list[_HeaterView] = []
        before = self._info()
        for heater in config.heaters:
            is_on = _is_on(self.hass.states.get(heater.entity), heater.is_climate)
            if self._evaluated and not self._disabled and is_on and not self._unavailable(heater.entity):
                await self._set(heater, False)
                is_on = False
                changed.append(f"{heater.entity} off")
            devices.append(_HeaterView(entity=heater.entity, power=heater.power, on=is_on, want=False))
        previous = self.status
        self._disabled = True
        self.status = "disabled" + (f": {'; '.join(changed)}" if changed else "")
        # Paused means nothing may heat from cheap grid either, so an automation on it goes quiet
        # with the block instead of announcing hours nothing will use.
        self.preheat_flag = False
        self.dry_spell = False
        self.cheap_grid = False
        if devices != self.devices or self.status != previous or self._info() != before:
            self.devices = devices
            self._notify()

    async def _set(self, heater: Heater, on: bool) -> None:
        if not drivable(heater.entity):
            # The last line of defence: whatever put it there (the service, a stored configuration
            # or a hand edit), a device outside the allowed domains is never switched.
            _LOGGER.error("Not switching %s: devices must be a switch, an input_boolean or a climate", heater.entity)
            return
        if heater.is_climate:
            if on:
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode",
                    {"entity_id": heater.entity, "hvac_mode": heater.hvac_mode}, blocking=True,
                )
            else:
                await self.hass.services.async_call("climate", "turn_off", {"entity_id": heater.entity}, blocking=True)
        else:
            await self.hass.services.async_call(
                "homeassistant", "turn_on" if on else "turn_off", {"entity_id": heater.entity}, blocking=True
            )


def _float_attr(hass: HomeAssistant, entity_id: str, attribute: str, default: float) -> float:
    state = hass.states.get(entity_id)
    if state is None:
        return default
    try:
        return float(state.attributes.get(attribute, default))
    except (TypeError, ValueError):
        return default
