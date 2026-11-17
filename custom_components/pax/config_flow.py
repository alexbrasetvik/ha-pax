"""Config flow: pick the heater, put it in pairing mode, read its PIN."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .api import MomentoClient, MomentoConnectionError, MomentoError, NotInPairingMode, protocol
from .const import CONF_PIN, DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULT_TITLE = "PAX Momento"


def looks_like_momento(info: BluetoothServiceInfoBleak) -> bool:
    return (info.name or "").strip().lower() == protocol.ADVERTISED_NAME.lower()


def _label(info: BluetoothServiceInfoBleak) -> str:
    name = (info.name or "").strip()
    if (
        not name
        or name.replace("-", "").replace(":", "").upper()
        == info.address.replace("-", "").replace(":", "").upper()
    ):
        return info.address
    return f"{name} ({info.address})"


class PaxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a PAX Momento."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self.context["title_placeholders"] = {"name": _label(discovery_info)}
        return await self.async_step_pair()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_pair()

        self._collect(async_discovered_service_info(self.hass, connectable=True))
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        # Heaters we recognise first; the rest in case its advertisement doesn't say.
        ordered = sorted(
            self._discovered.values(),
            key=lambda info: (not looks_like_momento(info), -info.rssi),
        )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In({i.address: _label(i) for i in ordered})}
            ),
        )

    def _collect(self, infos: Iterable[BluetoothServiceInfoBleak]) -> None:
        configured = self._async_current_ids(include_ignore=False)
        for info in infos:
            if info.address not in configured:
                self._discovered[info.address] = info

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._address is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._pair(errors)
            if result is not None:
                title, pin = result
                return self.async_create_entry(
                    title=title, data={CONF_ADDRESS: self._address, CONF_PIN: pin}
                )
        return self.async_show_form(step_id="pair", errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._address = entry_data[CONF_ADDRESS]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._pair(errors)
            if result is not None:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={CONF_PIN: result[1]}
                )
        return self.async_show_form(step_id="reauth_confirm", errors=errors)

    async def _pair(self, errors: dict[str, str]) -> tuple[str, int] | None:
        """Read and check the PIN; returns (title, pin), or fills ``errors``."""
        assert self._address is not None
        device = async_ble_device_from_address(self.hass, self._address, connectable=True)
        if device is None:
            errors["base"] = "cannot_connect"
            return None
        client = MomentoClient(device)
        try:
            model = await client.read_model_number()
            if model != protocol.MODEL_NUMBER:
                errors["base"] = "not_momento"
                return None
            pin = await client.pair()
            info = await client.read_device_info()
        except NotInPairingMode:
            errors["base"] = "not_in_pairing_mode"
        except MomentoConnectionError:
            errors["base"] = "cannot_connect"
        except MomentoError:
            errors["base"] = "pin_rejected"
        except Exception:
            _LOGGER.exception("Unexpected error pairing with %s", self._address)
            errors["base"] = "unknown"
        else:
            return info.description or DEFAULT_TITLE, pin
        finally:
            await client.disconnect()
        return None
