"""Regression tests for country/distance matching and resumable city audits."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from stubhub_all_event_scraper import filter_worldcities_by_geonames_feature as cities

ROW = {"name": "Austin", "country": "US", "lat": "30.27", "lng": "-97.74"}


def candidate(**overrides):
    result = {
        "name": "Austin",
        "country_code": "US",
        "latitude": 30.27,
        "longitude": -97.74,
        "feature_code": "PPLA",
    }
    result.update(overrides)
    return result


class CityFilterTests(unittest.TestCase):
    def test_country_and_distance_prevent_wrong_same_name_match(self):
        for result in (
            candidate(country_code="CA"),
            candidate(latitude=45),
            candidate(latitude=float("nan")),
        ):
            self.assertIsNone(cities.select_candidate(ROW, [result])[0])
        self.assertEqual(cities.select_candidate(ROW, [candidate()])[0]["name"], "Austin")

    def test_non_ascii_city_names_remain_distinct(self):
        self.assertNotEqual(cities.normalize_name("北京"), cities.normalize_name("上海"))
        self.assertEqual(cities.normalize_name("Montréal"), "montreal")

    def test_retry_recovery_does_not_keep_old_error(self):
        with (
            patch.object(
                cities, "fetch_candidates", side_effect=[URLError("timeout"), [candidate()]]
            ),
            patch.object(cities.time, "sleep"),
        ):
            result = cities.audit_row(ROW, count=5, timeout=1, retries=2, retry_delay=0)
        self.assertEqual(result["decision"], "keep")
        self.assertEqual(result["error"], "")

    def test_failed_audit_is_not_reused_as_valid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.csv"
            cities.append_audit(path, {**ROW, "status": "error", "decision": "drop"})
            self.assertEqual(cities.load_audit(path), {})

    def test_nested_outputs_and_foreign_cached_rows_are_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, output, audit = (
                root / "input.csv",
                root / "nested" / "output.csv",
                root / "nested" / "audit.csv",
            )
            input_path.write_text("name,country,lat,lng\nAustin,US,30.27,-97.74\n")
            cities.append_audit(
                audit, {**ROW, "name": "Old City", "status": "matched", "decision": "keep"}
            )
            with (
                patch.object(cities, "fetch_candidates", return_value=[candidate()]) as fetch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cities.main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(output),
                        "--audit",
                        str(audit),
                        "--delay",
                        "0",
                    ]
                )
            self.assertEqual(result, 0)
            fetch.assert_called_once()
            with output.open(newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)

    def test_errors_produce_audit_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "input.csv"
            path.write_text("name,country,lat,lng\nAustin,US,30.27,-97.74\n")
            audit = root / "audit.csv"
            with (
                patch.object(cities, "fetch_candidates", side_effect=URLError("blocked")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cities.main(
                    [
                        "--input",
                        str(path),
                        "--output",
                        str(root / "out.csv"),
                        "--audit",
                        str(audit),
                        "--retries",
                        "1",
                        "--delay",
                        "0",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertIn("error", audit.read_text())

    def test_invalid_arguments_fail_before_network_io(self):
        for arguments in (["--retries", "0"], ["--progress-every", "0"], ["--delay", "nan"]):
            with (
                self.subTest(arguments=arguments),
                patch.object(cities, "fetch_candidates") as fetch,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cities.main(arguments)
            fetch.assert_not_called()
