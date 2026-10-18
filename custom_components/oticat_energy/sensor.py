"""Sensors for the battery plan."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EnergyConfigEntry
from .entity import EnergyEntity

ACTIONS = ["charge", "hold", "self_consume"]


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, c in entry.runtime_data.items():
        entities: list[SensorEntity] = [
            ActionSensor(c),
            SocTargetSensor(c),
            PlanMoneySensor(c, "savings", "savings_eur"),
            PlanMoneySensor(c, "planned_cost", "cost_eur"),
        ]
        # Only when the site has water heaters set up.
        if c.heater.active:
            entities.extend([HeaterSensor(c), DryDaysSensor(c), CheapHoursSensor(c)])
        async_add_entities(entities, config_subentry_id=subentry_id)


class ActionSensor(EnergyEntity, SensorEntity):
    """What the battery should do this hour. The full plan and the inverter segments are attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ACTIONS
    # The hour-by-hour plan changes every 15 minutes and would bloat the recorder.
    _unrecorded_attributes = frozenset({"hours", "segments", "sources"})

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "action")

    @property
    def native_value(self) -> str:
        return self.coordinator.data["action"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        plan = d.get("plan") or {}
        return {
            "status": d["status"],
            "error": d["error"],
            "grid_charge": d["grid_charge"],
            "soc_target": d["soc_target"],
            "generated_at": plan.get("generated_at"),
            "horizon_end": plan.get("horizon_end"),
            "segments": d["segments"],
            "hours": plan.get("hours", []),
            "sources": plan.get("sources"),
            "inverter": self.coordinator.writer.status if self.coordinator.writer else None,
        }


class SocTargetSensor(EnergyEntity, SensorEntity):
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "soc_target")

    @property
    def native_value(self) -> int:
        return self.coordinator.data["soc_target"]


class PlanMoneySensor(EnergyEntity, SensorEntity):
    """Euros over the plan's horizon. Unknown while running on the fallback."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, key: str, field: str) -> None:
        super().__init__(coordinator, key)
        self._field = field

    @property
    def native_value(self) -> float | None:
        d = self.coordinator.data
        if d["status"] == "fallback" or not d.get("plan"):
            return None
        return d["plan"].get(self._field)


class HeaterSensor(EnergyEntity, SensorEntity):
    """How many water heaters are running, and what each one is doing.

    Only exists when the site has water heaters configured. The controller updates it the
    moment a heater changes, not on the 15-minute plan refresh.
    """

    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "heaters")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.heater.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        return sum(1 for heater in self.coordinator.heater.devices if heater.on)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.heater.view()


class DryDaysSensor(EnergyEntity, SensorEntity):
    """How many full days in a row the site has not exported surplus.

    The water heaters' dry-spell rule: from `dry_days` on, they may also heat from the grid in the
    hours at or below the site's cheap price. Counted from the recorder's hourly export, so it
    needs a grid export (or signed grid) sensor with statistics. Only exists when the site has
    water heaters configured, which is where the thresholds live.
    """

    _attr_icon = "mdi:weather-sunny-off"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "days_without_export")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.heater.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        return self.coordinator.heater.days_without_export

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        heater = self.coordinator.heater
        config = heater.config
        return {
            "dry_days": config.dry_days if config else None,
            "export_min_kwh": config.export_min_kwh if config else None,
            "dry_spell": heater.dry_spell,
            "price": heater.price,
        }


class CheapHoursSensor(EnergyEntity, SensorEntity):
    """The hours of the plan the heaters may heat from the grid in, as windows of the clock.

    These are the preheat hours the service marks plus the hours at or below the dry-spell price,
    whatever the dry spell is doing right now: they are the schedule the top-up would use, so it
    can be read before it starts (in a dry spell the heaters heat in every one of them). The plan
    covers from now to the end of tomorrow, so a window that has already gone does not appear.
    """

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cheap_hours")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.heater.async_add_listener(self.async_write_ha_state))

    def _windows(self) -> list[dict[str, Any]]:
        heater = self.coordinator.heater
        config = heater.config
        plan = (self.coordinator.data or {}).get("plan") or {}
        if config is None:
            return []
        runs: list[list[datetime]] = []
        for hour in plan.get("hours", []):
            price = hour.get("price")
            cheap = hour.get("preheat") or (isinstance(price, (int, float)) and price <= config.cheap_price)
            start = dt_util.parse_datetime(hour.get("start") or "")
            if not cheap or start is None:
                continue
            # Two runs of the same kind, but never one across midnight: "00:00-08:00" is what the
            # next day's window has to read as, not the tail of an afternoon that was also cheap.
            same_day = runs and dt_util.as_local(runs[-1][1] - timedelta(hours=1)).date() == dt_util.as_local(start).date()
            if runs and runs[-1][1] == start and same_day:
                runs[-1][1] = start + timedelta(hours=1)
            else:
                runs.append([start, start + timedelta(hours=1)])
        out: list[dict[str, Any]] = []
        for start, end in runs:
            local_start, local_end = dt_util.as_local(start), dt_util.as_local(end)
            to = local_end.strftime("%H:%M")
            if to == "00:00" and local_end.date() != local_start.date():
                to = "24:00"
            out.append(
                {
                    "start": local_start.isoformat(),
                    "end": local_end.isoformat(),
                    "hours": int((end - start).total_seconds() // 3600),
                    "from": local_start.strftime("%H:%M"),
                    "to": to,
                }
            )
        return out

    @property
    def native_value(self) -> str:
        windows = self._windows()
        if not windows:
            return "none"
        # Three windows fit in a state; the whole schedule is in the attributes.
        shown = ", ".join(f"{w['from']}-{w['to']}" for w in windows[:3])
        return shown + (", …" if len(windows) > 3 else "")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        heater = self.coordinator.heater
        config = heater.config
        windows = self._windows()
        return {
            "windows": windows,
            "hours": sum(w["hours"] for w in windows),
            "next_start": windows[0]["start"] if windows else None,
            "next_end": windows[0]["end"] if windows else None,
            "cheap_price": config.cheap_price if config else None,
            "price": heater.price,
            "preheat": heater.preheat_flag,
            "dry_spell": heater.dry_spell,
            "days_without_export": heater.days_without_export,
        }
