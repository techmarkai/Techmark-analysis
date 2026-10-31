"""Authenticated, admin-only download of generated attendance CSV files."""

from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView

from .const import REPORT_DIR

EXPORT_URL = "/api/techmark_analysis/export/{filename}"
FILENAME_RE = re.compile(r"^attendance_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.csv$")


class AttendanceExportView(HomeAssistantView):
    url = EXPORT_URL
    name = "api:techmark_analysis:export"
    requires_auth = True  # bearer token or a short-lived signed path; never anonymous

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        if not request["hass_user"].is_admin:
            return web.Response(status=HTTPStatus.FORBIDDEN)
        if not FILENAME_RE.fullmatch(filename):  # also blocks path traversal
            return web.Response(status=HTTPStatus.NOT_FOUND)
        hass = request.app[KEY_HASS]
        path = Path(hass.config.path(REPORT_DIR, filename))
        if not await hass.async_add_executor_job(path.is_file):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
