"""The switch that turns the site's high-consumption device control on and off.

The setting belongs to the site on dom.oti.cat, like everything else about it, and the plan
response carries it out with the rest of the configuration. Flipping it here sends the change back
with the next plan request, so an automation — or a person — can pause the control without losing
the devices it drives. Off means the controller switches off what it was running and stops
deciding; the devices stay chosen and come back with the same switch.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyConfigEntry
from .entity import EnergyEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, c in entry.runtime_data.items():
        # Only when the site drives something: with no devices there is nothing to switch.
        if c.heater.active:
            async_add_entities([HighConsumptionSwitch(c)], config_subentry_id=subentry_id)


class HighConsumptionSwitch(EnergyEntity, SwitchEntity):
    """On while the site's water heaters are being driven from the plan."""

    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "high_consumption")

    @property
    def is_on(self) -> bool:
        return self.coordinator.heaters_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heaters_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heaters_enabled(False)
