"""Collect location-based StubHub Explore events with resumable CSV appends."""

import argparse
import base64
import csv
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import replace
from functools import partial
from pathlib import Path
from urllib.parse import urlencode

from ..config import ScraperConfig
from ..http import ResponseError, get_json
from ..runtime import atomic_output, configure_logging, worker_results
from .city_seeds import load_cities

logger = logging.getLogger(__name__)
CSV_WRITE_LOCK = threading.Lock()
PROGRESS_WRITE_LOCK = threading.Lock()
EVENT_FIELDS = [
    "eventId",
    "name",
    "url",
    "venueName",
    "venueId",
    "formattedVenueLocation",
    "categoryId",
    "dayOfWeek",
    "formattedDateWithoutYear",
    "formattedTime",
    "imageUrl",
    "priceClass",
    "eventState",
    "aggregateFavorites",
    "allowPublicPurchase",
    "hasActiveListings",
    "isDateConfirmed",
    "isFavorite",
    "isParkingEvent",
    "isRefetchedGlobalEvent",
    "isTbd",
    "isTimeConfirmed",
    "isUnderHundred",
]
CSV_FIELDNAMES = ["city", "country", "page", *EVENT_FIELDS]
COMPLETED = -1


class ScrapeResponseError(ResponseError):
    """A StubHub page is incomplete or cannot safely be parsed."""


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def city_output_stem(city: str, country: str, lat: str, lon: str) -> str:
    normalized = unicodedata.normalize("NFKD", f"{city}_{country}")
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.encode("ascii", "ignore").decode().lower()).strip(
        "_"
    )
    digest = hashlib.sha256(f"{city},{country},{lat},{lon}".encode()).hexdigest()[:12]
    return f"{slug or 'city'}_{digest}"


def load_progress(path: str) -> dict[tuple[str, str], int]:
    progress = {}
    if Path(path).exists():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    lat, lon, page = line.strip().split(",")
                    page = int(page)
                    if page < COMPLETED:
                        raise ValueError("invalid page")
                    previous = progress.get((lat, lon), 0)
                    progress[(lat, lon)] = (
                        COMPLETED if COMPLETED in (previous, page) else max(previous, page)
                    )
                except ValueError:
                    logger.warning("Ignoring invalid checkpoint: %s", line.strip())
    return progress


def update_progress(lat: str, lon: str, page: int, path: str) -> None:
    with PROGRESS_WRITE_LOCK:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{lat},{lon},{page}\n")
            handle.flush()
            os.fsync(handle.fileno())


def build_explore_url(lat: str, lon: str, page: int, config: ScraperConfig) -> str:
    params = {
        "method": "getExploreEvents",
        "lat": b64(lat),
        "lon": b64(lon),
        "page": page,
        "pageSize": config.explore_page_size,
    }
    if config.explore_sort_type:
        params["sortType"] = config.explore_sort_type
    return f"https://www.stubhub.com/explore?{urlencode(params)}"


def parse_events(content: dict, city: str, country: str, page: int) -> list[dict] | None:
    if not isinstance(content, dict) or not isinstance(content.get("events"), list):
        raise ScrapeResponseError("StubHub response requires an events list")
    events = content["events"]
    if not events:
        if type(content.get("total")) is int and content["total"] == 0:
            return None
        raise ScrapeResponseError("Empty page without an explicit zero total")
    if any(not isinstance(event, dict) or not event.get("eventId") for event in events):
        raise ScrapeResponseError("Every event must be an object with an eventId")
    return [
        {
            "city": city,
            "country": country,
            "page": page,
            **{field: event.get(field) for field in EVENT_FIELDS},
        }
        for event in events
    ]


def event_identity(row: dict) -> str:
    return str(row["eventId"])


def load_saved_identities(path: str) -> dict[tuple[str, str], set[str]]:
    """Recover written event IDs, including a page committed before its checkpoint."""
    identities = {}
    if Path(path).exists() and Path(path).stat().st_size:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != CSV_FIELDNAMES:
                raise ValueError("Existing events CSV has a different schema; use a new EVENTS_CSV")
            for row in reader:
                if (
                    None in row
                    or any(value is None for value in row.values())
                    or not row.get("eventId")
                ):
                    raise ValueError(
                        "Existing events CSV contains a partial row; repair it before resuming"
                    )
                identities.setdefault((row["city"], row["country"]), set()).add(event_identity(row))
    return identities


def save_events(rows: list[dict], path: str) -> None:
    with CSV_WRITE_LOCK:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_header = not destination.exists() or destination.stat().st_size == 0
        with destination.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())


def scrape_city(city_info: dict, progress: dict, config: ScraperConfig, saved=None) -> int:
    city, country, lat, lon = (city_info[field] for field in ("name", "country", "lat", "lng"))
    start_page = progress.get((lat, lon), 0)
    if start_page == COMPLETED or start_page >= config.max_pages_per_city:
        return 0
    seen = set((saved or {}).get((city, country), set()))
    total_events = 0
    stem = city_output_stem(city, country, lat, lon)
    for page in range(start_page, config.max_pages_per_city):
        content = get_json(build_explore_url(lat, lon, page, config), config)
        rows = parse_events(content, city, country, page)
        with atomic_output(Path(config.out_dir) / f"{stem}_p{page}.json") as handle:
            json.dump(content, handle)
        if rows is None:
            update_progress(lat, lon, COMPLETED, config.progress_log)
            return total_events
        new_rows = []
        for row in rows:
            identity = event_identity(row)
            if identity not in seen:
                seen.add(identity)
                new_rows.append(row)
        save_events(new_rows, config.combined_csv)
        total_events += len(new_rows)
        update_progress(lat, lon, page + 1, config.progress_log)
        # Previously committed rows do not count as pagination overlap during crash recovery.
        duplicate_ratio = 1 - len({event_identity(row) for row in rows}) / len(rows)
        if page > start_page:
            duplicate_ratio = 1 - len(new_rows) / len(rows)
        if (page > start_page and not new_rows) or duplicate_ratio >= config.duplicate_stop_ratio:
            logger.info("Stopping %s at duplicate-heavy page %s", city, page)
            update_progress(lat, lon, COMPLETED, config.progress_log)
            return total_events
        if config.wait_seconds:
            time.sleep(config.wait_seconds)
    logger.warning(
        "%s reached the %s-page cap; coverage is partial", city, config.max_pages_per_city
    )
    return total_events


def run(config: ScraperConfig) -> int:
    if (
        len(
            {
                Path(path).resolve()
                for path in (config.input_csv, config.combined_csv, config.progress_log)
            }
        )
        != 3
    ):
        raise ValueError("City input, event output and checkpoint paths must differ")
    cities = load_cities(config.input_csv)
    saved = load_saved_identities(config.combined_csv)
    progress = load_progress(config.progress_log)
    if progress and (
        not Path(config.combined_csv).exists() or not Path(config.combined_csv).stat().st_size
    ):
        logger.warning("Ignoring checkpoints because the events CSV is missing or empty")
        progress = {}
        # Remove stale checkpoints only after discovering the replacement input is valid.
        with atomic_output(config.progress_log):
            pass
    save_events([], config.combined_csv)
    failures = 0
    collect = partial(scrape_city, progress=progress, config=config, saved=saved)
    for future in worker_results(collect, cities, config.concurrent_cities):
        try:
            future.result()
        except Exception:
            failures += 1
            logger.exception("City collection failed; its last committed checkpoint is retained")
    logger.info("Finished %s cities; %s failed", len(cities), failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cancelled-event collect stubhub", description=__doc__)
    parser.add_argument("--input-csv", help="City seed CSV")
    parser.add_argument("--events-csv", help="Event output CSV")
    parser.add_argument(
        "--progress-log", help="Resume checkpoint file; use a fresh file for a new crawl"
    )
    args = parser.parse_args(argv)
    config = ScraperConfig()
    config = replace(
        config,
        **{
            key: value
            for key, value in {
                "input_csv": args.input_csv,
                "combined_csv": args.events_csv,
                "progress_log": args.progress_log,
            }.items()
            if value is not None
        },
    )
    configure_logging(config.log_file, config.log_level)
    try:
        return run(config)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
