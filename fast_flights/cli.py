"""Command-line interface for fast_flights.

Usage:
    flights JFK LAX 2026-12-15
    flights JFK LAX 15.12.2026 --return-date 22.12.2026 --adults 2
    flights NRT SIN 2026-07-15 --sort cheapest --limit 5
    flights SFO LHR 2026-09-10 --seat business --json
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import re
import sys
from typing import Optional
from urllib.parse import urlencode

try:
    import typer
except ImportError:
    sys.stderr.write(
        "flights CLI requires typer. Install with:\n"
        "  pip install 'fast-flights[cli]'\n"
    )
    raise SystemExit(1)

from .booking import build_booking_url
from .querying import FlightQuery, Passengers, create_query
from .schema import FlightItinerary
from .search import search as do_search
from .types import SeatType


# Date input parsing — accept both ISO and CZ formats. Hand-rolled to avoid
# dateparser/dateutil's cold-start cost and ISO-vs-DMY ambiguity.
def _parse_date(s: str) -> _dt.date:
    """Accepts YYYY-MM-DD (ISO) or D[D].M[M].YYYY (CZ). Returns a date."""
    s = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _dt.date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        return _dt.date(y, mo, d)
    raise ValueError(f"date must be YYYY-MM-DD or D.M.YYYY (got {s!r})")


# ──────────────────── helpers ────────────────────


def _fmt_duration(minutes: Optional[int]) -> str:
    if minutes is None:
        return "?"
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}"


def _render(it: FlightItinerary, currency: str, booking_url: Optional[str]) -> dict:
    """Flatten an itinerary into the common dict shape used by the table /
    JSON output."""
    legs = [
        {
            "name": l.carrier_code + l.flight_number,
            "number": l.flight_number,
            "carrier": l.carrier_code,
            "from": l.from_code,
            "to": l.to_code,
            "dep_time": l.departure.time.strftime("%H:%M"),
            "arr_time": l.arrival.time.strftime("%H:%M"),
            "dep_date": str(l.departure.date),
            "arr_date": str(l.arrival.date),
            "duration_min": l.duration_min,
            "plane": l.plane_type,
            "operated_by": l.operated_by,
        }
        for l in it.legs
    ]
    first, last = it.legs[0], it.legs[-1]
    departure = f"{first.departure.date} {first.departure.time.strftime('%H:%M')}"
    arrival = f"{last.arrival.date} {last.arrival.time.strftime('%H:%M')}"
    duration = _fmt_duration(it.total_duration_min)
    price_label = f"{it.price} {currency}" if it.price else "n/a"
    return {
        "from": first.from_code,
        "to": last.to_code,
        "departure": departure,
        "arrival": arrival,
        "duration": duration,
        "transfers": it.stops,
        "price": price_label,
        "share_url": booking_url,
        "legs": legs,
        # Google-Flights-specific extras (kept for power-users):
        "airlines": it.airlines,
        "currency": currency,
        "arrival_offset_days": it.arrival_offset_days,
        "carbon_g": it.carbon.emission_g,
        "carbon_delta_pct": it.carbon.delta_pct,
        "total_duration_min": it.total_duration_min,
    }


def _stops_cell(stops: int) -> str:
    if stops == 0:
        return "[green]direct[/green]"
    if stops == 1:
        return "[yellow]1-stop[/yellow]"
    return f"[red]{stops}-stop[/red]"


def _print_table(
    items: list[dict],
    *,
    route_label: str,
    date_label: str,
    pax_label: str,
    sort: str,
    search_url: str,
) -> None:
    """Pretty table with clickable booking + Search links via OSC 8 hyperlinks.

    Rich emits OSC 8 escapes that modern terminals (iTerm2, recent
    Terminal.app, Alacritty, Kitty, WezTerm, VS Code, GNOME Terminal) render
    as clickable links. When stdout isn't a TTY, Rich falls back to plain
    text automatically.
    """
    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console()

    sort_note = "[dim](best)[/dim]" if sort == "best" else "[cyan](cheapest — incl. resellers)[/cyan]"
    console.print(
        f"\n[bold]{route_label}[/bold]  [dim]·[/dim]  {date_label}  "
        f"[dim]·[/dim]  {pax_label}  [dim]·[/dim]  {sort_note}\n"
    )

    if not items:
        console.print("[dim]no flights returned[/dim]\n")
        console.print(f"[dim][link={search_url}]Search ↗[/link][/dim]\n")
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", padding=(0, 1), expand=False)
    table.add_column("price", justify="right", style="bold green", no_wrap=True)
    table.add_column("dep", no_wrap=True)
    table.add_column("arr", no_wrap=True)
    table.add_column("duration", no_wrap=True)
    table.add_column("transfers", justify="right", no_wrap=True)
    table.add_column("legs", overflow="ellipsis")

    for it in items:
        legs = it["legs"]
        # Just the time portion of "YYYY-MM-DD HH:MM"
        dep_time = it["departure"].split(" ", 1)[-1] if it["departure"] else ""
        arr_time = it["arrival"].split(" ", 1)[-1] if it["arrival"] else ""
        if it.get("arrival_offset_days"):
            arr_time += f" [yellow]+{it['arrival_offset_days']}d[/yellow]"

        leg_names = " → ".join(l["name"] for l in legs)
        url = it.get("share_url")
        if url:
            leg_names = f"[link={url}]{leg_names}[/link]"

        # Append "op. XX" under the airline name when codeshare's operating
        # carrier differs from the marketing one.
        op_notes = sorted({l["operated_by"] for l in legs if l.get("operated_by")})
        if op_notes:
            leg_names += f"\n[dim]op. {', '.join(op_notes)}[/dim]"

        table.add_row(
            it["price"],
            dep_time,
            arr_time,
            it["duration"],
            _stops_cell(it["transfers"]),
            leg_names,
        )

    console.print(table)
    console.print(
        f"[dim]· {len(items)} result{'s' if len(items) != 1 else ''} · "
        f"click legs for booking · --json for machine-readable · "
        f"[link={search_url}]Search ↗[/link][/dim]\n"
    )


def _build_search_url(
    from_airport: str,
    to_airport: str,
    date_iso: str,
    return_date_iso: Optional[str],
    *,
    seat: SeatType = "economy",
    adults: int = 1,
    children: int = 0,
    currency: str = "USD",
    language: str = "en",
) -> str:
    """Canonical Google Flights URL preserving the complete CLI query.

    Natural-language ``q=`` URLs do not reliably retain return dates.  Reuse
    the same protobuf query sent to Google so route, dates, cabin and passenger
    counts survive when the link is opened.
    """
    flights = [
        FlightQuery(
            date=date_iso,
            from_airport=from_airport.upper(),
            to_airport=to_airport.upper(),
        )
    ]
    trip = "one-way"
    if return_date_iso:
        flights.append(
            FlightQuery(
                date=return_date_iso,
                from_airport=to_airport.upper(),
                to_airport=from_airport.upper(),
            )
        )
        trip = "round-trip"
    params = create_query(
        flights=flights,
        trip=trip,
        seat=seat,
        passengers=Passengers(adults=adults, children=children),
        language=language,
        currency=currency,
    ).params()
    return "https://www.google.com/travel/flights/search?" + urlencode(params)


def _select_result_url(trip: str, booking_url: Optional[str], search_url: str) -> str:
    """Use a complete search link when a booking link lacks the return leg."""
    if trip == "round-trip":
        return search_url
    return booking_url or search_url


# ──────────────────── command ────────────────────


def main(
    from_airport: str = typer.Argument(..., metavar="FROM", help="3-letter origin IATA code, e.g. JFK"),
    to_airport: str = typer.Argument(..., metavar="TO", help="3-letter destination IATA code, e.g. LAX"),
    date: Optional[str] = typer.Argument(None, metavar="[DATE]",
        help="Departure date YYYY-MM-DD or D.M.YYYY (defaults to today)"),
    return_date: Optional[str] = typer.Option(
        None, "--return-date", "-r",
        help="Return date for round-trip in YYYY-MM-DD or D.M.YYYY",
    ),
    seat: str = typer.Option(
        "economy", "--seat", "-s",
        help="economy | premium-economy | business | first",
    ),
    adults: int = typer.Option(1, "--adults", "-a", min=1, max=9),
    children: int = typer.Option(0, "--children", "-c", min=0, max=8),
    currency: str = typer.Option("USD", "--currency", help="ISO 4217 code"),
    sort: str = typer.Option(
        "best", "--sort",
        help="best (default; airline-direct fares) | cheapest (includes reseller quotes — Kiwi, Gotogate, etc.)",
    ),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=50),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON instead of a table"),
    no_rate_limit: bool = typer.Option(
        False, "--no-rate-limit",
        help="Disable the SQLite-backed rate limiter",
    ),
) -> None:
    """Search Google Flights and print results.

    Each result includes a booking deep-link URL — no extra request, the
    airport directory comes back in the same response.
    """
    if seat not in ("economy", "premium-economy", "business", "first"):
        typer.echo("error: --seat must be one of: economy, premium-economy, business, first", err=True)
        raise typer.Exit(2)
    if sort not in ("best", "cheapest"):
        typer.echo("error: --sort must be 'best' or 'cheapest'", err=True)
        raise typer.Exit(2)

    try:
        date_iso = (_parse_date(date) if date else _dt.date.today()).isoformat()
        return_date_iso = _parse_date(return_date).isoformat() if return_date else None
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)

    queries = [FlightQuery(date=date_iso, from_airport=from_airport.upper(), to_airport=to_airport.upper())]
    trip = "one-way"
    if return_date_iso:
        queries.append(FlightQuery(date=return_date_iso, from_airport=to_airport.upper(), to_airport=from_airport.upper()))
        trip = "round-trip"

    search_url = _build_search_url(
        from_airport, to_airport, date_iso, return_date_iso,
        seat=seat, adults=adults, children=children, currency=currency,
    )

    try:
        resp = do_search(
            *queries,
            trip=trip, seat=seat,
            adults=adults, children=children,
            currency=currency, sort=sort,
            rate_limit=not no_rate_limit,
        )
    except Exception as e:
        typer.echo(f"search failed: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(1)

    itineraries = sorted(
        resp.top_flights.itineraries + resp.other_flights.itineraries,
        key=lambda x: x.price or 1_000_000,
    )[:limit]

    rendered = []
    for it in itineraries:
        url = build_booking_url(
            it, resp.airport_directory,
            seat=seat, adults=adults,
            trip=trip if trip == "round-trip" else "one-way",
            currency=currency,
        )
        rendered.append(_render(it, currency, _select_result_url(trip, url, search_url)))

    if json_output:
        typer.echo(_json.dumps(
            {
                "query": {
                    "from": from_airport.upper(),
                    "to": to_airport.upper(),
                    "date": date_iso,
                    "return_date": return_date_iso,
                    "seat": seat,
                    "adults": adults,
                    "children": children,
                    "currency": currency,
                    "sort": sort,
                    "url": search_url,
                },
                "results": rendered,
            },
            indent=2, default=str,
        ))
    else:
        route_label = f"{from_airport.upper()} → {to_airport.upper()}"
        if return_date_iso:
            route_label += f" → {from_airport.upper()}"
            date_label = f"{date_iso} → {return_date_iso}"
        else:
            date_label = date_iso
        pax_parts = [f"{adults} adult{'s' if adults != 1 else ''}"]
        if children:
            pax_parts.append(f"{children} child{'ren' if children != 1 else ''}")
        pax_parts.append(seat)
        pax_label = ", ".join(pax_parts)

        _print_table(
            rendered,
            route_label=route_label,
            date_label=date_label,
            pax_label=pax_label,
            sort=sort,
            search_url=search_url,
        )


def _entrypoint() -> None:
    """Console-script entry point."""
    typer.run(main)


if __name__ == "__main__":
    _entrypoint()
