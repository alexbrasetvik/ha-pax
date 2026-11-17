"""The PAX integration: PAX Momento towel warmers over Bluetooth LE."""

from __future__ import annotations

from datetime import timedelta

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

from .api import MomentoAuthError, MomentoClient, MomentoError
from .const import ATTR_DURATION, CONF_PIN, DOMAIN, MAX_BOOST, SERVICE_START_OR_EXTEND
from .coordinator import MomentoConfigEntry, MomentoCoordinator

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_START_OR_EXTEND,
        entity_domain=Platform.SWITCH,
        schema={
            vol.Optional(ATTR_DURATION): vol.All(
                cv.time_period, vol.Range(min=timedelta(minutes=1), max=MAX_BOOST)
            )
        },
        func="async_start_or_extend",
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MomentoConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if device is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="not_found",
            translation_placeholders={"address": address},
        )

    client = MomentoClient(device, entry.data[CONF_PIN])
    try:
        info = await client.read_device_info()
    except MomentoAuthError as err:
        await client.disconnect()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="pin_rejected"
        ) from err
    except MomentoError as err:
        await client.disconnect()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="unreachable",
            translation_placeholders={"error": str(err)},
        ) from err

    coordinator = MomentoCoordinator(hass, entry, client, info)
    client.on_disconnect = coordinator.handle_disconnect

    @callback
    def _async_advertisement(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        client.set_ble_device(service_info.device)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.disconnect()
        raise
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MomentoConfigEntry) -> bool:
    """Unload a config entry (the coordinator's shutdown hook disconnects)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
