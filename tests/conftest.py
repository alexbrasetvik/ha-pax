"""Fixtures: a fake Momento behind a fake BleakClient, so tests exercise the real client."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pax.api import protocol
from custom_components.pax.api.protocol import Slot
from custom_components.pax.const import CONF_PIN, DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"

ADDRESS = "63EA77EC-4085-DDEC-63E5-56519A82B7CF"
PIN = 4711
BLE_DEVICE = BLEDevice(ADDRESS, None, details=None)
ESTABLISH = "custom_components.pax.api.client.establish_connection"
BLE_DEVICE_FROM_ADDRESS = "homeassistant.components.bluetooth.async_ble_device_from_address"
FLOW_BLE_DEVICE_FROM_ADDRESS = "custom_components.pax.config_flow.async_ble_device_from_address"

# Characteristics the fake only serves after a correct PIN.
_PROTECTED = {
    protocol.CH_STATUS,
    protocol.CH_SENSOR_DATA,
    protocol.CH_TIMER_ACTIVE_HOURS,
    protocol.CH_HEATING_LEVEL,
    protocol.CH_NUMBER_OF_SLOTS,
    protocol.CH_SLOT_SELECTION,
    protocol.CH_SLOT_DATA,
    protocol.CH_CLOCK,
}


def service_info(address: str = ADDRESS, name: str = "") -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, name, details=None),
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        ),
        connectable=True,
        time=0,
        tx_power=None,
        raw=None,
    )


class FakeMomento:
    """Behaves like the real heater did when probed (software 1.02)."""

    def __init__(self) -> None:
        self.pin = PIN
        self.pairing_mode = False
        self.model = protocol.MODEL_NUMBER
        self.description = "Guest bath"
        self.flags = 0x00
        self.temperature = 24
        self.status = 0
        self.level = 4  # 100 %
        self.timer_hours = 6
        self.slots = {n: Slot.unused(n).encode() for n in range(28)}
        self.clock: bytes | None = None
        self.authenticated = False
        self.slot_pointer = 0
        # When set, the heater ignores slot changes until evaluate() is called.
        self.lagging = False
        self.writes: list[tuple[str, bytes]] = []

    def press_button(self) -> None:
        """First press heats on the button timer, the next one stops it."""
        self.flags = 0x00 if self.flags & 0x04 else 0x05

    def evaluate(self, now: datetime | None = None) -> None:
        """Heat on the schedule while an active slot covers now."""
        now = now or dt_util.now()
        covered = False
        for raw in self.slots.values():
            slot = Slot.decode(raw)
            if (
                slot.active
                and slot.weekdays & (1 << now.weekday())
                and slot.start <= now.time().replace(second=0, microsecond=0) <= slot.end
            ):
                covered = True
        if covered:
            self.flags = 0x21
        elif self.flags & 0x20:
            self.flags = 0x00

    def sensor_data(self) -> bytes:
        return bytes([0, 50, self.flags, self.temperature, 0])

    def read(self, uuid: str) -> bytes:
        if uuid in _PROTECTED and not self.authenticated:
            raise BleakError("insufficient authentication")
        text = {
            protocol.CH_MODEL_NUMBER: self.model,
            protocol.CH_MANUFACTURER: "Pax",
            protocol.CH_HARDWARE_REVISION: "01.00",
            protocol.CH_SOFTWARE_REVISION: "1.02",
            protocol.CH_USER_DESCRIPTION: self.description,
        }
        if uuid in text:
            return text[uuid].encode()
        if uuid == protocol.CH_PIN_CODE:
            return protocol.encode_pin(self.pin if self.pairing_mode else 0)
        if uuid == protocol.CH_PIN_CONFIRMATION:
            return bytes([int(self.authenticated)])
        if uuid == protocol.CH_STATUS:
            return bytes([self.status])
        if uuid == protocol.CH_SENSOR_DATA:
            return self.sensor_data()
        if uuid == protocol.CH_HEATING_LEVEL:
            return bytes([self.level])
        if uuid == protocol.CH_TIMER_ACTIVE_HOURS:
            return bytes([self.timer_hours])
        if uuid == protocol.CH_NUMBER_OF_SLOTS:
            return bytes([len(self.slots)])
        if uuid == protocol.CH_SLOT_DATA:
            data = self.slots[self.slot_pointer]
            self.slot_pointer = (self.slot_pointer + 1) % len(self.slots)
            return data
        if uuid == protocol.CH_CLOCK:
            return self.clock or bytes([1, 0, 0, 0])
        raise BleakError(f"unexpected read of {uuid}")

    def write(self, uuid: str, data: bytes) -> None:
        self.writes.append((uuid, data))
        if uuid == protocol.CH_PIN_CODE:
            self.authenticated = protocol.decode_pin(data) == self.pin
            return
        if uuid in _PROTECTED and not self.authenticated:
            raise BleakError("insufficient authentication")
        if uuid == protocol.CH_SLOT_SELECTION:
            self.slot_pointer = data[0]
        elif uuid == protocol.CH_SLOT_DATA:
            self.slots[data[0]] = bytes(data)
            if not self.lagging:
                self.evaluate()
        elif uuid == protocol.CH_HEATING_LEVEL:
            self.level = data[0]
        elif uuid == protocol.CH_TIMER_ACTIVE_HOURS:
            self.timer_hours = data[0]
        elif uuid == protocol.CH_CLOCK:
            self.clock = bytes(data)
        else:
            raise BleakError(f"unexpected write to {uuid}")

    def slot(self, number: int) -> Slot:
        return Slot.decode(self.slots[number])


class FakeBleakClient:
    """The parts of BleakClient that MomentoClient uses."""

    def __init__(self, device: FakeMomento, disconnected_callback: Any) -> None:
        self.device = device
        self.is_connected = True
        self._disconnected_callback = disconnected_callback

    async def read_gatt_char(self, uuid: str) -> bytearray:
        if not self.is_connected:
            raise BleakError("not connected")
        return bytearray(self.device.read(uuid))

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        if not self.is_connected:
            raise BleakError("not connected")
        assert response, "the app always writes with response"
        self.device.write(uuid, bytes(data))

    async def disconnect(self) -> None:
        self.is_connected = False

    def drop(self) -> None:
        """The link goes away (out of range, heater power cut)."""
        self.is_connected = False
        self._disconnected_callback(self)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


@pytest.fixture
async def oslo(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Oslo")


@pytest.fixture
def momento() -> FakeMomento:
    return FakeMomento()


@pytest.fixture
def fake_bleak(momento: FakeMomento) -> Generator[SimpleNamespace]:
    """Patch establish_connection to hand out FakeBleakClients."""
    clients: list[FakeBleakClient] = []
    state = SimpleNamespace(clients=clients, reachable=True)

    async def _establish(client_class: Any, device: Any, name: str, **kwargs: Any) -> Any:
        if not state.reachable:
            raise BleakError("device not found")
        momento.authenticated = False  # each connection starts unauthenticated
        client = FakeBleakClient(momento, kwargs["disconnected_callback"])
        clients.append(client)
        return client

    with patch(ESTABLISH, side_effect=_establish):
        yield state


@pytest.fixture
def ble_device_known() -> Generator[None]:
    with (
        patch(BLE_DEVICE_FROM_ADDRESS, return_value=BLE_DEVICE),
        patch(FLOW_BLE_DEVICE_FROM_ADDRESS, return_value=BLE_DEVICE),
    ):
        yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Guest bath",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_PIN: PIN},
    )


async def load(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    oslo: None,
    enable_bluetooth: None,
    ble_device_known: None,
    fake_bleak: SimpleNamespace,
    config_entry: MockConfigEntry,
) -> MockConfigEntry:
    await load(hass, config_entry)
    assert config_entry.state.value == "loaded"
    return config_entry
