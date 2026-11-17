"""Select platform: heating level and timer length."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.protocol import ALLOWED_HEATING_LEVELS, TIMER_HOURS
from .coordinator import MomentoConfigEntry, MomentoCoordinator
from .entity import MomentoEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MomentoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([HeatingLevelSelect(coordinator), TimerLengthSelect(coordinator)])


class HeatingLevelSelect(MomentoEntity, SelectEntity):
    """Heating power, as the app offers it."""

    _attr_translation_key = "heating_level"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [str(level) for level in ALLOWED_HEATING_LEVELS]

    def __init__(self, coordinator: MomentoCoordinator) -> None:
        super().__init__(coordinator, "heating_level")

    @property
    def current_option(self) -> str | None:
        level = self.coordinator.data.heating_level
        option = str(level)
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_heating_level(int(option))


class TimerLengthSelect(MomentoEntity, SelectEntity):
    """How long a button press, or switching the heater on, heats for."""

    _attr_translation_key = "timer_length"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [str(hours) for hours in TIMER_HOURS]

    def __init__(self, coordinator: MomentoCoordinator) -> None:
        super().__init__(coordinator, "timer_length")

    @property
    def current_option(self) -> str | None:
        option = str(self.coordinator.data.timer_hours)
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_timer_hours(int(option))
