"""Statistics sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.entities import PRIMARY_SENSOR_KEYS, obsolete_unique_ids
from .entity import TechmarkEntity


@dataclass(frozen=True, kw_only=True)
class TechmarkSensorDescription(SensorEntityDescription):
    period: str | None  # "day" | "month" | None
    stat: str | None = None
    value_fn: Callable | None = None


def _count(key: str, name: str, period: str, stat: str) -> TechmarkSensorDescription:
    return TechmarkSensorDescription(key=key, name=name, period=period, stat=stat, state_class=SensorStateClass.TOTAL)


# The only primary statistics sensors (keys keep existing entity IDs; see core/entities.py).
SENSORS: tuple[TechmarkSensorDescription, ...] = (
    _count("known_customers_today", "Unique customers today", "day", "unique_customers"),
    _count("known_customers_month", "Unique customers this month", "month", "unique_customers"),
    _count("employees_present_today", "Employees present today", "day", "employees_present"),
    _count("returning_customers_month", "Returning customers this month", "month", "returning_customers"),
    TechmarkSensorDescription(
        key="camera_clock_skew",
        name="Camera clock skew",
        period=None,
        value_fn=lambda rt: rt.clock_skew,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)
assert tuple(d.key for d in SENSORS if d.entity_category is None) == PRIMARY_SENSOR_KEYS


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    for uid in obsolete_unique_ids([e.unique_id for e in entries if e.domain == "sensor"], str(entry.unique_id)):
        if entity_id := registry.async_get_entity_id("sensor", DOMAIN, uid):
            registry.async_remove(entity_id)  # removed sensor: no orphan entity left behind
    async_add_entities(TechmarkSensor(entry.runtime_data, d) for d in SENSORS)


class TechmarkSensor(TechmarkEntity, SensorEntity):
    entity_description: TechmarkSensorDescription

    def __init__(self, runtime, description: TechmarkSensorDescription) -> None:
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.rt.store is not None

    @property
    def native_value(self):
        d = self.entity_description
        if d.value_fn:
            return d.value_fn(self.rt)
        source = self.rt.today if d.period == "day" else self.rt.month
        return source.get(d.stat)

    @property
    def last_reset(self) -> datetime | None:
        p = self.entity_description.period
        if p == "day":
            return dt_util.start_of_local_day()
        if p == "month":
            today = dt_util.now().date()
            return dt_util.start_of_local_day(date(today.year, today.month, 1))
        return None
