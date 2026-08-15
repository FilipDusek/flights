"""Command-line interface for Google Flights Explore (destination inspiration).

Usage:
    flights-explore CPH                             # anywhere, next 6 months, 1 week
    flights-explore Copenhagen Europe --month sep --trip-length weekend
    flights-explore CPH Thailand -d 2026-11-10 -r 2026-11-24 --currency EUR
    flights-explore CPH --max-price 1500 --stops 0 --json
    flights-explore CPH --bounds 70,25,54,4         # Scandinavia-ish box
"""

from __future__ import annotations

import json as _json
import sys
from typing import Optional

try:
    import typer
except ImportError:
    sys.stderr.write(
        "flights-explore CLI requires typer. Install with:\n"
        "  pip install 'fast-flights[cli]'\n"
    )
    raise SystemExit(1)

from .cli import _parse_date
from .explore import ALLIANCES, ExploreError, explore, parse_month


def _fmt_duration(minutes: Optional[int]) -> str:
    if minutes is None:
        return "?"
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}"


def _stops_cell(stops: Optional[int]) -> str:
    if stops is None:
        return "[dim]?[/dim]"
    if stops == 0:
        return "[green]direct[/green]"
    if stops == 1:
        return "[yellow]1-stop[/yellow]"
    return f"[red]{stops}-stop[/red]"


def main(
    origin: str = typer.Argument(..., metavar="ORIGIN",
        help="Origin: IATA code or city name, e.g. CPH or Copenhagen"),
    destination: Optional[str] = typer.Argument(None, metavar="[DESTINATION]",
        help="Optional region/country/city to search within, e.g. Europe, Thailand (default: anywhere)"),
    depart: Optional[str] = typer.Option(None, "--depart", "-d",
        help="Specific departure date YYYY-MM-DD or D.M.YYYY (switches off flexible dates)"),
    return_date: Optional[str] = typer.Option(None, "--return-date", "-r",
        help="Specific return date (required with --depart unless --one-way)"),
    month: str = typer.Option("any", "--month", "-m",
        help="Flexible dates: month name or 1-12 within the next half year; 'any' = next 6 months"),
    trip_length: str = typer.Option("week", "--trip-length", "-l",
        help="Flexible dates: weekend | week | two-weeks"),
    one_way: bool = typer.Option(False, "--one-way", help="One-way instead of round trip"),
    stops: Optional[int] = typer.Option(None, "--stops", min=0, max=2,
        help="Max stops: 0 = nonstop only, 1, 2 (default: any)"),
    airlines: Optional[str] = typer.Option(None, "--airlines",
        help="Comma-list of airline IATA codes or alliances (STAR_ALLIANCE, ONEWORLD, SKYTEAM)"),
    max_price: Optional[int] = typer.Option(None, "--max-price", "-p",
        help="Price cap in the display currency"),
    bounds: Optional[str] = typer.Option(None, "--bounds",
        help="Geographic box 'ne_lat,ne_lng,sw_lat,sw_lng' to search within (overrides DESTINATION region)"),
    seat: str = typer.Option("economy", "--seat", "-s",
        help="economy | premium-economy | business | first"),
    adults: int = typer.Option(1, "--adults", "-a", min=1, max=9),
    children: int = typer.Option(0, "--children", "-c", min=0, max=8),
    carry_on: int = typer.Option(0, "--carry-on", min=0, max=2, help="Carry-on bags to include in fares"),
    flights_only: bool = typer.Option(False, "--flights-only",
        help="Exclude Google's drive-there suggestions"),
    currency: str = typer.Option("", "--currency", help="ISO 4217 display currency (default: Google decides)"),
    lang: str = typer.Option("en-US", "--lang", help="Interface language for names/labels"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=100),
    include_unpriced: bool = typer.Option(False, "--include-unpriced",
        help="Also list destinations Google returned without a fare"),
    json_output: bool = typer.Option(False, "--json", help="Emit results as JSON instead of a table"),
    no_rate_limit: bool = typer.Option(False, "--no-rate-limit",
        help="Disable the SQLite-backed rate limiter"),
) -> None:
    """Explore cheap flight destinations from an origin — the Google Flights
    'Explore' map, as structured data.

    Flexible by default (cheapest week-long round trip in the next 6 months
    per destination). Pin dates with --depart/--return-date, a month with
    --month, or a geographic box with --bounds.
    """
    if seat not in ("economy", "premium-economy", "business", "first"):
        typer.echo("error: --seat must be one of: economy, premium-economy, business, first", err=True)
        raise typer.Exit(2)
    if trip_length not in ("weekend", "week", "two-weeks"):
        typer.echo("error: --trip-length must be weekend, week, or two-weeks", err=True)
        raise typer.Exit(2)

    try:
        month_num = parse_month(month)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)

    try:
        depart_iso = _parse_date(depart).isoformat() if depart else None
        return_iso = _parse_date(return_date).isoformat() if return_date else None
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    if depart_iso and not one_way and not return_iso:
        typer.echo("error: --depart needs --return-date (or --one-way)", err=True)
        raise typer.Exit(2)

    box = None
    if bounds:
        try:
            ne_lat, ne_lng, sw_lat, sw_lng = (float(x) for x in bounds.split(","))
            box = [[ne_lat, ne_lng], [sw_lat, sw_lng]]
        except ValueError:
            typer.echo("error: --bounds must be 'ne_lat,ne_lng,sw_lat,sw_lng'", err=True)
            raise typer.Exit(2)

    airline_list = None
    if airlines:
        airline_list = [a.strip().upper().replace("-", "_") for a in airlines.split(",") if a.strip()]

    try:
        result = explore(
            origin,
            destination,
            depart=depart_iso,
            return_date=return_iso,
            month=month_num,
            trip_length=trip_length,  # type: ignore[arg-type]
            one_way=one_way,
            stops=stops,
            airlines=airline_list,
            max_price=max_price,
            bounds=box,
            seat=seat,
            adults=adults,
            children=children,
            carry_on=carry_on,
            flights_only=flights_only,
            language=lang,
            currency=currency,
            rate_limit=not no_rate_limit,
        )
    except ExploreError as e:
        typer.echo(f"explore failed: {e}", err=True)
        raise typer.Exit(1)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)

    dests = result.destinations
    if not include_unpriced:
        dests = [d for d in dests if d.price is not None]
    dests = dests[:limit]

    if json_output:
        typer.echo(_json.dumps(
            {
                "query": {
                    "origin": origin,
                    "destination": destination,
                    "depart": depart_iso,
                    "return_date": return_iso,
                    "month": month_num or "any",
                    "trip_length": trip_length if not depart_iso else None,
                    "one_way": one_way,
                    "stops": stops,
                    "airlines": airline_list,
                    "max_price": max_price,
                    "bounds": box,
                    "seat": seat,
                    "adults": adults,
                    "children": children,
                    "currency": currency or None,
                    "url": result.explore_url,
                },
                "results": [
                    {
                        "destination": d.name,
                        "country": d.country,
                        "airport": d.airport,
                        "depart": d.depart_date,
                        "return": d.return_date,
                        "price": d.price,
                        "currency": d.currency,
                        "price_label": f"{d.price} {d.currency}".strip() if d.price is not None else "n/a",
                        "airline": d.airline,
                        "airline_name": d.airline_name,
                        "stops": d.stops,
                        "duration_min": d.duration_min,
                        "drive_min": d.drive_min,
                        "lat": d.lat,
                        "lng": d.lng,
                        "share_url": d.flights_url,
                    }
                    for d in dests
                ],
            },
            indent=2, default=str,
        ))
        return

    from rich import box as rich_box
    from rich.console import Console
    from rich.table import Table

    console = Console()
    where = destination or ("map area" if box else "anywhere")
    if depart_iso:
        when = depart_iso + (f" → {return_iso}" if return_iso else " (one-way)")
    else:
        month_label = month if month_num else "next 6 months"
        when = f"{trip_length} trip, {month_label}"
    console.print(f"\n[bold]{origin} → {where}[/bold]  [dim]·[/dim]  {when}\n")

    if not dests:
        console.print("[dim]no destinations returned — try loosening filters[/dim]\n")
        console.print(f"[dim][link={result.explore_url}]Explore ↗[/link][/dim]\n")
        return

    table = Table(box=rich_box.SIMPLE_HEAVY, header_style="bold", padding=(0, 1), expand=False)
    table.add_column("price", justify="right", style="bold green", no_wrap=True)
    table.add_column("destination", overflow="ellipsis")
    table.add_column("dates", no_wrap=True)
    table.add_column("airline")
    table.add_column("stops", justify="right", no_wrap=True)
    table.add_column("duration", no_wrap=True)

    for d in dests:
        price = f"{d.price} {d.currency}".strip() if d.price is not None else "n/a"
        name = f"{d.name} [dim]({d.country}[/dim] [dim]{d.airport or ''})[/dim]"
        if d.flights_url:
            name = f"[link={d.flights_url}]{name}[/link]"
        dates = d.depart_date or "?"
        if d.return_date:
            dates += f" → {d.return_date}"
        airline = d.airline_name or d.airline or "?"
        dur = _fmt_duration(d.duration_min)
        if d.drive_min:
            dur += f" [dim]+{d.drive_min}m drive[/dim]"
        table.add_row(price, name, dates, airline, _stops_cell(d.stops), dur)

    console.print(table)
    console.print(
        f"[dim]· {len(dests)} destination{'s' if len(dests) != 1 else ''} · "
        f"click a destination for flights · --json for machine-readable · "
        f"[link={result.explore_url}]Explore map ↗[/link][/dim]\n"
    )


def _entrypoint() -> None:
    """Console-script entry point."""
    typer.run(main)


if __name__ == "__main__":
    _entrypoint()
