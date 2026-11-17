"""Polling coordinator for one Momento, over a connection that stays open."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import DeviceInfo, MomentoAuthError, MomentoClient, MomentoError, protocol
from .api.protocol import SensorData, Slot
from .const import BOOST_SLOTS, CLOCK_SYNC_INTERVAL, DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type MomentoConfigEntry = ConfigEntry[MomentoCoordinator]


@dataclass
class MomentoData:
    sensor: SensorData
    status: int
    heating_level: int | None  # percent; None if the device reports a value we don't know
    timer_hours: int
    boost_until: datetime | None = None  # when Home Assistant's boost ends, if one runs


class MomentoCoordinator(DataUpdateCoordinator[MomentoData]):
    """Reads the heater's state every poll.

    The slots are read once per connection and then kept up to date from what this
    coordinator writes. Nothing else can change them while it's connected: the PAX
    app can't connect at the same time.
    """

    config_entry: MomentoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MomentoConfigEntry,
        client: MomentoClient,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {client.address}",
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
            always_update=False,
        )
        self.client = client
        self.device_info = device_info
        self._slots: dict[int, Slot] | None = None
        self._clock_set_at: float | None = None
        # Held from deciding what to write to the boost slots until it's written, so
        # a poll clearing a finished boost can't overwrite one that just started.
        self._slot_lock = asyncio.Lock()

    def handle_disconnect(self) -> None:
        """The next connection sets the clock again and reads the slots again.

        While disconnected, the PAX app may have changed the slots, or the heater may
        have lost power.
        """
        self._clock_set_at = None
        self._slots = None

    async def _load_slots(self) -> dict[int, Slot]:
        if self._slots is None:
            self._slots = {slot.number: slot for slot in await self.client.read_slots()}
        return self._slots

    async def _sync_clock(self) -> None:
        await self.client.write_clock(dt_util.now())
        self._clock_set_at = time.monotonic()

    async def _async_update_data(self) -> MomentoData:
        try:
            if (
                self._clock_set_at is None
                or time.monotonic() - self._clock_set_at > CLOCK_SYNC_INTERVAL.total_seconds()
            ):
                await self._sync_clock()
            sensor = await self.client.read_sensor_data()
            status = await self.client.read_status()
            level = await self.client.read_heating_level()
            timer_hours = await self.client.read_timer_hours()
            async with self._slot_lock:
                await self._clear_finished_boost()
        except MomentoAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="pin_rejected"
            ) from err
        except MomentoError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="unreachable",
                translation_placeholders={"error": str(err)},
            ) from err
        return MomentoData(
            sensor,
            status,
            level,
            timer_hours,
            self._boost_until(dt_util.now()),
        )

    # -- commands --------------------------------------------------------------

    async def _command(self, operation: Awaitable[None]) -> None:
        try:
            await operation
        except MomentoError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unreachable",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _write_slots(self, slots: list[Slot]) -> None:
        """Write ``slots``; callers hold the slot lock."""
        # Slot times are device-local, so set the clock first.
        await self._sync_clock()
        try:
            for slot in slots:
                await self.client.write_slot(slot)
                if self._slots is not None:
                    self._slots[slot.number] = slot
        except MomentoError:
            # Some slots may be written and some not, and the failed write may have
            # landed anyway. Read them all back before deciding anything from them.
            # The next poll then clears an armed slot that doesn't cover now.
            self._slots = None
            raise

    def _boost_until(self, now: datetime) -> datetime | None:
        """When the boost running now ends, or None if there isn't one."""
        if self._slots is None:
            return None
        slots = [self._slots[n] for n in BOOST_SLOTS if n in self._slots]
        return protocol.covered_until(slots, now)

    async def _clear_finished_boost(self) -> None:
        """Clear the boost slots once no boost covers now; callers hold the slot lock."""
        # Slots repeat weekly, so a finished boost would heat again next week.
        slots = await self._load_slots()
        now = dt_util.now()
        if self._boost_until(now) is None and any(
            slots[n].active for n in BOOST_SLOTS if n in slots
        ):
            _LOGGER.debug("Boost has ended, clearing its slots")
            await self._write_slots([Slot.unused(n) for n in BOOST_SLOTS])

    async def async_start_heating(self, duration: timedelta) -> None:
        """Heat for at least ``duration`` from now, through slots the heater enforces.

        A boost that already runs longer is kept as it is, so this starts or extends
        heating but never shortens it. The heater switches itself off at the end even
        if Home Assistant goes away.
        """
        async with self._slot_lock:
            await self._command(self._start_heating(duration))
        await self.async_refresh()

    async def _start_heating(self, duration: timedelta) -> None:
        await self._load_slots()  # to extend, not shorten, a boost that runs already
        now = dt_util.now()
        start = now.replace(second=0, microsecond=0)
        until = max(start + duration, self._boost_until(now) or start)
        try:
            slots = protocol.assign_slots(protocol.boost_windows(now, until - start), BOOST_SLOTS)
        except ValueError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_plan_boost",
                translation_placeholders={"error": str(err)},
            ) from err
        await self._write_slots(slots)

    async def async_stop_heating(self) -> None:
        async with self._slot_lock:
            await self._command(self._write_slots([Slot.unused(n) for n in BOOST_SLOTS]))
        await self.async_refresh()

    async def async_set_heating_level(self, percent: int) -> None:
        await self._command(self.client.write_heating_level(percent))
        await self.async_refresh()

    async def async_set_timer_hours(self, hours: int) -> None:
        await self._command(self.client.write_timer_hours(hours))
        await self.async_refresh()

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.disconnect()
