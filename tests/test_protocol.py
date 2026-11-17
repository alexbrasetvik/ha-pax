"""Codecs and slot planning, against bytes captured from a real Momento."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.pax.api import protocol
from custom_components.pax.api.protocol import DayWindow, HeatSource, SensorData, Slot

# 2026-09-28 is a Monday.
MONDAY = datetime(2026, 9, 28, 6, 57, 42, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "on", "source", "temperature"),
    [
        ("0032031f00", True, HeatSource.POWER_ON, 31),  # just after power-up
        ("0032052400", True, HeatSource.BUTTON, 36),  # after one button press
        ("0032002700", False, HeatSource.OFF, 39),  # after the second press
        ("0032212900", True, HeatSource.SCHEDULE, 41),  # boost slot active
    ],
)
def test_captured_sensor_data(raw: str, on: bool, source: HeatSource, temperature: int) -> None:
    data = SensorData.decode(bytes.fromhex(raw))
    assert data.heater_on is on
    assert data.source is source
    assert data.temperature == temperature
    assert data.mains_frequency == 50


def test_sensor_data_length_is_checked() -> None:
    with pytest.raises(ValueError, match="5 or 6 bytes"):
        SensorData.decode(b"\x00\x32")


def test_captured_unused_slot() -> None:
    assert Slot.decode(bytes.fromhex("1b007f0000000000")) == Slot.unused(27)


def test_captured_boost_slot_round_trips() -> None:
    raw = bytes.fromhex("1b01010639070701")  # the probe script's Monday 06:57-07:07 boost
    slot = Slot.decode(raw)
    assert slot == Slot(27, True, 0x01, time(6, 57), time(7, 7), visible=True)
    assert slot.encode() == raw


def test_slot_number_range() -> None:
    with pytest.raises(ValueError, match=r"0\.\.27"):
        Slot(28, True, 1, time(0), time(1)).encode()


def test_clock_uses_iso_weekday() -> None:
    assert protocol.encode_clock(MONDAY) == bytes([1, 6, 57, 42])
    assert protocol.encode_clock(MONDAY + timedelta(days=6))[0] == 7  # Sunday


def test_heating_levels() -> None:
    assert protocol.encode_heating_level(100) == b"\x04"
    assert protocol.encode_heating_level(40) == b"\x01"
    assert protocol.decode_heating_level(b"\x02") == 60
    assert protocol.decode_heating_level(b"\x09") is None
    with pytest.raises(ValueError, match="50%"):
        protocol.encode_heating_level(50)


def test_pin() -> None:
    assert protocol.decode_pin(protocol.encode_pin(12345678)) == 12345678
    assert protocol.decode_pin(b"\x00\x00\x00\x00") == 0
    assert protocol.decode_pin(b"") == 0


def test_boost_within_a_day() -> None:
    assert protocol.boost_windows(MONDAY, timedelta(minutes=10)) == [
        DayWindow(0, time(6, 57), time(7, 6))
    ]


def test_boost_across_midnight_splits_per_day() -> None:
    late_sunday = datetime(2026, 10, 4, 23, 30, 15, tzinfo=UTC)
    assert protocol.boost_windows(late_sunday, timedelta(hours=2)) == [
        DayWindow(6, time(23, 30), time(23, 59)),
        DayWindow(0, time(0, 0), time(1, 29)),
    ]


def test_boost_ending_at_midnight_stays_in_one_day() -> None:
    start = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)
    assert protocol.boost_windows(start, timedelta(hours=2)) == [
        DayWindow(0, time(22, 0), time(23, 59))
    ]


def test_boost_needs_a_duration() -> None:
    with pytest.raises(ValueError, match="at least a minute"):
        protocol.boost_windows(MONDAY, timedelta(seconds=30))


def test_pack_merges_same_times_into_one_mask() -> None:
    weekdays = [DayWindow(d, time(6, 0), time(6, 59)) for d in range(5)]
    evening = DayWindow(6, time(21, 0), time(21, 29))
    assert protocol.pack_windows([*weekdays, evening]) == [
        (0b0011111, time(6, 0), time(6, 59)),
        (0b1000000, time(21, 0), time(21, 29)),
    ]


def test_pack_rejects_bad_windows() -> None:
    with pytest.raises(ValueError, match="ends before"):
        protocol.pack_windows([DayWindow(0, time(7, 0), time(6, 0))])
    with pytest.raises(ValueError, match="weekday"):
        protocol.pack_windows([DayWindow(7, time(6, 0), time(7, 0))])


def test_assign_slots_clears_the_rest() -> None:
    windows = [DayWindow(0, time(6, 57), time(7, 6))]
    assert protocol.assign_slots(windows, (27, 26)) == [
        Slot(27, True, 0x01, time(6, 57), time(7, 6)),
        Slot.unused(26),
    ]


def test_assign_slots_refuses_to_overflow() -> None:
    windows = [DayWindow(d, time(d, 0), time(d, 30)) for d in range(3)]
    with pytest.raises(ValueError, match="don't fit"):
        protocol.assign_slots(windows, (27, 26))


def test_covered_until_follows_a_boost_across_midnight() -> None:
    late = MONDAY.replace(hour=23, minute=30, second=0)
    slots = protocol.assign_slots(protocol.boost_windows(late, timedelta(hours=2)), (27, 26))
    assert protocol.covered_until(slots, late) == late + timedelta(hours=2)
    # From just after midnight, only Tuesday's slot is left.
    after = late + timedelta(minutes=45)
    assert protocol.covered_until(slots, after) == late + timedelta(hours=2)


def test_covered_until_is_none_outside_the_slots() -> None:
    slots = protocol.assign_slots(protocol.boost_windows(MONDAY, timedelta(hours=1)), (27, 26))
    assert protocol.covered_until(slots, MONDAY + timedelta(hours=1)) is None
    # The same time next week is covered again, which is why finished boosts are cleared.
    assert protocol.covered_until(slots, MONDAY + timedelta(days=7)) is not None
    assert protocol.covered_until([Slot.unused(27), Slot.unused(26)], MONDAY) is None
