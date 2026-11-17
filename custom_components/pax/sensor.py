"""Sensor platform: rail temperature, what started the heating, and when it ends."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.protocol import HeatSource
from .coordinator import MomentoConfigEntry, MomentoCoordinator
from .entity import MomentoEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MomentoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            RailTemperatureSensor(coordinator),
            HeatSourceSensor(coordinator),
            HeatingUntilSensor(coordinator),
        ]
    )


class RailTemperatureSensor(MomentoEntity, SensorEntity):
    """Tracks the element: it rose about 1 °C every 20-25 s while heating."""

    _attr_translation_key = "rail_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: MomentoCoordinator) -> None:
        super().__init__(coordinator, "rail_temperature")

    @property
    def native_value(self) -> int:
        return self.coordinator.data.sensor.temperature


class HeatSourceSensor(MomentoEntity, SensorEntity):
    _attr_translation_key = "heat_source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [source.value for source in HeatSource]

    def __init__(self, coordinator: MomentoCoordinator) -> None:
        super().__init__(coordinator, "heat_source")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.sensor.source.value


class HeatingUntilSensor(MomentoEntity, SensorEntity):
    """When Home Assistant's boost ends; unknown when none is running.

    The heater doesn't say when its button timer runs out, so button heating
    shows as unknown too.
    """

    _attr_translation_key = "heating_until"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MomentoCoordinator) -> None:
        super().__init__(coordinator, "heating_until")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.boost_until
