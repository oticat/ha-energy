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
from datetime import timedelta
import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GRID_EXPORT_ENTITY,
    CONF_HEATER_CHEAP_PRICE,
    CONF_HEATER_CONSUMING_STATE,
    CONF_HEATER_DRY_DAYS,
    CONF_HEATER_EXPORT_MIN_KWH,
    CONF_HEATER_FLOW_ENTITY,
    CONF_HEATER_GRID_ENTITY,
    CONF_HEATER_MARGIN,
    CONF_HEATER_MAX_AZIMUTH,
    CONF_HEATER_MAX_IMPORT_W,
    CONF_HEATER_MIN_AZIMUTH,
    CONF_HEATER_MIN_ELEVATION,
    CONF_HEATER_SURPLUS_W,
    DEFAULT_HEATER_CHEAP_PRICE,
    DEFAULT_HEATER_CONSUMING_STATE,
    DEFAULT_HEATER_DRY_DAYS,
    DEFAULT_HEATER_EXPORT_MIN_KWH,
    DEFAULT_HEATER_HVAC_MODE,
    DEFAULT_HEATER_MARGIN,
    DEFAULT_HEATER_MAX_AZIMUTH,
    DEFAULT_HEATER_MAX_IMPORT_W,
    DEFAULT_HEATER_MIN_AZIMUTH,
    DEFAULT_HEATER_MIN_ELEVATION,
    DEFAULT_HEATER_SURPLUS_W,
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
    # Dry-spell top-up: after this many full days without exporting surplus, the heaters may
    # also heat from the grid in hours at or below cheap_price (the always-cheap hours come
    # from the service's preheat flag).
    export_entity: str | None = None
    dry_days: int = DEFAULT_HEATER_DRY_DAYS
    export_min_kwh: float = DEFAULT_HEATER_EXPORT_MIN_KWH
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
    ("export_min_kwh", CONF_HEATER_EXPORT_MIN_KWH, DEFAULT_HEATER_EXPORT_MIN_KWH),
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


def heater_devices(conf: dict[str, Any], applied: dict[str, Any] | None = None) -> list[Heater]:
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


def subentry_heaters(conf: dict[str, Any]) -> dict[str, Any]:
    """This side's own heater settings, for the service to adopt as its starting point.

    Sent until the service has applied a configuration: a site that had tuned these in Home
    Assistant must not drop to the service's defaults, which it once did.
    """
    return {
        **thresholds(conf),
        "grid_entity": conf.get(CONF_HEATER_GRID_ENTITY) or "",
        "flow_entity": conf.get(CONF_HEATER_FLOW_ENTITY) or "",
        "consuming_state": conf.get(CONF_HEATER_CONSUMING_STATE) or DEFAULT_HEATER_CONSUMING_STATE,
        "devices": [
            {"entity": h.entity, "power_w": h.power, "hvac_mode": h.hvac_mode} for h in heater_devices(conf)
        ],
    }


def config_from(conf: dict[str, Any], applied: dict[str, Any] | None = None) -> HeaterConfig | None:
    """Build the heater config from what the site drives, or None when it drives nothing.

    What the service sent comes first, since the choices are its now; what it has not sent comes
    from the subentry, which is what the site keeps running on when the service cannot be reached.
    """
    grid_entity = _sent(applied, "grid_entity") or conf.get(CONF_HEATER_GRID_ENTITY)
    if not grid_entity:
        return None
    heaters = heater_devices(conf, applied)
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
        flow_entity=_sent(applied, "flow_entity") or conf.get(CONF_HEATER_FLOW_ENTITY) or None,
        consuming=_sent(applied, "consuming_state") or conf.get(CONF_HEATER_CONSUMING_STATE) or DEFAULT_HEATER_CONSUMING_STATE,
        surplus_w=t["surplus_w"],
        max_import_w=t["max_import_w"],
        margin=t["margin"],
        min_elevation=t["min_elevation"],
        min_azimuth=t["min_azimuth"],
        max_azimuth=t["max_azimuth"],
        # The export register the day chart already uses; if the site has none, the signed grid
        # sensor is used instead.
        export_entity=conf.get(CONF_GRID_EXPORT_ENTITY) or None,
        dry_days=int(t["dry_days"]),
        export_min_kwh=t["export_min_kwh"],
        cheap_price=t["cheap_price"],
    )


def _is_on(state: Any, is_climate: bool) -> bool:
    if state is None:
        return False
    return state.state not in _CLIMATE_OFF if is_climate else state.state == "on"


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
        self.config = config_from(coordinator.conf, coordinator.applied_heaters())
        # Whether a switch-off has already let the devices go, and whether the controller has had a
        # say at all since it started. A restart while the control is off must not switch off what
        # the household turned on by hand, and it cannot tell that apart from a switch flipped while
        # it was running, so it waits for the first pass that actually drives something.
        self._disabled = False
        self._evaluated = False
        self.status = "disabled" if self.config is not None and not self.config.enabled else "idle"
        self.devices: list[_HeaterView] = []
        self.days_without_export = 0
        self.price: float | None = None
        self._dry_cache: tuple[float, int] | None = None
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
            self.config = config_from(self.coordinator.conf, self.coordinator.applied_heaters())

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
            "days_without_export": self.days_without_export,
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

    async def _dry_spell(self, config: HeaterConfig) -> int:
        """How many full local days in a row the site has not exported surplus.

        No tank sensor is needed: surplus only leaves once the heaters are full, so days without
        export are the best sign we have that they are running short. Cached for an hour, and
        only recomputed from the recorder's hourly export (the day chart's sensor, or the signed
        grid sensor when there is no export register).
        """
        now = dt_util.utcnow().timestamp()
        if self._dry_cache is not None and now - self._dry_cache[0] < 3600:
            return self._dry_cache[1]
        count = 0
        try:
            entity = config.export_entity
            sign = 1.0
            if entity is None:
                # A signed grid meter exports as a negative change.
                entity, sign = config.grid_entity, -1.0
            rows = await self.coordinator.entity_history(entity)
            days: dict[Any, float] = {}
            for row in rows:
                when = dt_util.parse_datetime(row["start"])
                if when is None:
                    continue
                day = dt_util.as_local(when).date()
                days[day] = days.get(day, 0.0) + sign * row["kwh"]
            day = dt_util.now().date() - timedelta(days=1)
            while day in days and days[day] < config.export_min_kwh:
                count += 1
                day -= timedelta(days=1)
        except Exception as err:  # noqa: BLE001 - a recorder problem must not stop the heaters
            _LOGGER.warning("Counting the days without export failed: %s", err)
            count = 0
        self._dry_cache = (now, count)
        return count

    async def _snapshot(self, config: HeaterConfig) -> Snapshot:
        on, on_seconds, off_seconds = self._snapshot_devices(config)
        self.price = self._current_price()
        self.days_without_export = await self._dry_spell(config)
        # The service's preheat flag is the always-cheap tier (price at or below the site's max).
        # On top of it, a dry spell adds the cheap hours: the heaters get topped up from the grid
        # a few days a year, when the sun has not exported for dry_days.
        always = bool((self.coordinator.data or {}).get("preheat"))
        cheap = (
            self.days_without_export >= config.dry_days
            and self.price is not None
            and self.price <= config.cheap_price
        )
        return Snapshot(
            grid_power=self._grid_power(config),
            sun_elevation=_float_attr(self.hass, "sun.sun", "elevation", -90.0),
            sun_azimuth=_float_attr(self.hass, "sun.sun", "azimuth", 180.0),
            preheat=always or cheap,
            on=on,
            on_seconds=on_seconds,
            off_seconds=off_seconds,
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
        if devices != self.devices:
            self.devices = devices
            self._notify()
        self.status = "; ".join(changed) if changed else "idle"
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
        for heater in config.heaters:
            is_on = _is_on(self.hass.states.get(heater.entity), heater.is_climate)
            if self._evaluated and not self._disabled and is_on and not self._unavailable(heater.entity):
                await self._set(heater, False)
                is_on = False
                changed.append(f"{heater.entity} off")
            devices.append(_HeaterView(entity=heater.entity, power=heater.power, on=is_on, want=False))
        self._disabled = True
        if devices != self.devices:
            self.devices = devices
            self._notify()
        self.status = "disabled" + (f": {'; '.join(changed)}" if changed else "")

    async def _set(self, heater: Heater, on: bool) -> None:
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
