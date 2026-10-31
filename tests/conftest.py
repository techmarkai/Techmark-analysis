import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Import the pure core as top-level package `core` without loading the HA package __init__.
sys.path.insert(0, str(ROOT / "custom_components" / "techmark_analysis"))

FIXTURES = ROOT / "tests" / "fixtures"
BOUNDARY = "boundary"
# Synthetic, non-image bytes framed like a JPEG. Never a real photo.
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict:
    return json.loads(fixture_bytes(name))


def part(body: bytes, ctype: str, extra: dict | None = None, length: bool = True) -> bytes:
    hdrs = [f"Content-Type: {ctype}"]
    if length:
        hdrs.append(f"Content-Length: {len(body)}")
    hdrs += [f"{k}: {v}" for k, v in (extra or {}).items()]
    return f"--{BOUNDARY}\r\n".encode() + "\r\n".join(hdrs).encode() + b"\r\n\r\n" + body + b"\r\n"


@pytest.fixture
def stream_bytes() -> bytes:
    heartbeat = (
        b'<?xml version="1.0" encoding="UTF-8"?><EventNotificationAlert version="2.0" '
        b'xmlns="http://www.hikvision.com/ver20/XMLSchema"><dateTime>2026-10-31T21:00:00+03:00</dateTime>'
        b"<eventType>videoloss</eventType><eventState>inactive</eventState></EventNotificationAlert>"
    )
    return (
        part(heartbeat, "application/xml; charset=\"UTF-8\"")
        + part(fixture_bytes("alarm_match_employee.json"), "application/json")
        + part(FAKE_JPEG, "image/jpeg", {"Content-ID": "TESTPID-0001"})
        + part(fixture_bytes("alarm_unmatched.json"), "application/json")
    )
