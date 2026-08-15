"""CLI tests — argument validation and offline behavior only.

These tests don't hit the network; they verify that the CLI surface
(argument validation, help text, exit codes) behaves correctly.
"""
from urllib.parse import parse_qs, urlparse

import typer
from typer.testing import CliRunner

from fast_flights.booking import build_booking_tfs
from fast_flights.cli import _build_search_url, _select_result_url, main
from fast_flights.querying import FlightQuery, Passengers, create_query


# Wrap the single-command entrypoint into a Typer app for testing.
# (typer.run() short-circuits to sys.exit, so we can't use it inside CliRunner.)
app = typer.Typer(add_completion=False)
app.command()(main)
runner = CliRunner()


def test_help_lists_flags():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for flag in ["--seat", "--sort", "--adults", "--currency", "--json", "--return-date"]:
        assert flag in result.output


def test_rejects_invalid_seat():
    result = runner.invoke(app, ["JFK", "LAX", "2026-12-15", "--seat", "luxury"])
    assert result.exit_code == 2
    assert "seat" in result.output.lower()


def test_rejects_invalid_sort():
    result = runner.invoke(app, ["JFK", "LAX", "2026-12-15", "--sort", "fastest"])
    assert result.exit_code == 2
    assert "sort" in result.output.lower()


def test_rejects_missing_arg():
    result = runner.invoke(app, ["JFK"])
    # typer/click exit with 2 for usage errors
    assert result.exit_code == 2


def test_lowercase_iata_codes_accepted():
    """The CLI accepts lowercase IATA codes; they get uppercased internally.
    We verify by triggering a downstream validation error AFTER input parsing."""
    result = runner.invoke(app, ["jfk", "lax", "2026-12-15", "--sort", "garbage"])
    assert result.exit_code == 2
    assert "sort" in result.output.lower()


def test_build_booking_tfs_is_deterministic():
    """The proto encoder must be byte-stable — important because we ship URLs."""
    a = build_booking_tfs(
        date="2026-12-15", from_code="JFK", to_code="LAX",
        airline_code="AA", flight_number="100",
        from_mid="/m/02_286", to_mid="/m/030qb3t",
        seat="economy", adults=1, trip="one-way",
    )
    b = build_booking_tfs(
        date="2026-12-15", from_code="JFK", to_code="LAX",
        airline_code="AA", flight_number="100",
        from_mid="/m/02_286", to_mid="/m/030qb3t",
        seat="economy", adults=1, trip="one-way",
    )
    assert a == b
    # Sanity: it's URL-safe base64, no padding
    assert "=" not in a
    assert "/" not in a
    assert "+" not in a


def test_build_booking_tfs_passenger_count_changes_output():
    """Adults are encoded positionally (repeated field 8); 1pax and 2pax must differ."""
    one = build_booking_tfs(
        date="2026-12-15", from_code="JFK", to_code="LAX",
        airline_code="AA", flight_number="100",
        from_mid="/m/02_286", to_mid="/m/030qb3t",
        adults=1,
    )
    two = build_booking_tfs(
        date="2026-12-15", from_code="JFK", to_code="LAX",
        airline_code="AA", flight_number="100",
        from_mid="/m/02_286", to_mid="/m/030qb3t",
        adults=2,
    )
    assert one != two
    import base64
    one_bytes = base64.urlsafe_b64decode(one + "=" * (-len(one) % 4))
    two_bytes = base64.urlsafe_b64decode(two + "=" * (-len(two) % 4))
    assert len(two_bytes) > len(one_bytes)


def test_roundtrip_search_url_preserves_both_dates():
    url = _build_search_url(
        "CPH", "PRG", "2026-10-24", "2026-10-27",
        seat="economy", adults=1, children=0, currency="DKK", language="en",
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    expected = create_query(
        flights=[
            FlightQuery(date="2026-10-24", from_airport="CPH", to_airport="PRG"),
            FlightQuery(date="2026-10-27", from_airport="PRG", to_airport="CPH"),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
        currency="DKK",
        language="en",
    ).params()

    assert parsed.path == "/travel/flights/search"
    assert params["tfs"] == [expected["tfs"]]
    assert params["curr"] == ["DKK"]
    assert params["hl"] == ["en"]


def test_roundtrip_result_uses_complete_search_url_not_outbound_only_booking_url():
    search_url = "https://www.google.com/travel/flights/search?tfs=complete-roundtrip"
    booking_url = "https://www.google.com/travel/flights/booking?tfs=outbound-only"

    assert _select_result_url("round-trip", booking_url, search_url) == search_url
    assert _select_result_url("one-way", booking_url, search_url) == booking_url
