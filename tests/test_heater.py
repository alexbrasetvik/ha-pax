"""Setup, the heater switch, selects and sensors, against the fake heater."""

from __future__ import annotations

from datetime import time, timedelta
from types import SimpleNamespace

import pytest
import voluptuous as vol
from bleak.exc import BleakError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pax.api import MomentoError, protocol
from custom_components.pax.api.protocol import Slot

from .conftest import FakeMomento, load

SWITCH = "switch.guest_bath"
TEMPERATURE = "sensor.guest_bath_rail_temperature"
SOURCE = "sensor.guest_bath_heat_source"
UNTIL = "sensor.guest_bath_heating_until"
LEVEL = "select.guest_bath_heating_level"
TIMER = "select.guest_bath_timer_length"

# 2026-09-28 is a Monday.
MORNING = "2026-09-28 06:57:30+02:00"
LATE = "2026-09-28 23:30:10+02:00"


async def _call(
    hass: HomeAssistant, domain: str, service: str, entity: str, **data: object
) -> None:
    await hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity, **data}, blocking=True)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 16) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def morning(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(MORNING)


async def test_setup_creates_entities_and_sets_the_clock(
    hass: HomeAssistant, setup_integration: MockConfigEntry, momento: FakeMomento
) -> None:
    assert hass.states.get(SWITCH).state == STATE_OFF
    assert hass.states.get(TEMPERATURE).state == "24"
    assert hass.states.get(SOURCE).state == "off"
    assert hass.states.get(LEVEL).state == "100"
    assert hass.states.get(TIMER).state == "6"
    assert hass.states.get(UNTIL).state == "unknown"
    # Monday 06:57:30, local time.
    assert momento.clock == bytes([1, 6, 57, 30])


async def test_device_info(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    [device] = dr.async_entries_for_config_entry(device_registry, setup_integration.entry_id)
    assert device.name == "Guest bath"
    assert device.manufacturer == "PAX"
    assert device.model == "Momento"
    assert device.model_id == "20"
    assert device.sw_version == "1.02"
    assert device.hw_version == "01.00"


async def test_turn_on_heats_for_the_timer_length(
    hass: HomeAssistant, setup_integration: MockConfigEntry, momento: FakeMomento
) -> None:
    await _call(hass, "select", "select_option", TIMER, option="2")
    await _call(hass, "switch", "turn_on", SWITCH)

    assert momento.slot(27) == Slot(27, True, 0x01, time(6, 57), time(8, 56))
    assert momento.slot(26) == Slot.unused(26)
    assert hass.states.get(SWITCH).state == STATE_ON
    assert hass.states.get(SOURCE).state == "schedule"
    # Slots 0..25 are untouched.
    assert all(momento.slot(n) == Slot.unused(n) for n in range(26))


async def test_late_turn_on_continues_after_midnight(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(LATE)
    await _call(hass, "select", "select_option", TIMER, option="2")
    await _call(hass, "switch", "turn_on", SWITCH)

    windows = {momento.slot(27), momento.slot(26)}
    assert windows == {
        Slot(27, True, 0x01, time(23, 30), time(23, 59)),  # Monday
        Slot(26, True, 0x02, time(0, 0), time(1, 29)),  # Tuesday
    } or windows == {
        Slot(26, True, 0x01, time(23, 30), time(23, 59)),
        Slot(27, True, 0x02, time(0, 0), time(1, 29)),
    }
    # The clock was set right before, since the slots are in device time.
    assert momento.clock == bytes([1, 23, 30, 10])


async def _start_or_extend(hass: HomeAssistant, **data: object) -> None:
    await _call(hass, "pax", "start_or_extend", SWITCH, **data)


async def test_start_or_extend_heats_for_the_duration(
    hass: HomeAssistant, setup_integration: MockConfigEntry, momento: FakeMomento
) -> None:
    await _start_or_extend(hass, duration={"minutes": 45})
    assert momento.slot(27) == Slot(27, True, 0x01, time(6, 57), time(7, 41))
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_start_or_extend_defaults_to_the_timer_length(
    hass: HomeAssistant, setup_integration: MockConfigEntry, momento: FakeMomento
) -> None:
    await _call(hass, "select", "select_option", TIMER, option="2")
    await _start_or_extend(hass)
    assert momento.slot(27) == Slot(27, True, 0x01, time(6, 57), time(8, 56))


async def test_start_or_extend_extends_a_running_boost(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _start_or_extend(hass, duration={"minutes": 45})
    freezer.tick(timedelta(minutes=30))
    await _start_or_extend(hass, duration={"minutes": 45})
    assert momento.slot(27) == Slot(27, True, 0x01, time(7, 27), time(8, 11))


async def test_start_or_extend_never_shortens(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _start_or_extend(hass, duration={"hours": 3})
    freezer.tick(timedelta(minutes=30))
    await _start_or_extend(hass, duration={"minutes": 45})
    # Still ends at 09:56, now from 07:27.
    assert momento.slot(27) == Slot(27, True, 0x01, time(7, 27), time(9, 56))


async def test_start_or_extend_keeps_the_midnight_handover(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(LATE)
    await _start_or_extend(hass, duration={"hours": 2})
    freezer.tick(timedelta(minutes=20))  # 23:50
    await _start_or_extend(hass, duration={"minutes": 30})
    windows = {(s.weekdays, s.start, s.end) for s in (momento.slot(27), momento.slot(26))}
    assert windows == {(0x01, time(23, 50), time(23, 59)), (0x02, time(0, 0), time(1, 29))}


async def test_heating_until_follows_the_boost(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(LATE)
    await _start_or_extend(hass, duration={"hours": 2})
    # 01:30 Oslo time (CEST) on Tuesday.
    assert hass.states.get(UNTIL).state == "2026-09-28T23:30:00+00:00"

    freezer.tick(timedelta(minutes=45))
    await _start_or_extend(hass, duration={"hours": 2})
    assert hass.states.get(UNTIL).state == "2026-09-29T00:15:00+00:00"

    await _call(hass, "switch", "turn_off", SWITCH)
    assert hass.states.get(UNTIL).state == "unknown"


async def test_heating_until_is_unknown_for_button_heating(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.press_button()
    await _poll(hass, freezer)
    assert hass.states.get(UNTIL).state == "unknown"


async def test_start_or_extend_rejects_too_long(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid):
        await _start_or_extend(hass, duration={"hours": 13})


async def test_start_or_extend_during_button_heating_adds_a_boost(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.press_button()
    await _poll(hass, freezer)
    await _start_or_extend(hass, duration={"minutes": 45})
    assert momento.slot(27).active


async def test_finished_boost_is_cleared(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _start_or_extend(hass, duration={"minutes": 45})
    await _poll(hass, freezer, seconds=30 * 60)
    assert momento.slot(27).active
    await _poll(hass, freezer, seconds=20 * 60)
    assert hass.states.get(UNTIL).state == "unknown"
    # Otherwise it would heat again next Monday at 06:57.
    assert momento.slot(27) == Slot.unused(27)
    assert momento.slot(26) == Slot.unused(26)


async def test_turn_off_clears_the_boost(
    hass: HomeAssistant, setup_integration: MockConfigEntry, momento: FakeMomento
) -> None:
    await _call(hass, "switch", "turn_on", SWITCH)
    await _call(hass, "switch", "turn_off", SWITCH)

    assert momento.slot(27) == Slot.unused(27)
    assert momento.slot(26) == Slot.unused(26)
    assert hass.states.get(SWITCH).state == STATE_OFF
    assert hass.states.get(SOURCE).state == "off"


async def test_button_press_shows_as_on(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.press_button()
    await _poll(hass, freezer)
    assert hass.states.get(SWITCH).state == STATE_ON
    assert hass.states.get(SOURCE).state == "button"


async def test_turn_off_cannot_stop_button_heating(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.press_button()
    await _poll(hass, freezer)
    with pytest.raises(HomeAssistantError, match="button"):
        await _call(hass, "switch", "turn_off", SWITCH)
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_switch_shows_the_request_until_the_heater_catches_up(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.lagging = True
    await _call(hass, "switch", "turn_on", SWITCH)
    assert momento.sensor_data()[2] == 0x00
    assert hass.states.get(SWITCH).state == STATE_ON

    momento.evaluate()
    await _poll(hass, freezer)
    assert hass.states.get(SWITCH).state == STATE_ON
    assert hass.states.get(SOURCE).state == "schedule"


async def test_switch_gives_up_when_the_heater_never_follows(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.lagging = True
    await _call(hass, "switch", "turn_on", SWITCH)
    assert hass.states.get(SWITCH).state == STATE_ON
    await _poll(hass, freezer, seconds=150)
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_selects_write_the_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry, momento: FakeMomento
) -> None:
    await _call(hass, "select", "select_option", LEVEL, option="60")
    assert momento.level == 2
    assert hass.states.get(LEVEL).state == "60"

    await _call(hass, "select", "select_option", TIMER, option="3")
    assert momento.timer_hours == 3
    assert hass.states.get(TIMER).state == "3"


async def test_unknown_level_has_no_option(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.level = 3  # 80 %, which the app doesn't offer
    await _poll(hass, freezer)
    assert hass.states.get(LEVEL).state == "unknown"


async def test_reconnects_reauthenticates_and_sets_the_clock(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    fake_bleak: SimpleNamespace,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_bleak.clients[-1].drop()
    momento.clock = None
    momento.temperature = 40
    await _poll(hass, freezer)
    assert len(fake_bleak.clients) == 2
    assert hass.states.get(TEMPERATURE).state == "40"
    assert momento.clock is not None


async def test_clock_is_set_again_hourly(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    writes = sum(1 for uuid, _ in momento.writes if uuid == protocol.CH_CLOCK)
    await _poll(hass, freezer)
    assert sum(1 for uuid, _ in momento.writes if uuid == protocol.CH_CLOCK) == writes
    with pytest.MonkeyPatch.context() as mp:
        # The coordinator measures the interval on the monotonic clock.
        import custom_components.pax.coordinator as coordinator_mod

        base = coordinator_mod.time.monotonic()
        mp.setattr(coordinator_mod.time, "monotonic", lambda: base + 3601)
        await _poll(hass, freezer)
    assert sum(1 for uuid, _ in momento.writes if uuid == protocol.CH_CLOCK) == writes + 1


async def test_unreachable_heater_goes_unavailable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_bleak: SimpleNamespace,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_bleak.clients[-1].drop()
    fake_bleak.reachable = False
    await _poll(hass, freezer)
    assert hass.states.get(SWITCH).state == "unavailable"

    fake_bleak.reachable = True
    await _poll(hass, freezer)
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_command_on_unreachable_heater_raises(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_bleak: SimpleNamespace,
) -> None:
    fake_bleak.clients[-1].drop()
    fake_bleak.reachable = False
    with pytest.raises(HomeAssistantError, match="Could not reach"):
        await _call(hass, "switch", "turn_on", SWITCH)


async def test_wrong_pin_starts_reauth(
    hass: HomeAssistant,
    oslo: None,
    enable_bluetooth: None,
    ble_device_known: None,
    fake_bleak: SimpleNamespace,
    config_entry: MockConfigEntry,
    momento: FakeMomento,
) -> None:
    momento.pin = 1234
    await load(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_heater_not_heard_retries_setup(
    hass: HomeAssistant,
    oslo: None,
    enable_bluetooth: None,
    fake_bleak: SimpleNamespace,
    config_entry: MockConfigEntry,
) -> None:
    await load(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_disconnects(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_bleak: SimpleNamespace
) -> None:
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert not fake_bleak.clients[-1].is_connected


async def test_turn_on_with_an_unusable_timer_length_raises(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    momento.timer_hours = 0  # a raw byte; the app only offers 2..6
    await _poll(hass, freezer)
    with pytest.raises(HomeAssistantError, match="at least a minute"):
        await _call(hass, "switch", "turn_on", SWITCH)
    assert hass.states.get(SWITCH).state == STATE_OFF
    assert momento.slot(27) == Slot.unused(27)


async def test_turn_off_during_button_and_slot_heating_says_so(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _call(hass, "switch", "turn_on", SWITCH)
    momento.flags = 0x25  # on, from a slot and from the button timer
    momento.lagging = True  # keep the flags as they are
    await _poll(hass, freezer)
    assert hass.states.get(SOURCE).state == "schedule"
    with pytest.raises(HomeAssistantError, match="button"):
        await _call(hass, "switch", "turn_off", SWITCH)
    assert momento.slot(27) == Slot.unused(27)
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_empty_read_makes_the_heater_unavailable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    read = momento.read
    momento.read = lambda uuid: b"" if uuid == protocol.CH_STATUS else read(uuid)  # type: ignore[method-assign]
    with pytest.raises(MomentoError, match="empty"):
        await setup_integration.runtime_data.client.read_status()
    await _poll(hass, freezer)
    assert hass.states.get(SWITCH).state == "unavailable"


async def test_late_disconnect_of_an_old_connection_keeps_the_new_one(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_bleak: SimpleNamespace,
    freezer: FrozenDateTimeFactory,
) -> None:
    old = fake_bleak.clients[-1]
    await setup_integration.runtime_data.client.disconnect()
    await _poll(hass, freezer)
    assert len(fake_bleak.clients) == 2
    old.drop()  # its disconnected callback arrives only now
    await _poll(hass, freezer)
    assert len(fake_bleak.clients) == 2
    assert fake_bleak.clients[-1].is_connected


async def test_slots_are_read_again_after_reconnecting(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    fake_bleak: SimpleNamespace,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_bleak.clients[-1].drop()
    # Meanwhile the PAX app sets slot 27 to heat 06:50..07:59 on Mondays.
    momento.slots[27] = Slot(27, True, 0x01, time(6, 50), time(7, 59)).encode()
    await _poll(hass, freezer)
    assert hass.states.get(UNTIL).state == "2026-09-28T06:00:00+00:00"


async def test_failed_slot_write_reads_the_slots_back(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    momento: FakeMomento,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(LATE)
    await _call(hass, "switch", "turn_on", SWITCH)
    write = momento.write

    def lose_the_response(uuid: str, data: bytes) -> None:
        write(uuid, data)  # the write lands...
        if uuid == protocol.CH_SLOT_DATA and data[0] == 26:
            raise BleakError("...but its response is lost")

    momento.write = lose_the_response  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError, match="Could not reach"):
        await _call(hass, "switch", "turn_off", SWITCH)
    momento.write = write  # type: ignore[method-assign]
    await _poll(hass, freezer)
    assert momento.slot(27) == Slot.unused(27)
    assert momento.slot(26) == Slot.unused(26)
    assert hass.states.get(UNTIL).state == "unknown"
