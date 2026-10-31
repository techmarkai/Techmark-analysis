"""Async Hikvision ISAPI client. Read-only: it never changes camera configuration."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from .core.stream_parser import boundary_from_content_type
from .core.xmlsafe import safe_fromstring


class CameraError(Exception):
    """Camera answered with an error."""


class CameraUnavailable(CameraError):
    """Camera unreachable or timed out."""


class AuthError(CameraError):
    """Credentials rejected."""


def _xml(body: bytes) -> ET.Element:
    try:
        return safe_fromstring(body)
    except (ET.ParseError, ValueError, RecursionError) as err:
        raise CameraError("invalid or unsafe XML from camera") from err


def _text(el: ET.Element, path: str) -> str:
    found = el.find(path)
    return (found.text or "").strip() if found is not None else ""


class HikClient:
    def __init__(self, client: httpx.AsyncClient, host: str, username: str, password: str) -> None:
        self._c = client
        self._base = f"http://{host}/ISAPI"
        self._auth = httpx.DigestAuth(username, password)

    async def _request(self, method: str, path: str, content: bytes | None = None) -> ET.Element:
        try:
            r = await self._c.request(method, self._base + path, auth=self._auth, content=content, timeout=10)
        except httpx.TimeoutException:
            raise CameraUnavailable(f"timeout on {path}") from None
        except httpx.HTTPError as err:
            raise CameraUnavailable(f"{type(err).__name__} on {path}") from None
        if r.status_code == 401:
            raise AuthError("authentication failed")
        if r.status_code != 200:
            raise CameraError(f"HTTP {r.status_code} on {path}")
        return _xml(r.content)

    async def device_info(self) -> dict[str, str]:
        r = await self._request("GET", "/System/deviceInfo")
        return {k: _text(r, k) for k in ("serialNumber", "model", "firmwareVersion", "deviceName", "macAddress")}

    async def get_time(self) -> dict[str, str]:
        r = await self._request("GET", "/System/time")
        return {k: _text(r, k) for k in ("timeMode", "localTime", "timeZone")}

    async def list_libraries(self) -> list[dict[str, str]]:
        r = await self._request("GET", "/Intelligent/FDLib")
        return [
            {"fdid": _text(e, "FDID"), "name": _text(e, "name"), "threshold": _text(e, "thresholdValue")}
            for e in r.iter("FDLibBaseCfg")
        ]

    async def app_states(self) -> dict[str, bool]:
        r = await self._request("GET", "/Custom/OpenPlatform/App")
        return {_text(a, "packageName"): _text(a, "runStatus") == "true" for a in r.iter("App")}

    async def search_faces(self, fdid: str, limit: int = 100) -> list[dict[str, str]]:
        """Read-only library search (the camera exposes search as POST)."""
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><FDSearchDescription version="2.0" '
            'xmlns="http://www.hikvision.com/ver20/XMLSchema"><searchID>TECHMARK1</searchID>'
            f"<searchResultPosition>1</searchResultPosition><maxResults>{int(limit)}</maxResults>"
            f"<FDID>{int(fdid)}</FDID></FDSearchDescription>"
        ).encode()
        r = await self._request("POST", "/Intelligent/FDLib/FDSearch", body)
        return [{"name": _text(m, "name"), "fpid": _text(m, "FPID")} for m in r.iter("MatchElement")]

    @asynccontextmanager
    async def alert_stream(self, read_timeout: float) -> AsyncIterator[tuple[str, AsyncIterator[bytes]]]:
        """Open the event stream. A read timeout acts as the heartbeat watchdog."""
        timeout = httpx.Timeout(10, read=read_timeout)
        try:
            async with self._c.stream(
                "GET", self._base + "/Event/notification/alertStream", auth=self._auth, timeout=timeout
            ) as r:
                if r.status_code == 401:
                    raise AuthError("authentication failed")
                if r.status_code != 200:
                    raise CameraError(f"HTTP {r.status_code} on alertStream")
                boundary = boundary_from_content_type(r.headers.get("content-type", "")) or "boundary"
                yield boundary, r.aiter_raw()
        except httpx.ReadTimeout:
            raise CameraUnavailable("no data within watchdog period") from None
        except httpx.TimeoutException:
            raise CameraUnavailable("timeout opening alertStream") from None
        except httpx.HTTPError as err:
            raise CameraUnavailable(f"{type(err).__name__} on alertStream") from None
