"""PAX Momento GATT layout and payload codecs.

Reverse engineered from the PAX Connect 1.7.0 iOS app (a React Native bundle), then
checked against a real Momento (hardware 01.00, software 1.02). No Bluetooth or
Home Assistant imports here, so everything is plain data in, plain data out.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import IntEnum, StrEnum

# --- GATT ---------------------------------------------------------------------

SERVICE_CONNECTION = "e6834e4b-7b3a-48e6-91e4-f1d005f564d3"
SERVICE_STATUS = "1a46a853-e5ed-4696-bac0-70e346884a26"
SERVICE_CONFIG = "75430500-d8c7-11e6-88f3-8fb4792b4153"
SERVICE_UUIDS: tuple[str, ...] = (SERVICE_CONNECTION, SERVICE_STATUS, SERVICE_CONFIG)

# Pairing and PIN check, shared with PAX's Calima fans.
CH_PIN_CODE = "4cad343a-209a-40b7-b911-4d9b3df569b2"
CH_PIN_CONFIRMATION = "d1ae6b70-ee12-4f6d-b166-d2063dcaffe1"
CH_USER_DESCRIPTION = "b85fa07a-9382-4838-871c-81d045dcc2ff"

CH_STATUS = "25a824ad-3021-4de9-9f2f-60cf8d17bded"
CH_SENSOR_DATA = "528b80e8-c47a-4c0a-bdf1-916a7748f412"


def _config_char(n: int) -> str:
    return f"754305{n:02x}-d8c7-11e6-88f3-8fb4792b4153"


CH_TIMER_ACTIVE_HOURS = _config_char(0x02)
CH_HEATING_LEVEL = _config_char(0x03)
CH_NUMBER_OF_SLOTS = _config_char(0x06)
CH_SLOT_SELECTION = _config_char(0x07)
CH_SLOT_DATA = _config_char(0x08)
CH_CLOCK = _config_char(0x0B)
# Not used here: 0x01 factory settings changed, 0x04/0x05 Calima fan link, 0x09 slot
# names, 0x0a LED test, 0x0c reset, 0x0d ioControl, 0x0e mode, 0x0f DFU bootloader.

CH_MODEL_NUMBER = "00002a24-0000-1000-8000-00805f9b34fb"
CH_HARDWARE_REVISION = "00002a27-0000-1000-8000-00805f9b34fb"
CH_SOFTWARE_REVISION = "00002a28-0000-1000-8000-00805f9b34fb"
CH_MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"

MODEL_NUMBER = "20"

# The app recognises a Momento by this advertised name, compared case-insensitively.
# The connection service is shared with Calima fans, so it doesn't identify a heater.
ADVERTISED_NAME = "Pax Momento"

# --- Values -------------------------------------------------------------------


class Status(IntEnum):
    """Device status byte. It stayed NO_ERROR on a real unit whose clock was unset."""

    NO_ERROR = 0
    NO_SYNC_TO_CALIMA = 1
    RTC_NOT_SET = 2


# Wire value -> heating level in percent.
HEATING_LEVELS: dict[int, int] = {0: 20, 1: 40, 2: 60, 3: 80, 4: 100}
# The subset the app offers.
ALLOWED_HEATING_LEVELS: tuple[int, ...] = (40, 60, 100)
# Button timer lengths the app offers, in hours.
TIMER_HOURS: tuple[int, ...] = (2, 3, 4, 5, 6)

MAX_SLOT_NUMBER = 27
ALL_WEEKDAYS = 0x7F


def encode_heating_level(percent: int) -> bytes:
    for wire, value in HEATING_LEVELS.items():
        if value == percent:
            return bytes([wire])
    raise ValueError(f"unsupported heating level {percent}%")


def decode_heating_level(data: bytes) -> int | None:
    return HEATING_LEVELS.get(data[0]) if data else None


def encode_pin(pin: int) -> bytes:
    return struct.pack("<I", pin)


def decode_pin(data: bytes) -> int:
    """The PIN, or 0 when the device isn't in pairing mode."""
    return int(struct.unpack_from("<I", data)[0]) if len(data) >= 4 else 0


def encode_clock(now: datetime) -> bytes:
    """Day of week (1 = Monday .. 7 = Sunday), hour, minute, second."""
    return bytes([now.isoweekday(), now.hour, now.minute, now.second])


def decode_text(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


class HeatSource(StrEnum):
    """Why the heater is on, from the sensor data trigger flags."""

    OFF = "off"
    POWER_ON = "power_on"  # heats by itself for a while after getting power
    BUTTON = "button"
    SCHEDULE = "schedule"
    OTHER = "other"  # on, but no flag we know (or the linked Calima fan)


@dataclass(frozen=True)
class SensorData:
    button_state: int
    mains_frequency: int
    heater_on: bool
    is_booting: bool
    button_timer: bool
    repeatable_button_timer: bool
    fan: bool
    scheduled_timer: bool
    temperature: int  # rail temperature, °C
    raw: bytes

    @classmethod
    def decode(cls, data: bytes) -> SensorData:
        if len(data) not in (5, 6):
            raise ValueError(f"sensor data should be 5 or 6 bytes, got {data.hex()}")
        flags = data[2]
        return cls(
            button_state=data[0],
            mains_frequency=data[1],
            heater_on=bool(flags & 0x01),
            is_booting=bool(flags & 0x02),
            button_timer=bool(flags & 0x04),
            repeatable_button_timer=bool(flags & 0x08),
            fan=bool(flags & 0x10),
            scheduled_timer=bool(flags & 0x20),
            temperature=data[3],
            raw=bytes(data),
        )

    @property
    def started_at_heater(self) -> bool:
        """Heating that clearing the slots doesn't stop: a button timer or the power-on run.

        It can run alongside a slot; ``source`` then says SCHEDULE.
        """
        return self.heater_on and (
            self.button_timer or self.repeatable_button_timer or self.is_booting
        )

    @property
    def source(self) -> HeatSource:
        if not self.heater_on:
            return HeatSource.OFF
        if self.scheduled_timer:
            return HeatSource.SCHEDULE
        if self.button_timer or self.repeatable_button_timer:
            return HeatSource.BUTTON
        if self.is_booting:
            return HeatSource.POWER_ON
        return HeatSource.OTHER


# --- Schedule slots -----------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One of the device's weekly schedule slots.

    The heater runs from ``start`` through ``end`` (both inclusive, minute
    resolution) on every weekday in ``weekdays`` (bit 0 = Monday .. bit 6 = Sunday).
    A slot can't cross midnight.
    """

    number: int
    active: bool
    weekdays: int
    start: time
    end: time
    visible: bool = True

    @classmethod
    def unused(cls, number: int) -> Slot:
        """How an untouched slot reads back from the device."""
        return cls(number, False, ALL_WEEKDAYS, time(0, 0), time(0, 0), visible=False)

    @classmethod
    def decode(cls, data: bytes) -> Slot:
        if len(data) != 8:
            raise ValueError(f"slot data should be 8 bytes, got {data.hex()}")
        return cls(
            number=data[0],
            active=bool(data[1]),
            weekdays=data[2] & ALL_WEEKDAYS,
            start=time(data[3], data[4]),
            end=time(data[5], data[6]),
            visible=bool(data[7]),
        )

    def encode(self) -> bytes:
        if not 0 <= self.number <= MAX_SLOT_NUMBER:
            raise ValueError(f"slot number must be 0..{MAX_SLOT_NUMBER}")
        return bytes(
            [
                self.number,
                int(self.active),
                self.weekdays & ALL_WEEKDAYS,
                self.start.hour,
                self.start.minute,
                self.end.hour,
                self.end.minute,
                int(self.visible),
            ]
        )


@dataclass(frozen=True, order=True)
class DayWindow:
    """Heating on one weekday (0 = Monday), from ``start`` through ``end`` inclusive."""

    weekday: int
    start: time
    end: time


def split_into_days(start: datetime, end: datetime) -> list[DayWindow]:
    """Cover ``start``..``end`` (minute resolution, both inclusive) with per-day windows.

    Slots can't cross midnight, so this splits a window that does into one per day.
    """
    start = start.replace(second=0, microsecond=0)
    end = end.replace(second=0, microsecond=0)
    windows: list[DayWindow] = []
    day_start = start
    while day_start <= end:
        day_end = min(end, day_start.replace(hour=23, minute=59))
        windows.append(DayWindow(day_start.weekday(), day_start.time(), day_end.time()))
        day_start = datetime.combine(
            day_start.date() + timedelta(days=1), time(0, 0), tzinfo=day_start.tzinfo
        )
    return windows


def pack_windows(windows: list[DayWindow]) -> list[tuple[int, time, time]]:
    """Group windows with the same times into (weekday mask, start, end), one per slot.

    A weekly schedule that heats 06:00-07:00 on every weekday then needs one slot,
    not five.
    """
    masks: dict[tuple[time, time], int] = {}
    for w in windows:
        if not 0 <= w.weekday <= 6:
            raise ValueError(f"weekday must be 0..6, got {w.weekday}")
        if w.end < w.start:
            raise ValueError(f"window ends before it starts: {w}")
        masks[(w.start, w.end)] = masks.get((w.start, w.end), 0) | (1 << w.weekday)
    return [(mask, start, end) for (start, end), mask in sorted(masks.items())]


def boost_windows(now: datetime, duration: timedelta) -> list[DayWindow]:
    """Windows that heat from the current minute for ``duration``."""
    if duration < timedelta(minutes=1):
        raise ValueError("duration must be at least a minute")
    start = now.replace(second=0, microsecond=0)
    return split_into_days(start, start + duration - timedelta(minutes=1))


def assign_slots(windows: list[DayWindow], numbers: tuple[int, ...]) -> list[Slot]:
    """Lay ``windows`` out over the slots ``numbers``, clearing the ones left over."""
    packed = pack_windows(windows)
    if len(packed) > len(numbers):
        raise ValueError(f"{len(packed)} windows don't fit in {len(numbers)} slots")
    slots = [
        Slot(number, True, mask, start, end)
        for number, (mask, start, end) in zip(numbers, packed, strict=False)
    ]
    slots += [Slot.unused(number) for number in numbers[len(packed) :]]
    return slots


def covered_until(slots: list[Slot], now: datetime) -> datetime | None:
    """When heating on ``slots`` stops, if they cover the current minute; else None.

    Follows coverage across midnight, from one slot into the next. It looks at today
    and tomorrow only, which covers a boost (boosts last under a day).
    """
    minute = now.replace(second=0, microsecond=0)
    intervals: list[tuple[datetime, datetime]] = []
    for day in (minute.date(), minute.date() + timedelta(days=1)):
        for slot in slots:
            if slot.active and slot.weekdays & (1 << day.weekday()):
                start = datetime.combine(day, slot.start, tzinfo=now.tzinfo)
                end = datetime.combine(day, slot.end, tzinfo=now.tzinfo)
                intervals.append((start, end + timedelta(minutes=1)))
    until = minute
    extended = True
    while extended:
        extended = False
        for start, end in intervals:
            if start <= until < end:
                until = end
                extended = True
    return until if until > minute else None
