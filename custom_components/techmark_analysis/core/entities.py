"""Entity inventory (pure, testable): which sensor keys exist and which are obsolete."""

from __future__ import annotations

# The only primary statistics sensors. Keys are unique_id suffixes; keeping the historical
# keys preserves existing entity IDs (and their history) for the renamed sensors.
PRIMARY_SENSOR_KEYS = (
    "known_customers_today",  # "Unique customers today"
    "known_customers_month",  # "Unique customers this month"
    "employees_present_today",
    "returning_customers_month",
)

# Removed in 0.3.0; their registry entries are deleted so no orphan entities remain.
OBSOLETE_SENSOR_KEYS = frozenset({
    "returning_customers_today",
    "customer_visits_today",
    "customer_visits_month",
    "unmatched_face_events_today",
    "unmatched_face_events_month",
    "low_quality_faces_today",
    "door_entries_today",
    "door_exits_today",
})


def obsolete_unique_ids(unique_ids: list[str], prefix: str) -> list[str]:
    """Registry unique_ids (`<prefix>_<key>`) belonging to removed sensors."""
    return [u for u in unique_ids if u.startswith(f"{prefix}_") and u[len(prefix) + 1:] in OBSOLETE_SENSOR_KEYS]
