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
    CONF_HEATER_CONSUMING_STATE,
    CONF_HEATER_FLOW_ENTITY,
    CONF_HEATER_GRID_ENTITY,
    CONF_HEATER_MARGIN,
    CONF_HEATER_MAX_AZIMUTH,
    CONF_HEATER_MAX_IMPORT_W,
    CONF_HEATER_MIN_AZIMUTH,
    CONF_HEATER_MIN_ELEVATION,
    CONF_HEATER_SURPLUS_W,
    DEFAULT_HEATER_CONSUMING_STATE,
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


def config_from(conf: dict[str, Any]) -> HeaterConfig | None:
    """Build the heater config from a subentry's data, or None when no heater is set up."""
    grid_entity = conf.get(CONF_HEATER_GRID_ENTITY)
    if not grid_entity:
        return None
    heaters: list[Heater] = []
    for i in range(1, MAX_HEATERS + 1):
        entity = conf.get(heater_key(i, "entity"))
        if not entity:
            continue
        power = conf.get(heater_key(i, "power"))
        heaters.append(
            Heater(
                entity=entity,
                power=float(power or 0),
                hvac_mode=conf.get(heater_key(i, "hvac_mode")) or DEFAULT_HEATER_HVAC_MODE,
            )
        )
    if not heaters:
        return None
    return HeaterConfig(
        heaters=heaters,
        grid_entity=grid_entity,
        flow_entity=conf.get(CONF_HEATER_FLOW_ENTITY) or None,
        consuming=conf.get(CONF_HEATER_CONSUMING_STATE) or DEFAULT_HEATER_CONSUMING_STATE,
        surplus_w=float(conf.get(CONF_HEATER_SURPLUS_W, DEFAULT_HEATER_SURPLUS_W)),
        max_import_w=float(conf.get(CONF_HEATER_MAX_IMPORT_W, DEFAULT_HEATER_MAX_IMPORT_W)),
        margin=float(conf.get(CONF_HEATER_MARGIN, DEFAULT_HEATER_MARGIN)),
        min_elevation=float(conf.get(CONF_HEATER_MIN_ELEVATION, DEFAULT_HEATER_MIN_ELEVATION)),
        min_azimuth=float(conf.get(CONF_HEATER_MIN_AZIMUTH, DEFAULT_HEATER_MIN_AZIMUTH)),
        max_azimuth=float(conf.get(CONF_HEATER_MAX_AZIMUTH, DEFAULT_HEATER_MAX_AZIMUTH)),
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
        self.config = config_from(coordinator.conf)
        self.status = "idle"
        self.devices: list[_HeaterView] = []
        self._unsubs: list[Any] = []
        self._lock = asyncio.Lock()
        self._pending = False

    @property
    def active(self) -> bool:
        return self.config is not None

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
        """The heaters' state, for the action sensor's attributes."""
        return {
            "status": self.status,
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

    def _snapshot(self, config: HeaterConfig) -> Snapshot:
        now = dt_util.utcnow()
        on, on_seconds, off_seconds = [], [], []
        for heater in config.heaters:
            state = self.hass.states.get(heater.entity)
            is_on = _is_on(state, heater.is_climate)
            elapsed = (now - state.last_changed).total_seconds() if state is not None else None
            on.append(is_on)
            on_seconds.append(elapsed if is_on else None)
            off_seconds.append(elapsed if not is_on else None)
        return Snapshot(
            grid_power=self._grid_power(config),
            sun_elevation=_float_attr(self.hass, "sun.sun", "elevation", -90.0),
            sun_azimuth=_float_attr(self.hass, "sun.sun", "azimuth", 180.0),
            preheat=bool((self.coordinator.data or {}).get("preheat")),
            on=on,
            on_seconds=on_seconds,
            off_seconds=off_seconds,
        )

    # --- apply ----------------------------------------------------------------------------

    async def _apply(self) -> None:
        config = self.config
        assert config is not None
        snapshot = self._snapshot(config)
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
        self.devices = devices
        self.status = "; ".join(changed) if changed else "idle"
        if changed:
            _LOGGER.debug("Water heaters: %s", self.status)
            # Republish so the action sensor's `heaters` attribute shows what just happened,
            # instead of waiting up to 15 minutes for the next plan refresh. Never before the
            # coordinator's first refresh, to keep it out of the entry setup.
            if self.coordinator.data is not None:
                self.coordinator.async_set_updated_data(self.coordinator.view())

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
