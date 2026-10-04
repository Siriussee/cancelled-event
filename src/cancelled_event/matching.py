#!/usr/bin/env python3
"""Find StubHub events that match Ticketmaster cancelled events.

The matcher is intentionally conservative: it requires exact date and location
agreement before applying venue and title/attraction similarity checks.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from .config import env_field
from .runtime import write_csv

STUBHUB_URL_DATE_RE = re.compile(r"-(\d{1,2})-(\d{1,2})-(\d{4})/event/")
CANCELLED_MARKER_RE = re.compile(r"\b(?:annul[eé]|cancelled|canceled)\b", re.IGNORECASE)

VENUE_MATCH_THRESHOLD = 0.86
TITLE_MATCH_THRESHOLD = 0.80
TITLE_CONTAINMENT_MIN_CHARS = 8
TITLE_CONTAINMENT_MIN_TOKENS = 2

CSV_FIELDNAMES = [
    "match_confidence",
    "event_date",
    "country",
    "stubhub_city",
    "stubhub_state",
    "ticketmaster_city",
    "ticketmaster_state",
    "stubhub_eventId",
    "ticketmaster_event_id",
    "ticketmaster_discovery_id",
    "stubhub_name",
    "ticketmaster_name",
    "stubhub_venueName",
    "ticketmaster_venueName",
    "title_score",
    "venue_score",
    "attraction_score",
    "matched_attraction",
    "stubhub_url",
    "ticketmaster_url",
    "stubhub_allowPublicPurchase",
    "stubhub_hasActiveListings",
    "stubhub_eventState",
    "ticketmaster_statusCode",
]


@dataclass(frozen=True)
class MatchingConfig:
    """Configuration for StubHub/Ticketmaster cancelled-event matching."""

    stubhub_csv: str = env_field("STUBHUB_EVENTS_CSV", "", str)
    ticketmaster_csv: str = env_field(
        "TICKETMASTER_CANCELLED_EVENTS_CSV", "output/ticketmaster_cancelled_events.csv"
    )
    output_csv: str = env_field("CANCELLED_EVENT_INTERSECTION_CSV", "output/matches.csv")

    def __post_init__(self) -> None:
        if not self.stubhub_csv:
            # Only the path is needed; unrelated scraper settings should not affect matching.
            object.__setattr__(self, "stubhub_csv", os.getenv("EVENTS_CSV", "output/events.csv"))


def normalize_text(value: str) -> str:
    """Normalize text for conservative cross-platform comparisons."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char)).lower()
    ascii_text = ascii_text.replace("&", " and ").replace("+", " and ")
    ascii_text = re.sub(r"\bw/\s*", " with ", ascii_text)
    ascii_text = re.sub(r"\bfeat\.?\b", " featuring ", ascii_text)
    ascii_text = re.sub(r"[\W_]+", " ", ascii_text)
    return " ".join(ascii_text.split())


def normalize_title(value: str) -> str:
    """Normalize event names while ignoring explicit cancellation markers."""
    return normalize_text(CANCELLED_MARKER_RE.sub(" ", value or ""))


def similarity(left: str, right: str, *, title: bool = False) -> float:
    """Return a stable 0..1 string similarity score."""
    normalize = normalize_title if title else normalize_text
    left_normalized = normalize(left)
    right_normalized = normalize(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def title_similarity(left: str, right: str) -> float:
    """Score titles, accepting a substantial whole-phrase short title.

    Ticketmaster commonly appends tour names while StubHub keeps only the
    artist name. SequenceMatcher penalizes that asymmetry heavily. Phrase
    containment is safe here only when the shorter title has at least two
    tokens and eight characters; date, city/state, and venue must still match.
    """
    left_normalized = normalize_title(left)
    right_normalized = normalize_title(right)
    if not left_normalized or not right_normalized:
        return 0.0

    score = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    shorter, longer = sorted((left_normalized, right_normalized), key=len)
    phrase_pattern = rf"(?:^|\s){re.escape(shorter)}(?:$|\s)"
    if (
        len(shorter) >= TITLE_CONTAINMENT_MIN_CHARS
        and len(shorter.split()) >= TITLE_CONTAINMENT_MIN_TOKENS
        and re.search(phrase_pattern, longer)
    ):
        return 1.0
    return score


def stubhub_event_date(row: dict[str, str]) -> str:
    """Extract the full event date from a StubHub event URL."""
    match = STUBHUB_URL_DATE_RE.search(row.get("url", ""))
    if not match:
        return ""

    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def stubhub_city(row: dict[str, str]) -> str:
    """Return the venue city from StubHub's formatted location field."""
    formatted_location = row.get("formattedVenueLocation", "")
    if formatted_location:
        return formatted_location.split(",")[0].strip()
    return row.get("city", "")


def stubhub_state(row: dict[str, str]) -> str:
    """Return the two-letter US state from StubHub's formatted location when present."""
    parts = [part.strip() for part in row.get("formattedVenueLocation", "").split(",")]
    if len(parts) >= 3 and parts[-1].upper() in {
        "US",
        "USA",
        "UNITED STATES",
        "UNITED STATES OF AMERICA",
    }:
        return parts[-2].upper()
    return ""


def stubhub_country(row: dict[str, str]) -> str:
    """Prefer the venue's country over the discovery query's seed country."""
    aliases = {"USA": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "CANADA": "CA"}
    location = row.get("formattedVenueLocation", "")
    if "," in location:
        country = location.rsplit(",", 1)[-1].strip().upper()
        if country in aliases:
            return aliases[country]
        if country in {"US", "CA"}:
            return country
    return (row.get("country") or "").strip().upper()


def ticketmaster_event_id(row: dict[str, str]) -> str:
    """Return the most useful Ticketmaster event identifier available in the CSV."""
    return row.get("tmId") or row.get("id") or ""


def _dedupe_rows(
    rows: Iterable[dict[str, str]], key_name: str, fallback_key: str = ""
) -> list[dict[str, str]]:
    deduped: dict[str, dict[str, str]] = {}
    for index, row in enumerate(rows):
        key = row.get(key_name) or row.get(fallback_key) or f"row:{index}"
        deduped.setdefault(key, row)
    return list(deduped.values())


def _is_truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _stubhub_is_available(row: dict[str, str]) -> bool:
    allow_public_purchase = row.get("allowPublicPurchase")
    if allow_public_purchase not in (None, "") and not _is_truthy(allow_public_purchase):
        return False
    return True


def _ticketmaster_index(
    ticketmaster_rows: Iterable[dict[str, str]],
) -> dict[tuple[str, str, str], list[dict[str, str]]]:
    index: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in _dedupe_rows(ticketmaster_rows, "tmId", "id"):
        if normalize_text(row.get("statusCode", "")) not in {"cancelled", "canceled"}:
            continue
        country = (row.get("venueCountryCode") or row.get("countryCode") or "").upper()
        key = (country, row.get("localDate", ""), normalize_text(row.get("venueCity", "")))
        if all(key):
            index.setdefault(key, []).append(row)
    return index


def _best_attraction_score(stubhub_name: str, attraction_names: str) -> tuple[float, str]:
    best_score = 0.0
    best_name = ""
    normalized_stubhub_name = normalize_title(stubhub_name)
    if not normalized_stubhub_name:
        return best_score, best_name

    for attraction_name in attraction_names.split("|"):
        normalized_attraction = normalize_text(attraction_name)
        if not normalized_attraction:
            continue

        score = similarity(stubhub_name, attraction_name, title=True)
        if normalized_attraction == normalized_stubhub_name or (
            len(normalized_attraction) >= 4
            and (
                normalized_attraction in normalized_stubhub_name
                or normalized_stubhub_name in normalized_attraction
            )
        ):
            score = 1.0

        if score > best_score:
            best_score = score
            best_name = attraction_name

    return best_score, best_name


def _is_high_confidence(title_score: float, venue_score: float) -> bool:
    if venue_score < VENUE_MATCH_THRESHOLD:
        return False
    return title_score >= TITLE_MATCH_THRESHOLD


def _format_score(score: float) -> str:
    return f"{score:.3f}"


def _match_row(
    stubhub_row: dict[str, str],
    ticketmaster_row: dict[str, str],
    event_date: str,
    title_score: float,
    venue_score: float,
    attraction_score: float,
    matched_attraction: str,
) -> dict[str, str]:
    return {
        "match_confidence": "high",
        "event_date": event_date,
        "country": stubhub_country(stubhub_row),
        "stubhub_city": stubhub_city(stubhub_row),
        "stubhub_state": stubhub_state(stubhub_row),
        "ticketmaster_city": ticketmaster_row.get("venueCity", ""),
        "ticketmaster_state": ticketmaster_row.get("venueStateCode", ""),
        "stubhub_eventId": stubhub_row.get("eventId", ""),
        "ticketmaster_event_id": ticketmaster_event_id(ticketmaster_row),
        "ticketmaster_discovery_id": ticketmaster_row.get("id", ""),
        "stubhub_name": stubhub_row.get("name", ""),
        "ticketmaster_name": ticketmaster_row.get("name", ""),
        "stubhub_venueName": stubhub_row.get("venueName", ""),
        "ticketmaster_venueName": ticketmaster_row.get("venueName", ""),
        "title_score": _format_score(title_score),
        "venue_score": _format_score(venue_score),
        "attraction_score": _format_score(attraction_score),
        "matched_attraction": matched_attraction,
        "stubhub_url": stubhub_row.get("url", ""),
        "ticketmaster_url": ticketmaster_row.get("url", ""),
        "stubhub_allowPublicPurchase": stubhub_row.get("allowPublicPurchase", ""),
        "stubhub_hasActiveListings": stubhub_row.get("hasActiveListings", ""),
        "stubhub_eventState": stubhub_row.get("eventState", ""),
        "ticketmaster_statusCode": ticketmaster_row.get("statusCode", ""),
    }


def find_high_confidence_matches(
    stubhub_rows: Iterable[dict[str, str]], ticketmaster_rows: Iterable[dict[str, str]]
) -> list[dict[str, str]]:
    """Find high-confidence StubHub events cancelled in Ticketmaster."""
    matches: list[dict[str, str]] = []
    ticketmaster_by_location = _ticketmaster_index(ticketmaster_rows)

    for stubhub_row in _dedupe_rows(stubhub_rows, "eventId"):
        if not _stubhub_is_available(stubhub_row):
            continue

        event_date = stubhub_event_date(stubhub_row)
        if not event_date:
            continue

        country = stubhub_country(stubhub_row)
        key = (country, event_date, normalize_text(stubhub_city(stubhub_row)))
        for ticketmaster_row in ticketmaster_by_location.get(key, []):
            if (
                country == "US"
                and stubhub_state(stubhub_row)
                and ticketmaster_row.get("venueStateCode")
                and stubhub_state(stubhub_row) != ticketmaster_row["venueStateCode"].upper()
            ):
                continue

            venue_score = similarity(
                stubhub_row.get("venueName", ""), ticketmaster_row.get("venueName", "")
            )
            title_score = title_similarity(
                stubhub_row.get("name", ""), ticketmaster_row.get("name", "")
            )
            attraction_score, matched_attraction = _best_attraction_score(
                stubhub_row.get("name", ""), ticketmaster_row.get("attractionNames", "")
            )

            if not _is_high_confidence(title_score, venue_score):
                continue

            matches.append(
                _match_row(
                    stubhub_row,
                    ticketmaster_row,
                    event_date,
                    title_score,
                    venue_score,
                    attraction_score,
                    matched_attraction,
                )
            )

    return sorted(
        matches,
        key=lambda row: (
            row["event_date"],
            normalize_text(row["stubhub_city"]),
            normalize_text(row["stubhub_name"]),
            row["stubhub_eventId"],
            row["ticketmaster_event_id"],
        ),
    )


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    """Read a CSV file into dictionaries."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "name" not in reader.fieldnames:
            raise ValueError("Event CSV requires a name column")
        return list(reader)


def write_matches_csv(matches: list[dict[str, str]], output_csv: str | Path) -> None:
    """Write the high-confidence intersection CSV."""
    write_csv(output_csv, matches, CSV_FIELDNAMES)


def run(config: MatchingConfig) -> list[dict[str, str]]:
    """Read both input CSVs, write the high-confidence intersection, and return rows."""
    if Path(config.output_csv).resolve() in {
        Path(config.stubhub_csv).resolve(),
        Path(config.ticketmaster_csv).resolve(),
    }:
        raise ValueError("Output CSV must differ from both input CSVs")
    stubhub_rows = read_csv_rows(config.stubhub_csv)
    ticketmaster_rows = read_csv_rows(config.ticketmaster_csv)
    matches = find_high_confidence_matches(stubhub_rows, ticketmaster_rows)
    write_matches_csv(matches, config.output_csv)
    return matches


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cancelled-event match",
        description="Find high-confidence StubHub events cancelled in Ticketmaster.",
    )
    parser.add_argument("--stubhub-csv", help="StubHub event CSV path.")
    parser.add_argument("--ticketmaster-csv", help="Ticketmaster cancelled-event CSV path.")
    parser.add_argument("--output-csv", help="Output CSV path for high-confidence matches.")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> MatchingConfig:
    base = MatchingConfig()
    return replace(
        base,
        stubhub_csv=args.stubhub_csv or base.stubhub_csv,
        ticketmaster_csv=args.ticketmaster_csv or base.ticketmaster_csv,
        output_csv=args.output_csv or base.output_csv,
    )


def main(argv: list[str] | None = None) -> None:
    config = _config_from_args(_parse_args(argv))
    matches = run(config)
    print(f"Saved {len(matches)} high-confidence matches to {config.output_csv}")


if __name__ == "__main__":
    main()
