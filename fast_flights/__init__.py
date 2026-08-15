from . import integrations

from .querying import (
    FlightQuery,
    Query,
    Passengers,
    create_query,
    create_query as create_filter,  # alias
)
from .fetcher import get_flights, fetch_flights_html
from .search import search, make_client, SortMode
from .schema import (
    FlightSearchResponse,
    FlightSection,
    FlightItinerary,
    FlightLeg,
    Carbon,
    Airport,
    PriceTracking,
    FilterMeta,
)
from .booking import build_booking_url, build_booking_tfs
from .explore import (
    explore,
    resolve_place,
    ExplorePlace,
    ExploreDestination,
    ExploreResult,
    ExploreError,
)

__all__ = [
    "FlightQuery",
    "Query",
    "Passengers",
    "create_query",
    "create_filter",
    "get_flights",
    "fetch_flights_html",
    "integrations",
    # Labeled-response API
    "search",
    "make_client",
    "SortMode",
    "FlightSearchResponse",
    "FlightSection",
    "FlightItinerary",
    "FlightLeg",
    "Carbon",
    "Airport",
    "PriceTracking",
    "FilterMeta",
    # Booking deep-links
    "build_booking_url",
    "build_booking_tfs",
    # Explore (destination inspiration)
    "explore",
    "resolve_place",
    "ExplorePlace",
    "ExploreDestination",
    "ExploreResult",
    "ExploreError",
]
