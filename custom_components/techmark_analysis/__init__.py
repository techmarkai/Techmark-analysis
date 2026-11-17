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

from .const import DB_FILENAME, DOMAIN
from .coordinator import TechmarkRuntime
from .core.store import delete_database_files
from .isapi import AuthError, CameraError, HikClient
from .services import async_setup_services
from .views import AttendanceExportView

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type TechmarkConfigEntry = ConfigEntry[TechmarkRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    hass.http.register_view(AttendanceExportView())
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


async def async_remove_entry(hass: HomeAssistant, entry: TechmarkConfigEntry) -> None:
    """Removing the integration erases its history database, so adding it again starts from zero.

    The camera's face records and any exported report files are not touched.
    """
    if any(e.entry_id != entry.entry_id for e in hass.config_entries.async_entries(DOMAIN)):
        return  # another camera entry still uses the shared database
    try:
        removed = await hass.async_add_executor_job(delete_database_files, hass.config.path(DB_FILENAME))
    except OSError as err:
        _LOGGER.warning("Could not delete the Techmark history database: %s", err)
    else:
        _LOGGER.info("Techmark history database removed (%s)", ", ".join(removed) or "nothing to remove")


async def _async_reload(hass: HomeAssistant, entry: TechmarkConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
