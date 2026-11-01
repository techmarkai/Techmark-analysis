"""Live authorization tests against a running Home Assistant.

Skipped unless these environment variables are set (never commit their values):
  TM_HA_URL          e.g. https://ha.example.invalid
  TM_USER_TOKEN      access token of a NON-admin test user
  TM_SIGNED_URL      fresh, unused signed download path from an admin export_attendance call
  TM_EXPIRED_URL     signed download path older than 5 minutes
"""

import json
import os
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get("TM_HA_URL", "").rstrip("/")
USER = os.environ.get("TM_USER_TOKEN", "")
SIGNED = os.environ.get("TM_SIGNED_URL", "")
EXPIRED = os.environ.get("TM_EXPIRED_URL", "")
pytestmark = pytest.mark.skipif(not (BASE and USER and SIGNED and EXPIRED), reason="live HA env not set")


def req(path, token=None, method="GET", body=None):
    r = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    r.add_header("User-Agent", "techmark-live-tests/1.0")  # default urllib UA is blocked by some proxies
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


@pytest.mark.parametrize(
    "service,data",
    [
        ("export_attendance", {"range": "today"}),
        ("generate_report", {"period": "day"}),
        ("list_faces", {"library": "employee"}),
    ],
)
def test_non_admin_cannot_call_services(service, data):
    status, _ = req(f"/api/services/techmark_analysis/{service}?return_response", USER, "POST", data)
    assert status == 401


def test_non_admin_cannot_download():
    status, _ = req(SIGNED.split("?")[0], USER)
    assert status == 403


def test_non_admin_sees_no_employee_details():
    status, body = req("/api/states/sensor.techmark_employees_present_today", USER)
    assert status == 200
    attrs = json.loads(body)["attributes"]
    # Only standard entity metadata; no per-employee names or timestamps.
    assert set(attrs) <= {"friendly_name", "state_class", "last_reset", "unit_of_measurement", "icon"}


def test_anonymous_and_bad_signatures_rejected():
    path, _, query = SIGNED.partition("?")
    assert req(path)[0] == 401
    assert req(path + "?" + query[:-2] + ("AA" if not query.endswith("AA") else "BB"))[0] == 401  # tampered
    assert req(EXPIRED)[0] == 401  # expired
    other = "/api/techmark_analysis/export/attendance_2000-01-01_2000-01-01.csv?" + query
    assert req(other)[0] == 401  # signature bound to another path


def test_signed_link_single_use():
    first, body = req(SIGNED)
    assert first == 200 and body.lstrip(b"\xef\xbb\xbf").startswith(b"Employee Name,Employee ID,Date")
    assert req(SIGNED)[0] == 403  # reused
