"""Fetch matched events' listing floor prices, or optional Explore display quotes, in USD."""

import argparse
import hashlib
import logging
from collections import Counter
from dataclasses import replace
from pathlib import Path

from ..http import ResponseError
from ..runtime import atomic_output, configure_logging, read_csv, save_json, utc_now, write_csv
from .common import event_id
from .config import PriceConfig
from .event_page import BrowserPriceSession, refresh_event_pages
from .explore import locate_sources, refresh_explore
from .listing_api import refresh_listings
from .session import SETTINGS_URL, PriceSession

logger = logging.getLogger(__name__)

PRICE_FIELDS = [
    "Price (USD)",
    "Price Status",
    "SH From Price",
    "SH Price Currency",
    "Price Checked At (UTC)",
    "SH Floor Price",
    "Price Source",
    "SH Listing ID",
    "SH Listing Count",
    "Price Basis",
    "Price Scope",
    "SH Page Min Price",
    "SH Listings Observed",
    "SH Listing Coverage",
    "SH Price Quantity",
    "SH Grid Listing Count",
]


def enriched_rows(rows, checks):
    return [
        {
            **row,
            "Price (USD)": checks[event_id(row)]["usd"],
            "Price Status": checks[event_id(row)]["status"],
            "SH Price Currency": checks[event_id(row)]["currency"],
            "Price Checked At (UTC)": checks[event_id(row)]["checked_at"],
            "SH From Price": (
                checks[event_id(row)]["quote"]
                if checks[event_id(row)].get("source", "explore") == "explore"
                else ""
            ),
            "SH Floor Price": (
                checks[event_id(row)]["quote"]
                if checks[event_id(row)].get("source") in {"listing_grid", "event_page_grid"}
                and checks[event_id(row)]["status"] == "priced"
                else ""
            ),
            "Price Source": checks[event_id(row)].get("source", "explore"),
            "SH Listing ID": checks[event_id(row)].get("listing_id", ""),
            "SH Listing Count": checks[event_id(row)].get("listing_count", ""),
            "Price Basis": checks[event_id(row)].get("basis", "Explore displayed from-price"),
            "Price Scope": checks[event_id(row)].get("scope", ""),
            "SH Page Min Price": (
                checks[event_id(row)]["quote"]
                if checks[event_id(row)].get("source") == "event_page_grid"
                else ""
            ),
            "SH Listings Observed": checks[event_id(row)].get("observed_count", ""),
            "SH Listing Coverage": checks[event_id(row)].get("coverage", ""),
            "SH Price Quantity": checks[event_id(row)].get("quantity", ""),
            "SH Grid Listing Count": checks[event_id(row)].get("grid_count", ""),
        }
        for row in rows
    ]


def markdown_report(rows, checks):
    counts = Counter(check["status"] for check in checks.values())
    lines = [
        "# StubHub price refresh",
        "",
        "Event-page prices request USD directly and use page one only. "
        "A floor requires complete coverage or "
        "agreement with the page's event minimum; page_priced is only the observed page minimum. "
        "Listing POST floors require complete pagination. "
        "Explore quotes are a separate optional source. Prices are per ticket as returned; "
        "fees and checkout totals are not independently verified. Blank amounts remain "
        "unknown; only an explicit empty listing result is marked no_listings.",
        "",
        f"Unique events: {len(checks)}. Statuses: {dict(counts)}.",
        "",
        "| Name | Date | Location | StubHub URL | Ticketmaster URL | Price (USD) | "
        "Price Status | Price Scope |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def escape(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    for row in rows:
        sh = row.get("StubHub URL") or row.get("stubhub_url", "")
        tm = row.get("Ticketmaster URL") or row.get("ticketmaster_url", "")
        location = row.get("Location") or ", ".join(
            filter(
                None,
                [
                    row.get("stubhub_venueName"),
                    row.get("stubhub_city"),
                    row.get("stubhub_state"),
                    row.get("country"),
                ],
            )
        )
        values = [
            row.get("Name") or row.get("stubhub_name", ""),
            row.get("Date") or row.get("event_date", ""),
            location,
            f"[StubHub]({sh})" if sh else "",
            " · ".join(
                f"[{'TicketWeb' if 'ticketweb.' in url else 'Ticketmaster'}]({url})"
                for url in tm.split(" | ")
                if url
            ),
            row["Price (USD)"] or "Unknown",
            row["Price Status"],
            row["Price Scope"],
        ]
        lines.append("| " + " | ".join(map(escape, values)) + " |")
    return "\n".join(lines) + "\n"


def run(config):
    fields, rows = read_csv(config.input_csv)
    ids = {event_id(row) for row in rows}
    sources = locate_sources(ids, config) if ids and config.source == "explore" else {}
    output = Path(config.output_dir)
    # Prevent accidental replacement of a source or stale evidence by a rerun.
    if output.exists() and any(output.iterdir()):
        raise ValueError("Price output directory must be empty; use a fresh run directory")
    output.mkdir(parents=True, exist_ok=True)
    if config.source == "event-page":
        session = BrowserPriceSession(config, output)
    else:
        session = PriceSession(config, output)
    report = {
        "started_at": utc_now(),
        "inputs": {},
        "settings_url": SETTINGS_URL if config.source != "event-page" else None,
        "currency_mode": "direct_usd" if config.source == "event-page" else "fresh_fx",
        "source_lookup": sources,
        "page_radius": config.page_radius,
        "sort_type": config.explore_sort_type,
        "page_size": config.explore_page_size,
        "source": config.source,
        "listing_page_size": config.listing_page_size,
        "listing_max_pages": config.listing_max_pages,
        "browser_wait_seconds": config.browser_wait_seconds,
        "browser_executable": config.browser_executable,
    }
    inputs = [("matches", config.input_csv)]
    if config.source == "explore":
        inputs.extend([("events", config.events_csv), ("cities", config.cities_csv)])
    for label, path in inputs:
        report["inputs"][label] = {
            "path": path,
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        }
    currencies = []
    try:
        if ids and config.source != "event-page":
            try:
                settings = session.fetch(SETTINGS_URL)
                report["settings_checked_at"] = utc_now()
                save_json(output / "location_settings.json", settings)
                if (
                    not isinstance(settings.get("currencies"), list)
                    or not settings["currencies"]
                    or any(not isinstance(item, dict) for item in settings["currencies"])
                ):
                    raise ResponseError("Location settings require a nonempty currencies list")
                currencies = settings["currencies"]
            except ResponseError as exc:
                report["settings_error"] = str(exc)
                logger.warning("USD conversions may be unavailable: %s", exc)
        if config.source == "event-page":
            checks, requests = refresh_event_pages(rows, session, output, currencies)
            report["browser_warmup"] = session.warmup
        elif config.source == "listings":
            checks, requests = refresh_listings(rows, config, session, output, currencies)
        else:
            checks, requests = refresh_explore(ids, sources, config, session, output, currencies)
        result_rows = enriched_rows(rows, checks)
        report.update(
            completed_at=utc_now(),
            checks=checks,
            requests=requests,
            status_counts=dict(Counter(check["status"] for check in checks.values())),
        )
        save_json(output / "checks.json", report)
        write_csv(
            output / "enriched.csv",
            result_rows,
            fields + [f for f in PRICE_FIELDS if f not in fields],
        )
        with atomic_output(output / "report.md") as handle:
            handle.write(markdown_report(result_rows, checks))
        logger.info("Price results: %s; saved to %s", report["status_counts"], output)
        errors = {
            "source_missing",
            "request_error",
            "fx_error",
            "ambiguous_currency",
            "invalid_price",
            "incomplete",
        }
        return int(bool(errors & report["status_counts"].keys()) or "settings_error" in report)
    finally:
        session.close()


def main(argv=None):
    config = PriceConfig()
    parser = argparse.ArgumentParser(prog="cancelled-event prices", description=__doc__)
    for option in (
        "input_csv",
        "events_csv",
        "cities_csv",
        "source_raw_dir",
        "output_dir",
        "log_file",
        "browser_executable",
    ):
        parser.add_argument("--" + option.replace("_", "-"), default=getattr(config, option))
    parser.add_argument("--page-radius", type=int, default=config.page_radius)
    parser.add_argument("--request-interval", type=float, default=config.request_interval)
    parser.add_argument(
        "--source", choices=("event-page", "listings", "explore"), default=config.source
    )
    parser.add_argument("--browser-wait-seconds", type=float, default=config.browser_wait_seconds)
    parser.add_argument("--listing-page-size", type=int, default=config.listing_page_size)
    parser.add_argument("--listing-max-pages", type=int, default=config.listing_max_pages)
    args = parser.parse_args(argv)
    try:
        config = replace(config, **vars(args))
        configure_logging(config.log_file, config.log_level)
        return run(config)
    except (OSError, ValueError, ResponseError) as exc:
        parser.exit(1, f"Price refresh failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
