"""Oticat Energy: battery charge plans from PVPC prices, solar and load forecasts.

One config entry holds the service URL. Each inverter is a subentry with its own site
token, entities, device and coordinator.

The entry's options also hold the entity ids this Home Assistant allows the service to use. They are
set in the install flow and changed later with Configure; the service chooses which of them a site
reads and drives, but nothing else is read, uploaded or switched.
"""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_APPROVED,
    CONF_APPROVED_ENTITIES,
    CONF_APPROVED_INVERTERS,
    CONF_GRID_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_HEATER_FLOW_ENTITY,
    CONF_HEATER_GRID_ENTITY,
    CONF_INVERTER_PREFIX,
    CONF_LOAD_ENTITY,
    CONF_SOC_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_ZONE,
    DOMAIN,
    MAX_HEATERS,
    SUBENTRY_INVERTER,
    heater_key,
)
from .coordinator import EnergyCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]

type EnergyConfigEntry = ConfigEntry[dict[str, EnergyCoordinator]]

# The subentry keys that hold an entity the site already uses, so an entry created before the
# permission existed keeps working: what it was already reading and driving is allowed, and the
# user is told to review it.
_SUBENTRY_ENTITY_KEYS = (
    CONF_SOC_ENTITY,
    CONF_LOAD_ENTITY,
    CONF_GRID_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_ZONE,
    CONF_HEATER_GRID_ENTITY,
    CONF_HEATER_FLOW_ENTITY,
) + tuple(heater_key(i, "entity") for i in range(1, MAX_HEATERS + 1))


def _migrate_approval(hass: HomeAssistant, entry: EnergyConfigEntry) -> None:
    """Allow what an entry from before 0.19.0 was already using, and ask the user to review it.

    The alternative is a silent stop: its subentries name entities that the new check would refuse,
    so every existing site would lose its meters, its heaters and its inverter on upgrade. What was
    already wired up is what its owner had already consented to, so exactly that is allowed — not
    the whole installation, which at this point in the startup is only half loaded anyway — and a
    notification points at the screen where the rest can be allowed.
    """
    if CONF_APPROVED in entry.options:
        return
    entities: set[str] = set()
    allowed_inverters: set[str] = set()
    for subentry in entry.subentries.values():
        data = subentry.data or {}
        for key in _SUBENTRY_ENTITY_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value:
                entities.add(value)
        prefix = data.get(CONF_INVERTER_PREFIX)
        if isinstance(prefix, str) and prefix:
            allowed_inverters.add(prefix)
    # 0.18.0's blanket switch, superseded by the approved set itself.
    options = {k: v for k, v in entry.options.items() if k != "share_catalogue"}
    hass.config_entries.async_update_entry(
        entry,
        options={
            **options,
            CONF_APPROVED: {
                CONF_APPROVED_ENTITIES: sorted(entities),
                CONF_APPROVED_INVERTERS: sorted(allowed_inverters),
            },
        },
    )
    persistent_notification.async_create(
        hass,
        "Oticat Energy now asks this Home Assistant which entities a site may use. The entities "
        "this installation was already reading and driving were allowed automatically; nothing "
        "else was. Allow more, or narrow those, in **Settings › Devices & services › Oticat Energy "
        "› Configure**.",
        title="Oticat Energy: review the entities it may use",
        notification_id=f"{DOMAIN}_{entry.entry_id}_approval",
    )


async def async_setup_entry(hass: HomeAssistant, entry: EnergyConfigEntry) -> bool:
    _migrate_approval(hass, entry)
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
