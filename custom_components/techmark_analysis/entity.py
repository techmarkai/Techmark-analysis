"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import CONF_MODEL, DOMAIN
from .coordinator import TechmarkRuntime


class TechmarkEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: TechmarkRuntime, key: str) -> None:
        self.rt = runtime
        entry = runtime.entry
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer="Hikvision",
            model=entry.data.get(CONF_MODEL),
            name="Techmark",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.rt.signal, self.async_write_ha_state))
