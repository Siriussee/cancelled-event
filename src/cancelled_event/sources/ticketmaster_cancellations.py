#!/usr/bin/env python3
"""Collect validated, date-partitioned Ticketmaster Discover cancellation snapshots."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import env_bool, env_field, validate_number
from ..runtime import atomic_output, configure_logging, write_csv

ENDPOINT = "https://www.ticketmaster.com/api/search/events/category"
PAGE_SIZE = 20
MAX_API_PAGES = 49  # page=0..48 work; page=49 returns HTTP 400.
TM_URL_DATE_RE = re.compile(r"-(\d{1,2})-(\d{1,2})-(\d{4})/event/")

CATEGORIES = {
    "concerts": {
        "id": "KZFzniwnSyZfZ7v7nJ",
        "name": "Concerts",
        "segment_name": "Music",
        "referer": "https://www.ticketmaster.com/discover/concerts",
    },
    "sports": {
        "id": "KZFzniwnSyZfZ7v7nE",
        "name": "Sports",
        "segment_name": "Sports",
        "referer": "https://www.ticketmaster.com/discover/sports",
    },
}

CSV_FIELDNAMES = [
    "countryCode",
    "page",
    "id",
    "tmId",
    "name",
    "url",
    "statusCode",
    "localDate",
    "dateTime",
    "venueName",
    "venueCity",
    "venueStateCode",
    "venueStateName",
    "venueCountryCode",
    "venueCountryName",
    "venueLatitude",
    "venueLongitude",
    "attractionIds",
    "attractionNames",
    "attractionUrls",
    "segmentId",
    "segmentName",
    "attractionSegmentIds",
    "attractionSegmentNames",
]

logger = logging.getLogger(__name__)


class TicketmasterScrapeError(Exception):
    """Raised when Ticketmaster returns an incomplete or unusable response."""


def _split_csv(value: str) -> tuple[str, ...]:
    parts = tuple(part.strip().upper() for part in value.split(",") if part.strip())
    return parts or ("US",)


def _split_categories(value: str) -> tuple[str, ...]:
    parts = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    return parts or ("concerts", "sports")


@dataclass(frozen=True)
class TicketmasterConfig:
    """Date/category slices and request settings, read at instance creation."""

    countries: tuple[str, ...] = env_field("TICKETMASTER_COUNTRY_CODES", "US", _split_csv)
    categories: tuple[str, ...] = env_field(
        "TICKETMASTER_CATEGORIES", "concerts,sports", _split_categories
    )
    start_date: str = env_field("TICKETMASTER_START_DATE", "")
    days: int = env_field("TICKETMASTER_DAYS", 30, int)
    output_csv: str = env_field(
        "TICKETMASTER_CANCELLED_EVENTS_CSV", "output/ticketmaster_cancelled_events.csv"
    )
    raw_output_dir: str = env_field(
        "TICKETMASTER_WGET_OUTPUT_DIR", "output/ticketmaster_cancelled_events"
    )
    save_raw_responses: bool = env_field("TICKETMASTER_SAVE_RAW_RESPONSES", True, env_bool)
    resume_raw_responses: bool = env_field("TICKETMASTER_RESUME_RAW_RESPONSES", False, env_bool)
    max_pages_per_query: int = env_field("TICKETMASTER_MAX_PAGES_PER_QUERY", 0, int)
    request_interval: float = env_field("TICKETMASTER_REQUEST_INTERVAL", 2.0, float)
    request_timeout: int = env_field("REQUEST_TIMEOUT", 30, int)
    max_retries: int = env_field("TICKETMASTER_MAX_RETRIES", 5, int)
    retry_delay: float = env_field("RETRY_DELAY", 2.0, float)
    block_retry_delay: float = env_field("TICKETMASTER_BLOCK_RETRY_DELAY", 30.0, float)
    user_agent: str = env_field(
        "USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36",
    )
    log_file: str = env_field(
        "TICKETMASTER_SCRAPER_LOG_FILE", "log/ticketmaster_cancelled_events.log"
    )
    log_level: str = env_field("LOG_LEVEL", "INFO")

    def __post_init__(self) -> None:
        for name in ("days", "request_timeout", "max_retries"):
            validate_number(name, getattr(self, name), 1)
        for name in ("max_pages_per_query", "request_interval", "retry_delay", "block_retry_delay"):
            validate_number(name, getattr(self, name))
        countries = tuple(dict.fromkeys(code.strip().upper() for code in self.countries))
        categories = tuple(dict.fromkeys(category.strip().lower() for category in self.categories))
        if not countries or any(not re.fullmatch(r"[A-Z]{2}", code) for code in countries):
            raise ValueError("countries must contain two-letter country codes")
        if not categories or any(category not in CATEGORIES for category in categories):
            raise ValueError("categories must contain concerts or sports")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL")
        object.__setattr__(self, "countries", countries)
        object.__setattr__(self, "categories", categories)
        if self.start_date:
            try:
                date.fromisoformat(self.start_date)
            except ValueError as exc:
                raise ValueError("Ticketmaster start_date must use YYYY-MM-DD") from exc


def configured_start_date(config: TicketmasterConfig) -> date:
    """Return the configured start date, defaulting to the current date."""
    return date.fromisoformat(config.start_date) if config.start_date else date.today()


def date_window(config: TicketmasterConfig) -> list[date]:
    """Return every local calendar date in the configured inclusive window."""
    first_date = configured_start_date(config)
    return [first_date + timedelta(days=offset) for offset in range(config.days)]


def build_events_url(
    country_code: str,
    category: str,
    local_date: date,
    page: int,
) -> str:
    """Build a country-wide Ticketmaster Discover API URL."""
    category_id = CATEGORIES[category]["id"]
    params = {
        "page": str(page),
        "region": "200",
        "countryCodes": country_code.upper(),
        "distance": "6214",
        "distanceUnit": "miles",
        "startDate": local_date.isoformat(),
        "endDate": local_date.isoformat(),
    }
    return f"{ENDPOINT}/{category_id}?{urlencode(params)}"


def _headers(category: str, config: TicketmasterConfig) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "x-tmlangcode": "en-us",
        "x-tmregion": "200",
        "X-TMPlatform": "global",
        "X-TMClient-App": "marketplace_fe",
        "Referer": CATEGORIES[category]["referer"],
        "User-Agent": config.user_agent,
    }


def get_json(url: str, category: str, config: TicketmasterConfig) -> dict[str, Any]:
    """GET one Ticketmaster JSON response."""
    request = Request(url, headers=_headers(category, config), method="GET")
    try:
        with urlopen(request, timeout=config.request_timeout) as response:
            raw_body = response.read()
    except HTTPError as exc:
        error_body = exc.read(1000).decode("utf-8", errors="replace")
        exc.close()
        raise TicketmasterScrapeError(f"Ticketmaster HTTP {exc.code}: {error_body[:1000]}") from exc
    except URLError as exc:
        raise TicketmasterScrapeError(f"Ticketmaster request failed: {exc.reason}") from exc

    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise TicketmasterScrapeError(f"Ticketmaster returned invalid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise TicketmasterScrapeError("Ticketmaster returned a non-object JSON response")
    return decoded


def _events_response(
    response: dict[str, Any], page: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(response, dict):
        raise TicketmasterScrapeError("Ticketmaster response must be an object")
    events = response.get("events")
    total = response.get("total")
    if not isinstance(events, list):
        raise TicketmasterScrapeError("Ticketmaster response missing events")
    if type(total) is not int or total < 0:
        raise TicketmasterScrapeError("Ticketmaster response missing a valid total")
    if any(not isinstance(event, dict) or not event.get("id") for event in events):
        raise TicketmasterScrapeError("Ticketmaster events require objects with IDs")
    if page is not None:
        expected = min(PAGE_SIZE, max(0, total - page * PAGE_SIZE))
        if len(events) != expected or len({str(event["id"]) for event in events}) != len(events):
            raise TicketmasterScrapeError(f"Incomplete or repeated records on page {page}")
    return events, total


def _raw_response_path(
    country_code: str,
    category: str,
    local_date: date,
    page: int,
    config: TicketmasterConfig,
) -> Path:
    filename = f"{country_code.lower()}_{category}_{local_date.isoformat()}_p{page}.json"
    return Path(config.raw_output_dir) / filename


def _save_raw_response(
    response: dict[str, Any],
    country_code: str,
    category: str,
    local_date: date,
    page: int,
    config: TicketmasterConfig,
) -> None:
    output_file = _raw_response_path(country_code, category, local_date, page, config)
    with atomic_output(output_file) as handle:
        json.dump(response, handle, indent=2, sort_keys=True)


def fetch_page(
    country_code: str,
    category: str,
    local_date: date,
    page: int,
    config: TicketmasterConfig,
) -> dict[str, Any]:
    """Fetch one Discover page with retry, backoff, and conservative pacing."""
    raw_path = _raw_response_path(country_code, category, local_date, page, config)
    if config.resume_raw_responses and raw_path.is_file():
        try:
            cached = json.loads(raw_path.read_text(encoding="utf-8"))
            _events_response(cached, page)
            return cached
        except (OSError, ValueError, TicketmasterScrapeError) as exc:
            logger.warning("Ignoring unusable cached response %s: %s", raw_path, exc)

    url = build_events_url(country_code, category, local_date, page)
    last_error: Exception | None = None
    response = None

    for attempt in range(config.max_retries):
        try:
            response = get_json(url, category, config)
            _events_response(response, page)
            break
        except (TicketmasterScrapeError, TimeoutError) as exc:
            last_error = exc
            response = None
            if attempt == config.max_retries - 1:
                break
            wait_time = config.retry_delay * (2**attempt)
            if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                wait_time = max(wait_time, config.block_retry_delay)
            wait_time = min(wait_time, 60.0)
            logger.warning(
                "Ticketmaster %s %s %s page %s attempt %s/%s failed: %s; retrying in %.1fs",
                country_code,
                category,
                local_date,
                page,
                attempt + 1,
                config.max_retries,
                exc,
                wait_time,
            )
            time.sleep(wait_time)

    if response is None:
        raise TicketmasterScrapeError(
            f"Ticketmaster {country_code} {category} {local_date} page {page} failed after "
            f"{config.max_retries} attempts: {last_error}"
        )
    if config.save_raw_responses:
        _save_raw_response(response, country_code, category, local_date, page, config)
    if config.request_interval:
        time.sleep(config.request_interval)
    return response


def _event_is_cancelled(item: dict[str, Any]) -> bool:
    return item.get("cancelled") is True or item.get("eventChangeStatus") == "eventCancelled"


def _url_local_date(url: str) -> str:
    match = TM_URL_DATE_RE.search(url or "")
    if not match:
        return ""
    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _event_local_date(item: dict[str, Any]) -> str:
    dates = item.get("dates") if isinstance(item.get("dates"), dict) else {}
    start_date = dates.get("startDate")
    time_zone = item.get("timeZone")
    if start_date and time_zone:
        try:
            parsed = datetime.fromisoformat(str(start_date).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
            return parsed.astimezone(ZoneInfo(str(time_zone))).date().isoformat()
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return _url_local_date(str(item.get("url") or ""))


def normalize_event(
    item: dict[str, Any], country_code: str, category: str, page: int
) -> dict[str, str]:
    """Flatten one Discover API event into the existing CSV contract."""
    venue = item.get("venue") if isinstance(item.get("venue"), dict) else {}
    dates = item.get("dates") if isinstance(item.get("dates"), dict) else {}
    category_metadata = CATEGORIES[category]
    tm_id = str(item.get("id") or "")

    return {
        "countryCode": country_code.upper(),
        "page": str(page),
        "id": str(item.get("discoveryId") or tm_id),
        "tmId": tm_id,
        "name": str(item.get("title") or ""),
        "url": str(item.get("url") or ""),
        "statusCode": "cancelled" if _event_is_cancelled(item) else "",
        "localDate": _event_local_date(item),
        "dateTime": str(dates.get("startDate") or ""),
        "venueName": str(venue.get("name") or ""),
        "venueCity": str(venue.get("city") or ""),
        "venueStateCode": str(venue.get("state") or ""),
        "venueStateName": "",
        "venueCountryCode": str(
            venue.get("countryCode") or venue.get("country") or country_code
        ).upper(),
        "venueCountryName": str(venue.get("countryName") or ""),
        "venueLatitude": str(venue.get("latitude") or ""),
        "venueLongitude": str(venue.get("longitude") or ""),
        "attractionIds": "",
        "attractionNames": "",
        "attractionUrls": "",
        "segmentId": str(category_metadata["id"]),
        "segmentName": str(category_metadata["segment_name"]),
        "attractionSegmentIds": "",
        "attractionSegmentNames": "",
    }


FetchPage = Callable[[str, str, date, int, TicketmasterConfig], dict[str, Any]]


def collect_date_category(
    country_code: str,
    category: str,
    local_date: date,
    config: TicketmasterConfig,
    fetch_page_func: FetchPage = fetch_page,
) -> list[dict[str, str]]:
    """Collect one country/category/date slice and keep exact-date cancellations."""
    first_response = fetch_page_func(country_code, category, local_date, 0, config)
    first_events, total = _events_response(first_response)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    if total_pages > MAX_API_PAGES and not config.max_pages_per_query:
        raise TicketmasterScrapeError(
            f"Ticketmaster {country_code} {category} {local_date} reports {total} events "
            f"({total_pages} pages), above the API's {MAX_API_PAGES}-page accessible limit"
        )

    pages_to_fetch = min(total_pages, MAX_API_PAGES)
    if config.max_pages_per_query:
        pages_to_fetch = min(pages_to_fetch, config.max_pages_per_query)

    rows: list[dict[str, str]] = []
    returned_count = 0
    seen_ids: set[str] = set()

    def add_events(events: list[dict[str, Any]], page: int) -> None:
        nonlocal returned_count
        expected = min(PAGE_SIZE, max(0, total - page * PAGE_SIZE))
        if len(events) != expected:
            raise TicketmasterScrapeError(
                f"Incomplete page {page}: expected {expected} events, got {len(events)}"
            )
        for item in events:
            identity = str(item["id"])
            if identity in seen_ids:
                raise TicketmasterScrapeError(f"Repeated event ID across pages: {identity}")
            seen_ids.add(identity)
        returned_count += len(events)
        for item in events:
            if not _event_is_cancelled(item):
                continue
            row = normalize_event(item, country_code, category, page)
            if row["localDate"] == local_date.isoformat():
                rows.append(row)

    add_events(first_events, 0)
    for page in range(1, pages_to_fetch):
        response = fetch_page_func(country_code, category, local_date, page, config)
        events, page_total = _events_response(response)
        if page_total != total:
            raise TicketmasterScrapeError(
                "Event total changed during pagination; retry with a fresh raw directory"
            )
        add_events(events, page)

    if returned_count < total:
        logger.warning(
            "Debug page cap enabled: snapshot is partial (%s/%s events)", returned_count, total
        )
    logger.info(
        "Ticketmaster %s %s %s: %s calls, %s/%s events returned, %s cancelled",
        country_code,
        category,
        local_date,
        pages_to_fetch,
        returned_count,
        total,
        len(rows),
    )
    return rows


def _deduplicate_events(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[str, dict[str, str]] = {}
    for index, row in enumerate(rows):
        key = row.get("tmId") or row.get("id") or f"row:{index}"
        deduped.setdefault(key, row)
    return sorted(
        deduped.values(),
        key=lambda row: (row.get("localDate", ""), row.get("name", ""), row.get("tmId", "")),
    )


def write_events_csv(rows: list[dict[str, str]], output_csv: str | Path) -> None:
    """Write a Ticketmaster cancelled-event snapshot to CSV."""
    write_csv(output_csv, rows, CSV_FIELDNAMES)


def run(
    config: TicketmasterConfig,
    fetch_page_func: FetchPage = fetch_page,
) -> list[dict[str, str]]:
    """Collect the configured date/category window and write its CSV snapshot."""
    rows: list[dict[str, str]] = []
    for country_code in config.countries:
        for local_date in date_window(config):
            for category in config.categories:
                rows.extend(
                    collect_date_category(
                        country_code,
                        category,
                        local_date,
                        config,
                        fetch_page_func=fetch_page_func,
                    )
                )

    rows = _deduplicate_events(rows)
    write_events_csv(rows, config.output_csv)
    return rows


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cancelled-event collect ticketmaster",
        description=(
            "Collect cancelled US Concerts and Sports from Ticketmaster Discover "
            "(default: today plus 29 days)."
        ),
    )
    parser.add_argument(
        "--country",
        action="append",
        dest="country_flags",
        help="Country code to collect. Repeat for multiple countries. Default: US.",
    )
    parser.add_argument(
        "--countries",
        dest="countries_csv",
        help="Comma-separated country codes. Ignored when --country is provided.",
    )
    parser.add_argument(
        "--category",
        action="append",
        dest="category_flags",
        choices=sorted(CATEGORIES),
        help="Category to collect. Repeat as needed. Default: concerts and sports.",
    )
    parser.add_argument("--start-date", help="First local event date, YYYY-MM-DD. Default: today.")
    parser.add_argument("--days", type=int, help="Number of local dates to collect. Default: 30.")
    parser.add_argument("--output-csv", help="CSV output path.")
    parser.add_argument("--raw-output-dir", help="Directory for raw Discover JSON pages.")
    parser.add_argument(
        "--request-interval",
        type=float,
        help="Delay after every successful request in seconds. Default: 2.0.",
    )
    parser.add_argument(
        "--max-pages-per-query",
        type=int,
        help="Debug cap for each date/category query. Use 0 or omit for all pages.",
    )
    parser.add_argument(
        "--resume-raw",
        action="store_true",
        help="Reuse valid pages already present in --raw-output-dir.",
    )
    parser.add_argument("--no-raw", action="store_true", help="Do not save raw JSON pages.")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> TicketmasterConfig:
    base = TicketmasterConfig()
    countries = base.countries
    if args.country_flags:
        countries = _split_csv(",".join(args.country_flags))
    elif args.countries_csv:
        countries = _split_csv(args.countries_csv)

    categories = base.categories
    if args.category_flags:
        categories = tuple(args.category_flags)

    return replace(
        base,
        countries=countries,
        categories=categories,
        start_date=args.start_date if args.start_date is not None else base.start_date,
        days=args.days if args.days is not None else base.days,
        output_csv=args.output_csv or base.output_csv,
        raw_output_dir=args.raw_output_dir or base.raw_output_dir,
        request_interval=(
            args.request_interval if args.request_interval is not None else base.request_interval
        ),
        max_pages_per_query=(
            args.max_pages_per_query
            if args.max_pages_per_query is not None
            else base.max_pages_per_query
        ),
        resume_raw_responses=True if args.resume_raw else base.resume_raw_responses,
        save_raw_responses=False if args.no_raw else base.save_raw_responses,
    )


def main(argv: list[str] | None = None) -> None:
    config = _config_from_args(_parse_args(argv))
    configure_logging(config.log_file, config.log_level)
    window = date_window(config)
    logger.info(
        "Starting Ticketmaster cancelled-event scrape: countries=%s categories=%s "
        "dates=%s..%s interval=%.1fs",
        ",".join(config.countries),
        ",".join(config.categories),
        window[0],
        window[-1],
        config.request_interval,
    )
    rows = run(config)
    print(f"Saved {len(rows)} Ticketmaster cancelled events to {config.output_csv}")


if __name__ == "__main__":
    main()
