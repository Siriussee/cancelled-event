"""Enrich unique StubHub event/category pairs with validated venue JSON."""

import argparse
import csv
import json
import logging
import re
import time
from collections import Counter
from dataclasses import replace
from functools import partial
from pathlib import Path
from urllib.parse import urlencode

from ..config import VenueFetcherConfig
from ..http import get_json
from ..runtime import atomic_output, configure_logging, worker_results

logger = logging.getLogger(__name__)


def validate_event_data(event_id: str, category_id: str) -> bool:
    return all(re.fullmatch(r"[0-9]+", str(value or "")) for value in (event_id, category_id))


def load_unique_events(path: str) -> set[tuple[str, str]]:
    events = set()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not {"eventId", "categoryId"} <= set(reader.fieldnames or []):
            raise ValueError("Events CSV requires eventId and categoryId columns")
        for row in reader:
            event_id, category_id = row.get("eventId"), row.get("categoryId")
            if validate_event_data(event_id, category_id):
                events.add((event_id, category_id))
            else:
                logger.warning("Skipping invalid event/category IDs")
    return events


def fetch_venue_map(event_id: str, category_id: str, config: VenueFetcherConfig) -> dict:
    if not validate_event_data(event_id, category_id):
        raise ValueError("Event and category IDs must contain only digits")
    url = (
        f"https://www.stubhub.com/Browse/VenueMap/GetVenueMapSeatingConfig/{event_id}"
        f"?{urlencode({'categoryId': category_id})}"
    )
    return get_json(
        url,
        config,
        data=urlencode(
            {
                "categoryId": category_id,
                "withFees": "true",
                "withSeats": "false",
            }
        ),
    )


def process_event(event: tuple[str, str], config: VenueFetcherConfig) -> str:
    event_id, category_id = event
    path = Path(config.venue_dir) / f"{event_id}_{category_id}_venue.json"
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached
                and not cached.get("error")
                and not cached.get("errors")
            ):
                return "skipped"
        except (OSError, json.JSONDecodeError):
            pass
        logger.warning("Refetching unusable venue cache: %s", path)
    venue = fetch_venue_map(event_id, category_id, config)
    with atomic_output(path) as handle:
        json.dump(venue, handle, indent=2)
    if config.wait_seconds:
        time.sleep(config.wait_seconds)
    return "saved"


def run(config: VenueFetcherConfig) -> int:
    events = load_unique_events(config.events_csv)
    counts = Counter()
    for future in worker_results(
        partial(process_event, config=config), sorted(events), config.concurrent_venues
    ):
        try:
            counts[future.result()] += 1
        except Exception:
            counts["failed"] += 1
            logger.exception("Venue fetch failed")
    logger.info(
        "Venues: %s saved, %s skipped, %s failed",
        counts["saved"],
        counts["skipped"],
        counts["failed"],
    )
    return 1 if counts["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cancelled-event venues", description=__doc__)
    parser.add_argument("--events-csv", help="StubHub event CSV")
    parser.add_argument("--output-dir", help="Venue JSON directory")
    args = parser.parse_args(argv)
    config = VenueFetcherConfig()
    config = replace(
        config,
        events_csv=args.events_csv or config.events_csv,
        venue_dir=args.output_dir or config.venue_dir,
    )
    configure_logging(config.log_file, config.log_level)
    try:
        return run(config)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
