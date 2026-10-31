import random

from conftest import BOUNDARY, FAKE_JPEG, part
from core.stream_parser import MultipartParser, boundary_from_content_type


def parse_all(data: bytes, chunk_sizes) -> list:
    p = MultipartParser(BOUNDARY)
    out, i = [], 0
    sizes = iter(chunk_sizes)
    while i < len(data):
        n = next(sizes)
        out += p.feed(data[i : i + n])
        i += n
    return out, p


def test_chunking_independent(stream_bytes):
    whole, _ = parse_all(stream_bytes, iter(lambda: len(stream_bytes), None))
    ones, _ = parse_all(stream_bytes, iter(lambda: 1, None))
    rnd = random.Random(7)
    randoms, _ = parse_all(stream_bytes, iter(lambda: rnd.randint(1, 900), None))
    assert len(whole) == 4
    for other in (ones, randoms):
        assert [(x.headers, x.body) for x in other] == [(x.headers, x.body) for x in whole]


def test_without_content_length():
    data = part(b'{"eventType":"x"}', "application/json", length=False) + f"--{BOUNDARY}--\r\n".encode()
    parts, p = parse_all(data, iter(lambda: 5, None))
    assert len(parts) == 1 and parts[0].body == b'{"eventType":"x"}'
    assert p.parts_dropped == 0


def test_truncated_final_part_not_emitted():
    data = part(b"abc", "application/json") + part(b"0123456789", "application/json")[:-6]
    parts, _ = parse_all(data, iter(lambda: len(data), None))
    assert [x.body for x in parts] == [b"abc"]


def test_garbage_between_parts_skipped():
    data = b"noise\r\n" + part(b"a", "application/json") + b"\x00garbage\xff" + part(b"b", "application/json")
    parts, _ = parse_all(data, iter(lambda: 3, None))
    assert [x.body for x in parts] == [b"a", b"b"]


def test_malformed_header_dropped_next_parsed():
    bad = f"--{BOUNDARY}\r\nthis is not a header\r\n\r\nxxxx\r\n".encode()
    data = bad + part(b"good", "application/json")
    parts, p = parse_all(data, iter(lambda: len(data), None))
    assert [x.body for x in parts] == [b"good"]
    assert p.parts_dropped == 1


def test_oversize_part_dropped_and_skipped():
    p = MultipartParser(BOUNDARY, max_part_bytes=10)
    big = part(b"X" * 50, "image/jpeg")
    out = p.feed(big[:30]) + p.feed(big[30:]) + p.feed(part(b"ok", "application/json"))
    assert [x.body for x in out] == [b"ok"]
    assert p.parts_dropped == 1


def test_boundary_inside_body_respected_with_length():
    body = FAKE_JPEG[:10] + f"\r\n--{BOUNDARY}\r\n".encode() + FAKE_JPEG[10:]
    data = part(body, "image/jpeg") + part(b"next", "application/json")
    parts, _ = parse_all(data, iter(lambda: 7, None))
    assert [x.body for x in parts] == [body, b"next"]


def test_boundary_from_content_type():
    assert boundary_from_content_type('multipart/mixed; boundary="boundary"') == "boundary"
    assert boundary_from_content_type("multipart/mixed") is None
