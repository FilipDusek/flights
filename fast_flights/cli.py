"""Command-line interface for fast_flights.

Usage:
    flights JFK LAX 2026-12-15
    flights JFK LAX 15.12.2026 --return-date 22.12.2026 --adults 2
    flights NRT SIN 2026-07-15 --sort cheapest --limit 5
    flights SFO LHR 2026-09-10 --seat business --json

Date input: YYYY-MM-DD (ISO) or D.M.YYYY (CZ); aligned across the
companion idos and cd-trains CLIs.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import re
import sys
from typing import Optional

try:
    import typer
except ImportError:
    sys.stderr.write(
        "flights CLI requires typer. Install with:\n"
        "  pip install 'fast-flights[cli]'\n"
    )
    raise SystemExit(1)

from .booking import build_booking_url
from .querying import FlightQuery
from .schema import FlightItinerary
from .search import search as do_search


# Canonical date input parsing, mirrored verbatim across idos-py, cd-trains,
# and fast-flights so the user can pass the same strings to all three CLIs.
# We deliberately don't pull in dateparser/dateutil — they're either slow
# (dateparser cold start ~150ms) or get ISO/DMY swapped.
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
    """Flatten an itinerary into a dict suitable for table or JSON output."""
    legs = [
        {
            "carrier": l.carrier_code + l.flight_number,
            "from": l.from_code,
            "to": l.to_code,
            "departure": f"{l.departure.date} {l.departure.time.strftime('%H:%M')}",
            "arrival": f"{l.arrival.date} {l.arrival.time.strftime('%H:%M')}",
            "duration_min": l.duration_min,
            "plane": l.plane_type,
            "operated_by": l.operated_by,
        }
        for l in it.legs
    ]
    return {
        "price": it.price,
        "currency": currency,
        "airlines": it.airlines,
        "stops": it.stops,
        "total_duration_min": it.total_duration_min,
        "arrival_offset_days": it.arrival_offset_days,
        "carbon_g": it.carbon.emission_g,
        "carbon_delta_pct": it.carbon.delta_pct,
        "legs": legs,
        "booking_url": booking_url,
    }


def _stops_cell(stops: int) -> str:
    if stops == 0:
        return "[green]direct[/green]"
    if stops == 1:
        return "[yellow]1-stop[/yellow]"
    return f"[red]{stops}-stop[/red]"


def _print_table(
    items: list[dict],
    currency: str,
    *,
    route_label: str,
    date_label: str,
    pax_label: str,
    sort: str,
) -> None:
    """Pretty table with clickable booking links via OSC 8 hyperlinks.

    Rich emits OSC 8 escapes that modern terminals (iTerm2, recent
    Terminal.app, Alacritty, Kitty, WezTerm, VS Code, GNOME Terminal) render
    as clickable links. When stdout isn't a TTY, Rich falls back to plain
    text automatically.
    """
    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console()

    # Header line
    sort_note = "[dim](best)[/dim]" if sort == "best" else "[cyan](cheapest — incl. resellers)[/cyan]"
    console.print(
        f"\n[bold]{route_label}[/bold]  [dim]·[/dim]  {date_label}  "
        f"[dim]·[/dim]  {pax_label}  [dim]·[/dim]  {sort_note}\n"
    )

    if not items:
        console.print("[dim]no flights returned[/dim]\n")
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", padding=(0, 1), expand=False)
    table.add_column("price", justify="right", style="bold green", no_wrap=True)
    table.add_column("type", no_wrap=True)
    table.add_column("duration", justify="right", no_wrap=True)
    table.add_column("route", no_wrap=True)
    table.add_column("times", style="dim", no_wrap=True)
    table.add_column("airlines", overflow="ellipsis")

    for it in items:
        legs = it["legs"]
        route = " → ".join([legs[0]["from"]] + [l["to"] for l in legs])
        airlines = ", ".join(it["airlines"])
        dep_arr = f"{legs[0]['departure'][11:]}–{legs[-1]['arrival'][11:]}"
        if it["arrival_offset_days"]:
            dep_arr += f" [yellow]+{it['arrival_offset_days']}d[/yellow]"

        price_label = f"{it['price'] or '?'} {currency}"
        url = it.get("booking_url")
        price_cell = f"[link={url}]{price_label}[/link]" if url else price_label

        # Append a dim "operated by" line under the airline names when codeshares
        # have a different operating carrier than the marketing one.
        op_notes = sorted({l["operated_by"] for l in legs if l.get("operated_by")})
        if op_notes:
            airlines += f"\n[dim]op. {', '.join(op_notes)}[/dim]"

        table.add_row(
            price_cell,
            _stops_cell(it["stops"]),
            _fmt_duration(it["total_duration_min"]),
            route,
            dep_arr,
            airlines,
        )

    console.print(table)
    if any(it.get("booking_url") for it in items):
        console.print(
            f"[dim]· {len(items)} result{'s' if len(items) != 1 else ''}. "
            f"Click any price to open the booking page. "
            f"For terminals without hyperlink support, use --json.[/dim]\n"
        )


# ──────────────────── command ────────────────────


def main(
    from_airport: str = typer.Argument(..., metavar="FROM", help="3-letter origin IATA code, e.g. JFK"),
    to_airport: str = typer.Argument(..., metavar="TO", help="3-letter destination IATA code, e.g. LAX"),
    date: str = typer.Argument(..., metavar="DATE",
        help="Departure date in YYYY-MM-DD or D.M.YYYY"),
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
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
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
        date_iso = _parse_date(date).isoformat()
        return_date_iso = _parse_date(return_date).isoformat() if return_date else None
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)

    queries = [FlightQuery(date=date_iso, from_airport=from_airport.upper(), to_airport=to_airport.upper())]
    trip = "one-way"
    if return_date_iso:
        queries.append(FlightQuery(date=return_date_iso, from_airport=to_airport.upper(), to_airport=from_airport.upper()))
        trip = "round-trip"

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
        rendered.append(_render(it, currency, url))

    if json_output:
        typer.echo(_json.dumps(rendered, indent=2, default=str))
    else:
        # Build the labels for the pretty header
        route_label = f"{from_airport.upper()} → {to_airport.upper()}"
        if return_date:
            route_label += f" → {from_airport.upper()}"
            date_label = f"{date} → {return_date}"
        else:
            date_label = date
        pax_parts = [f"{adults} adult{'s' if adults != 1 else ''}"]
        if children:
            pax_parts.append(f"{children} child{'ren' if children != 1 else ''}")
        pax_parts.append(seat)
        pax_label = ", ".join(pax_parts)

        _print_table(
            rendered, currency,
            route_label=route_label,
            date_label=date_label,
            pax_label=pax_label,
            sort=sort,
        )


def _entrypoint() -> None:
    """Console-script entry point."""
    typer.run(main)


if __name__ == "__main__":
    _entrypoint()
