"""Services: reports and read-only face library listing. No enrollment in this version."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .isapi import CameraError

REPORT_SCHEMA = vol.Schema({vol.Required("period"): vol.In(["day", "month"]), vol.Optional("date"): cv.date})
FACES_SCHEMA = vol.Schema({vol.Required("library"): vol.In(["employee", "customer"])})


def _runtime(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    raise ServiceValidationError("Techmark Analysis is not loaded")


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def generate_report(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass)
        result = await rt.async_generate_report(call.data["period"], call.data.get("date") or dt_util.now().date())
        return result if call.return_response else None

    async def list_faces(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass)
        cfg = rt.processor.cfg
        fdid = next((f for f, role in cfg.roles.items() if role == call.data["library"]), None)
        if fdid is None:
            raise ServiceValidationError("Library not configured")
        try:
            faces = await rt.client.search_faces(fdid)
        except CameraError as err:
            raise HomeAssistantError(f"Camera error: {err}") from err
        return {"library": call.data["library"], "fdid": fdid, "faces": faces}

    # Admin-only: responses contain employee names and attendance (personal data).
    async_register_admin_service(
        hass, DOMAIN, "generate_report", generate_report, schema=REPORT_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    async_register_admin_service(
        hass, DOMAIN, "list_faces", list_faces, schema=FACES_SCHEMA, supports_response=SupportsResponse.ONLY
    )
