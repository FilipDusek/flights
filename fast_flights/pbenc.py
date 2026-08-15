"""Minimal protobuf wire-format encoder for building `tfs` URL parameters.

The checked-in flights.proto covers the regular search query, but Explore
URLs need extra fields (typed places, price cap, bags, flags) that aren't
worth a protoc regeneration cycle — the wire format is trivial to emit by
hand and this stays dependency-free.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field_bytes(number: int, payload: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(payload)) + payload


def field_str(number: int, value: str) -> bytes:
    return field_bytes(number, value.encode("utf-8"))


def to_tfs(payload: bytes) -> str:
    """URL-safe base64 without padding, as Google's tfs= expects."""
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")
