"""Async Hikvision ISAPI client. Read-only: it never changes camera configuration."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape as xml_escape

import httpx

from .core.faces import exact_pids, parse_face_search
from .core.stream_parser import boundary_from_content_type
from .core.xmlsafe import safe_fromstring


class CameraError(Exception):
    """Camera answered with an error."""


class CameraUnavailable(CameraError):
    """Camera unreachable or timed out."""


class AuthError(CameraError):
    """Credentials rejected."""


class CameraRejected(CameraError):
    """Camera refused a write (e.g. no usable face). Not retried."""


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
        return _xml(await self._request_raw(method, path, content))

    async def _request_raw(self, method: str, path: str, content: bytes | None = None) -> bytes:
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
        return r.content

    async def device_info(self) -> dict[str, str]:
        r = await self._request("GET", "/System/deviceInfo")
        return {k: _text(r, k) for k in ("serialNumber", "model", "firmwareVersion", "deviceName", "macAddress")}

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
        raw = await self._request_raw("POST", "/Intelligent/FDLib/FDSearch", body)
        try:
            return parse_face_search(raw)
        except (ET.ParseError, ValueError, RecursionError) as err:
            raise CameraError("invalid or unsafe XML from camera") from err

    async def find_face_pids_by_name(self, fdid: str, name: str) -> list[str]:
        """Read-only lookup used before an enrollment retry."""
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><FDSearchDescription version="2.0" '
            'xmlns="http://www.hikvision.com/ver20/XMLSchema"><searchID>TECHMARK2</searchID>'
            "<searchResultPosition>1</searchResultPosition><maxResults>10</maxResults>"
            f"<FDID>{int(fdid)}</FDID><name>{xml_escape(name)}</name></FDSearchDescription>"
        ).encode()
        raw = await self._request_raw("POST", "/Intelligent/FDLib/FDSearch", body)
        try:
            return exact_pids(parse_face_search(raw), name)
        except (ET.ParseError, ValueError, RecursionError) as err:
            raise CameraError("invalid or unsafe XML from camera") from err

    async def add_face(self, fdid: str, name: str, jpeg: bytes) -> str:
        """Add one face picture to a library. Returns the camera PID.

        CAMERA WRITE. Used only by enrollment (disabled by default). Request/response
        format follows Hikvision ISAPI pictureUpload; to be confirmed in test T-B.
        """
        meta = (
            '<?xml version="1.0" encoding="UTF-8"?><PictureUploadData version="2.0" '
            f'xmlns="http://www.hikvision.com/ver20/XMLSchema"><FDID>{int(fdid)}</FDID><FaceAppendData>'
            f"<name>{xml_escape(name)}</name><customHumanID>{xml_escape(name)}</customHumanID>"
            "</FaceAppendData></PictureUploadData>"
        ).encode()
        files = {
            "FaceAppendData": ("FaceAppendData.xml", meta, "application/xml"),
            "importImage": ("face.jpg", jpeg, "image/jpeg"),
        }
        try:
            r = await self._c.post(
                self._base + "/Intelligent/FDLib/pictureUpload", auth=self._auth, files=files, timeout=30
            )
        except httpx.HTTPError:
            raise CameraUnavailable("pictureUpload unreachable") from None
        if r.status_code == 401:
            raise AuthError("authentication failed")
        if r.status_code != 200:
            raise CameraRejected(f"HTTP {r.status_code} on pictureUpload")
        pid = _text(_xml(r.content), ".//PID")
        if not pid.isdigit():
            raise CameraRejected("pictureUpload returned no PID")
        return pid

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
