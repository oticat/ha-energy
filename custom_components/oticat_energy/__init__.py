"""Oticat Energy: battery charge plans from PVPC prices, solar and load forecasts.

One config entry holds the service URL. Each inverter is a subentry with its own site
token, entities, device and coordinator.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import SUBENTRY_INVERTER
from .coordinator import EnergyCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type EnergyConfigEntry = ConfigEntry[dict[str, EnergyCoordinator]]


async def async_setup_entry(hass: HomeAssistant, entry: EnergyConfigEntry) -> bool:
    coordinators: dict[str, EnergyCoordinator] = {}
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_INVERTER:
            continue
        coordinator = EnergyCoordinator(hass, entry, subentry)
        await coordinator.async_setup()
        # One unreachable inverter must not keep the others from loading.
        await coordinator.async_refresh()
        coordinators[subentry_id] = coordinator
    entry.runtime_data = coordinators
    # Adding, changing or removing an inverter updates the entry; reload to match.
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: EnergyConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnergyConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
