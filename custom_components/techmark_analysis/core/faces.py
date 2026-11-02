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
        chid_el = m.find("customHumanID")
        if chid_el is None:
            chid_el = m.find("ModelingStatus/customHumanID")
        out.append(
            {
                "name": (name_el.text or "").strip() if name_el is not None else "",
                "person_id": pid if _PID.fullmatch(pid) else None,
                "custom_human_id": (chid_el.text or "").strip() if chid_el is not None else "",
            }
        )
    return out


def exact_pids(records: list[dict], name: str) -> list[str]:
    """PIDs of records that are exactly ours. The camera's name search may be fuzzy, so require an
    exact name, and reject any record whose reported customHumanID differs (empty = not reported)."""
    return [
        r["person_id"]
        for r in records
        if r["person_id"] and r["name"] == name and r["custom_human_id"] in ("", name)
    ]
