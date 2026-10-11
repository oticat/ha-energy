"""Binary sensors: is the plan charging from the grid this hour, and is it a cheap-grid hour."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyConfigEntry
from .entity import EnergyEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, c in entry.runtime_data.items():
        async_add_entities([GridChargeSensor(c), PreheatSensor(c)], config_subentry_id=subentry_id)


class GridChargeSensor(EnergyEntity, BinarySensorEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "grid_charge")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data["grid_charge"]


class PreheatSensor(EnergyEntity, BinarySensorEntity):
    """On when the current hour's buy price is low enough to heat from the grid without surplus."""

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "preheat")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("preheat", False)
