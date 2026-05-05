"""High-level search API: one call, parsed into a labeled FlightSearchResponse.

Wraps consent bypass, query construction, fetch, JSON extraction, and parsing.

`sort='cheapest'` re-sorts AND mixes in third-party reseller quotes (Kiwi,
Gotogate, etc.), so the same flight often shows a lower price than `'best'`.
"""
from __future__ import annotations

import base64
import json
from typing import Literal, Optional

from primp import Client
from selectolax.lexbor import LexborHTMLParser

from .fetcher import URL, _is_consent_page, _submit_consent
from .parser import _extract_data_array
from .querying import FlightQuery, Passengers, create_query
from .ratelimit import BUCKET_NAME, shared as _shared_limiter
from .schema import FlightSearchResponse


SortMode = Literal["best", "cheapest"]
_SORT_VALUES = {"best": 0, "cheapest": 2}


# ───────────────────── sort tfu encoding ─────────────────────


def _varint(n: int) -> bytes:
    out = b""
    while n >= 0x80:
        out += bytes([(n & 0x7F) | 0x80])
        n >>= 7
    return out + bytes([n])


def _vfield(num: int, val: int) -> bytes:
    return _varint((num << 3) | 0) + _varint(val)


def _msgfield(num: int, payload: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(payload)) + payload


def _build_sort_tfu(sort_mode: int) -> str:
    """Build the `tfu=` URL parameter that selects a sort tab.

    Empirically observed:
        field 4 = 0  → Best (the default; no tfu needed)
        field 4 = 2  → Cheapest (surfaces reseller quotes + budget multi-stops)
    Other field-4 values silently fall back to Best.
    """
    inner = (
        _vfield(1, 0) + _vfield(2, 0) + _vfield(3, 0)
        + _vfield(4, sort_mode)
        + _vfield(5, 1)
    )
    return base64.urlsafe_b64encode(_msgfield(2, inner)).decode("ascii").rstrip("=")


# ───────────────────── client / search ─────────────────────


def make_client(*, proxy: Optional[str] = None) -> Client:
    """Build a primp client with Google's EU consent wall pre-cleared.

    The returned client retains cookies — reuse across many searches in a
    session to amortize the consent-bypass cost.
    """
    client = Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
    )
    warm = create_query(
        flights=[FlightQuery(date="2030-01-01", from_airport="JFK", to_airport="LAX")],
        seat="economy", trip="one-way", passengers=Passengers(adults=1),
        language="en", currency="USD",
    )
    res = client.get(URL, params=warm.params())
    if _is_consent_page(res.text):
        _submit_consent(client, res.text)
    return client


def search(
    *flights: FlightQuery,
    trip: Literal["one-way", "round-trip", "multi-city"] = "one-way",
    seat: Literal["economy", "premium-economy", "business", "first"] = "economy",
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    currency: str = "USD",
    language: str = "en",
    sort: SortMode = "best",
    client: Optional[Client] = None,
    proxy: Optional[str] = None,
    rate_limit: bool = True,
    rate_limiter=None,
) -> FlightSearchResponse:
    """Run one search and return a labeled FlightSearchResponse.

    `sort='cheapest'` re-sorts and includes reseller quotes.

    `rate_limit=True` (default) acquires a slot from a SQLite-backed shared
    token bucket before the request. Set False, or pass `FAST_FLIGHTS_NO_RATE_LIMIT=1`,
    or supply a custom `rate_limiter=` (a pyrate_limiter.Limiter) to override.
    """
    client = client or make_client(proxy=proxy)
    query = create_query(
        flights=list(flights),
        trip=trip,
        seat=seat,
        passengers=Passengers(
            adults=adults, children=children,
            infants_in_seat=infants_in_seat, infants_on_lap=infants_on_lap,
        ),
        language=language,
        currency=currency,
    )

    params = query.params()
    if sort != "best":
        params["tfu"] = _build_sort_tfu(_SORT_VALUES[sort])

    if rate_limit:
        limiter = rate_limiter or _shared_limiter()
        if limiter is not None:
            limiter.try_acquire(BUCKET_NAME)  # blocks until a slot is free

    res = client.get(URL, params=params)
    js_node = LexborHTMLParser(res.text).css_first(r"script.ds\:1")
    if not js_node:
        raise RuntimeError("no ds:1 script in Google response (rate-limited?)")
    payload = json.loads(_extract_data_array(js_node.text()))
    return FlightSearchResponse.from_wire(payload)
