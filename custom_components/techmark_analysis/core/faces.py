"""Parse the camera's FDSearch (face library search) response. Read-only data, no images."""

from __future__ import annotations

import re

from .xmlsafe import safe_fromstring

_PID = re.compile(r"^[0-9]{1,20}$")


def parse_face_search(body: bytes) -> list[dict[str, str | None]]:
    """Return [{"name", "person_id"}] per MatchElement.

    The person ID is the direct child <PID> of <MatchElement> (the same value face
    events report as human_id). Missing or malformed IDs become None. Picture URLs,
    certificate numbers and birth dates are deliberately not returned.
    Raises ValueError / xml.etree.ElementTree.ParseError on malformed XML.
    """
    root = safe_fromstring(body)
    out = []
    for m in root.iter("MatchElement"):
        name_el = m.find("name")
        pid_el = m.find("PID")
        pid = (pid_el.text or "").strip() if pid_el is not None else ""
        ms = m.find("ModelingStatus")
        chid_el = m.find("customHumanID")
        if chid_el is None:
            chid_el = m.find("ModelingStatus/customHumanID")
        out.append(
            {
                "name": (name_el.text or "").strip() if name_el is not None else "",
                "person_id": pid if _PID.fullmatch(pid) else None,
                "custom_human_id": (chid_el.text or "").strip() if chid_el is not None else "",
                "modeling_status": (ms.findtext("status") or "").strip() if ms is not None else "",
            }
        )
    return out


def exact_records(records: list[dict], name: str) -> list[dict]:
    """Records that are exactly ours. The camera's name search may be fuzzy, so require an exact
    name, and reject any record whose reported customHumanID differs (empty = not reported)."""
    return [r for r in records if r["person_id"] and r["name"] == name and r["custom_human_id"] in ("", name)]


def exact_pids(records: list[dict], name: str) -> list[str]:
    return [r["person_id"] for r in exact_records(records, name)]


def normalize_modeling(status: str) -> str:
    """Camera ModelingStatus -> success | failed | pending | unknown. Observed: success, failed."""
    s = (status or "").strip().lower()
    if s in ("success", "succ", "succeeded", "modeled"):
        return "success"
    if s in ("failed", "fail", "failure", "error"):
        return "failed"
    if s in ("modeling", "processing", "wait", "waiting", "none", "notmodeled", "unmodeled"):
        return "pending"
    return "unknown"


def our_enrollments(records: list[dict], salt: str) -> list[dict]:
    """Camera records HA enrolled with this database's salt: [{visitor_no, name, pid, status}]."""
    from .models import enrolled_identity  # noqa: PLC0415 - avoid widening module imports

    out = []
    for r in records:
        ident = enrolled_identity(r["name"])
        if ident is None or ident[0] != salt or r["custom_human_id"] not in ("", r["name"]):
            continue
        out.append({"visitor_no": ident[1], "name": r["name"], "pid": r["person_id"],
                    "status": normalize_modeling(r["modeling_status"])})
    return out
