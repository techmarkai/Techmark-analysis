"""Stream connectivity sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import TechmarkEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([StreamConnected(entry.runtime_data)])


class StreamConnected(TechmarkEntity, BinarySensorEntity):
    _attr_name = "Event stream"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "stream_connected")

    @property
    def is_on(self) -> bool:
        return self.rt.connected

    @property
    def extra_state_attributes(self) -> dict:
        return {"last_error": self.rt.last_error, **self.rt.counters}
