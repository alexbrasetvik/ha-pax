"""BLE transport for a PAX Momento: one persistent connection, one operation at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
)

from . import protocol
from .protocol import SensorData, Slot

_LOGGER = logging.getLogger(__name__)


class MomentoError(Exception):
    """Base class for Momento failures."""


class MomentoConnectionError(MomentoError):
    """Could not connect, or the connection dropped mid-operation."""


class MomentoAuthError(MomentoError):
    """The device rejected the PIN."""


class NotInPairingMode(MomentoError):
    """The PIN read back as 0, which the device does outside pairing mode."""


@dataclass(frozen=True)
class DeviceInfo:
    model_number: str
    manufacturer: str
    hardware_revision: str
    software_revision: str
    description: str


class MomentoClient:
    """Talks to one Momento.

    Connects on demand and stays connected. Every (re)connection writes the PIN
    first, as the app does. ``on_disconnect`` tells the coordinator whenever the
    current connection goes away, whether it dropped or was closed here, so it sets
    the clock and reads the slots again on the next connection.
    """

    def __init__(
        self,
        device: BLEDevice,
        pin: int | None = None,
        *,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._ble_device = device
        self._pin = pin
        self.on_disconnect = on_disconnect
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, device: BLEDevice) -> None:
        """Adopt a fresher BLEDevice, e.g. when another proxy hears it better."""
        self._ble_device = device

    # -- connection ------------------------------------------------------------

    async def _ensure_connected(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.name or self.address,
                disconnected_callback=self._handle_disconnect,
                ble_device_callback=lambda: self._ble_device,
            )
        except (BleakError, BleakNotFoundError, TimeoutError) as err:
            raise MomentoConnectionError(str(err) or type(err).__name__) from err
        self._client = client
        _LOGGER.debug("%s: connected", self.address)
        if self._pin is not None:
            try:
                await self._authenticate(self._pin)
            except MomentoError:
                await self._disconnect()
                raise
        return client

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._notify_disconnect()
        try:
            await client.disconnect()
        except BleakError as err:
            _LOGGER.debug("%s: error while disconnecting: %s", self.address, err)

    def _handle_disconnect(self, client: BleakClient) -> None:
        if client is not self._client:
            # An older connection that _disconnect already dropped and reported. Its
            # callback can arrive after the next connection is up.
            return
        _LOGGER.debug("%s: disconnected", self.address)
        self._client = None
        self._notify_disconnect()

    def _notify_disconnect(self) -> None:
        if self.on_disconnect is not None:
            self.on_disconnect()

    # -- raw GATT (callers hold the lock) --------------------------------------

    async def _read(self, char: str) -> bytes:
        client = await self._ensure_connected()
        try:
            return bytes(await client.read_gatt_char(char))
        except (BleakError, TimeoutError) as err:
            await self._disconnect()
            raise MomentoConnectionError(f"reading {char}: {err}") from err

    async def _read_byte(self, char: str) -> int:
        data = await self._read(char)
        if not data:
            raise MomentoError(f"reading {char}: empty value")
        return data[0]

    async def _write(self, char: str, data: bytes) -> None:
        client = await self._ensure_connected()
        try:
            # The app always writes with response.
            await client.write_gatt_char(char, data, response=True)
        except (BleakError, TimeoutError) as err:
            await self._disconnect()
            raise MomentoConnectionError(f"writing {char}: {err}") from err
        _LOGGER.debug("%s: wrote %s to %s", self.address, data.hex(), char)

    async def _authenticate(self, pin: int) -> None:
        await self._write(protocol.CH_PIN_CODE, protocol.encode_pin(pin))
        confirmation = await self._read(protocol.CH_PIN_CONFIRMATION)
        if not confirmation or not confirmation[0]:
            raise MomentoAuthError("PIN rejected")

    # -- pairing ---------------------------------------------------------------

    async def pair(self) -> int:
        """Read the PIN (device in pairing mode), check it, and use it from now on."""
        async with self._lock:
            pin = protocol.decode_pin(await self._read(protocol.CH_PIN_CODE))
            if pin == 0:
                raise NotInPairingMode
            await self._authenticate(pin)
            self._pin = pin
            return pin

    # -- operations ------------------------------------------------------------

    async def read_device_info(self) -> DeviceInfo:
        async with self._lock:
            text = [
                protocol.decode_text(await self._read(char))
                for char in (
                    protocol.CH_MODEL_NUMBER,
                    protocol.CH_MANUFACTURER,
                    protocol.CH_HARDWARE_REVISION,
                    protocol.CH_SOFTWARE_REVISION,
                    protocol.CH_USER_DESCRIPTION,
                )
            ]
        return DeviceInfo(*text)

    async def read_model_number(self) -> str:
        async with self._lock:
            return protocol.decode_text(await self._read(protocol.CH_MODEL_NUMBER))

    async def read_sensor_data(self) -> SensorData:
        async with self._lock:
            data = await self._read(protocol.CH_SENSOR_DATA)
        try:
            return SensorData.decode(data)
        except ValueError as err:
            raise MomentoError(str(err)) from err

    async def read_status(self) -> int:
        async with self._lock:
            return await self._read_byte(protocol.CH_STATUS)

    async def read_heating_level(self) -> int | None:
        async with self._lock:
            return protocol.decode_heating_level(await self._read(protocol.CH_HEATING_LEVEL))

    async def write_heating_level(self, percent: int) -> None:
        async with self._lock:
            await self._write(protocol.CH_HEATING_LEVEL, protocol.encode_heating_level(percent))

    async def read_timer_hours(self) -> int:
        async with self._lock:
            return await self._read_byte(protocol.CH_TIMER_ACTIVE_HOURS)

    async def write_timer_hours(self, hours: int) -> None:
        async with self._lock:
            await self._write(protocol.CH_TIMER_ACTIVE_HOURS, bytes([hours]))

    async def write_clock(self, now: datetime) -> None:
        async with self._lock:
            await self._write(protocol.CH_CLOCK, protocol.encode_clock(now))

    async def read_slots(self) -> list[Slot]:
        """All schedule slots.

        Writing 0 to the selection resets a read pointer; each read then returns
        the next slot (checked on a real unit: 28 slots, 0..27 in order).
        """
        async with self._lock:
            await self._write(protocol.CH_SLOT_SELECTION, b"\x00")
            count = await self._read_byte(protocol.CH_NUMBER_OF_SLOTS)
            slots = []
            for _ in range(count):
                data = await self._read(protocol.CH_SLOT_DATA)
                try:
                    slots.append(Slot.decode(data))
                except ValueError as err:
                    raise MomentoError(str(err)) from err
            return slots

    async def write_slot(self, slot: Slot) -> None:
        async with self._lock:
            await self._write(protocol.CH_SLOT_DATA, slot.encode())
