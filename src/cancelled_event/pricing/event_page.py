"""Read actual first-page listings embedded in a browser-loaded StubHub event document."""

import json
import logging
import time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..http import ResponseError
from ..runtime import atomic_output, utc_now
from .common import event_id
from .grid import field, listing_page, listing_quote, listing_url

logger = logging.getLogger(__name__)


class EmbeddedJSON(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = {}
        self.identity = None
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            identity = dict(attrs).get("id")
            if identity in {"app-context", "index-data"}:
                if identity in self.scripts:
                    raise ResponseError(f"Duplicate embedded JSON: {identity}")
                self.identity, self.fragments = identity, []

    def handle_data(self, data):
        if self.identity:
            self.fragments.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.identity:
            self.scripts[self.identity] = "".join(self.fragments)
            self.identity, self.fragments = None, []


def first_page_url(row):
    parts = urlsplit(listing_url(row))
    query = {
        "quantity": 0,
        "sortBy": "NEWPRICE",
        "sortDirection": 0,
        "page": 1,
        "estimatedFees": "true",
        "currency": "USD",
    }
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def document_grid(html, identity, expected_currency=None):
    parser = EmbeddedJSON()
    parser.feed(html)
    try:
        app = json.loads(parser.scripts["app-context"])
        data = json.loads(parser.scripts["index-data"])
    except (KeyError, ValueError) as exc:
        raise ResponseError("Event document lacks usable app-context/index-data JSON") from exc
    if not isinstance(app, dict) or not isinstance(data, dict):
        raise ResponseError("Embedded event data must be JSON objects")
    if str(app.get("eventId")) != identity or str(data.get("eventId")) != identity:
        raise ResponseError("Event document belongs to a different event")
    grid = data.get("grid")
    if not isinstance(grid, dict):
        raise ResponseError("Event document requires an embedded listing grid")
    if type(grid.get("quantity")) is not int or grid["quantity"] != 0:
        raise ResponseError("Event grid did not honor quantity=0 (any quantity)")
    for key in (
        "sectionIds",
        "rowIds",
        "seatIds",
        "ticketClassIds",
        "listingNoteIds",
        "ticketTypeGroupIds",
        "instantDelivery",
        "favorites",
        "newListingsOnly",
        "priceDropListingsOnly",
        "removeObstructedView",
    ):
        if grid.get(key):
            raise ResponseError(f"Event grid has an unexpected listing filter: {key}")
    if "currencyCode" not in app:
        raise ResponseError("Event document requires explicit display currency")
    if expected_currency is not None and app["currencyCode"] != expected_currency:
        raise ResponseError(f"Event document did not honor requested currency={expected_currency}")
    return {**grid, "currencyCode": app["currencyCode"]}


def grid_result(grid, identity, currencies):
    # The validated parent document owns the event. StubHub emits a zero-ID grid
    # for empty inventory; allow that sentinel only with both explicit zero counts.
    empty_sentinel = (
        field(grid, "EventId") == 0
        and grid.get("items") == []
        and type(grid.get("totalCount")) is int
        and grid["totalCount"] == 0
        and type(grid.get("totalListingsCount")) is int
        and grid["totalListingsCount"] == 0
    )
    items, total, _ = listing_page(
        grid, "0" if empty_sentinel else identity, 1, grid.get("pageSize", 10)
    )
    if total is None:
        raise ResponseError("Event page requires totalCount for listing coverage")
    if len(items) > total:
        raise ResponseError("Event page returns more listings than totalCount")
    inventory_total = grid.get("totalListingsCount", total)
    if type(inventory_total) is not int or inventory_total < total:
        raise ResponseError("Event totalListingsCount contradicts grid totalCount")
    remaining = grid.get("itemsRemaining")
    if remaining is not None and (type(remaining) is not int or remaining != total - len(items)):
        raise ResponseError("Event page itemsRemaining disagrees with listing counts")
    ids = [str(field(item, "Id")) for item in items]
    if len(ids) != len(set(ids)):
        raise ResponseError("Event page repeats listing IDs")
    active = [item for item in items if not item.get("showRecentlySold") and not item.get("isSold")]
    result = {
        "listing_count": inventory_total,
        "grid_count": total,
        "observed_count": len(active),
        "returned_count": len(items),
        "quantity": grid["quantity"],
        "coverage": (
            "complete"
            if len(active) == inventory_total
            and ("totalListingsCount" in grid or not grid.get("betterValueTickets"))
            else "partial"
        ),
        "sort_by": grid.get("sortBy"),
        "sort_direction": grid.get("sortDirection"),
        "estimated_fees": grid.get("estimatedFees"),
        "declared_minimum": grid.get("minPrice"),
        "recommended_tickets": grid.get("betterValueTickets"),
        "disclosure": grid.get("listingGridDisclosure"),
        "grid_event_id": field(grid, "EventId"),
    }
    if inventory_total == 0:
        return {**result, "status": "no_listings", "scope": "event"}
    if not active:
        return {**result, "status": "incomplete", "scope": "page 1 only"}
    if any(field(item, "RawPrice") is None for item in active):
        return {**result, "status": "invalid_price", "scope": "page 1 only"}
    try:
        quotes = [listing_quote(item, grid, currencies) for item in active]
    except ValueError as exc:
        return {**result, "status": str(exc), "scope": "page 1 only"}
    minimum = min(quotes, key=lambda item: Decimal(item["usd_unrounded"]))
    try:
        declared = Decimal(str(grid.get("minPrice")))
        declared_valid = declared.is_finite() and declared > 0
    except InvalidOperation:
        declared, declared_valid = None, False
    raw_minimum = min(Decimal(quote["amount"]) for quote in quotes)
    corroborated = declared_valid and declared == raw_minimum
    if result["coverage"] == "complete":
        if declared_valid and not corroborated:
            raise ResponseError("Complete event grid contradicts its declared minimum")
        status, scope = "priced", "event (all listings on page 1)"
    elif corroborated:
        status, scope = "priced", "event (page 1 matches declared minimum)"
    else:
        status, scope = "page_priced", "page 1 only"
    return {**result, **minimum, "status": status, "scope": scope}


class BrowserPriceSession:
    """One ephemeral browser session; warm normal pages and fetch only event page one."""

    def __init__(self, config, output):
        self.config, self.output = config, output
        self.warmup = []
        self.last_request = None
        self.playwright = self.browser = self.page = None
        self.ready = False

    def pace(self):
        if self.last_request is not None:
            time.sleep(
                max(0, self.config.request_interval - (time.monotonic() - self.last_request))
            )
        self.last_request = time.monotonic()

    def start(self):
        if self.ready:
            return
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as exc:
            raise ResponseError(
                "Event-page prices require the optional 'browser' dependency"
            ) from exc
        self.browser_error = Error
        try:
            self.playwright = sync_playwright().start()
            launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            if self.config.browser_executable:
                launch["executable_path"] = self.config.browser_executable
            self.browser = self.playwright.chromium.launch(**launch)
            context = self.browser.new_context(
                locale="en-US",
                timezone_id="America/Los_Angeles",
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{self.browser.version} Safari/537.36",
            )
            self.page = context.new_page()
            self.page.set_default_timeout(self.config.request_timeout * 1000)
            for index, url in enumerate(
                ("https://www.stubhub.com/", "https://www.stubhub.com/explore")
            ):
                self.pace()
                response = self.page.goto(url, wait_until="domcontentloaded")
                self.page.wait_for_timeout(self.config.browser_wait_seconds * 1000)
                path = self.output / "raw" / f"browser_warmup_{index}.html"
                with atomic_output(path) as handle:
                    handle.write(self.page.content())
                self.warmup.append(
                    {
                        "url": url,
                        "initial_http_status": response.status if response else None,
                        "raw_file": str(path),
                        "checked_at": utc_now(),
                    }
                )
            self.ready = True
        except Error as exc:
            self.close()
            raise ResponseError(f"Browser initialization failed: {exc}") from exc
        except OSError:
            self.close()
            raise

    def fetch_document(self, url, raw):
        self.start()
        self.pace()
        try:
            response = self.page.goto(url, wait_until="domcontentloaded")
            if response is None:
                raise ResponseError("Browser navigation returned no HTTP response")
            html = response.text()
            with atomic_output(raw) as handle:
                handle.write(html)
            if response.status != 200:
                raise ResponseError(f"Event document returned HTTP {response.status}")
            return html
        except self.browser_error as exc:
            raise ResponseError(f"Browser event request failed: {exc}") from exc

    def close(self):
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            try:
                if self.playwright is not None:
                    self.playwright.stop()
            finally:
                self.playwright = self.browser = self.page = None
                self.ready = False


def refresh_event_pages(rows, session, output, currencies):
    urls = {}
    for row in rows:
        urls.setdefault(event_id(row), first_page_url(row))
    checks, requests = {}, []
    for identity, url in sorted(urls.items()):
        check = checks[identity] = {
            "status": "request_error",
            "usd": "",
            "currency": "",
            "quote": "",
            "checked_at": "",
            "source": "event_page_grid",
            "scope": "page 1 only",
            "listing_id": "",
            "listing_count": "",
            "observations": [],
            "requests": [len(requests)],
            "basis": "Per-ticket USD display price; fee-estimate flag retained in evidence",
        }
        raw = output / "raw" / f"event_{identity}_page1.html"
        record = {"url": url, "method": "GET", "page": 1, "currency": "USD", "raw_file": str(raw)}
        requests.append(record)
        try:
            html = session.fetch_document(url, raw)
            grid = document_grid(html, identity, expected_currency="USD")
            check.update(grid_result(grid, identity, currencies))
            check["observations"].append(
                {
                    "raw_file": str(raw),
                    "page": 1,
                    "total": check["listing_count"],
                    "scope": check["scope"],
                }
            )
        except (ResponseError, OSError) as exc:
            check["error"] = record["error"] = str(exc)
            logger.warning("Event-page price lookup failed for %s: %s", identity, exc)
        finally:
            record["checked_at"] = check["checked_at"] = utc_now()
        logger.info("Event-page price %s: %s", identity, check["status"])
    return checks, requests
