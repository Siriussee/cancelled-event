#!/usr/bin/env python3
"""Tests for the Ticketmaster Discover cancelled-event scraper."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from cancelled_event.sources import ticketmaster_cancellations as tm


def discover_event(**overrides: object) -> dict:
    event = {
        "id": "0500000000000001",
        "discoveryId": "discovery-1",
        "title": "Example Cancelled Event",
        "url": "https://www.ticketmaster.com/example-austin-texas-10-04-2026/event/1",
        "dates": {"startDate": "2026-10-05T00:00:00Z"},
        "timeZone": "America/Chicago",
        "cancelled": True,
        "eventChangeStatus": "eventCancelled",
        "venue": {
            "name": "Example Hall",
            "city": "Austin",
            "state": "TX",
            "country": "US",
            "countryCode": "US",
            "countryName": "United States",
            "latitude": 30.2,
            "longitude": -97.7,
        },
    }
    event.update(overrides)
    return event


class TicketmasterCancelledEventScraperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._logger_disabled = tm.logger.disabled
        tm.logger.disabled = True

    def tearDown(self) -> None:
        tm.logger.disabled = self._logger_disabled

    def test_defaults_are_us_concerts_sports_30_days_and_two_seconds(self) -> None:
        config = tm.TicketmasterConfig(
            countries=("US",),
            categories=("concerts", "sports"),
            days=30,
            request_interval=2.0,
        )

        self.assertEqual(config.countries, ("US",))
        self.assertEqual(config.categories, ("concerts", "sports"))
        self.assertEqual(config.days, 30)
        self.assertEqual(config.request_interval, 2.0)

    def test_build_url_uses_country_category_and_single_date_slice(self) -> None:
        url = tm.build_events_url("US", "concerts", date(2026, 10, 4), 3)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertTrue(parsed.path.endswith("/KZFzniwnSyZfZ7v7nJ"))
        self.assertEqual(query["page"], ["3"])
        self.assertEqual(query["countryCodes"], ["US"])
        self.assertEqual(query["startDate"], ["2026-10-04"])
        self.assertEqual(query["endDate"], ["2026-10-04"])
        self.assertNotIn("status", query)
        self.assertNotIn("size", query)

    def test_date_window_is_30_dates_including_start(self) -> None:
        config = tm.TicketmasterConfig(start_date="2026-10-04", days=30)

        window = tm.date_window(config)

        self.assertEqual(window[0], date(2026, 10, 4))
        self.assertEqual(window[-1], date(2026, 11, 2))
        self.assertEqual(len(window), 30)

    def test_normalize_event_converts_utc_to_venue_local_date(self) -> None:
        row = tm.normalize_event(discover_event(), "US", "concerts", 4)

        self.assertEqual(row["id"], "discovery-1")
        self.assertEqual(row["tmId"], "0500000000000001")
        self.assertEqual(row["localDate"], "2026-10-04")
        self.assertEqual(row["statusCode"], "cancelled")
        self.assertEqual(row["venueName"], "Example Hall")
        self.assertEqual(row["venueCity"], "Austin")
        self.assertEqual(row["segmentName"], "Music")

    def test_collect_slice_paginates_and_filters_status_and_exact_local_date(self) -> None:
        calls: list[tuple[str, str, date, int]] = []
        cancelled = discover_event()
        active = discover_event(
            id="active",
            discoveryId="active-discovery",
            cancelled=False,
            eventChangeStatus="none",
        )
        outside_date = discover_event(
            id="outside",
            discoveryId="outside-discovery",
            dates={"startDate": "2026-10-06T00:00:00Z"},
        )

        def fake_fetch(
            country: str,
            category: str,
            local_date: date,
            page: int,
            config: tm.TicketmasterConfig,
        ) -> dict:
            calls.append((country, category, local_date, page))
            return {
                "total": 21,
                "events": [cancelled] + [dict(active, id=f"active-{i}") for i in range(19)]
                if page == 0
                else [outside_date],
            }

        rows = tm.collect_date_category(
            "US",
            "concerts",
            date(2026, 10, 4),
            tm.TicketmasterConfig(request_interval=0, save_raw_responses=False),
            fetch_page_func=fake_fetch,
        )

        self.assertEqual([call[3] for call in calls], [0, 1])
        self.assertEqual([row["tmId"] for row in rows], ["0500000000000001"])

    def test_slice_refuses_to_silently_cross_page_49_limit(self) -> None:
        def fake_fetch(
            country: str,
            category: str,
            local_date: date,
            page: int,
            config: tm.TicketmasterConfig,
        ) -> dict:
            return {"total": 981, "events": []}

        with self.assertRaises(tm.TicketmasterScrapeError):
            tm.collect_date_category(
                "US",
                "concerts",
                date(2026, 10, 4),
                tm.TicketmasterConfig(request_interval=0, save_raw_responses=False),
                fetch_page_func=fake_fetch,
            )

    def test_fetch_page_can_resume_from_valid_saved_raw_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = tm.TicketmasterConfig(
                raw_output_dir=tmpdir,
                resume_raw_responses=True,
                request_interval=0,
            )
            raw_path = tm._raw_response_path("US", "concerts", date(2026, 10, 4), 0, config)
            raw_path.write_text(
                json.dumps({"total": 1, "events": [discover_event()]}),
                encoding="utf-8",
            )

            response = tm.fetch_page("US", "concerts", date(2026, 10, 4), 0, config)

        self.assertEqual(response["total"], 1)
        self.assertEqual(response["events"][0]["id"], "0500000000000001")

    def test_environment_is_read_after_module_import_and_categories_are_normalized(self):
        with patch.dict(
            os.environ, {"TICKETMASTER_DAYS": "7", "TICKETMASTER_SAVE_RAW_RESPONSES": "false"}
        ):
            config = tm.TicketmasterConfig(categories=("SPORTS",), countries=("us",))
        self.assertEqual(config.days, 7)
        self.assertFalse(config.save_raw_responses)
        self.assertEqual(config.categories, ("sports",))
        self.assertEqual(config.countries, ("US",))

    def test_invalid_config_and_response_shapes_are_rejected(self):
        for settings in (
            {"countries": ()},
            {"countries": ("../us",)},
            {"request_interval": float("nan")},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                tm.TicketmasterConfig(**settings)
        for response in ([], {"events": [], "total": True}, {"events": [None], "total": 1}):
            with self.subTest(response=response), self.assertRaises(tm.TicketmasterScrapeError):
                tm._events_response(response)

    def test_missing_events_and_repeated_ids_fail_without_replacing_snapshot(self):
        for events in ([], [discover_event(), discover_event()]):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "events.csv"
                output.write_text("previous complete snapshot")
                config = tm.TicketmasterConfig(
                    output_csv=str(output),
                    categories=("concerts",),
                    days=1,
                    start_date="2026-10-04",
                )

                def fetch(*args, events=events):
                    return {"total": 2, "events": events}

                with self.assertRaises(tm.TicketmasterScrapeError):
                    tm.run(config, fetch_page_func=fetch)
                self.assertEqual(output.read_text(), "previous complete snapshot")

    def test_fetch_retries_json_errors_and_stops_on_final_failure(self):
        config = tm.TicketmasterConfig(
            max_retries=2, retry_delay=0, request_interval=0, save_raw_responses=False
        )
        with (
            patch.object(
                tm, "get_json", side_effect=tm.TicketmasterScrapeError("bad JSON")
            ) as fetch,
            patch.object(tm.time, "sleep"),
        ):
            with self.assertRaises(tm.TicketmasterScrapeError):
                tm.fetch_page("US", "concerts", date(2026, 10, 4), 0, config)
        self.assertEqual(fetch.call_count, 2)

    def test_malformed_cached_response_is_refetched(self):
        with tempfile.TemporaryDirectory() as directory:
            config = tm.TicketmasterConfig(
                raw_output_dir=directory, resume_raw_responses=True, request_interval=0
            )
            raw = tm._raw_response_path("US", "concerts", date(2026, 10, 4), 0, config)
            raw.write_text("[]")
            with patch.object(tm, "get_json", return_value={"total": 0, "events": []}) as fetch:
                self.assertEqual(
                    tm.fetch_page("US", "concerts", date(2026, 10, 4), 0, config)["total"], 0
                )
            fetch.assert_called_once()
            self.assertEqual(json.loads(raw.read_text())["total"], 0)

    def test_write_events_csv_writes_snapshot_with_header(self) -> None:
        rows = [{field: "" for field in tm.CSV_FIELDNAMES}]
        rows[0]["countryCode"] = "US"
        rows[0]["tmId"] = "evt-1"
        rows[0]["name"] = "Cancelled Event"
        rows[0]["statusCode"] = "cancelled"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "ticketmaster.csv"
            tm.write_events_csv(rows, output_file)
            with output_file.open(newline="", encoding="utf-8") as handle:
                written = list(csv.DictReader(handle))

        self.assertEqual(written[0]["countryCode"], "US")
        self.assertEqual(written[0]["tmId"], "evt-1")
        self.assertEqual(written[0]["statusCode"], "cancelled")

    def test_config_from_args_accepts_window_and_interval(self) -> None:
        args = tm._parse_args(
            [
                "--country",
                "US",
                "--category",
                "concerts",
                "--start-date",
                "2026-10-04",
                "--days",
                "7",
                "--request-interval",
                "2.5",
                "--max-pages-per-query",
                "2",
            ]
        )

        config = tm._config_from_args(args)

        self.assertEqual(config.countries, ("US",))
        self.assertEqual(config.categories, ("concerts",))
        self.assertEqual(config.start_date, "2026-10-04")
        self.assertEqual(config.days, 7)
        self.assertEqual(config.request_interval, 2.5)
        self.assertEqual(config.max_pages_per_query, 2)


if __name__ == "__main__":
    unittest.main()
