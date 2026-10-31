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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import TechmarkEntity


@dataclass(frozen=True, kw_only=True)
class TechmarkSensorDescription(SensorEntityDescription):
    period: str | None  # "day" | "month" | None
    stat: str | None = None
    value_fn: Callable | None = None
    door: bool = False


def _count(key: str, name: str, period: str, stat: str, door: bool = False) -> TechmarkSensorDescription:
    return TechmarkSensorDescription(
        key=key, name=name, period=period, stat=stat, door=door, state_class=SensorStateClass.TOTAL
    )


SENSORS: tuple[TechmarkSensorDescription, ...] = (
    _count("employees_present_today", "Employees present today", "day", "employees_present"),
    _count("known_customers_today", "Known customers today", "day", "known_customers_unique"),
    _count("returning_customers_today", "Returning customers today", "day", "returning_customers"),
    _count("customer_visits_today", "Customer visits today", "day", "customer_visits"),
    _count("unmatched_face_events_today", "Unmatched face events today", "day", "unmatched_face_events"),
    _count("low_quality_faces_today", "Low-quality faces today", "day", "low_quality_faces"),
    _count("door_entries_today", "Door entries today", "day", "door_entries", door=True),
    _count("door_exits_today", "Door exits today", "day", "door_exits", door=True),
    _count("known_customers_month", "Known customers this month", "month", "known_customers_unique"),
    _count("returning_customers_month", "Returning customers this month", "month", "returning_customers"),
    _count("customer_visits_month", "Customer visits this month", "month", "customer_visits"),
    _count("unmatched_face_events_month", "Unmatched face events this month", "month", "unmatched_face_events"),
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


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(TechmarkSensor(entry.runtime_data, d) for d in SENSORS)


class TechmarkSensor(TechmarkEntity, SensorEntity):
    entity_description: TechmarkSensorDescription

    def __init__(self, runtime, description: TechmarkSensorDescription) -> None:
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if self.entity_description.door:
            return bool(self.rt.processor.cfg.line_directions)
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
