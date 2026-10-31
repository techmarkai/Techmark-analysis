"""Incremental multipart/mixed parser for the Hikvision alertStream."""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_HEADER_BYTES = 16 * 1024


@dataclass(slots=True)
class Part:
    headers: dict[str, str]
    body: bytes = field(repr=False)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


class MultipartParser:
    """Feed raw stream chunks, get complete parts back.

    Uses Content-Length when present (so a boundary string inside a JPEG cannot split
    it), otherwise scans for the next boundary. Any malformed or oversize part is
    dropped and the parser resynchronises on the next boundary.
    """

    def __init__(self, boundary: str, max_part_bytes: int = 8 * 1024 * 1024) -> None:
        self._delim = b"--" + boundary.encode("latin-1")
        self._max = max_part_bytes
        self._buf = bytearray()
        self._skip = 0
        self.parts_ok = 0
        self.parts_dropped = 0

    def feed(self, data: bytes) -> list[Part]:
        if self._skip:
            n = min(self._skip, len(data))
            self._skip -= n
            data = data[n:]
        self._buf += data
        out: list[Part] = []
        while self._next(out):
            pass
        return out

    def _next(self, out: list[Part]) -> bool:
        buf, delim = self._buf, self._delim
        start = buf.find(delim)
        if start < 0:
            keep = len(delim) - 1
            if len(buf) > keep:
                del buf[: len(buf) - keep]
            return False
        if start:
            del buf[:start]
        hdr_start = len(delim)
        hdr_end = buf.find(b"\r\n\r\n", hdr_start)
        if hdr_end < 0:
            if buf[hdr_start : hdr_start + 2] == b"--":  # closing delimiter
                del buf[: hdr_start + 2]
                return True
            if len(buf) - hdr_start > MAX_HEADER_BYTES:
                self.parts_dropped += 1
                del buf[:hdr_start]
                return True
            return False
        headers = _parse_headers(bytes(buf[hdr_start:hdr_end]))
        body_start = hdr_end + 4
        if headers is None:
            self.parts_dropped += 1
            del buf[:body_start]
            return True
        clen = _int(headers.get("content-length"))
        if clen is not None:
            if clen > self._max:
                self.parts_dropped += 1
                avail = len(buf) - body_start
                self._skip = max(0, clen - avail)
                del buf[: body_start + min(clen, avail)]
                return True
            if len(buf) < body_start + clen:
                return False
            body = bytes(buf[body_start : body_start + clen])
            del buf[: body_start + clen]
        else:
            nxt = buf.find(delim, body_start)
            if nxt < 0:
                if len(buf) - body_start > self._max:
                    self.parts_dropped += 1
                    del buf[: len(buf) - (len(delim) - 1)]
                    return True
                return False
            body = bytes(buf[body_start:nxt])
            if body.endswith(b"\r\n"):
                body = body[:-2]
            del buf[:nxt]
        self.parts_ok += 1
        out.append(Part(headers, body))
        return True


def _parse_headers(raw: bytes) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    for line in raw.split(b"\r\n"):
        if not line.strip():
            continue
        if line.startswith(b"--"):  # trailing chars of a closing delimiter
            continue
        name, sep, value = line.partition(b":")
        if not sep or not name.strip():
            return None
        headers[name.strip().decode("latin-1").lower()] = value.strip().decode("latin-1")
    return headers or None


def _int(value: str | None) -> int | None:
    try:
        n = int(value) if value is not None else None
    except ValueError:
        return None
    return n if n is not None and n >= 0 else None


def boundary_from_content_type(content_type: str) -> str | None:
    for item in content_type.split(";"):
        key, _, val = item.strip().partition("=")
        if key.lower() == "boundary" and val:
            return val.strip('"')
    return None
