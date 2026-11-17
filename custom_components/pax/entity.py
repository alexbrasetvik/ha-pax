"""Shared base entity for PAX."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import MomentoCoordinator


class MomentoEntity(CoordinatorEntity[MomentoCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MomentoCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        address: str = entry.data[CONF_ADDRESS]
        info = coordinator.device_info
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            model_id=info.model_number or None,
            name=entry.title,
            sw_version=info.software_revision or None,
            hw_version=info.hardware_revision or None,
        )
