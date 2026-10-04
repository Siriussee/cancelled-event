"""Venue cache, collision and failure-reporting regressions."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from stubhub_all_event_scraper import venue_map_fetcher as venue
from stubhub_all_event_scraper.config import VenueFetcherConfig


class VenueMapTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(venue.logger, "disabled", True))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = VenueFetcherConfig(venue_dir=self.directory.name, wait_seconds=0)

    def test_ids_reject_signed_numbers_paths_and_whitespace(self):
        self.assertTrue(venue.validate_event_data("123", "2"))
        for identity in ("-1", "+1", "../1", " 1", None, "nan"):
            self.assertFalse(venue.validate_event_data(identity, "2"))

    def test_distinct_categories_never_overwrite_each_other(self):
        with patch.object(venue, "fetch_venue_map", side_effect=[{"category": 2}, {"category": 3}]):
            self.assertEqual(venue.process_event(("123", "2"), self.config), "saved")
            self.assertEqual(venue.process_event(("123", "3"), self.config), "saved")
        paths = sorted(Path(self.directory.name).glob("*.json"))
        self.assertEqual([json.loads(path.read_text())["category"] for path in paths], [2, 3])

    def test_invalid_cache_is_refetched_but_valid_cache_is_skipped(self):
        path = Path(self.directory.name) / "123_2_venue.json"
        for invalid in ("<html>", "{}", '{"error":"blocked"}', "[]"):
            path.write_text(invalid)
            with patch.object(venue, "fetch_venue_map", return_value={"seating": [1]}) as fetch:
                self.assertEqual(venue.process_event(("123", "2"), self.config), "saved")
                fetch.assert_called_once()
        with patch.object(venue, "fetch_venue_map") as fetch:
            self.assertEqual(venue.process_event(("123", "2"), self.config), "skipped")
        fetch.assert_not_called()

    def test_failed_refetch_preserves_cache_and_returns_nonzero(self):
        path = Path(self.directory.name) / "123_2_venue.json"
        path.write_text("bad cache")
        csv_path = Path(self.directory.name) / "events.csv"
        csv_path.write_text("eventId,categoryId\n123,2\n")
        config = replace(self.config, events_csv=str(csv_path))
        with patch.object(venue, "fetch_venue_map", side_effect=RuntimeError("blocked")):
            self.assertEqual(venue.run(config), 1)
        self.assertEqual(path.read_text(), "bad cache")

    def test_input_schema_and_pair_deduplication(self):
        path = Path(self.directory.name) / "events.csv"
        path.write_text("eventId,categoryId\n123,2\n123,2\n123,3\n-1,2\n")
        self.assertEqual(venue.load_unique_events(str(path)), {("123", "2"), ("123", "3")})
        path.write_text("eventId\n123\n")
        with self.assertRaises(ValueError):
            venue.load_unique_events(str(path))
