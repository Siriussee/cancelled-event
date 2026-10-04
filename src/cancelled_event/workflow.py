"""Match collected cancellation snapshots and fetch StubHub listing floor prices."""

import argparse
import hashlib
from dataclasses import replace
from pathlib import Path

from .http import ResponseError
from .matching import MatchingConfig
from .matching import run as match_events
from .pricing.config import PriceConfig
from .pricing.refresh import run as fetch_prices
from .runtime import configure_logging, read_csv, save_json, utc_now


def run(matching, prices, output_dir):
    """Use existing collected snapshots and publish matches and prices in one fresh run."""
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Workflow output directory must be empty; use a fresh run directory")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "started_at": utc_now(),
        "stubhub_csv": matching.stubhub_csv,
        "ticketmaster_csv": matching.ticketmaster_csv,
        "price_source": prices.source,
        "stages": {},
    }
    try:
        manifest["inputs"] = {
            label: {"path": path, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
            for label, path in (
                ("stubhub", matching.stubhub_csv),
                ("ticketmaster", matching.ticketmaster_csv),
            )
        }
        matching = replace(matching, output_csv=str(output / "matches.csv"))
        matches = match_events(matching)
        manifest["stages"]["matching"] = {
            "status": "complete",
            "rows": len(matches),
            "unique_events": len({row["stubhub_eventId"] for row in matches}),
            "output": matching.output_csv,
        }
        prices = replace(
            prices,
            input_csv=matching.output_csv,
            events_csv=matching.stubhub_csv,
            output_dir=str(output / "prices"),
        )
        status = fetch_prices(prices)
        _, rows = read_csv(output / "prices" / "enriched.csv")
        manifest["stages"]["prices"] = {
            "status": "complete" if status == 0 else "needs_attention",
            "exit_code": status,
            "rows": len(rows),
            "output": str(output / "prices" / "enriched.csv"),
            "evidence": str(output / "prices" / "checks.json"),
        }
        manifest["status"] = "complete" if status == 0 else "needs_attention"
        return status
    except (OSError, ValueError, ResponseError) as exc:
        manifest.update(status="failed", error=str(exc))
        raise
    finally:
        manifest["completed_at"] = utc_now()
        save_json(output / "manifest.json", manifest)


def main(argv=None):
    matching, prices = MatchingConfig(), PriceConfig()
    parser = argparse.ArgumentParser(prog="cancelled-event run", description=__doc__)
    parser.add_argument("--stubhub-csv", default=matching.stubhub_csv)
    parser.add_argument("--ticketmaster-csv", default=matching.ticketmaster_csv)
    parser.add_argument("--output-dir", default="output/cancelled-workflow")
    parser.add_argument(
        "--source", choices=("event-page", "listings", "explore"), default=prices.source
    )
    parser.add_argument("--browser-executable", default=prices.browser_executable)
    parser.add_argument("--browser-wait-seconds", type=float, default=prices.browser_wait_seconds)
    parser.add_argument("--cities-csv", default=prices.cities_csv)
    parser.add_argument("--source-raw-dir", default=prices.source_raw_dir)
    parser.add_argument("--request-interval", type=float, default=prices.request_interval)
    parser.add_argument("--listing-page-size", type=int, default=prices.listing_page_size)
    parser.add_argument("--listing-max-pages", type=int, default=prices.listing_max_pages)
    args = parser.parse_args(argv)
    try:
        matching = replace(
            matching, stubhub_csv=args.stubhub_csv, ticketmaster_csv=args.ticketmaster_csv
        )
        prices = replace(
            prices,
            source=args.source,
            cities_csv=args.cities_csv,
            source_raw_dir=args.source_raw_dir,
            request_interval=args.request_interval,
            listing_page_size=args.listing_page_size,
            listing_max_pages=args.listing_max_pages,
            browser_executable=args.browser_executable,
            browser_wait_seconds=args.browser_wait_seconds,
        )
        configure_logging(prices.log_file, prices.log_level)
        return run(matching, prices, args.output_dir)
    except (OSError, ValueError, ResponseError) as exc:
        parser.exit(1, f"Cancelled-event workflow failed: {exc}\n")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
