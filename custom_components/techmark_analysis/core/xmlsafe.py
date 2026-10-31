"""XML parsing that refuses DTDs and entity declarations.

A first pass with pyexpat (the same engine and encoding handling ElementTree uses)
raises on any DOCTYPE or entity declaration; only then is the document built with
ElementTree. Camera documents are small, so the double parse is negligible.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.parsers import expat


class UnsafeXML(ValueError):
    """Document contains a DTD or entity declaration."""


def _refuse(*_args) -> None:
    raise UnsafeXML("DTD/entity declarations are not allowed")


def safe_fromstring(body: bytes) -> ET.Element:
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _refuse
    guard.EntityDeclHandler = _refuse
    guard.UnparsedEntityDeclHandler = _refuse
    guard.ExternalEntityRefHandler = _refuse
    try:
        guard.Parse(body, True)
    except expat.ExpatError as err:
        raise ET.ParseError(str(err)) from None
    root = ET.fromstring(body)
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    return root
