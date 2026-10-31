import xml.etree.ElementTree as ET

import pytest
from conftest import fixture_bytes
from core.faces import parse_face_search


def test_person_id_valid_missing_malformed():
    got = parse_face_search(fixture_bytes("fdsearch_response.xml"))
    assert got == [
        {"name": "Test Employee A", "person_id": "1"},  # direct <PID>, not ModelingStatus/PID
        {"name": "Test Employee B", "person_id": "27"},
        {"name": "No Pid Person", "person_id": None},
        {"name": "Bad Pid Person", "person_id": None},
    ]


def test_sensitive_fields_not_returned():
    for row in parse_face_search(fixture_bytes("fdsearch_response.xml")):
        assert set(row) == {"name", "person_id"}


def test_empty_and_malformed_xml():
    assert parse_face_search(b"<FDSearchResult><numOfMatches>0</numOfMatches></FDSearchResult>") == []
    with pytest.raises(ET.ParseError):
        parse_face_search(b"<FDSearchResult><MatchList>")
    with pytest.raises(ValueError):
        parse_face_search(b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
