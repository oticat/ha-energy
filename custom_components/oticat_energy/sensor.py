"""Sensors for the battery plan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyConfigEntry
from .entity import EnergyEntity

ACTIONS = ["charge", "hold", "self_consume"]


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, c in entry.runtime_data.items():
        async_add_entities(
            [
                ActionSensor(c),
                SocTargetSensor(c),
                PlanMoneySensor(c, "savings", "savings_eur"),
                PlanMoneySensor(c, "planned_cost", "cost_eur"),
            ],
            config_subentry_id=subentry_id,
        )


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
