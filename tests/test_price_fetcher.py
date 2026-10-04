"""Price conversion, source recovery and live-refresh failure regressions."""

import csv
import json
import os
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from stubhub_all_event_scraper import http
from stubhub_all_event_scraper import price_fetcher as price
from stubhub_all_event_scraper.event_scraper import city_output_stem

CITY = {"name": "Test City", "country": "US", "lat": "1.2", "lng": "-3.4"}
CURRENCIES = [
    {"code": "CAD", "symbol": "C$", "extendedSymbol": "CA$", "currentRate": "1.6"},
    {"code": "USD", "symbol": "$", "extendedSymbol": "US$", "currentRate": "1.12"},
    {"code": "EUR", "symbol": "€", "currentRate": "1"},
]


def page(*events):
    return {"events": list(events), "total": len(events)}


class PriceFetcherTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(price.logger, "disabled", True))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = price.PriceConfig(
            input_csv=str(self.root / "matches.csv"),
            events_csv=str(self.root / "events.csv"),
            cities_csv=str(self.root / "cities.csv"),
            output_dir=str(self.root / "prices"),
            request_interval=0,
            max_retries=1,
            page_radius=1,
        )
        Path(self.config.cities_csv).write_text("country,name,lat,lng\nUS,Test City,1.2,-3.4\n")
        Path(self.config.events_csv).write_text(
            "eventId,city,country,page\n1,Test City,US,2\n2,Test City,US,2\n"
        )
        Path(self.config.input_csv).write_text("stubhub_eventId,ticketmaster_url\n1,tm-one\n")

    def sources(self, *ids):
        return {identity: {"city": CITY, "page": 2} for identity in ids}

    def refresh(self, ids, session, config=None, currencies=CURRENCIES):
        return price.refresh(
            set(ids), self.sources(*ids), config or self.config, session, self.root, currencies
        )

    def test_explicit_currency_and_decimal_conversion(self):
        for quote, amount, currency in (
            ("C$1,234.56", "1234.56", "CAD"),
            ("CA$20", "20", "CAD"),
            ("From USD 10.01", "10.01", "USD"),
            ("15.25 EUR", "15.25", "EUR"),
        ):
            self.assertEqual(price.parse_price(quote, CURRENCIES), (Decimal(amount), currency))
        self.assertEqual(price.to_usd(Decimal("10.05"), "CAD", CURRENCIES), Decimal("7.04"))
        self.assertEqual(price.to_usd(Decimal("20"), "USD", []), Decimal("20.00"))

    def test_ambiguous_symbols_and_invalid_amounts_are_unknown(self):
        for quote in ("$12", "C$0", "C$-5", "€1.234,56", "C$NaN", "C$10-20", ""):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                price.parse_price(quote, CURRENCIES)
        currencies = [{"code": "SEK", "symbol": "kr"}, {"code": "NOK", "symbol": "kr"}]
        with self.assertRaisesRegex(ValueError, "ambiguous_currency"):
            price.parse_price("kr 20", currencies)
        self.assertEqual(
            price.price_result({"formattedFromPrice": "$20"}, []),
            {"status": "ambiguous_currency", "usd": "", "currency": "", "quote": "$20"},
        )

    def test_missing_nonfinite_zero_and_negative_rates_are_errors(self):
        for currencies in (
            [],
            [{"code": "USD", "currentRate": "1"}],
            [{"code": "USD", "currentRate": "1"}, {"code": "CAD", "currentRate": "NaN"}],
            [{"code": "USD", "currentRate": "0"}, {"code": "CAD", "currentRate": "-1"}],
        ):
            result = price.price_result({"formattedFromPrice": "C$20"}, currencies)
            self.assertEqual(
                (result["status"], result["usd"], result["currency"]), ("fx_error", "", "CAD")
            )

    def test_listing_boolean_does_not_discard_valid_quote(self):
        session = Mock()
        session.fetch.return_value = page(
            {
                "eventId": 1,
                "formattedFromPrice": "C$20",
                "hasActiveListings": False,
                "allowPublicPurchase": False,
            }
        )
        checks, requests = self.refresh(["1"], session)
        self.assertEqual(checks["1"]["status"], "priced")
        self.assertEqual(checks["1"]["usd"], "14.00")
        self.assertFalse(checks["1"]["observations"][0]["hasActiveListings"])
        self.assertEqual(len(requests), 1)
        self.assertTrue(Path(requests[0]["raw_file"]).exists())

    def test_shared_page_is_fetched_once_and_stops_after_targets_found(self):
        session = Mock()
        session.fetch.return_value = page({"eventId": 1}, {"eventId": 2})
        checks, requests = self.refresh(["1", "2"], session)
        session.fetch.assert_called_once()
        self.assertEqual(len(requests), 1)
        self.assertEqual([checks[key]["status"] for key in ("1", "2")], ["no_quote"] * 2)

    def test_page_drift_and_no_match_are_bounded(self):
        session = Mock()
        session.fetch.side_effect = lambda url: (
            page({"eventId": 1}) if (parse_qs(urlsplit(url).query)["page"] == ["3"]) else page()
        )
        checks, requests = self.refresh(["1"], session)
        self.assertEqual(checks["1"]["status"], "no_quote")
        self.assertEqual({request["page"] for request in requests}, {1, 2, 3})
        session.fetch.side_effect = None
        session.fetch.return_value = page({"eventId": 99})
        checks, requests = self.refresh(["1"], session)
        self.assertEqual(checks["1"]["status"], "not_found")
        self.assertEqual(len(requests), 3)

    def test_empty_page_with_nonzero_total_is_valid_for_bounded_lookup(self):
        checks, requests = self.refresh(
            ["1"], Mock(fetch=Mock(return_value={"events": [], "total": 32}))
        )
        self.assertEqual(checks["1"]["status"], "not_found")
        self.assertEqual(len(requests), 3)

    def test_pipeline_environment_paths_are_reused(self):
        with patch.dict(
            os.environ,
            {
                "PRICE_INPUT_CSV": "",
                "STUBHUB_EVENTS_CSV": "",
                "EVENTS_CSV": "sh.csv",
                "CANCELLED_EVENT_INTERSECTION_CSV": "matched.csv",
            },
        ):
            config = price.PriceConfig()
        self.assertEqual(config.events_csv, "sh.csv")
        self.assertEqual(config.input_csv, "matched.csv")

    def test_failed_or_malformed_page_cannot_be_reported_as_not_found(self):
        for response in ({"events": []}, {"events": [None]}, {"events": [{}]}):
            checks, _ = self.refresh(["1"], Mock(fetch=Mock(return_value=response)))
            self.assertEqual(checks["1"]["status"], "request_error")
        session = Mock()
        session.fetch.side_effect = [http.ResponseError("HTTP 403"), page(), page()]
        checks, requests = self.refresh(["1"], session)
        self.assertEqual(checks["1"]["status"], "request_error")
        self.assertIn("HTTP 403", requests[0]["error"])

    def test_missing_source_and_invalid_ids(self):
        checks, requests = price.refresh({"1"}, {}, self.config, Mock(), self.root, [])
        self.assertEqual(checks["1"]["status"], "source_missing")
        self.assertEqual(requests, [])
        for identity in ("../1", "-1", "", "nan"):
            with self.assertRaises(ValueError):
                price.event_id({"SH Event ID": identity})
        self.assertEqual(
            price.event_id({"StubHub URL": "https://www.stubhub.com/event/123/"}), "123"
        )

    def test_raw_lookup_recovers_actual_seed_signs_and_lowest_source_page(self):
        raw = self.root / "source"
        raw.mkdir()
        for stem in (price.legacy_stem(CITY), city_output_stem("Test City", "US", "1.2", "-3.4")):
            for index in (2, 3):
                (raw / f"{stem}_p{index}.json").write_text(json.dumps(page({"eventId": 1})))
        sources = price.locate_sources({"1"}, replace(self.config, source_raw_dir=str(raw)))
        self.assertEqual(sources["1"]["city"]["lng"], "-3.4")
        self.assertEqual(sources["1"]["page"], 2)
        Path(self.config.cities_csv).write_text(
            "country,name,lat,lng\nUS,Test City,1.2,-3.4\nUS,Test City,1.2,3.4\n"
        )
        with self.assertRaisesRegex(ValueError, "Ambiguous seed"):
            price.locate_sources({"1"}, replace(self.config, source_raw_dir=str(raw)))

    def test_csv_lookup_rejects_ambiguous_city_and_missing_columns(self):
        self.assertEqual(price.locate_sources({"1"}, self.config)["1"]["page"], 2)
        Path(self.config.cities_csv).write_text(
            "country,name,lat,lng\nUS,Test City,1.2,-3.4\nUS,Test City,5,6\n"
        )
        with self.assertRaisesRegex(ValueError, "Ambiguous query city"):
            price.locate_sources({"1"}, self.config)
        Path(self.config.events_csv).write_text("eventId\n1\n")
        with self.assertRaises(ValueError):
            price.locate_sources({"1"}, self.config)

    def test_preserves_rows_links_and_updates_existing_price_column(self):
        path = Path(self.config.input_csv)
        original = "SH Event ID,Ticketmaster URL,Price (USD)\n1,tm-one,100\n1,tm-two,100\n"
        path.write_text(original)
        session = Mock()
        session.fetch.side_effect = [
            {"currencies": CURRENCIES},
            page({"eventId": 1, "formattedFromPrice": "US$20"}),
        ]
        with patch.object(price, "PriceSession", return_value=session):
            self.assertEqual(price.run(self.config), 0)
        output = Path(self.config.output_dir)
        with (output / "enriched.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["Ticketmaster URL"] for row in rows], ["tm-one", "tm-two"])
        self.assertEqual([row["Price (USD)"] for row in rows], ["20.00", "20.00"])
        report = json.loads((output / "checks.json").read_text())
        self.assertEqual(report["status_counts"], {"priced": 1})
        self.assertEqual(len(report["requests"]), 1)
        self.assertIn("sha256", report["inputs"]["matches"])
        self.assertEqual(path.read_text(), original)
        with self.assertRaisesRegex(ValueError, "must be empty"):
            price.run(self.config)

    def test_fresh_rate_failure_retains_observation_and_nonzero_status(self):
        session = Mock()
        session.fetch.side_effect = [
            http.ResponseError("settings blocked"),
            page({"eventId": 1, "formattedFromPrice": "C$20"}),
        ]
        with patch.object(price, "PriceSession", return_value=session):
            self.assertEqual(price.run(self.config), 1)
        report = json.loads((Path(self.config.output_dir) / "checks.json").read_text())
        self.assertEqual(report["checks"]["1"]["status"], "fx_error")
        self.assertEqual(report["checks"]["1"]["quote"], "C$20")
        self.assertEqual(report["settings_error"], "settings blocked")

    def test_negative_or_nonfinite_limits_fail_early(self):
        for settings in (
            {"page_radius": -1},
            {"request_interval": float("nan")},
            {"request_interval": -1},
        ):
            with self.assertRaises(ValueError):
                replace(self.config, **settings)

    def test_transport_shares_cookies_and_paces_every_retry(self):
        result = Mock(returncode=0, stdout='{"events": [], "total": 0}')
        pace = Mock()
        cookie_file = self.root / "cookies"
        with (
            patch.object(
                http.subprocess, "run", side_effect=[Mock(returncode=22, stderr="HTTP 403"), result]
            ) as transport,
            patch.object(http.time, "sleep"),
        ):
            http.get_json(
                "https://www.stubhub.com/test",
                replace(self.config, max_retries=2),
                cookie_file=cookie_file,
                before_request=pace,
            )
        self.assertEqual(pace.call_count, 2)
        command = transport.call_args.args[0]
        self.assertEqual(command[command.index("--cookie") + 1], str(cookie_file))
        self.assertEqual(command[command.index("--cookie-jar") + 1], str(cookie_file))


if __name__ == "__main__":
    unittest.main()
