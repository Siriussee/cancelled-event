"""Exercise the installed command boundary and a complete offline browser workflow."""

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_matching import stubhub_row, ticketmaster_row

from cancelled_event import cli
from cancelled_event.pricing import refresh
from cancelled_event.runtime import write_csv

COMMANDS = (
    (),
    ("collect",),
    ("collect", "stubhub"),
    ("collect", "ticketmaster"),
    ("match",),
    ("prices",),
    ("run",),
    ("venues",),
    ("filter-cities",),
    ("verify-ticketmaster",),
)


class CLITests(unittest.TestCase):
    def test_all_help_routes_work_without_browser_or_runtime_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in COMMANDS:
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-m", "cancelled_event", *command, "--help"],
                        cwd=directory,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage: cancelled-event", result.stdout)
                    self.assertIn("--help", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_unknown_stage_option_is_rejected(self):
        result = subprocess.run(
            [sys.executable, "-m", "cancelled_event", "match", "--not-a-real-option"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_run_matches_snapshots_and_parses_direct_usd_browser_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sh = stubhub_row(
                eventId="161331875",
                name="Chad Gray",
                city="Los Angeles",
                venueName="The Bellwether",
                formattedVenueLocation="Los Angeles, CA, USA",
                url="https://www.stubhub.com/chad-gray-los-angeles-tickets-10-4-2026/event/161331875/",
            )
            tm = ticketmaster_row(
                name="Chad Gray",
                localDate="2026-10-04",
                venueName="The Bellwether",
                venueCity="Los Angeles",
                venueStateCode="CA",
                venueStateName="California",
            )
            write_csv(root / "sh.csv", [sh], list(sh))
            write_csv(root / "tm.csv", [tm], list(tm))
            original = (root / "sh.csv").read_bytes()
            html = (Path(__file__).parent / "fixtures/chad_usd_event_page.html").read_text()
            session = Mock(warmup=[])

            def fetch_document(url, raw):
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_text(html)
                return html

            session.fetch_document.side_effect = fetch_document
            with (
                patch.dict(os.environ, {"PRICE_LOG_FILE": str(root / "prices.log")}),
                patch.object(refresh, "BrowserPriceSession", return_value=session),
                patch("cancelled_event.workflow.configure_logging"),
            ):
                status = cli.main(
                    [
                        "run",
                        "--stubhub-csv",
                        str(root / "sh.csv"),
                        "--ticketmaster-csv",
                        str(root / "tm.csv"),
                        "--output-dir",
                        str(root / "run"),
                    ]
                )
            self.assertEqual(status, 0)
            output = root / "run"
            self.assertTrue((output / "matches.csv").is_file())
            with (output / "prices/enriched.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Price (USD)"], "113.97")
            self.assertEqual(rows[0]["Price Status"], "priced")
            self.assertEqual(rows[0]["stubhub_url"], sh["url"])
            self.assertEqual(rows[0]["ticketmaster_url"], tm["url"])
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertTrue(manifest["stages"]["matching"]["output"].endswith("matches.csv"))
            session.fetch_document.assert_called_once()
            self.assertIn("currency=USD", session.fetch_document.call_args.args[0])
            session.close.assert_called_once()
            self.assertEqual((root / "sh.csv").read_bytes(), original)
