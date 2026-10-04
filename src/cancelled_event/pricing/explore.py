"""Recover discovery coordinates and refresh optional Explore display quotes."""

import hashlib
import json
import logging
import re
import unicodedata
from pathlib import Path

from ..http import ResponseError
from ..runtime import read_csv, save_json, utc_now
from ..sources.city_seeds import load_cities
from ..sources.stubhub_events import build_explore_url, city_output_stem
from .common import parse_price, to_usd

logger = logging.getLogger(__name__)


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


def refresh_explore(ids, sources, config, session, output, currencies):
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
