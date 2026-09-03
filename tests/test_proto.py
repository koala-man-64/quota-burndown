import pytest

from quota_burndown import proto
from quota_burndown.proto import FIXED32, FIXED64, LENGTH_DELIMITED, VARINT
from quota_burndown.proto import encode_field as f
from quota_burndown.proto import encode_message as m


def test_varint_roundtrip_and_truncation():
    for value in (0, 1, 127, 128, 300, 28806, 256000, 2 ** 40):
        encoded = proto.encode_varint(value)
        assert proto.read_varint(encoded, 0) == (value, len(encoded))
    with pytest.raises(proto.WireError):
        proto.read_varint(b"\x80\x80", 0)


def test_decode_all_wire_types():
    buf = m(f(1, VARINT, 5), f(2, FIXED64, 7), f(3, LENGTH_DELIMITED, b"abc"), f(4, FIXED32, 9))
    assert proto.decode(buf) == [(1, 0, 5), (2, 1, 7), (3, 2, b"abc"), (4, 5, 9)]
    with pytest.raises(proto.WireError):
        proto.decode(b"\x0a\x05ab")  # length-delimited field claims 5 bytes, only 2 present
    with pytest.raises(proto.WireError):
        proto.decode(b"\x1b")  # wire type 3 (start group) is not supported


def test_walk_finds_antigravity_shaped_paths_and_strings():
    inner = m(f(1, VARINT, 28806), f(4, VARINT, 256000))
    gen = m(f(10, LENGTH_DELIMITED, inner), f(2, LENGTH_DELIMITED, "gemini-3.8-flash"))
    root = m(
        f(1, LENGTH_DELIMITED, m(f(9, LENGTH_DELIMITED, gen), f(3, LENGTH_DELIMITED, "request_id"))),
        f(5, LENGTH_DELIMITED, "high"),
    )
    walked = proto.walk(root)
    assert walked.first((1, 9, 10, 1)) == 28806
    assert walked.first((1, 9, 10, 4)) == 256000
    assert walked.first((1, 9, 10, 2)) is None
    assert "gemini-3.8-flash" in walked.strings and "request_id" in walked.strings and "high" in walked.strings


def test_walk_does_not_recurse_into_plain_text_or_binary():
    root = m(f(1, LENGTH_DELIMITED, "high"), f(2, LENGTH_DELIMITED, b"\x00\xff\xfe"))
    walked = proto.walk(root)
    assert walked.strings == ["high"]
    assert not any(path[0] == 1 and len(path) > 1 for path in walked.numbers)
    assert not any(path[0] == 2 for path in walked.numbers)


def test_printable_text_rejects_binary_and_blank():
    assert proto.printable_text(b"gemini-3.8-flash-high") == "gemini-3.8-flash-high"
    assert proto.printable_text(b"   ") is None
    assert proto.printable_text(b"\x01\x02") is None
    assert proto.printable_text(b"\xff\xfe") is None
