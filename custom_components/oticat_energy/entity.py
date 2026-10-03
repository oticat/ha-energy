"""Base entity for Oticat Energy: one device per inverter subentry."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnergyCoordinator


class EnergyEntity(CoordinatorEntity[EnergyCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnergyCoordinator, key: str) -> None:
        super().__init__(coordinator)
        subentry = coordinator.subentry
        self._attr_translation_key = key
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="oti.cat",
            model="Battery planning",
            entry_type=DeviceEntryType.SERVICE,
        )
