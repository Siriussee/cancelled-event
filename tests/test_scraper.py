"""Regression checks for resumable StubHub collection and response validation."""

import csv
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from stubhub_all_event_scraper import event_scraper as scraper
from stubhub_all_event_scraper.config import ScraperConfig

CITY = {"name": "Test City", "country": "US", "lat": "1.2", "lng": "-3.4"}


def page(*ids):
    return {
        "events": [{"eventId": identity, "name": f"Event {identity}"} for identity in ids],
        "total": 100,
    }


class EventScraperTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(scraper.logger, "disabled", True))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = ScraperConfig(
            input_csv=str(root / "cities.csv"),
            combined_csv=str(root / "events.csv"),
            progress_log=str(root / "progress.log"),
            out_dir=str(root / "raw"),
            wait_seconds=0,
            max_retries=1,
            max_pages_per_city=5,
        )
        Path(self.config.input_csv).write_text("country,name,lat,lng\nUS,Test City,1.2,-3.4\n")

    def rows(self):
        with open(self.config.combined_csv, newline="") as handle:
            return list(csv.DictReader(handle))

    def test_url_encodes_coordinates_and_sort_type(self):
        config = replace(self.config, explore_sort_type="Distance & Date")
        query = parse_qs(urlsplit(scraper.build_explore_url("1.2", "-3.4", 2, config)).query)
        self.assertEqual(query["lon"], [scraper.b64("-3.4")])
        self.assertEqual(query["sortType"], ["Distance & Date"])
        self.assertEqual(query["page"], ["2"])

    def test_response_validation_never_treats_missing_total_as_completion(self):
        for response in (
            {},
            [],
            {"events": []},
            {"events": [], "total": False},
            {"events": [], "total": 9},
            {"events": [None]},
            {"events": [{}]},
        ):
            with self.subTest(response=response), self.assertRaises(scraper.ScrapeResponseError):
                scraper.parse_events(response, "City", "US", 0)
        self.assertIsNone(scraper.parse_events({"events": [], "total": 0}, "City", "US", 0))

    def test_preserves_metadata_and_stable_csv_schema(self):
        response = page(123)
        response["events"][0].update(venueId=4, hasActiveListings=False, formattedTime="7 PM")
        rows = scraper.parse_events(response, "City", "US", 0)
        self.assertEqual(list(rows[0]), scraper.CSV_FIELDNAMES)
        self.assertIs(rows[0]["hasActiveListings"], False)
        self.assertEqual(rows[0]["formattedTime"], "7 PM")

    def test_coordinates_must_be_finite_and_geographically_valid(self):
        for lat, lon in [("nan", "0"), ("91", "0"), ("0", "181"), ("inf", "0")]:
            self.assertFalse(scraper.validate_city_data({**CITY, "lat": lat, "lng": lon}))
        self.assertTrue(scraper.validate_city_data(CITY))

    def test_duplicate_input_coordinates_are_scheduled_once(self):
        with open(self.config.input_csv, "a") as handle:
            handle.write("US,Test City,1.2,-3.4\n")
        self.assertEqual(len(scraper.load_cities(self.config.input_csv)), 1)

    def test_wrong_city_schema_is_rejected(self):
        Path(self.config.input_csv).write_text("country,name,lat,long\nUS,Test City,1.2,-3.4\n")
        with self.assertRaises(ValueError):
            scraper.load_cities(self.config.input_csv)

    def test_raw_names_distinguish_coordinate_signs_and_non_ascii_names(self):
        negative = scraper.city_output_stem("City / Test", "US", "1.2", "-3.4")
        positive = scraper.city_output_stem("City / Test", "US", "1.2", "3.4")
        self.assertNotEqual(negative, positive)
        self.assertNotIn("/", negative)
        self.assertNotEqual(
            scraper.city_output_stem("北京", "CN", "1", "2"),
            scraper.city_output_stem("上海", "CN", "1", "2"),
        )

    def test_deduplicates_pages_and_records_completion(self):
        with patch.object(
            scraper, "get_json", side_effect=[page(1, 2, 3, 4), page(2, 3, 4, 5)]
        ) as fetch:
            count = scraper.scrape_city(CITY, {}, self.config)
        self.assertEqual(count, 5)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([row["eventId"] for row in self.rows()], ["1", "2", "3", "4", "5"])
        self.assertEqual(
            scraper.load_progress(self.config.progress_log)[("1.2", "-3.4")], scraper.COMPLETED
        )

    def test_crash_between_csv_and_checkpoint_does_not_duplicate_or_stop_early(self):
        scraper.save_events(
            scraper.parse_events(page(1), "Test City", "US", 0), self.config.combined_csv
        )
        saved = scraper.load_saved_identities(self.config.combined_csv)
        with patch.object(
            scraper, "get_json", side_effect=[page(1), page(2), {"events": [], "total": 0}]
        ) as fetch:
            scraper.scrape_city(CITY, {}, self.config, saved)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([row["eventId"] for row in self.rows()], ["1", "2"])

    def test_completed_city_is_not_requested_again(self):
        with patch.object(scraper, "get_json") as fetch:
            self.assertEqual(
                scraper.scrape_city(CITY, {("1.2", "-3.4"): scraper.COMPLETED}, self.config), 0
            )
        fetch.assert_not_called()

    def test_page_limit_is_enforced(self):
        config = replace(self.config, max_pages_per_city=2)
        with patch.object(scraper, "get_json", side_effect=[page(1), page(2)]) as fetch:
            scraper.scrape_city(CITY, {}, config)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(scraper.load_progress(config.progress_log)[("1.2", "-3.4")], 2)

    def test_fetch_failure_keeps_last_committed_page(self):
        with patch.object(scraper, "get_json", side_effect=[page(1), RuntimeError("blocked")]):
            with self.assertRaises(RuntimeError):
                scraper.scrape_city(CITY, {}, self.config)
        self.assertEqual(scraper.load_progress(self.config.progress_log)[("1.2", "-3.4")], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_partial_failure_returns_nonzero_status(self):
        with patch.object(scraper, "get_json", side_effect=RuntimeError("blocked")):
            self.assertEqual(scraper.run(self.config), 1)

    def test_missing_output_does_not_skip_based_on_stale_checkpoint(self):
        scraper.update_progress("1.2", "-3.4", scraper.COMPLETED, self.config.progress_log)
        with patch.object(scraper, "get_json", return_value={"events": [], "total": 0}) as fetch:
            self.assertEqual(scraper.run(self.config), 0)
        fetch.assert_called_once()

    def test_partial_csv_is_rejected_before_requests(self):
        scraper.save_events(
            scraper.parse_events(page(1), "Test City", "US", 0), self.config.combined_csv
        )
        with open(self.config.combined_csv, "a") as handle:
            handle.write("Test City,US,0,2")
        with patch.object(scraper, "get_json") as fetch, self.assertRaises(ValueError):
            scraper.run(self.config)
        fetch.assert_not_called()

    def test_empty_existing_csv_receives_a_header(self):
        Path(self.config.combined_csv).touch()
        scraper.save_events(
            scraper.parse_events(page(1), "Test City", "US", 0), self.config.combined_csv
        )
        self.assertEqual(self.rows()[0]["eventId"], "1")

    def test_concurrent_csv_appends_keep_one_header_and_all_rows(self):
        def append(identity):
            rows = scraper.parse_events(page(identity), "City", "US", 0)
            scraper.save_events(rows, self.config.combined_csv)

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(append, range(1, 26)))
        rows = self.rows()
        self.assertEqual(len(rows), 25)
        self.assertEqual({row["eventId"] for row in rows}, {str(i) for i in range(1, 26)})

    def test_checkpoint_order_never_regresses(self):
        Path(self.config.progress_log).write_text("1.2,-3.4,8\ninvalid\n1.2,-3.4,2\n")
        self.assertEqual(scraper.load_progress(self.config.progress_log)[("1.2", "-3.4")], 8)
