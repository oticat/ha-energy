"""Writes the plan's segments into a Deye inverter's time-of-use programs.

Works with the ha-solarman integration, whose entities are named
<prefix>_program_<n>_time / _soc / _power / _charging and <prefix>_time_of_use.
The same names in the input helper domains (input_datetime, input_number, input_select)
are accepted too, which is what the simulated inverter uses.

Only values that differ from the current state are written, so a run with nothing to
change sends no Modbus writes at all, and a program changed by hand is put back on the
next run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

GRID_OPTION = "Grid"
NO_CHARGE_OPTION = "Disabled"
TOU_OPTION = "Week"

# For each field: (suffix, native domain, helper domain)
FIELDS = {
    "time": ("time", "time", "input_datetime"),
    "soc": ("soc", "number", "input_number"),
    "power": ("power", "number", "input_number"),
    "charging": ("charging", "select", "input_select"),
}


class DeyeWriter:
    """Applies segments to a Deye inverter through its HA entities."""

    def __init__(self, hass: HomeAssistant, prefix: str, program_power: int) -> None:
        self.hass = hass
        self.prefix = prefix
        self.program_power = program_power
        self.status = "idle"
        self.writes = 0
        self._lock = asyncio.Lock()

    def _find(self, name: str, domains: tuple[str, ...]) -> str | None:
        for domain in domains:
            entity_id = f"{domain}.{name}"
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return entity_id
        return None

    async def _set(self, entity_id: str, kind: str, value: Any) -> None:
        domain = entity_id.split(".")[0]
        if kind == "time":
            service, data = ("set_value", {"time": value}) if domain == "time" else ("set_datetime", {"time": value})
        elif kind == "option":
            service, data = "select_option", {"option": value}
        else:
            service, data = "set_value", {"value": value}
        await self.hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
        self.writes += 1

    async def async_apply(self, segments: list[dict[str, Any]]) -> None:
        async with self._lock:
            try:
                self.status = await self._apply(segments)
            except Exception as err:  # noqa: BLE001 - report it on the sensor, keep planning
                _LOGGER.warning("Writing the inverter programs failed: %s", err)
                self.status = f"error: {err}"

    async def _apply(self, segments: list[dict[str, Any]]) -> str:
        if len(segments) < 6:
            return f"error: plan has {len(segments)} segments, the inverter needs 6"

        tou = self._find(f"{self.prefix}_time_of_use", ("select", "input_select"))
        if tou is None:
            return f"error: {self.prefix}_time_of_use not found"

        entities: list[dict[str, str]] = []
        for n in range(1, 7):
            found = {}
            for key, (suffix, native, helper) in FIELDS.items():
                entity_id = self._find(f"{self.prefix}_program_{n}_{suffix}", (native, helper))
                if entity_id is None:
                    return f"error: {self.prefix}_program_{n}_{suffix} not found"
                found[key] = entity_id
            entities.append(found)

        if self.hass.states.get(tou).state != TOU_OPTION:
            await self._set(tou, "option", TOU_OPTION)

        for seg, ent in zip(segments[:6], entities):
            want_time = f"{seg['start']}:00"
            if self.hass.states.get(ent["time"]).state[:5] != seg["start"]:
                await self._set(ent["time"], "time", want_time)
            if _num(self.hass.states.get(ent["soc"]).state) != seg["soc"]:
                await self._set(ent["soc"], "value", seg["soc"])
            if _num(self.hass.states.get(ent["power"]).state) != self.program_power:
                await self._set(ent["power"], "value", self.program_power)
            want_charging = GRID_OPTION if seg["grid_charge"] else NO_CHARGE_OPTION
            if self.hass.states.get(ent["charging"]).state != want_charging:
                await self._set(ent["charging"], "option", want_charging)
        return "ok"


def _num(value: str) -> float | None:
    try:
        return round(float(value))
    except ValueError:
        return None
