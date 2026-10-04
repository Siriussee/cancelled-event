#!/usr/bin/env python3
"""Filter data/worldcities.csv using Open-Meteo/GeoNames feature codes.

The script queries Open-Meteo's public geocoding API, selects the closest
normalized-name match for each input row, writes a full audit CSV, and writes a
filtered city CSV containing only accepted GeoNames feature codes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

from .runtime import write_csv

ALLOWED_FEATURE_CODES = {"PPLC", "PPLA", "PPLA2", "PPL"}
API_BASE = "https://geocoding-api.open-meteo.com/v1/search"
AUDIT_FIELDS = [
    "country",
    "name",
    "lat",
    "lng",
    "decision",
    "status",
    "selected_name",
    "selected_country_code",
    "selected_feature_code",
    "selected_population",
    "selected_latitude",
    "selected_longitude",
    "selected_distance_km",
    "selected_admin1",
    "selected_admin2",
    "candidate_count",
    "error",
]


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    a = min(1.0, max(0.0, a))
    return 2 * earth_radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_audit(path: Path) -> dict[tuple[str, str, str, str], dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        return {
            (row["country"], row["name"], row["lat"], row["lng"]): row
            for row in rows
            if row.get("country") and row.get("name") and row.get("status") != "error"
        }


def fetch_candidates(name: str, country: str, count: int, timeout: float) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "name": name,
            "count": count,
            "language": "en",
            "format": "json",
            "countryCode": country,
        }
    )
    request = urllib.request.Request(
        f"{API_BASE}?{query}",
        headers={"User-Agent": "stubhub-city-feature-filter/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
        raise ValueError("Invalid geocoding response")
    return payload.get("results") or []


def select_candidate(
    row: dict[str, str], candidates: Iterable[dict]
) -> tuple[dict | None, float | None]:
    source_name = normalize_name(row["name"])
    lat = float(row["lat"])
    lon = float(row["lng"])
    scored = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("country_code", "")).upper() != row["country"].upper():
            continue
        candidate_name = normalize_name(str(candidate.get("name", "")))
        candidate_lat = candidate.get("latitude")
        candidate_lon = candidate.get("longitude")
        if candidate_lat is None or candidate_lon is None:
            continue
        try:
            candidate_lat, candidate_lon = float(candidate_lat), float(candidate_lon)
        except (TypeError, ValueError):
            continue
        if not (
            math.isfinite(candidate_lat)
            and math.isfinite(candidate_lon)
            and -90 <= candidate_lat <= 90
            and -180 <= candidate_lon <= 180
        ):
            continue
        distance = haversine_km(lat, lon, candidate_lat, candidate_lon)
        exact_name = candidate_name == source_name
        scored.append((0 if exact_name and source_name else 1, distance, candidate))

    if not scored:
        return None, None

    scored.sort(key=lambda item: (item[0], item[1]))
    exact_rank, distance, candidate = scored[0]

    if exact_rank == 0 and distance <= 25.0:
        return candidate, distance

    if distance <= 2.0:
        return candidate, distance

    return None, distance


def audit_row(
    row: dict[str, str],
    *,
    count: int,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> dict[str, str]:
    error = ""
    candidates: list[dict] = []

    for attempt in range(1, retries + 1):
        try:
            candidates = fetch_candidates(row["name"], row["country"], count, timeout)
            error = ""
            break
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt == retries:
                return {
                    **{field: "" for field in AUDIT_FIELDS},
                    "country": row["country"],
                    "name": row["name"],
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "decision": "drop",
                    "status": "error",
                    "candidate_count": "0",
                    "error": error,
                }
            time.sleep(retry_delay * attempt)

    selected, distance = select_candidate(row, candidates)
    if selected is None:
        return {
            **{field: "" for field in AUDIT_FIELDS},
            "country": row["country"],
            "name": row["name"],
            "lat": row["lat"],
            "lng": row["lng"],
            "decision": "drop",
            "status": "no_match",
            "candidate_count": str(len(candidates)),
            "selected_distance_km": "" if distance is None else f"{distance:.6f}",
            "error": error,
        }

    feature_code = str(selected.get("feature_code", ""))
    decision = "keep" if feature_code in ALLOWED_FEATURE_CODES else "drop"
    return {
        "country": row["country"],
        "name": row["name"],
        "lat": row["lat"],
        "lng": row["lng"],
        "decision": decision,
        "status": "matched",
        "selected_name": str(selected.get("name", "")),
        "selected_country_code": str(selected.get("country_code", "")),
        "selected_feature_code": feature_code,
        "selected_population": str(selected.get("population", "")),
        "selected_latitude": str(selected.get("latitude", "")),
        "selected_longitude": str(selected.get("longitude", "")),
        "selected_distance_km": "" if distance is None else f"{distance:.6f}",
        "selected_admin1": str(selected.get("admin1", "")),
        "selected_admin2": str(selected.get("admin2", "")),
        "candidate_count": str(len(candidates)),
        "error": error,
    }


def append_audit(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/worldcities.csv")
    parser.add_argument("--output", default="data/worldcities.filtered.csv")
    parser.add_argument("--audit", default="data/worldcities_feature_audit.csv")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.08)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--progress-every", type=int, default=50)
    args = parser.parse_args(argv)
    for name in ("count", "retries", "progress_every"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.count > 100:
        parser.error("--count must be at most 100")
    for name in ("delay", "retry_delay", "timeout"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == "timeout" and value == 0):
            requirement = "positive" if name == "timeout" else "non-negative"
            parser.error(f"--{name.replace('_', '-')} must be finite and {requirement}")

    input_path = Path(args.input)
    output_path = Path(args.output)
    audit_path = Path(args.audit)

    if len({input_path.resolve(), output_path.resolve(), audit_path.resolve()}) != 3:
        parser.error("Input, output and audit paths must be different")
    with input_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        input_fields = reader.fieldnames or []
        rows = list(reader)
    if not {"name", "country", "lat", "lng"} <= set(input_fields):
        parser.error("Input requires name, country, lat and lng columns")
    from .event_scraper import validate_city_data

    if any(not validate_city_data(row) for row in rows):
        parser.error("Input contains missing fields or invalid coordinates")

    audit = load_audit(audit_path)
    keys = {(row["country"], row["name"], row["lat"], row["lng"]) for row in rows}
    audit = {key: row for key, row in audit.items() if key in keys}
    processed = len(audit)
    total = len(rows)
    started = time.time()

    print(f"Loaded {total} rows; {processed} already audited", flush=True)
    for row in rows:
        key = (row["country"], row["name"], row["lat"], row["lng"])
        if key in audit:
            continue

        audited = audit_row(
            row,
            count=args.count,
            timeout=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
        append_audit(audit_path, audited)
        audit[key] = audited
        processed += 1

        if processed % args.progress_every == 0 or processed == total:
            elapsed = time.time() - started
            rate = processed / elapsed if elapsed > 0 else 0
            print(f"progress {processed}/{total} rows | {rate:.2f} rows/s", flush=True)

        if args.delay > 0:
            time.sleep(args.delay)

    kept_rows = [
        row
        for row in rows
        if audit[(row["country"], row["name"], row["lat"], row["lng"])]["decision"] == "keep"
    ]
    write_csv(output_path, kept_rows, input_fields)

    by_status: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for row in audit.values():
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        code = row.get("selected_feature_code", "")
        if code:
            by_code[code] = by_code.get(code, 0) + 1

    print(f"kept {len(kept_rows)} of {total} rows", flush=True)
    print(f"status_counts {json.dumps(by_status, sort_keys=True)}", flush=True)
    print(f"feature_code_counts {json.dumps(by_code, sort_keys=True)}", flush=True)
    return 1 if by_status.get("error", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
