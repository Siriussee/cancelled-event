"""Listing floors must cover the whole event, with explicit currency and evidence."""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from stubhub_all_event_scraper import http, listing_prices
from stubhub_all_event_scraper import price_fetcher as price

CURRENCIES = [{"code": "USD", "currentRate": "1"}, {"code": "CAD", "currentRate": "1.4"}]


def grid(*items, total=None, pages=1, **extra):
    return {
        "Items": list(items),
        "TotalCount": len(items) if total is None else total,
        "NumPages": pages,
        **extra,
    }


def ticket(identity, amount, currency="USD"):
    return {"Id": identity, "EventId": 1, "RawPrice": amount, "CurrencyCode": currency}


class ListingPriceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = price.PriceConfig(
            input_csv=str(self.root / "matches.csv"),
            # Listing lookups must work without any discovery artifacts.
            events_csv=str(self.root / "missing-events.csv"),
            cities_csv=str(self.root / "missing-cities.csv"),
            output_dir=str(self.root / "prices"),
            request_interval=0,
            listing_page_size=2,
            max_retries=1,
            source="listings",
        )
        self.rows = [{"stubhub_eventId": "1", "stubhub_url": "https://www.stubhub.com/event/1/"}]
        self.enterContext(patch.object(listing_prices.logger, "disabled", True))
        self.enterContext(patch.object(price.logger, "disabled", True))

    def refresh(self, responses, config=None, rows=None, currencies=CURRENCIES):
        session = Mock(fetch=Mock(side_effect=responses))
        checks, requests = listing_prices.refresh_listings(
            rows or self.rows, config or self.config, session, self.root, currencies
        )
        return checks["1"], requests, session

    def test_floor_uses_all_pages_not_first_sorted_or_recommended_item(self):
        result, requests, session = self.refresh(
            [
                grid(ticket(11, 80), ticket(12, 50), total=3, pages=2),
                grid(ticket(13, 14, "CAD"), total=3, pages=2),
            ]
        )
        self.assertEqual(
            (result["status"], result["usd"], result["listing_id"]), ("priced", "10.00", "13")
        )
        self.assertEqual(result["listing_count"], 3)
        self.assertEqual(session.fetch.call_count, 2)
        self.assertEqual([r["body"]["CurrentPage"] for r in requests], [1, 2])

    def test_only_explicit_empty_inventory_is_no_listings(self):
        for payload in (grid(pages=0), grid(pages=1), {"items": [], "numPages": 0}):
            with self.subTest(payload=payload):
                result, _, _ = self.refresh([payload])
                self.assertEqual(
                    (result["status"], result["usd"], result["listing_count"]),
                    ("no_listings", "", 0),
                )
        for payload in (
            {"Items": []},
            {"Items": [], "NumPages": 1},
            grid(total=4, pages=2),
            {"Items": [], "error": "blocked"},
        ):
            with self.subTest(payload=payload):
                result, _, _ = self.refresh([payload])
                self.assertEqual(result["status"], "request_error")

    def test_partial_pages_never_publish_candidate_floor(self):
        first = grid(ticket(11, 20), total=2, pages=2)
        for responses, config, expected in (
            ([first, http.ResponseError("HTTP 403")], self.config, "request_error"),
            ([first], replace(self.config, listing_max_pages=1), "incomplete"),
            ([first, grid(ticket(11, 20), total=2, pages=2)], self.config, "request_error"),
            ([first, grid(ticket(12, 10), total=3, pages=2)], self.config, "request_error"),
            ([grid(ticket(11, 20), total=2, pages=1)], self.config, "request_error"),
        ):
            with self.subTest(expected=expected, responses=responses):
                result, _, _ = self.refresh(responses, config)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["usd"], "")
                self.assertEqual(result["listing_id"], "")

    def test_unknown_price_or_currency_cannot_be_excluded_from_minimum(self):
        for item, expected in (
            ({"Id": 12, "Price": "$1"}, "ambiguous_currency"),
            (ticket(12, "NaN"), "invalid_price"),
            (ticket(12, 0), "invalid_price"),
            (ticket(12, True), "invalid_price"),
            ({"Id": 12}, "invalid_price"),
            (ticket(12, 1, "XXX"), "fx_error"),
        ):
            with self.subTest(item=item):
                result, _, _ = self.refresh([grid(ticket(11, 20), item)])
                self.assertEqual((result["status"], result["usd"]), (expected, ""))

    def test_camel_case_and_explicit_dollar_currency(self):
        result, _, _ = self.refresh(
            [{"items": [{"id": 11, "price": "$23.45"}], "totalCount": 1, "currencyCode": "USD"}]
        )
        self.assertEqual((result["status"], result["usd"]), ("priced", "23.45"))

    def test_minimum_listing_is_selected_before_rounding_to_cents(self):
        result, _, _ = self.refresh([grid(ticket(11, "20.234"), ticket(12, "20.231"))])
        self.assertEqual((result["usd"], result["listing_id"]), ("20.23", "12"))
        self.assertEqual(result["quote"], "USD 20.231")

    def test_contradictory_quote_and_currency_is_unknown(self):
        result, _, _ = self.refresh([grid({**ticket(11, 20, "CAD"), "Price": "US$20"})])
        self.assertEqual((result["status"], result["usd"]), ("ambiguous_currency", ""))

    def test_schema_and_event_identity_failures_are_unknown(self):
        for payload in (
            grid({**ticket(11, 20), "EventId": 2}),
            grid(ticket(11, 20), CurrentPage=2),
            grid(ticket(11, 20), totalCount=5),
            grid(ticket(11, 20), PageSize=0),
            grid(ticket(11, 20), total=True),
            grid({**ticket(11, 20), "Id": None}),
        ):
            with self.subTest(payload=payload):
                result, _, _ = self.refresh([payload])
                self.assertEqual(result["status"], "request_error")
                self.assertEqual(result["usd"], "")

    def test_all_rows_and_links_preserved_duplicate_event_fetched_once(self):
        original = "SH Event ID,Ticketmaster URL,Price (USD)\n1,tm-one,100\n1,tm-two,100\n"
        Path(self.config.input_csv).write_text(original)
        session = Mock(fetch=Mock(side_effect=[{"currencies": CURRENCIES}, grid(ticket(11, 25))]))
        with patch.object(price, "PriceSession", return_value=session):
            self.assertEqual(price.run(self.config), 0)
        with (Path(self.config.output_dir) / "enriched.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["Ticketmaster URL"] for row in rows], ["tm-one", "tm-two"])
        self.assertEqual([row["Price (USD)"] for row in rows], ["25.00"] * 2)
        self.assertEqual(rows[0]["SH From Price"], "")
        self.assertEqual(rows[0]["SH Floor Price"], "USD 25")
        self.assertEqual(rows[0]["Price Source"], "listing_grid")
        self.assertEqual(session.fetch.call_count, 2)
        self.assertEqual(Path(self.config.input_csv).read_text(), original)
        report = json.loads((Path(self.config.output_dir) / "checks.json").read_text())
        self.assertEqual(set(report["inputs"]), {"matches"})

    def test_urls_are_validated_before_any_listing_requests(self):
        for url in (
            "https://evil.example/event/1/",
            "https://www.stubhub.com/event/2/",
            "http://www.stubhub.com/event/1/",
            "https://user@www.stubhub.com/event/1/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.refresh([], rows=[{"eventId": "1", "url": url}])

    def test_http_failure_body_is_retained_and_json_is_posted(self):
        raw = self.root / "failed.json"
        failed = Mock(returncode=22, stdout='{"challenge":"blocked"}', stderr="HTTP 403")
        with patch.object(http.subprocess, "run", return_value=failed) as transport:
            with self.assertRaisesRegex(http.ResponseError, "403"):
                http.get_json(
                    "https://www.stubhub.com/event/1/",
                    self.config,
                    json_data={"CurrentPage": 1},
                    response_path=raw,
                )
        self.assertEqual(json.loads(raw.read_text()), {"challenge": "blocked"})
        command = transport.call_args.args[0]
        self.assertIn("Content-Type: application/json", command)
        self.assertEqual(json.loads(command[command.index("--data") + 1]), {"CurrentPage": 1})


if __name__ == "__main__":
    unittest.main()
