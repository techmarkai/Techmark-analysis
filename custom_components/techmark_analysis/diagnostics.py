"""Diagnostics with credentials redacted."""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL

REDACT = {CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_SERIAL}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict:
    rt = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "options": dict(entry.options),
        "connected": rt.connected,
        "last_error": rt.last_error,
        "counters": rt.counters,
        "correlation": {"attached": rt.correlator.attached, "uncorrelated_images": rt.correlator.uncorrelated_images},
        "clock_skew_seconds": rt.clock_skew,
        "enrollment": {
            "enabled": rt.enroller.cfg.enabled,
            "pending": rt.enroller.pending,
            "pending_bytes": rt.enroller.pending_bytes,
            **rt.enroller.stats,
        },
        "today": {k: v for k, v in rt.today.items() if k != "attendance"},
    }
