"""Google Flights Explore: destination inspiration from an origin.

Wraps the same internal RPC the https://www.google.com/travel/explore map
uses (`GetExploreDestinations` on FlightsFrontendService), so it needs no
browser and no API key. One POST returns ~50-90 destinations with the
cheapest found itinerary each (price, carrier, stops, duration, dates).

Wire protocol notes (reverse-engineered 2026-08-15):
  - request body is `f.req=[null, "<pblite JSON>"]`; the pblite mirrors the
    tfs protobuf used by regular flight search, positionally.
  - place specs are `[kg_mid, type]` — type 4 = city, 6 = country/region/
    continent, or `[iata, 3]` for airports; `[]` means "anywhere".
  - the response is a `)]}'`-prefixed stream of `[["wrb.fr", ...]]`
    envelopes: the first carries the geo list, later ones carry prices.
  - freeform place names resolve through the `H028ib` autocomplete RPC
    (batchexecute), whose entity kinds are 1 = airport, 3 = city,
    4+ = country/region/continent.
"""

from __future__ import annotations

import json
import re
from base64 import b64decode
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlencode

from primp import Client

from . import pbenc
from .ratelimit import BUCKET_NAME, shared as _shared_limiter

EXPLORE_RPC_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetExploreDestinations"
)
BATCHEXECUTE_URL = "https://www.google.com/_/FlightsFrontendUi/data/batchexecute"
# Build label observed in the wild; Google accepts stale ones, this is a hint.
_BL = "boq_travel-frontend-flights-ui_20260812.02_p0"

TripLength = Literal["weekend", "week", "two-weeks"]
_TRIP_LENGTH_CODE = {"weekend": 1, "week": 2, "two-weeks": 3}
_SEAT_CODE = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}
# H028ib entity kind -> (leg place type, tfs Airport.type); airports use IATA.
_KIND_TO_LEG_TYPE = {1: 3, 3: 4}  # airport, city; anything >= 4 is a region -> 6
ALLIANCES = ("STAR_ALLIANCE", "ONEWORLD", "SKYTEAM")


class ExploreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExplorePlace:
    name: str  # display name from Google's autocomplete, e.g. "Copenhagen"
    description: str  # e.g. "Capital of Denmark", "Continent"; "" for airports
    kind: str  # "airport" | "city" | "region"
    place_id: str  # KG mid ("/m/01lfy") for city/region, IATA code for airport

    def leg_spec(self) -> list:
        type_code = {"airport": 3, "city": 4, "region": 6}[self.kind]
        return [[[self.place_id, type_code]]]

    def tfs_type(self) -> int:
        # tfs proto type codes are pblite codes minus 2
        return {"airport": 1, "city": 2, "region": 4}[self.kind]


@dataclass(frozen=True)
class ExploreDestination:
    name: str  # city name, e.g. "Paris"
    country: str  # e.g. "France"
    mid: str  # Knowledge Graph id, e.g. "/m/05qtj"
    lat: float
    lng: float
    airport: Optional[str]  # arrival IATA of the cheapest itinerary; None if unpriced
    depart_date: Optional[str]  # "YYYY-MM-DD" of the cheapest found itinerary
    return_date: Optional[str]  # None for one-way searches
    price: Optional[int]  # cheapest round-trip/one-way total; None = no fare found
    currency: str  # display currency of `price` (e.g. "DKK"); "" when unpriced
    airline: Optional[str]  # marketing carrier IATA code; "multi" = mixed carriers
    airline_name: Optional[str]  # e.g. "Ryanair", "SWISS and Austrian"
    stops: Optional[int]  # stops of the cheapest itinerary (0 = nonstop)
    duration_min: Optional[int]  # outbound duration in minutes
    drive_min: Optional[int]  # drive to an alternate arrival airport, if Google warns about one
    image_url: Optional[str]  # destination thumbnail
    flights_url: Optional[str]  # deep link to the regular flight search for these dates


@dataclass(frozen=True)
class ExploreResult:
    destinations: list[ExploreDestination]
    bounds: Optional[list]  # [[ne_lat, ne_lng], [sw_lat, sw_lng]] Google chose for the map
    explore_url: str  # deep link reproducing this explore search in the browser


def make_client(proxy: Optional[str] = None) -> Client:
    return Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
    )


def _rpc_params(hl: str, currency: str) -> dict[str, str]:
    params = {
        "f.sid": "-1234567890123456789",
        "bl": _BL,
        "hl": hl,
        "soc-app": "162",
        "soc-platform": "1",
        "soc-device": "1",
        "_reqid": "111111",
        "rt": "c",
    }
    if currency:
        params["curr"] = currency
    return params


def _parse_wrb_messages(text: str) -> list:
    """Extract every `[["wrb.fr", ...]]` payload from a `)]}'` chunk stream.

    The stream's length prefixes count UTF-16 code units, which makes them
    unreliable after decoding — scanning with raw_decode is robust instead.
    """
    dec = json.JSONDecoder()
    msgs = []
    i = 0
    while True:
        j = text.find('[["wrb.fr"', i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(text[j:])
        except ValueError:
            i = j + 10
            continue
        for env in obj:
            if env and env[0] == "wrb.fr" and isinstance(env[2], str):
                msgs.append(json.loads(env[2]))
        i = j + end
    return msgs


def resolve_place(query: str, *, client: Optional[Client] = None) -> ExplorePlace:
    """Resolve a freeform place name via Google's own autocomplete (H028ib).

    Accepts city names ("Copenhagen"), countries/regions/continents
    ("Thailand", "Southern Europe"), and IATA airport codes ("CPH").
    """
    client = client or make_client()
    inner = json.dumps([query, [1, 2, 3, 4], None, [1, 1, 1], 4], separators=(",", ":"))
    body = "f.req=" + json.dumps(
        [[["H028ib", inner, None, "generic"]]], separators=(",", ":")
    )
    params = {
        "rpcids": "H028ib",
        "source-path": "/travel/explore",
        "hl": "en-US",
        "rt": "c",
        "f.sid": "-1234567890123456789",
        "_reqid": "111111",
        "soc-app": "162",
        "soc-platform": "1",
        "soc-device": "1",
    }
    resp = client.post(
        BATCHEXECUTE_URL + "?" + urlencode(params),
        content=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    msgs = _parse_wrb_messages(resp.text)
    if not msgs or not msgs[0] or not msgs[0][0]:
        raise ExploreError(f"Google's location autocomplete knows no place matching {query!r}")

    def entry_to_place(e: list) -> ExplorePlace:
        kind_code = e[0]
        if kind_code == 1:  # airport: IATA sits at index 5/8
            return ExplorePlace(name=e[1], description=e[3] or "", kind="airport", place_id=e[5])
        kind = "city" if kind_code == 3 else "region"
        return ExplorePlace(name=e[1], description=e[3] or "", kind=kind, place_id=e[4])

    # Groups come back scored; the first entry of the first group is the best
    # match. Airport sub-entries are nested one level deeper.
    for group in msgs[0][0]:
        for e in group:
            if isinstance(e, list) and e and isinstance(e[0], int):
                return entry_to_place(e)
            if isinstance(e, list) and e and isinstance(e[0], list):
                return entry_to_place(e[0])
    raise ExploreError(f"could not interpret autocomplete response for {query!r}")


def _build_inner(
    origin: ExplorePlace,
    destination: Optional[ExplorePlace],
    *,
    one_way: bool,
    depart: Optional[str],
    return_date: Optional[str],
    month: int,
    trip_length: TripLength,
    stops: Optional[int],
    airlines: Optional[list[str]],
    max_price: Optional[int],
    bounds: Optional[list],
    seat: str,
    passengers: list[int],
    carry_on: int,
    flights_only: bool,
) -> list:
    # pblite stops code: 0 = any, 1 = nonstop, 2 = <=1 stop, 3 = <=2 stops
    stops_code = 0 if stops is None else min(stops, 2) + 1

    def leg(o: Optional[ExplorePlace], d: Optional[ExplorePlace], date: Optional[str]) -> list:
        return [
            o.leg_spec() if o else [],
            d.leg_spec() if d else [],
            None,
            stops_code,
            airlines or None,
            None,
            date,
        ]

    legs = [leg(origin, destination, depart)]
    if not one_way:
        legs.append(leg(destination, origin, return_date))

    search: list = [None] * 26
    search[2] = 2 if one_way else 1
    search[4] = [] if (depart or (month == 0 and trip_length == "week")) else [month, _TRIP_LENGTH_CODE[trip_length]]
    search[5] = _SEAT_CODE[seat]
    search[6] = passengers
    search[7] = [None, max_price] if max_price else None
    search[10] = [carry_on, 0] if carry_on else None
    search[13] = legs
    search[17] = 1 if depart else 0
    search[25] = 1 if flights_only else None
    while search and search[-1] is None:
        search.pop()

    return [
        [],
        bounds,
        None,
        search,
        None,
        1,
        None,
        0,
        None,
        0 if one_way else 1,
        [1280, 800],
        4 if one_way else 2,
    ]


def _search_tfs(
    origin: ExplorePlace,
    dest_iata: str,
    depart: str,
    return_date: Optional[str],
    *,
    seat: str,
    passengers: list[int],
    airlines: Optional[list[str]],
) -> str:
    """tfs for a regular flight-search deep link to one found destination."""

    def leg(frm: bytes, to: bytes, date: str) -> bytes:
        payload = pbenc.field_str(2, date)
        for a in airlines or []:
            payload += pbenc.field_str(6, a)
        payload += pbenc.field_bytes(13, frm) + pbenc.field_bytes(14, to)
        return pbenc.field_bytes(3, payload)

    origin_pb = pbenc.field_str(2, origin.place_id)
    if origin.kind != "airport":
        origin_pb = pbenc.field_varint(1, origin.tfs_type()) + origin_pb
    dest_pb = pbenc.field_str(2, dest_iata)

    body = leg(origin_pb, dest_pb, depart)
    if return_date:
        body += leg(dest_pb, origin_pb, return_date)
    for i, count in enumerate(passengers):
        body += pbenc.field_varint(8, i + 1) * count
    body += pbenc.field_varint(9, _SEAT_CODE[seat])
    body += pbenc.field_varint(19, 2 if not return_date else 1)
    return pbenc.to_tfs(body)


def _explore_tfs(
    origin: ExplorePlace,
    destination: Optional[ExplorePlace],
    *,
    one_way: bool,
    depart: Optional[str],
    return_date: Optional[str],
    airlines: Optional[list[str]],
    max_price: Optional[int],
    seat: str,
    passengers: list[int],
    carry_on: int,
    flights_only: bool,
) -> str:
    """tfs reproducing this explore search at google.com/travel/explore."""

    def place_pb(p: Optional[ExplorePlace]) -> Optional[bytes]:
        if p is None:
            return None
        return pbenc.field_varint(1, p.tfs_type()) + pbenc.field_str(2, p.place_id)

    def leg(frm: Optional[bytes], to: Optional[bytes], date: Optional[str]) -> bytes:
        payload = b""
        if date:
            payload += pbenc.field_str(2, date)
        for a in airlines or []:
            payload += pbenc.field_str(6, a)
        if frm:
            payload += pbenc.field_bytes(13, frm)
        if to:
            payload += pbenc.field_bytes(14, to)
        return pbenc.field_bytes(3, payload)

    # Leading constants observed in every explore tfs Google emits.
    body = pbenc.field_varint(1, 28) + pbenc.field_varint(2, 3)
    o_pb, d_pb = place_pb(origin), place_pb(destination)
    body += leg(o_pb, d_pb, depart)
    if not one_way:
        body += leg(d_pb, o_pb, return_date)
    for i, count in enumerate(passengers):
        body += pbenc.field_varint(8, i + 1) * count
    body += pbenc.field_varint(9, _SEAT_CODE[seat])
    if max_price:
        body += pbenc.field_varint(12, max_price)
    if carry_on:
        body += pbenc.field_bytes(13, pbenc.field_varint(2, carry_on) + pbenc.field_varint(3, 0))
    if flights_only:
        body += pbenc.field_varint(14, 1)
    body += pbenc.field_varint(19, 2 if one_way else 1)
    return pbenc.to_tfs(body)


_MONTH_NAMES = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]
    )
}


def parse_month(value: str) -> int:
    """'any'/'' -> 0 (next 6 months), else 1-12 from a number or English name."""
    v = value.strip().lower()
    if v in ("", "any", "next-6-months"):
        return 0
    if v.isdigit() and 1 <= int(v) <= 12:
        return int(v)
    for name, num in _MONTH_NAMES.items():
        if name.startswith(v):
            return num
    raise ValueError(f"month must be 1-12, a month name, or 'any' (got {value!r})")


def explore(
    origin: str | ExplorePlace,
    destination: str | ExplorePlace | None = None,
    *,
    depart: Optional[str] = None,
    return_date: Optional[str] = None,
    month: int = 0,
    trip_length: TripLength = "week",
    one_way: bool = False,
    stops: Optional[int] = None,
    airlines: Optional[list[str]] = None,
    max_price: Optional[int] = None,
    bounds: Optional[list] = None,
    seat: str = "economy",
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    carry_on: int = 0,
    flights_only: bool = False,
    language: str = "en-US",
    currency: str = "",
    proxy: Optional[str] = None,
    rate_limit: bool = True,
) -> ExploreResult:
    """Find cheap destinations from `origin` — Google Flights Explore.

    Args:
        origin: IATA code, city, country or region (freeform; resolved via
            Google's autocomplete), or a pre-resolved ExplorePlace.
        destination: optional region/country/city to constrain results
            ("Europe", "Thailand"); None = anywhere.
        depart/return_date: specific dates "YYYY-MM-DD". When omitted the
            search is flexible (see month/trip_length).
        month: 1-12 to pin a month within the next half year, 0 = any
            (next 6 months). Ignored with specific dates.
        trip_length: "weekend" | "week" | "two-weeks" (flexible mode only).
        one_way: single leg instead of round trip.
        stops: max stops (0 = nonstop, 1, 2); None = any.
        airlines: IATA airline codes or alliance names
            (STAR_ALLIANCE / ONEWORLD / SKYTEAM).
        max_price: price cap in the display currency.
        bounds: [[ne_lat, ne_lng], [sw_lat, sw_lng]] geographic box to
            search within (the "map area" feature).
        carry_on: carry-on bags to include in fares.
        flights_only: exclude drive-there suggestions.
        currency: ISO display currency (e.g. "EUR"); "" = Google decides.
        rate_limit: shares the SQLite token bucket with regular searches.

    Returns an ExploreResult; destinations are sorted cheapest-first with
    unpriced ones (no fare found) last.
    """
    if seat not in _SEAT_CODE:
        raise ValueError(f"seat must be one of {sorted(_SEAT_CODE)}")
    if depart and not one_way and not return_date:
        raise ValueError("round-trip with a specific depart date needs return_date (or pass one_way=True)")

    if rate_limit:
        limiter = _shared_limiter()
        if limiter is not None:
            limiter.try_acquire(BUCKET_NAME)

    client = make_client(proxy)
    origin_p = origin if isinstance(origin, ExplorePlace) else resolve_place(origin, client=client)
    dest_p = (
        destination
        if isinstance(destination, ExplorePlace) or destination is None
        else resolve_place(destination, client=client)
    )

    passengers = [adults, children, infants_in_seat, infants_on_lap]
    inner = _build_inner(
        origin_p,
        dest_p,
        one_way=one_way,
        depart=depart,
        return_date=return_date,
        month=month,
        trip_length=trip_length,
        stops=stops,
        airlines=airlines,
        max_price=max_price,
        bounds=bounds,
        seat=seat,
        passengers=passengers,
        carry_on=carry_on,
        flights_only=flights_only,
    )
    body = "f.req=" + json.dumps(
        [None, json.dumps(inner, separators=(",", ":"))], separators=(",", ":")
    )
    resp = client.post(
        EXPLORE_RPC_URL + "?" + urlencode(_rpc_params(language, currency)),
        content=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    if resp.status_code != 200:
        raise ExploreError(f"GetExploreDestinations returned HTTP {resp.status_code}")
    text = resp.text
    if text.lstrip().startswith("<"):
        raise ExploreError(
            "Google returned HTML instead of data (likely a consent or anti-bot wall); try again or use a proxy"
        )

    msgs = _parse_wrb_messages(text)
    if not msgs:
        raise ExploreError("no data messages in response — Google may have changed the format")

    geo: dict[str, list] = {}
    order: list[str] = []
    prices: dict[str, list] = {}
    result_bounds = None
    for m in msgs:
        # geo message: [meta, ?, [?, bounds], [destinations], ...]
        if len(m) > 3 and isinstance(m[3], list) and m[3] and isinstance(m[3][0], list):
            for d in m[3][0]:
                if isinstance(d, list) and d and isinstance(d[0], str) and d[0] not in geo:
                    geo[d[0]] = d
                    order.append(d[0])
            if len(m) > 2 and isinstance(m[2], list) and len(m[2]) > 1:
                result_bounds = m[2][1]
        # price message: [meta, null*3, [entries], ...]
        if len(m) > 4 and isinstance(m[4], list) and m[4] and isinstance(m[4][0], list):
            for e in m[4][0]:
                if isinstance(e, list) and e and isinstance(e[0], str):
                    prices[e[0]] = e

    currency_re = re.compile(r"\x1a\x03([A-Z]{3})")
    destinations = []
    for mid in order:
        d = geo[mid]
        e = prices.get(mid)
        price = airline = airline_name = None
        n_stops = duration = drive = None
        cur = ""
        dep_date = d[11] if len(d) > 11 else None
        ret_date = d[12] if len(d) > 12 else None
        arrival_iata = d[15] if len(d) > 15 else None
        if e:
            try:
                price = e[1][0][1]
            except (IndexError, TypeError):
                price = None
            # currency is only present inside the base64 token protobuf
            try:
                raw = b64decode(e[1][1] + "==")
                m_cur = currency_re.search(raw.decode("latin-1"))
                if m_cur:
                    cur = m_cur.group(1)
            except Exception:
                cur = ""
            it = e[6] if len(e) > 6 and isinstance(e[6], list) else None
            if it:
                airline = it[0]
                airline_name = it[1]
                n_stops = it[2]
                duration = it[3]
                arrival_iata = it[5] or arrival_iata
                drive = (it[8] or None) if len(it) > 8 else None

        flights_url = None
        if arrival_iata and dep_date:
            tfs = _search_tfs(
                origin_p,
                arrival_iata,
                dep_date,
                ret_date if not one_way else None,
                seat=seat,
                passengers=passengers,
                airlines=airlines,
            )
            q = {"tfs": tfs}
            if language:
                q["hl"] = language
            if currency:
                q["curr"] = currency
            flights_url = "https://www.google.com/travel/flights/search?" + urlencode(q)

        destinations.append(
            ExploreDestination(
                name=d[2],
                country=d[4] if len(d) > 4 else "",
                mid=mid,
                lat=d[1][0],
                lng=d[1][1],
                airport=arrival_iata,
                depart_date=dep_date,
                return_date=None if one_way else ret_date,
                price=price,
                currency=cur,
                airline=airline,
                airline_name=airline_name,
                stops=n_stops,
                duration_min=duration,
                drive_min=drive,
                image_url=d[3] if len(d) > 3 else None,
                flights_url=flights_url,
            )
        )

    destinations.sort(key=lambda x: (x.price is None, x.price or 0))

    explore_q = {
        "tfs": _explore_tfs(
            origin_p,
            dest_p,
            one_way=one_way,
            depart=depart,
            return_date=return_date,
            airlines=airlines,
            max_price=max_price,
            seat=seat,
            passengers=passengers,
            carry_on=carry_on,
            flights_only=flights_only,
        )
    }
    if language:
        explore_q["hl"] = language
    if currency:
        explore_q["curr"] = currency
    explore_url = "https://www.google.com/travel/explore?" + urlencode(explore_q)

    return ExploreResult(destinations=destinations, bounds=result_bounds, explore_url=explore_url)
