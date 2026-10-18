"""Binary sensors: is the plan charging from the grid this hour, and is it a cheap-grid hour."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyConfigEntry
from .entity import EnergyEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, c in entry.runtime_data.items():
        entities: list[BinarySensorEntity] = [GridChargeSensor(c), PreheatSensor(c)]
        # Only when the site has water heaters set up: it is the flag their controller uses.
        if c.heater.active:
            entities.append(CheapGridSensor(c))
        async_add_entities(entities, config_subentry_id=subentry_id)


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


class CheapGridSensor(EnergyEntity, BinarySensorEntity):
    """On when the water heaters may heat from the grid this hour.

    Either the service marked the hour cheap (the plan's preheat flag) or the site is in a dry
    spell and the hour is at or below its cheap price. It is the flag the controller itself acts
    on, so an automation that follows it signals exactly when the heaters do — the plan's own
    preheat flag stays as it is, for sites that drive something else with it.
    """

    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cheap_grid")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.heater.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self.coordinator.heater.cheap_grid

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        heater = self.coordinator.heater
        config = heater.config
        return {
            "price": heater.price,
            "cheap_price": config.cheap_price if config else None,
            "preheat": heater.preheat_flag,
            "dry_spell": heater.dry_spell,
            "days_without_export": heater.days_without_export,
            "dry_days": config.dry_days if config else None,
        }
