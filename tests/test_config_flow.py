"""Config flow: pick a device, pair, and re-pair."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pax.config_flow import looks_like_momento
from custom_components.pax.const import CONF_PIN, DOMAIN

from .conftest import ADDRESS, PIN, FakeMomento, service_info

DISCOVERED = "custom_components.pax.config_flow.async_discovered_service_info"
SETUP_ENTRY = "custom_components.pax.async_setup_entry"

pytestmark = pytest.mark.usefixtures("enable_bluetooth", "ble_device_known", "fake_bleak")


async def _start(hass: HomeAssistant) -> str:
    others = [service_info("11:22:33:44:55:66", "Living room"), service_info()]
    with patch(DISCOVERED, return_value=others):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["step_id"] == "pair"
    return result["flow_id"]


async def test_pairs_and_creates_entry(hass: HomeAssistant, momento: FakeMomento) -> None:
    flow_id = await _start(hass)
    momento.pairing_mode = True
    with patch(SETUP_ENTRY, return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Guest bath"
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_PIN: PIN}
    assert result["result"].unique_id == ADDRESS


async def test_untitled_heater_gets_a_default_title(
    hass: HomeAssistant, momento: FakeMomento
) -> None:
    flow_id = await _start(hass)
    momento.pairing_mode = True
    momento.description = ""
    with patch(SETUP_ENTRY, return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["title"] == "PAX Momento"


async def test_not_in_pairing_mode_keeps_the_form(
    hass: HomeAssistant, momento: FakeMomento
) -> None:
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_in_pairing_mode"}

    momento.pairing_mode = True
    with patch(SETUP_ENTRY, return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_other_devices_are_refused(hass: HomeAssistant, momento: FakeMomento) -> None:
    flow_id = await _start(hass)
    momento.model = "10"  # a Calima fan
    momento.pairing_mode = True
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["errors"] == {"base": "not_momento"}


async def test_unreachable(hass: HomeAssistant, fake_bleak: SimpleNamespace) -> None:
    flow_id = await _start(hass)
    fake_bleak.reachable = False
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_nothing_heard_aborts(hass: HomeAssistant) -> None:
    with patch(DISCOVERED, return_value=[]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_configured_heater_is_not_offered(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    with patch(DISCOVERED, return_value=[service_info()]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT


async def test_reauth_stores_the_new_pin(
    hass: HomeAssistant, config_entry: MockConfigEntry, momento: FakeMomento
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    momento.pin = 999
    momento.pairing_mode = True
    with patch(SETUP_ENTRY, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PIN] == 999


async def test_discovered_heater_pairs(hass: HomeAssistant, momento: FakeMomento) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info(name="Pax Momento"),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    momento.pairing_mode = True
    with patch(SETUP_ENTRY, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_PIN: PIN}


async def test_discovered_configured_heater_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info(name="Pax Momento"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def test_recognises_heaters_by_name() -> None:
    assert looks_like_momento(service_info(name="Pax Momento"))
    assert looks_like_momento(service_info(name="PAX MOMENTO"))
    assert not looks_like_momento(service_info(name="PAX Calima"))
    assert not looks_like_momento(service_info(name="Living room"))
    assert not looks_like_momento(service_info(name=""))
