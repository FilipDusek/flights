"""Build Google Flights booking deep-link URLs.

The booking page lives at `https://www.google.com/travel/flights/booking` and
takes two URL parameters: `tfs` (a richer protobuf identifying the specific
flight: airline, flight number, airport MIDs) and `tfu` (a small wrapper —
the constant `EgIIACIA` works as a generic stub).

This module exposes:
    build_booking_tfs(...)  — low-level, takes flat fields
    build_booking_url(itinerary, airports, ...)  — high-level, takes a parsed
                                                    FlightItinerary + airport
                                                    directory from schema.py
"""
from __future__ import annotations

import base64
from typing import Iterable, Literal, Optional

from .schema import Airport as ParsedAirport
from .schema import FlightItinerary

BOOKING_URL = "https://www.google.com/travel/flights/booking"

# A generic tfu that opts out of selecting any specific reseller — Google
# resolves the booking page entirely from the tfs parameter.
STUB_TFU = "EgIIACIA"

SeatStr = Literal["economy", "premium-economy", "business", "first"]
TripStr = Literal["one-way", "round-trip"]

_SEAT_VALUES = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}
_TRIP_VALUES = {"round-trip": 1, "one-way": 2}


# ──────────────────────── protobuf primitives ────────────────────────


def _varint(n: int) -> bytes:
    out = b""
    while n >= 0x80:
        out += bytes([(n & 0x7F) | 0x80])
        n >>= 7
    return out + bytes([n])


def _tag(field_num: int, wire_type: int) -> bytes:
    return _varint((field_num << 3) | wire_type)


def _str_field(num: int, s: str) -> bytes:
    b = s.encode("utf-8")
    return _tag(num, 2) + _varint(len(b)) + b


def _vfield(num: int, val: int) -> bytes:
    return _tag(num, 0) + _varint(val)


def _msgfield(num: int, payload: bytes) -> bytes:
    return _tag(num, 2) + _varint(len(payload)) + payload


# ──────────────────────── public API ────────────────────────


def build_booking_tfs(
    *,
    date: str,
    from_code: str,
    to_code: str,
    airline_code: str,
    flight_number: str,
    from_mid: str,
    to_mid: str,
    seat: SeatStr = "economy",
    adults: int = 1,
    trip: TripStr = "one-way",
) -> str:
    """Build the `tfs` URL parameter for a specific flight's booking page.

    Reverse-engineered against working URLs Google produces from the UI; this
    function reproduces them byte-for-byte.

    Layout (proto field numbers):
      field 1  = 28 (constant — meaning unknown)
      field 2  = trip enum (1=round-trip, 2=one-way)
      field 3  = FlightInfo message:
        field 2  = date (YYYY-MM-DD)
        field 4  = SelectedFlight message:
          field 1 = origin code, field 2 = date, field 3 = destination code,
          field 5 = airline code, field 6 = flight number
        field 13 = origin airport message: field 1 = 2, field 2 = MID
        field 14 = destination airport message: field 1 = 3, field 2 = MID
      field 8  = passenger enum (1=ADULT) — REPEATED, one entry per passenger
      field 9  = seat enum (1=economy ... 4=first)
      field 14 = 1 (constant)
      field 16 = TripInfo message: field 1 = max-uint64 sentinel
      field 19 = trip enum (duplicated; same value as field 2)

    `from_mid` / `to_mid` are Freebase machine IDs (e.g. `/m/02_286` for
    New York). They're discoverable from a parsed search response via
    `FlightSearchResponse.airport_directory`.
    """
    selected = (
        _str_field(1, from_code)
        + _str_field(2, date)
        + _str_field(3, to_code)
        + _str_field(5, airline_code)
        + _str_field(6, flight_number)
    )

    origin = _vfield(1, 2) + _str_field(2, from_mid)
    dest = _vfield(1, 3) + _str_field(2, to_mid)

    flight_info = (
        _str_field(2, date)
        + _msgfield(4, selected)
        + _msgfield(13, origin)
        + _msgfield(14, dest)
    )

    # field 16 carries field 1 = uint64 max as a sentinel
    trip_info = _vfield(1, 0xFFFFFFFFFFFFFFFF)

    # field 8 is REPEATED — one entry per passenger
    passengers = b"".join(_vfield(8, 1) for _ in range(adults))

    trip_int = _TRIP_VALUES[trip]
    full = (
        _vfield(1, 28)
        + _vfield(2, trip_int)
        + _msgfield(3, flight_info)
        + passengers
        + _vfield(9, _SEAT_VALUES[seat])
        + _vfield(14, 1)
        + _msgfield(16, trip_info)
        + _vfield(19, trip_int)
    )

    return base64.urlsafe_b64encode(full).decode("ascii").rstrip("=")


def build_booking_url(
    itinerary: FlightItinerary,
    airports: Iterable[ParsedAirport],
    *,
    seat: SeatStr = "economy",
    adults: int = 1,
    trip: TripStr = "one-way",
    currency: str = "USD",
    language: str = "en",
    leg_index: int = 0,
) -> Optional[str]:
    """Build a booking deep-link URL from a parsed FlightItinerary.

    `airports` is the directory from `FlightSearchResponse.airport_directory`
    (used to resolve airport codes → Freebase MIDs).

    For multi-leg itineraries, `leg_index` selects which leg's airline + flight
    number anchors the booking. The default (first leg) works for most cases;
    Google's booking page resolves the rest of the itinerary from the tfs.

    Returns None if a required field (MID, flight number, etc.) is missing.
    """
    leg = itinerary.legs[leg_index]
    if not (leg.carrier_code and leg.flight_number):
        return None

    mids = {a.code: a.mid for a in airports}
    from_mid = mids.get(leg.from_code)
    to_mid = mids.get(leg.to_code)
    if not (from_mid and to_mid):
        return None

    tfs = build_booking_tfs(
        date=leg.departure.date.isoformat(),
        from_code=leg.from_code,
        to_code=leg.to_code,
        airline_code=leg.carrier_code,
        flight_number=leg.flight_number,
        from_mid=from_mid,
        to_mid=to_mid,
        seat=seat,
        adults=adults,
        trip=trip,
    )
    return f"{BOOKING_URL}?tfs={tfs}&tfu={STUB_TFU}&hl={language}&curr={currency}"
