"""Techmark Analysis: camera-side face analytics into Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import TechmarkRuntime
from .isapi import AuthError, CameraError, HikClient
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type TechmarkConfigEntry = ConfigEntry[TechmarkRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TechmarkConfigEntry) -> bool:
    client = HikClient(
        get_async_client(hass), entry.data[CONF_HOST], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    try:
        await client.device_info()
    except AuthError as err:
        raise ConfigEntryAuthFailed("Camera rejected the credentials") from err
    except CameraError as err:
        # Not fatal: the stream runner keeps retrying and records the outage as a data gap.
        _LOGGER.warning("Camera not reachable at startup (%s); will keep retrying", err)
    runtime = TechmarkRuntime(hass, entry, client)
    await runtime.async_start()
    entry.runtime_data = runtime
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TechmarkConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: TechmarkConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
