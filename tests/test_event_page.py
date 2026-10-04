"""First-page floor extraction using sanitized October 4 live document captures."""

import csv
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from cancelled_event.http import ResponseError
from cancelled_event.pricing import event_page as pages
from cancelled_event.pricing import refresh as price

FIXTURES = Path(__file__).parent / "fixtures"
CURRENCIES = [
    {"code": "CAD", "currentRate": "1.603729000"},
    {"code": "USD", "currentRate": "1.125700000"},
]
CHAD_ID = "161331875"
CHAD_URL = "https://www.stubhub.com/chad-gray-los-angeles-tickets-10-4-2026/event/161331875/"


def capture(name="chad"):
    return (FIXTURES / f"{name}_event_page.html").read_text()


class EventPageTests(unittest.TestCase):
    def setUp(self):
        self.grid = pages.document_grid(capture(), CHAD_ID)
        self.enterContext(patch.object(pages.logger, "disabled", True))

    def test_real_chad_floor_preserves_cents_and_uses_buyer_currency(self):
        self.assertEqual(self.grid["items"][0]["listingCurrencyCode"], "USD")
        self.assertEqual(self.grid["items"][0]["price"], "C$162")
        result = pages.grid_result(self.grid, CHAD_ID, CURRENCIES)
        self.assertEqual(result["status"], "priced")
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["quote"], "CAD 162.37")
        self.assertEqual(result["usd"], "113.97")
        self.assertEqual(result["listing_id"], "13367389979")

    def test_partial_real_control_is_corroborated_without_reading_more_pages(self):
        grid = pages.document_grid(capture("control"), "161659240")
        result = pages.grid_result(grid, "161659240", CURRENCIES)
        self.assertEqual(result["status"], "priced")
        self.assertEqual(result["scope"], "event (page 1 matches declared minimum)")
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual((result["observed_count"], result["grid_count"]), (10, 311))
        self.assertEqual(result["listing_count"], 3598)
        self.assertEqual(result["quote"], "CAD 47.33")
        # The real minimum is a one-ticket listing; default quantity=2 would miss it.
        self.assertEqual(grid["items"][0]["availableQuantities"], [1])

    def test_partial_without_corroboration_exposes_only_page_minimum(self):
        for minimum in (None, 1, "NaN", True):
            with self.subTest(minimum=minimum):
                grid = {
                    **self.grid,
                    "totalCount": 3,
                    "totalListingsCount": 3,
                    "itemsRemaining": 1,
                    "minPrice": minimum,
                }
                result = pages.grid_result(grid, CHAD_ID, CURRENCIES)
                self.assertEqual(result["status"], "page_priced")
                row = price.enriched_rows(
                    [{"SH Event ID": CHAD_ID}],
                    {CHAD_ID: {**result, "source": "event_page_grid", "checked_at": ""}},
                )[0]
                self.assertEqual(row["SH Floor Price"], "")
                self.assertEqual(row["SH Page Min Price"], "CAD 162.37")
                self.assertEqual(row["Price Scope"], "page 1 only")

    def test_all_recommended_rows_is_not_complete_event_inventory(self):
        grid = {**self.grid, "totalListingsCount": 200}
        result = pages.grid_result(grid, CHAD_ID, CURRENCIES)
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("matches declared minimum", result["scope"])
        del grid["totalListingsCount"]
        self.assertEqual(pages.grid_result(grid, CHAD_ID, CURRENCIES)["coverage"], "partial")

    def test_complete_grid_contradicting_event_minimum_is_rejected(self):
        with self.assertRaisesRegex(ResponseError, "contradicts"):
            pages.grid_result({**self.grid, "minPrice": 1}, CHAD_ID, CURRENCIES)

    def test_only_explicit_empty_inventory_is_no_listings(self):
        grid = {
            **self.grid,
            "items": [],
            "totalCount": 0,
            "itemsRemaining": 0,
            "totalListingsCount": 0,
        }
        self.assertEqual(pages.grid_result(grid, CHAD_ID, CURRENCIES)["status"], "no_listings")
        grid["totalListingsCount"] = 200
        self.assertEqual(pages.grid_result(grid, CHAD_ID, CURRENCIES)["status"], "incomplete")

    def test_zero_event_sentinel_requires_explicit_empty_inventory(self):
        captured_grid = pages.document_grid(capture("empty"), "161949052")
        self.assertEqual(
            pages.grid_result(captured_grid, "161949052", CURRENCIES)["status"], "no_listings"
        )
        with self.assertRaisesRegex(ResponseError, "different event"):
            pages.document_grid(capture("empty"), CHAD_ID)
        grid = {
            **self.grid,
            "eventId": 0,
            "items": [],
            "totalCount": 0,
            "totalListingsCount": 0,
            "itemsRemaining": 0,
        }
        result = pages.grid_result(grid, CHAD_ID, CURRENCIES)
        self.assertEqual(result["status"], "no_listings")
        self.assertEqual(result["grid_event_id"], 0)
        for extra in ({"totalListingsCount": 1}, {"items": self.grid["items"]}, {"eventId": 2}):
            with self.subTest(extra=extra), self.assertRaises(ResponseError):
                pages.grid_result({**grid, **extra}, CHAD_ID, CURRENCIES)

    def test_sold_and_unknown_prices_cannot_establish_event_floor(self):
        grid = deepcopy(self.grid)
        grid["items"][0].update(rawPrice=1, showRecentlySold=True)
        grid["minPrice"] = 1
        result = pages.grid_result(grid, CHAD_ID, CURRENCIES)
        self.assertEqual((result["status"], result["quote"]), ("page_priced", "CAD 162.37"))
        for amount in (None, "NaN", 0, True):
            with self.subTest(amount=amount):
                grid = deepcopy(self.grid)
                grid["items"][0]["rawPrice"] = amount
                self.assertEqual(
                    pages.grid_result(grid, CHAD_ID, CURRENCIES)["status"], "invalid_price"
                )

    def test_filters_quantity_and_wrong_identity_are_rejected(self):
        for key, value in (
            ("quantity", 2),
            ("sectionIds", [1]),
            ("ticketTypeGroupIds", [1]),
            ("favorites", True),
        ):
            with self.subTest(key=key), self.assertRaises(ResponseError):
                grid = {**self.grid, key: value}
                html = (
                    '<script id="app-context">'
                    + json.dumps({"eventId": int(CHAD_ID), "currencyCode": "CAD"})
                    + "</script>"
                )
                html += (
                    '<script id="index-data">'
                    + json.dumps({"eventId": int(CHAD_ID), "grid": grid})
                    + "</script>"
                )
                pages.document_grid(html, CHAD_ID)
        with self.assertRaisesRegex(ResponseError, "different event"):
            pages.document_grid(capture(), "1")

    def test_challenge_and_duplicate_scripts_cannot_be_prices(self):
        for html in ("<html>CAPTCHA</html>", capture() + capture()):
            with self.subTest(html=html[:30]), self.assertRaises(ResponseError):
                pages.document_grid(html, CHAD_ID)

    def test_wrong_page_duplicate_ids_and_inconsistent_counts_are_rejected(self):
        for extra in (
            {"currentPage": 2},
            {"itemsRemaining": 20},
            {"totalListingsCount": 1},
            {"totalCount": True},
            {"items": [self.grid["items"][0]] * 2},
        ):
            with self.subTest(extra=extra), self.assertRaises(ResponseError):
                pages.grid_result({**self.grid, **extra}, CHAD_ID, CURRENCIES)

    def test_first_page_url_removes_input_filters(self):
        url = pages.first_page_url(
            {"SH Event ID": CHAD_ID, "StubHub URL": CHAD_URL + "?quantity=2&sectionIds=5&page=9"}
        )
        self.assertEqual(
            parse_qs(urlsplit(url).query),
            {
                "quantity": ["0"],
                "sortBy": ["NEWPRICE"],
                "sortDirection": ["0"],
                "page": ["1"],
                "estimatedFees": ["true"],
                "currency": ["USD"],
            },
        )

    def test_pipeline_keeps_duplicate_links_and_gets_exactly_one_page(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "matches.csv"
            original = f"SH Event ID,StubHub URL,Ticketmaster URL\n{CHAD_ID},{CHAD_URL},tm-one\n"
            original += f"{CHAD_ID},{CHAD_URL},tm-two\n"
            source.write_text(original)
            session = Mock(
                fetch=Mock(return_value={"currencies": CURRENCIES}),
                fetch_document=Mock(return_value=capture("chad_usd")),
                warmup=[],
            )
            config = price.PriceConfig(
                input_csv=str(source), output_dir=str(root / "prices"), source="event-page"
            )
            with patch.object(price, "BrowserPriceSession", return_value=session):
                self.assertEqual(price.run(config), 0)
            session.fetch_document.assert_called_once()
            session.fetch.assert_not_called()
            session.close.assert_called_once()
            self.assertEqual(source.read_text(), original)
            with (root / "prices/enriched.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Ticketmaster URL"] for row in rows], ["tm-one", "tm-two"])
            self.assertEqual([row["Price (USD)"] for row in rows], ["113.97"] * 2)
            self.assertEqual([row["SH Floor Price"] for row in rows], ["USD 113.97"] * 2)
            self.assertFalse((root / "prices/location_settings.json").exists())
            report = json.loads((root / "prices/checks.json").read_text())
            self.assertEqual([(r["method"], r["page"]) for r in report["requests"]], [("GET", 1)])
            self.assertEqual(report["currency_mode"], "direct_usd")

    def test_usd_request_must_be_honored_by_document_and_listing(self):
        with self.assertRaisesRegex(ResponseError, "currency=USD"):
            pages.document_grid(capture(), CHAD_ID, expected_currency="USD")
        grid = pages.document_grid(capture("chad_usd"), CHAD_ID, expected_currency="USD")
        result = pages.grid_result(grid, CHAD_ID, [])
        self.assertEqual((result["quote"], result["usd"]), ("USD 113.97", "113.97"))
        grid["items"][0]["buyerCurrencyCode"] = "CAD"
        self.assertEqual(pages.grid_result(grid, CHAD_ID, [])["status"], "ambiguous_currency")

    def test_ignored_usd_parameter_is_recorded_as_failure_without_price(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Mock(fetch_document=Mock(return_value=capture()))
            checks, _ = pages.refresh_event_pages(
                [{"SH Event ID": CHAD_ID, "StubHub URL": CHAD_URL}], session, Path(temp), []
            )
            self.assertEqual(checks[CHAD_ID]["status"], "request_error")
            self.assertEqual(checks[CHAD_ID]["usd"], "")
            self.assertIn("currency=USD", checks[CHAD_ID]["error"])

    def test_http_failure_body_is_saved_and_browser_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            session = pages.BrowserPriceSession(price.PriceConfig(request_interval=0), Path(temp))
            session.ready = True
            session.browser_error = RuntimeError
            session.page = Mock()
            session.page.goto.return_value = Mock(status=403, text=Mock(return_value="CAPTCHA"))
            raw = Path(temp) / "blocked.html"
            with self.assertRaisesRegex(ResponseError, "403"):
                session.fetch_document(CHAD_URL, raw)
            self.assertEqual(raw.read_text(), "CAPTCHA")
            browser, playwright = Mock(), Mock()
            session.browser = browser
            session.playwright = playwright
            session.close()
            session.close()
            browser.close.assert_called_once()
            playwright.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
