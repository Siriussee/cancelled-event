"""Fetch matched events' listing floor prices, or optional Explore display quotes, in USD."""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from .config import RequestConfig, env_field, validate_number
from .event_scraper import build_explore_url, city_output_stem, load_cities
from .http import ResponseError, get_json
from .runtime import atomic_output, configure_logging, write_csv

logger = logging.getLogger(__name__)
SETTINGS_URL = "https://www.stubhub.com/secure/Browse/DefaultMaster/GetLocationSettings"
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
DEFAULT_SYMBOLS = {
    "USD": {"US$", "USD"},
    "CAD": {"C$", "CA$", "CAD"},
    "EUR": {"€", "EUR"},
    "GBP": {"£", "GBP"},
}


@dataclass
class PriceConfig(RequestConfig):
    """Serial, bounded price refresh; inputs remain untouched."""

    input_csv: str = env_field("PRICE_INPUT_CSV", "")
    events_csv: str = env_field("STUBHUB_EVENTS_CSV", "")
    cities_csv: str = env_field("INPUT_CSV", "data/worldcities.csv")
    source_raw_dir: str = env_field("PRICE_SOURCE_RAW_DIR", "")
    output_dir: str = env_field("PRICE_OUTPUT_DIR", "output/prices")
    request_interval: float = env_field("PRICE_REQUEST_INTERVAL", 2.0, float)
    page_radius: int = env_field("PRICE_PAGE_RADIUS", 4, int)
    log_file: str = env_field("PRICE_LOG_FILE", "log/prices.log")
    explore_sort_type: str = env_field("EXPLORE_SORT_TYPE", "Distance")
    explore_page_size: int = env_field("EXPLORE_PAGE_SIZE", 100, int)
    source: str = env_field("PRICE_SOURCE", "event-page")
    listing_page_size: int = env_field("PRICE_LISTING_PAGE_SIZE", 50, int)
    listing_max_pages: int = env_field("PRICE_LISTING_MAX_PAGES", 100, int)
    browser_executable: str = env_field("PRICE_BROWSER_EXECUTABLE", "")
    browser_wait_seconds: float = env_field("PRICE_BROWSER_WAIT_SECONDS", 8.0, float)

    def __post_init__(self):
        super().__post_init__()
        if not self.input_csv:
            self.input_csv = os.getenv(
                "CANCELLED_EVENT_INTERSECTION_CSV",
                "output/stubhub_ticketmaster_cancelled_intersection.csv",
            )
        if not self.events_csv:
            self.events_csv = os.getenv("EVENTS_CSV", "output/events.csv")
        validate_number("request_interval", self.request_interval)
        validate_number("page_radius", self.page_radius)
        validate_number("explore_page_size", self.explore_page_size, 1)
        validate_number("listing_page_size", self.listing_page_size, 1)
        validate_number("listing_max_pages", self.listing_max_pages, 1)
        validate_number("browser_wait_seconds", self.browser_wait_seconds)
        if self.source not in {"listings", "explore", "event-page"}:
            raise ValueError("Price source must be event-page, listings or explore")


def utc_now():
    return datetime.now(UTC).isoformat()


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = list(reader)
    if not fields or len(fields) != len(set(fields)):
        raise ValueError(f"Missing or duplicate CSV headers: {path}")
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"Incomplete CSV row: {path}")
    return fields, rows


def event_id(row):
    identity = row.get("SH Event ID") or row.get("stubhub_eventId") or row.get("eventId")
    if not identity:
        match = re.search(
            r"/event/([0-9]+)(?:/|$)", row.get("StubHub URL") or row.get("stubhub_url") or ""
        )
        identity = match.group(1) if match else ""
    if not re.fullmatch(r"[0-9]+", identity):
        raise ValueError("Each input row requires a numeric SH event ID or StubHub event URL")
    return identity


def legacy_stem(city):
    def slug(value):
        ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", "_", ascii_value.lower()).strip("_")

    return "_".join(slug(city[key]) for key in ("name", "country", "lat", "lng"))


def explore_events(payload):
    """A bounded lookup can reach empty pages beyond a nonzero discovery total."""
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or any(
        not isinstance(event, dict) or not event.get("eventId") for event in events
    ):
        raise ResponseError("Explore requires an events list with event IDs")
    if not events and (type(payload.get("total")) is not int or payload["total"] < 0):
        raise ResponseError("Empty Explore page requires a nonnegative total")
    return events


def locate_sources(ids, config):
    """Use actual seed coordinates; legacy raw names alone lose coordinate signs."""
    cities = load_cities(config.cities_csv)
    sources = {}
    if config.source_raw_dir:
        directory = Path(config.source_raw_dir)
        if not directory.is_dir():
            raise ValueError(f"Source raw directory does not exist: {directory}")
        by_stem = {}
        for city in cities:
            for stem in (
                legacy_stem(city),
                city_output_stem(city["name"], city["country"], city["lat"], city["lng"]),
            ):
                by_stem.setdefault(stem, []).append(city)
        for path in sorted(directory.glob("*.json")):
            match = re.fullmatch(r"(.+)_p([0-9]+)\.json", path.name)
            if not match or match[1] not in by_stem:
                continue
            seeds = by_stem[match[1]]
            if len(seeds) != 1:
                raise ValueError(f"Ambiguous seed coordinates for raw file: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            events = explore_events(payload)
            for event in events or []:
                identity = str(event["eventId"])
                page = int(match[2])
                if identity in ids and (
                    identity not in sources or page < sources[identity]["page"]
                ):
                    sources[identity] = {"city": seeds[0], "page": page, "raw_source": str(path)}
        return sources
    fields, events = read_csv(config.events_csv)
    if not {"eventId", "city", "country", "page"} <= set(fields):
        raise ValueError("Event CSV requires eventId, city, country and page")
    by_name = {}
    for city in cities:
        by_name.setdefault((city["name"], city["country"].upper()), []).append(city)
    for row in events:
        identity = row["eventId"]
        if identity not in ids:
            continue
        seeds = by_name.get((row["city"], row["country"].upper()), [])
        if len(seeds) > 1:
            raise ValueError("Ambiguous query city; supply --source-raw-dir to recover coordinates")
        if not seeds:
            continue
        page = int(row["page"])
        if page < 0:
            raise ValueError("Source page must be nonnegative")
        if identity not in sources or page < sources[identity]["page"]:
            sources[identity] = {"city": seeds[0], "page": page}
    return sources


def parse_price(value, currencies):
    """Accept explicit currency tokens and English number grouping; never infer bare $."""
    tokens = {}
    for code, symbols in DEFAULT_SYMBOLS.items():
        for symbol in symbols:
            tokens.setdefault(symbol, set()).add(code)
    for item in currencies:
        code = item.get("code", "")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code):
            continue
        for symbol in (code, item.get("symbol"), item.get("extendedSymbol")):
            if isinstance(symbol, str) and symbol:
                tokens.setdefault(symbol, set()).add(code)
    text = re.sub(r"^from\s+", "", value.strip(), flags=re.IGNORECASE)
    number = r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)"
    for token in sorted(tokens, key=len, reverse=True):
        escaped = re.escape(token)
        match = re.fullmatch(rf"{escaped}\s*{number}|{number}\s*{escaped}", text)
        if not match:
            continue
        if token == "$" or len(tokens[token]) != 1:
            raise ValueError("ambiguous_currency")
        amount = Decimal((match[1] or match[2]).replace(",", ""))
        if amount <= 0:
            raise ValueError("invalid_price")
        return amount, next(iter(tokens[token]))
    if re.fullmatch(rf"\$\s*{number}", text):
        raise ValueError("ambiguous_currency")
    raise ValueError("invalid_price")


def usd_amount(amount, currency, currencies):
    """Keep precision for comparing listing prices before cent rounding."""
    if currency == "USD":
        return amount
    rates = {item.get("code"): item.get("currentRate") for item in currencies}
    try:
        usd_rate, source_rate = Decimal(str(rates["USD"])), Decimal(str(rates[currency]))
        if not all(rate.is_finite() and rate > 0 for rate in (usd_rate, source_rate)):
            raise ValueError("Invalid exchange rate")
        return amount * usd_rate / source_rate
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ValueError("fx_error") from exc


def to_usd(amount, currency, currencies):
    return usd_amount(amount, currency, currencies).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def price_result(event, currencies):
    quote = event.get("formattedFromPrice")
    result = {"status": "no_quote", "usd": "", "currency": "", "quote": quote or ""}
    if quote in (None, ""):
        return result
    if not isinstance(quote, str):
        return {**result, "status": "invalid_price"}
    try:
        amount, currency = parse_price(quote, currencies)
        result["currency"] = currency
        result["usd"] = str(to_usd(amount, currency, currencies))
        result["status"] = "priced"
    except ValueError as exc:
        result["status"] = str(exc)
    return result


class PriceSession:
    """Share cookies for settings and Explore; pace all requests, including retries."""

    def __init__(self, config, output):
        self.config = config
        self.cookie_file = output / ".session.cookies"
        self.last_request = None

    def pace(self):
        if self.last_request is not None:
            time.sleep(
                max(0, self.config.request_interval - (time.monotonic() - self.last_request))
            )
        self.last_request = time.monotonic()

    def fetch(self, url, **kwargs):
        return get_json(
            url, self.config, cookie_file=self.cookie_file, before_request=self.pace, **kwargs
        )

    def close(self):
        self.cookie_file.unlink(missing_ok=True)


def save_json(path, payload):
    with atomic_output(path) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def refresh(ids, sources, config, session, output, currencies):
    checks = {
        identity: {
            "status": "source_missing",
            "usd": "",
            "currency": "",
            "quote": "",
            "checked_at": "",
            "observations": [],
            "requests": [],
        }
        for identity in sorted(ids)
    }
    jobs = {}
    for identity, source in sources.items():
        city, page = source["city"], source["page"]
        for offset in range(-config.page_radius, config.page_radius + 1):
            current_page = page + offset
            if current_page < 0:
                continue
            url = build_explore_url(city["lat"], city["lng"], current_page, config)
            job = jobs.setdefault(
                url, {"page": current_page, "targets": set(), "distance": abs(offset)}
            )
            job["targets"].add(identity)
            job["distance"] = min(job["distance"], abs(offset))
    requests = []
    found = set()
    for url, job in sorted(jobs.items(), key=lambda item: (item[1]["distance"], item[0])):
        if job["targets"] <= found:
            continue
        checked_at = utc_now()
        record = {"url": url, "checked_at": checked_at, "page": job["page"]}
        try:
            payload = session.fetch(url)
            record["checked_at"] = checked_at = utc_now()
            raw = output / "raw" / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
            save_json(raw, payload)
            record["raw_file"] = str(raw)
            for event in explore_events(payload):
                identity = str(event["eventId"])
                if identity not in ids or identity in found:
                    continue
                checks[identity].update(price_result(event, currencies), checked_at=checked_at)
                checks[identity]["observations"].append(
                    {
                        "raw_file": str(raw),
                        "url": url,
                        "page": job["page"],
                        "checked_at": checked_at,
                        **{
                            key: event.get(key)
                            for key in (
                                "eventId",
                                "name",
                                "formattedFromPrice",
                                "allowPublicPurchase",
                                "hasActiveListings",
                                "eventState",
                            )
                        },
                    }
                )
                found.add(identity)
        except (ResponseError, OSError) as exc:
            record["error"] = str(exc)
            record["checked_at"] = checked_at = utc_now()
            logger.warning("Price page failed: %s", exc)
        index = len(requests)
        requests.append(record)
        for identity in job["targets"]:
            checks[identity]["requests"].append(index)
            if identity not in found:
                checks[identity]["checked_at"] = checked_at
        logger.info("Checked page %d: %d/%d target events found", job["page"], len(found), len(ids))
    for identity in sources.keys() - found:
        failed = any("error" in requests[index] for index in checks[identity]["requests"])
        checks[identity]["status"] = "request_error" if failed else "not_found"
    return checks, requests


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
        from .event_page_prices import BrowserPriceSession

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
            from .event_page_prices import refresh_event_pages

            checks, requests = refresh_event_pages(rows, session, output, currencies)
            report["browser_warmup"] = session.warmup
        elif config.source == "listings":
            from .listing_prices import refresh_listings

            checks, requests = refresh_listings(rows, config, session, output, currencies)
        else:
            checks, requests = refresh(ids, sources, config, session, output, currencies)
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
    parser = argparse.ArgumentParser(description=__doc__)
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
