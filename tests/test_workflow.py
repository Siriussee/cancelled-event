"""Exercise cancellation matching and price publication together, without network access."""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from test_matching import stubhub_row, ticketmaster_row

from cancelled_event import workflow as workflow
from cancelled_event.http import ResponseError
from cancelled_event.matching import MatchingConfig
from cancelled_event.pricing import refresh as price
from cancelled_event.runtime import write_csv


class CancelledWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "run"
        self.intersection = MatchingConfig(
            stubhub_csv=str(self.root / "sh.csv"),
            ticketmaster_csv=str(self.root / "tm.csv"),
        )
        self.prices = price.PriceConfig(source="listings", request_interval=0, max_retries=1)
        sh = stubhub_row(eventId="160906070")
        tm = ticketmaster_row()
        write_csv(self.intersection.stubhub_csv, [sh], list(sh))
        write_csv(self.intersection.ticketmaster_csv, [tm], list(tm))
        self.enterContext(patch.object(price.logger, "disabled", True))

    def test_match_flows_into_listing_prices_and_retains_source_links(self):
        response = {
            "Items": [{"Id": 11, "RawPrice": 21.25, "CurrencyCode": "USD"}],
            "TotalCount": 1,
            "NumPages": 1,
        }
        session = Mock(fetch=Mock(side_effect=[{"currencies": [{"code": "USD"}]}, response]))
        with patch.object(price, "PriceSession", return_value=session):
            self.assertEqual(workflow.run(self.intersection, self.prices, self.output), 0)
        with (self.output / "prices" / "enriched.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Price (USD)"], "21.25")
        self.assertEqual(rows[0]["stubhub_url"], stubhub_row()["url"])
        self.assertEqual(rows[0]["ticketmaster_url"], ticketmaster_row()["url"])
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["stages"]["matching"]["unique_events"], 1)
        self.assertEqual(manifest["stages"]["prices"]["rows"], 1)
        with self.assertRaisesRegex(ValueError, "must be empty"):
            workflow.run(self.intersection, self.prices, self.output)

    def test_blocked_price_preserves_matches_and_reports_needs_attention(self):
        session = Mock(
            fetch=Mock(side_effect=[{"currencies": [{"code": "USD"}]}, ResponseError("HTTP 403")])
        )
        with patch.object(price, "PriceSession", return_value=session):
            self.assertEqual(workflow.run(self.intersection, self.prices, self.output), 1)
        self.assertTrue((self.output / "matches.csv").exists())
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "needs_attention")
        self.assertEqual(manifest["stages"]["matching"]["status"], "complete")
        checks = json.loads((self.output / "prices" / "checks.json").read_text())
        self.assertEqual(checks["status_counts"], {"request_error": 1})

    def test_empty_match_set_publishes_headers_without_network_requests(self):
        row = ticketmaster_row(statusCode="onsale")
        write_csv(self.intersection.ticketmaster_csv, [row], list(row))
        session = Mock()
        with patch.object(price, "PriceSession", return_value=session):
            self.assertEqual(workflow.run(self.intersection, self.prices, self.output), 0)
        session.fetch.assert_not_called()
        with (self.output / "prices" / "enriched.csv").open() as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])

    def test_matching_failure_publishes_failure_manifest_without_price_requests(self):
        intersection = replace(self.intersection, ticketmaster_csv=str(self.root / "missing.csv"))
        with patch.object(price, "PriceSession") as session:
            with self.assertRaises(OSError):
                workflow.run(intersection, self.prices, self.output)
        session.assert_not_called()
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "failed")


if __name__ == "__main__":
    unittest.main()
