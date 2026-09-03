"""Schema-less protobuf wire-format walker.

Antigravity stores each model generation as a protobuf blob and ships no .proto file, so
field names are unknown. Field numbers, however, are stable, and the numeric fields we
care about sit at fixed paths (see usage/antigravity.py). This module decodes the wire
format (varint, 64-bit, length-delimited, 32-bit), recurses into length-delimited values
that parse cleanly as messages, and harvests printable strings along the way.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VARINT, FIXED64, LENGTH_DELIMITED, FIXED32 = 0, 1, 2, 5
MAX_FIELD_NUMBER = (1 << 29) - 1
MAX_DEPTH = 16


class WireError(ValueError):
    """The bytes are not a well-formed protobuf message."""


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise WireError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise WireError("varint too long")


def decode(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """Fields of one message as (field_number, wire_type, value). Consumes every byte or raises."""
    out: list[tuple[int, int, int | bytes]] = []
    pos = 0
    size = len(buf)
    while pos < size:
        tag, pos = read_varint(buf, pos)
        number, wire_type = tag >> 3, tag & 7
        if number < 1 or number > MAX_FIELD_NUMBER:
            raise WireError(f"bad field number {number}")
        if wire_type == VARINT:
            value, pos = read_varint(buf, pos)
        elif wire_type == FIXED64:
            if pos + 8 > size:
                raise WireError("truncated fixed64")
            value = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wire_type == LENGTH_DELIMITED:
            length, pos = read_varint(buf, pos)
            if pos + length > size:
                raise WireError("truncated length-delimited field")
            value = buf[pos:pos + length]
            pos += length
        elif wire_type == FIXED32:
            if pos + 4 > size:
                raise WireError("truncated fixed32")
            value = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        else:
            raise WireError(f"unsupported wire type {wire_type}")
        out.append((number, wire_type, value))
    return out


def looks_like_message(buf: bytes) -> bool:
    if not buf:
        return False
    try:
        decode(buf)
    except WireError:
        return False
    return True


def printable_text(buf: bytes) -> str | None:
    """The bytes as text when they are printable UTF-8 with some content, else None."""
    try:
        text = buf.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.strip():
        return None
    if all(ch.isprintable() or ch in "\n\r\t" for ch in text):
        return text
    return None


@dataclass
class Walked:
    numbers: dict[tuple[int, ...], list[int]] = field(default_factory=dict)
    strings: list[str] = field(default_factory=list)

    def first(self, path: tuple[int, ...]) -> int | None:
        values = self.numbers.get(path)
        return values[0] if values else None


def walk(buf: bytes) -> Walked:
    """Every numeric field keyed by its field-number path from the root, plus every printable
    length-delimited value. A length-delimited value is treated as a nested message when it
    decodes cleanly; it is also kept as a string when it reads as text, so ambiguous bytes
    show up in both views rather than being lost."""
    result = Walked()

    def visit(data: bytes, path: tuple[int, ...], depth: int) -> None:
        for number, wire_type, value in decode(data):
            here = path + (number,)
            if wire_type == LENGTH_DELIMITED:
                assert isinstance(value, bytes)
                text = printable_text(value)
                if text is not None:
                    result.strings.append(text)
                if depth < MAX_DEPTH and looks_like_message(value):
                    visit(value, here, depth + 1)
            else:
                assert isinstance(value, int)
                result.numbers.setdefault(here, []).append(value)

    visit(buf, (), 0)
    return result


# -- encoding helpers (used by tests and fixtures; not needed at runtime) ----------------

def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_field(number: int, wire_type: int, value: int | bytes | str) -> bytes:
    tag = encode_varint((number << 3) | wire_type)
    if wire_type == VARINT:
        return tag + encode_varint(int(value))
    if wire_type == FIXED64:
        return tag + int(value).to_bytes(8, "little")
    if wire_type == FIXED32:
        return tag + int(value).to_bytes(4, "little")
    if wire_type == LENGTH_DELIMITED:
        payload = value if isinstance(value, bytes) else str(value).encode("utf-8")
        return tag + encode_varint(len(payload)) + payload
    raise ValueError(f"unsupported wire type {wire_type}")


def encode_message(*parts: bytes) -> bytes:
    return b"".join(parts)
