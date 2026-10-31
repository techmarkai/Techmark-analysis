"""Admin-only services: reports, attendance CSV export, read-only face listing. No enrollment."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.attendance import RANGES
from .isapi import CameraError
from .views import EXPORT_URL

_LOGGER = logging.getLogger(__name__)

REPORT_SCHEMA = vol.Schema({vol.Required("period"): vol.In(["day", "month"]), vol.Optional("date"): cv.date})
FACES_SCHEMA = vol.Schema({vol.Required("library"): vol.In(["employee", "customer"])})
EXPORT_SCHEMA = vol.Schema(
    {
        vol.Required("range"): vol.In(RANGES),
        vol.Optional("start_date"): cv.date,
        vol.Optional("end_date"): cv.date,
    }
)
DOWNLOAD_LINK_TTL = timedelta(minutes=5)


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

    async def export_attendance(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass)
        try:
            result = await rt.async_export_attendance(
                call.data["range"], call.data.get("start_date"), call.data.get("end_date")
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        path = EXPORT_URL.format(filename=result["filename"])
        try:
            # Short-lived link bound to the caller's own login; the view re-checks admin.
            result["download_url"] = async_sign_path(hass, path, DOWNLOAD_LINK_TTL)
        except Exception:  # noqa: BLE001 - no user session (e.g. automation): authenticated path only
            _LOGGER.debug("No session to sign the download link; returning the authenticated path")
            result["download_url"] = path
        return result if call.return_response else None

    async def list_faces(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass)
        fdid = next((f for f, role in rt.processor.cfg.roles.items() if role == call.data["library"]), None)
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
        hass,
        DOMAIN,
        "export_attendance",
        export_attendance,
        schema=EXPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass, DOMAIN, "list_faces", list_faces, schema=FACES_SCHEMA, supports_response=SupportsResponse.ONLY
    )
