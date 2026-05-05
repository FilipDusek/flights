"""Labeled schema for the WizJSON response from Google Flights search.

Cross-referenced against the rendered UI across a variety of routes.

The wire format is **positional JSON** — there are no field names, only array
indices. This module gives names + types + provenance to each index.

Tier scales: Google reuses 1/2/3 across metrics but the meaning differs per
metric — see the tier-label dicts below. Always check the latest UI before
trusting these labels; Google can shift indices at any deploy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Literal, Optional

from .wiz import at, at_or, each


# ─────────────────────── enum-like tier helpers ───────────────────────

Tier = Literal[1, 2, 3]
# Each tier-using field has its OWN labeling — Google reuses 1/2/3 but the meaning differs.
EMISSION_TIER_LABELS = {1: "low", 2: "average", 3: "high"}        # 1=green badge, 2=grey, 3=red
CONTRAIL_TIER_LABELS = {1: "Low", 2: "Medium", 3: "High"}         # 1=better, 3=worse
LEGROOM_TIER_LABELS = {1: "Average", 2: "Below average", 3: "Above average"}  # 1=default, 2=tighter
PRICE_LEVEL_LABELS  = {1: "low", 2: "typical", 3: "high"}         # widget badge


# ─────────────────────── per-leg structures ───────────────────────


@dataclass
class FlightLegTime:
    """Combined date + time for a single leg endpoint.

    Wire shape: date is `[Y, M, D]` (leg[20]/[21]), time is `[H]` or `[H, M]`
    (leg[8]/[10]).
    """
    date: date
    time: time

    @classmethod
    def from_wire(cls, date_list: list[int], time_list: list[int]) -> FlightLegTime:
        y, m, d = date_list
        # Google encodes 0 hour and 0 minute as None (e.g. midnight = [None]).
        h = time_list[0] or 0
        mi = (time_list[1] if len(time_list) > 1 else 0) or 0
        return cls(date=date(y, m, d), time=time(h, mi))


@dataclass
class FlightLeg:
    """One leg of an itinerary. Wire path: `payload[2|3][0][N][0][2][LEG]`.

    The leg array is 33 positions long. Below are the positions actually used by
    the rendered UI.
    """
    # Position 2 — operating carrier name string (None = same as marketing carrier).
    # UI: "Operated by SkyWest" / "Plane and crew by Mesa Airlines".
    operated_by: Optional[str]

    # Positions 3, 6 — IATA airport codes for origin/destination of this leg.
    from_code: str
    to_code: str

    # Positions 4, 5 — full airport names ("John F. Kennedy International Airport").
    from_name: str
    to_name: str

    # Positions 8, 10 — local departure / arrival time `[H]` or `[H, M]`.
    # Positions 20, 21 — corresponding dates `[Y, M, D]`.
    departure: FlightLegTime
    arrival: FlightLegTime

    # Position 11 — leg duration in minutes (gate-to-gate, not including layover).
    duration_min: int

    # Position 13 — legroom tier. 1 = average, 2 = below average, 3 = above average.
    # UI: "Below average legroom (29 in)".
    legroom_tier: Optional[Tier]

    # Positions 14 / 30 — short / long form pitch string. Both present.
    seat_pitch_short: Optional[str]  # "29 in" — None for some discount carriers (LEVEL)
    seat_pitch: Optional[str]        # "29 inches"

    # Position 17 — plane type as marketed.
    # UI: "Boeing 737", "Canadair RJ 900", "Boeing 737MAX 8 Passenger".
    plane_type: Optional[str]

    # Position 19 — semantics unknown; always 0 in observed data even for legs
    # before a long layover. Kept for diagnostics. Compute layover from
    # `leg[N].arrival → leg[N+1].departure` instead (see FlightItinerary.layovers_min).
    _wire_field_19: int

    # Position 22 — `[carrier_code, flight_number, ?, carrier_name]`.
    # E.g. `["AA", "100", None, "American Airlines"]`.
    carrier_code: str
    flight_number: str
    carrier_name: str

    # Position 31 — exact per-leg CO2 emissions in grams (more precise than
    # flight-level rounded number).
    leg_emission_g: Optional[int]

    # Position 32 — contrail-warming-potential tier. 1 = Low, 2 = Medium, 3 = High.
    # UI: "Contrail warming potential: Low".
    contrail_tier: Optional[Tier]

    # Position 15 — codeshare partner list (None if not a codeshare). Each entry
    # is `[code, flight_number, ?, carrier_name]` for an additional marketing
    # carrier on the SAME physical flight (e.g. Vueling VY1873 also sold as IB5391).
    codeshares: list[tuple[str, str, str]] = field(default_factory=list)

    @classmethod
    def from_wire(cls, leg: list) -> FlightLeg:
        # WizJSON strips trailing nulls, so legs from low-cost carriers (LEVEL,
        # AirAsia, etc.) can be ~26 fields instead of the full 33. `at` returns
        # None for any out-of-bounds index transparently.
        carrier = at(leg, 22, default=[None, None, None, None]) or [None, None, None, None]
        codeshares = [
            (cs[0], cs[1], cs[3])
            for cs in (at(leg, 15) or [])
            if isinstance(cs, list) and len(cs) >= 4
        ]
        return cls(
            operated_by=at(leg, 2),
            from_code=leg[3],
            from_name=leg[4],
            to_name=leg[5],
            to_code=leg[6],
            departure=FlightLegTime.from_wire(leg[20], leg[8]),
            arrival=FlightLegTime.from_wire(leg[21], leg[10]),
            duration_min=leg[11],
            legroom_tier=at(leg, 13),
            seat_pitch_short=at(leg, 14) or None,  # treat empty "" as None
            seat_pitch=at(leg, 30) or None,
            plane_type=at(leg, 17) or None,
            _wire_field_19=at_or(leg, 19, default=0),
            carrier_code=carrier[0] or "",
            flight_number=carrier[1] or "",
            carrier_name=carrier[3] or "",
            leg_emission_g=at(leg, 31),
            contrail_tier=at(leg, 32),
            codeshares=codeshares,
        )


# ─────────────────────── per-itinerary structures ───────────────────────


@dataclass
class Carbon:
    """Aggregated emissions block for the whole itinerary.

    Wire path: `payload[2|3][0][N][0][22]`. List of 18 positions.
    """
    # Position 2 — emissions tier shown as the badge color in UI:
    # 1 = green (low), 2 = grey ("Avg emissions"), 3 = red (high).
    tier: Tier

    # Position 3 — % delta vs typical for this route. Negative = better.
    # UI: "-9% emissions" / "+18% emissions".
    delta_pct: int

    # Positions 7, 8 — itinerary's own emission and the typical-on-route value
    # (both in grams, rounded to nearest 1000).
    emission_g: int
    typical_g: int

    # Position 10 — lowest emission achievable on this route (eco-best alternative).
    lowest_g: Optional[int]

    @classmethod
    def from_wire(cls, c: list) -> Carbon:
        return cls(
            tier=at(c, 2),
            delta_pct=at(c, 3),
            emission_g=at(c, 7),
            typical_g=at(c, 8),
            lowest_g=at(c, 10),
        )


@dataclass
class CarrierAccessibility:
    """Per-carrier disability/assistance URL.

    Wire path: `payload[2|3][0][N][0][24]` (one entry per distinct marketing
    carrier in the itinerary).
    """
    code: str       # IATA airline code
    name: str       # full marketing name
    info_url: str   # special-needs / assistance URL


@dataclass
class FlightItinerary:
    """One flight itinerary (single set of legs at one price).

    Wire path: `payload[2|3][0][N]`. Outer length 11.
    """
    # Position 0[0] — primary carrier code (matches first leg's carrier).
    primary_carrier_code: str

    # Position 0[1] — list of all marketing carrier names involved.
    # E.g. ["British Airways", "American"] for codeshare combos.
    airlines: list[str]

    # Position 0[2] — legs (list[FlightLeg]).
    legs: list[FlightLeg]

    # Position 1[0][1] — price as integer in the requested currency.
    price: int

    # Position 1[1] — opaque session-bound booking ID. Encodes itinerary +
    # session timestamp + currency — used for booking-page deep linking.
    booking_id: str

    # Position 0[22] — Carbon block.
    carbon: Carbon

    # Position 0[24] — list of per-carrier accessibility info.
    accessibility: list[CarrierAccessibility]

    # Position 0[9] — total trip duration in minutes (gate-to-gate, includes layovers).
    # The single source of truth — sum of legs alone misses layover time.
    total_duration_min: int

    # Position 0[10] — arrival is N calendar days after departure (None = same day,
    # 1 = next day, 2 = two days later — useful for very long itineraries).
    arrival_offset_days: Optional[int]

    @property
    def is_direct(self) -> bool:
        return len(self.legs) == 1

    @property
    def stops(self) -> int:
        return max(0, len(self.legs) - 1)

    @property
    def layovers_min(self) -> list[int]:
        """Compute layover minutes between each leg's arrival and the next leg's
        departure. Returns one value per layover (i.e. len(legs)-1 values).
        Layovers happen at a single airport so naive local-time math is correct.
        """
        from datetime import datetime, timedelta
        out = []
        for i in range(len(self.legs) - 1):
            arr = datetime.combine(self.legs[i].arrival.date, self.legs[i].arrival.time)
            dep = datetime.combine(self.legs[i + 1].departure.date, self.legs[i + 1].departure.time)
            delta = dep - arr
            # Handle midnight rollover: if dep < arr by date alone but the next leg
            # genuinely starts the next day, dates already differ; otherwise add a day.
            if delta < timedelta(0):
                delta += timedelta(days=1)
            out.append(int(delta.total_seconds() // 60))
        return out

    @classmethod
    def from_wire(cls, raw: list) -> FlightItinerary:
        inner = raw[0]
        legs = [FlightLeg.from_wire(l) for l in (at(inner, 2) or [])]
        # Some LCC itineraries ship 24 fields instead of 25, dropping accessibility.
        access = [
            CarrierAccessibility(code=row[0], name=row[1], info_url=row[2])
            for row in (at(inner, 24) or [])
        ]
        return cls(
            primary_carrier_code=at(inner, 0, default=""),
            airlines=at(inner, 1) or [],
            legs=legs,
            price=at(raw, 1, 0, 1),
            booking_id=at(raw, 1, 1),
            carbon=Carbon.from_wire(at(inner, 22) or []),
            accessibility=access,
            total_duration_min=at(inner, 9),
            arrival_offset_days=at(inner, 10),
        )


# ─────────────────────── flight sections ───────────────────────


@dataclass
class FlightSection:
    """One section of search results.

    The page renders TWO sections at once on the "Best" tab:
    `top_flights` (`payload[2]`) and `other_flights` (`payload[3]`).
    """
    itineraries: list[FlightItinerary]

    @classmethod
    def from_wire(cls, section: list) -> FlightSection:
        rows = at(section, 0)
        if not isinstance(rows, list):
            return cls(itineraries=[])
        return cls(itineraries=[FlightItinerary.from_wire(f) for f in rows])


# ─────────────────────── side panels / metadata ───────────────────────


@dataclass
class Airport:
    """A single airport entry from the airport directory.

    Wire path: `payload[17][N]` and `payload[1][0][...][0]`.
    Shape: `[[code, 0], airport_name, [mid, city_name, [...img urls...]], [lat, lon], country_code, ...]`.
    """
    code: str           # IATA, e.g. "JFK"
    name: str           # "John F. Kennedy International Airport"
    city_name: str      # "New York"
    mid: str            # "/m/02_286" (Freebase MID — used in booking URLs)
    lat: float
    lon: float
    country_code: str   # "DK"
    country_name: str   # "Denmark"

    @classmethod
    def from_wire(cls, e: list) -> Optional[Airport]:
        code = at(e, 0, 0)
        if not (isinstance(code, str) and len(code) == 3 and code.isalpha()):
            return None
        mid = at(e, 2, 0)
        if not (isinstance(mid, str) and mid.startswith("/m/")):
            return None
        return cls(
            code=code,
            name=at(e, 1, default=""),
            mid=mid,
            city_name=at(e, 2, 1, default=""),
            lat=at(e, 3, 0, default=0.0),
            lon=at(e, 3, 1, default=0.0),
            country_code=at(e, 4, default=""),
            country_name=at(e, 6, default=""),
        )


@dataclass
class CarrierBaggageInfo:
    """Wire path: `payload[11][N]`. `[code, name, baggage_url]`."""
    code: str
    name: str
    baggage_url: str


@dataclass
class PriceHistoryPoint:
    """Wire path: `payload[5][10][N]`. `[unix_ms, price]`."""
    timestamp_ms: int
    price: int


@dataclass
class PriceTracking:
    """Wire path: `payload[5]`. The "Prices are currently typical" widget data.

    Powers the price graph and "Track prices" toggle.
    """
    # Position 0 — current price level: 1=low, 2=typical, 3=high.
    level: Tier
    # Position 1 — current best price `[null, X]`.
    current_best: int
    # Position 2 — typical-low end of price range.
    typical_low: int
    # Position 3 — % delta vs typical (negative = currently below typical).
    delta_vs_typical_pct: int
    # Position 4 — historical low ever seen.
    historical_low: int
    # Position 5 — historical high.
    historical_high: int
    # Position 10 — daily price history time series.
    price_history: list[PriceHistoryPoint]
    # Position 12 — destination city name (context for the graph).
    destination_city: str

    @classmethod
    def from_wire(cls, p: list) -> PriceTracking:
        # p[10] is wrapped in an outer single-element list: [[[ts, price], ...]]
        raw_history = at(p, 10, 0) or []
        history = [PriceHistoryPoint(ts, pr) for ts, pr in raw_history]
        return cls(
            level=at(p, 0),
            current_best=at(p, 1, 1, default=0),
            typical_low=at(p, 2, 1, default=0),
            delta_vs_typical_pct=at(p, 3, 1, default=0),
            historical_low=at(p, 4, 1, default=0),
            historical_high=at(p, 5, 1, default=0),
            price_history=history,
            destination_city=at(p, 12, default=""),
        )


@dataclass
class Alliance:
    """Wire path: inside `payload[7][1]`. `[code, display_name]`."""
    code: str    # "ONEWORLD" / "SKYTEAM" / "STAR_ALLIANCE"
    name: str    # "Oneworld" / etc


@dataclass
class CarrierRef:
    """Wire path: inside `payload[7][1]`. `[code, name]` — for filter dropdowns."""
    code: str
    name: str


@dataclass
class FilterMeta:
    """Wire path: `payload[7]`. Powers the filter sidebar: airlines, alliances,
    connecting airports, duration & stops sliders."""
    # Position 0 — `[[null, min_price], [null, max_price]]` (sliders).
    price_min: int
    price_max: int
    # Position 1 — alliances + airlines list for the Airlines filter.
    alliances: list[Alliance]
    airlines: list[CarrierRef]
    # Position 2 — list of `[code, city_name]` for the Connecting airports filter.
    connecting_airports: list[tuple[str, str]]
    # Position 3 — `[duration_min, duration_max]` total minutes (Duration slider).
    duration_min: int
    duration_max: int
    # Position 4 — possible stop counts present in result set, e.g. `[[0, 1, 2]]`.
    stop_counts: list[int]

    @classmethod
    def from_wire(cls, p: list) -> FilterMeta:
        # Multi-city + error responses populate only some sub-fields.
        return cls(
            price_min=at(p, 0, 0, 1, default=0),
            price_max=at(p, 0, 1, 1, default=0),
            alliances=[Alliance(code=a[0], name=a[1]) for a in (at(p, 1, 0) or [])],
            airlines=[CarrierRef(code=a[0], name=a[1]) for a in (at(p, 1, 1) or [])],
            connecting_airports=[(c[0], c[1]) for c in (at(p, 2, 0) or [])],
            duration_min=at(p, 3, 0, default=0),
            duration_max=at(p, 3, 1, default=0),
            stop_counts=at(p, 4, 0) or [],
        )


# ─────────────────────── top-level response ───────────────────────


@dataclass
class FlightSearchResponse:
    """Top-level WizJSON response from Google Flights search.

    Outer is a list of ~30 positions, mostly None placeholders. Below are the
    populated ones we've identified.
    """
    # Position 1 — airport directory for the queried pair (+ alternates).
    queried_airports: list[Airport]

    # Position 2 — "Top flights" section (Google's curated top picks; price+convenience).
    top_flights: FlightSection

    # Position 3 — "Other flights" section (everything else returned).
    other_flights: FlightSection

    # Position 5 — "Prices are currently typical" widget + price graph history.
    price_tracking: PriceTracking

    # Position 7 — filter sidebar metadata.
    filter_meta: FilterMeta

    # Position 11 — per-carrier baggage policy URLs.
    baggage_info: list[CarrierBaggageInfo]

    # Position 17 — full directory of every airport mentioned in any result.
    airport_directory: list[Airport]

    # Position 26 — per-carrier accessibility/assistance URLs (dup of itinerary-level).
    accessibility_info: list[CarrierAccessibility]

    @classmethod
    def from_wire(cls, p: list) -> FlightSearchResponse:
        # Queried airports — payload[1][0] is [origins_list, destinations_list].
        queried: list[Airport] = []
        for group in (at(p, 1) or []):
            for sub in (group if isinstance(group, list) else []):
                for entry in (sub if isinstance(sub, list) else []):
                    a = Airport.from_wire(entry)
                    if a:
                        queried.append(a)

        # Airport directory — payload[17] is a flat list of entries.
        directory: list[Airport] = []
        for entry in (at(p, 17) or []):
            a = Airport.from_wire(entry)
            if a:
                directory.append(a)

        sec2, sec3 = at(p, 2), at(p, 3)
        sec5, sec7 = at(p, 5), at(p, 7)

        return cls(
            queried_airports=queried,
            top_flights=FlightSection.from_wire(sec2) if isinstance(sec2, list) else FlightSection([]),
            other_flights=FlightSection.from_wire(sec3) if isinstance(sec3, list) else FlightSection([]),
            price_tracking=PriceTracking.from_wire(sec5) if isinstance(sec5, list) else None,
            filter_meta=FilterMeta.from_wire(sec7) if isinstance(sec7, list) else None,
            baggage_info=[CarrierBaggageInfo(*r) for r in (at(p, 11) or [])],
            airport_directory=directory,
            accessibility_info=[CarrierAccessibility(code=r[0], name=r[1], info_url=r[2]) for r in (at(p, 26) or [])],
        )

    @property
    def all_itineraries(self) -> list[FlightItinerary]:
        """Flatten both sections, sorted by price ascending. Dedup by booking_id."""
        seen, out = set(), []
        for it in self.top_flights.itineraries + self.other_flights.itineraries:
            if it.booking_id and it.booking_id in seen:
                continue
            seen.add(it.booking_id)
            out.append(it)
        return sorted(out, key=lambda x: x.price)


