"""CLI tests — argument validation and offline behavior only.

These tests don't hit the network; they verify that the CLI surface
(argument validation, help text, exit codes) behaves correctly.
"""
import typer
from typer.testing import CliRunner

from fast_flights.booking import build_booking_tfs
from fast_flights.cli import main


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
