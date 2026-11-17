"""Switch platform: the heater itself."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, PENDING_STATE_TIMEOUT
from .coordinator import MomentoConfigEntry, MomentoCoordinator
from .entity import MomentoEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MomentoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([MomentoHeaterSwitch(entry.runtime_data)])


class MomentoHeaterSwitch(MomentoEntity, SwitchEntity):
    """Whether the heater is on, however it was started.

    Switching on writes schedule slots covering the next timer-length hours, so the
    heater switches itself off at the end. The protocol has no "heat now" command;
    a schedule slot is the only way to start it remotely.
    """

    _attr_name = None
    _attr_translation_key = "heater"

    def __init__(self, coordinator: MomentoCoordinator) -> None:
        super().__init__(coordinator, "heater")
        # What was last asked for, shown until the heater reports it (or the timeout).
        self._pending: bool | None = None
        self._cancel_pending: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool:
        if self._pending is not None:
            return self._pending
        return self.coordinator.data.sensor.heater_on

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data.sensor.heater_on == self._pending:
            self._clear_pending()
        super()._handle_coordinator_update()

    def _expect(self, wanted: bool) -> None:
        self._clear_pending()
        self._pending = wanted
        self._cancel_pending = async_call_later(
            self.hass, PENDING_STATE_TIMEOUT, self._pending_expired
        )

    @callback
    def _clear_pending(self) -> None:
        self._pending = None
        if self._cancel_pending is not None:
            self._cancel_pending()
            self._cancel_pending = None

    @callback
    def _pending_expired(self, _now: datetime) -> None:
        self._cancel_pending = None
        self._pending = None
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._clear_pending()
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_start_or_extend()

    async def async_start_or_extend(self, duration: timedelta | None = None) -> None:
        """Heat for at least ``duration`` (default: the timer length) from now.

        Extends a Home Assistant boost that would end sooner and keeps one that runs
        longer. Heating started by the button keeps going too, for at least as long.
        """
        if duration is None:
            duration = timedelta(hours=self.coordinator.data.timer_hours)
        self._expect(True)
        try:
            await self.coordinator.async_start_heating(duration)
        except HomeAssistantError:
            self._clear_pending()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._expect(False)
        try:
            await self.coordinator.async_stop_heating()
        except HomeAssistantError:
            self._clear_pending()
            raise
        if self.coordinator.data.sensor.started_at_heater:
            # Clearing the slots only stops heating that a slot started.
            self._clear_pending()
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="started_at_heater",
            )
