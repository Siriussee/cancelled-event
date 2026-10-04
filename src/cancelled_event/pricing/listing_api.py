"""Fetch complete listing grids through the optional StubHub POST adapter."""

import logging
from decimal import Decimal

from ..http import ResponseError
from ..runtime import utc_now
from .common import event_id
from .grid import field, listing_page, listing_quote, listing_url

logger = logging.getLogger(__name__)


def listing_request(page, page_size):
    # ShowAllTickets avoids recommendation filtering and quantity-specific searches.
    return {
        "ShowAllTickets": True,
        "HideDuplicateTickets": False,
        "PageSize": page_size,
        "CurrentPage": page,
        "SortBy": "PRICE",
        "SortDirection": 0,
        "EstimatedFees": True,
    }


def refresh_listings(rows, config, session, output, currencies):
    urls = {}
    for row in rows:
        url = listing_url(row)
        urls.setdefault(event_id(row), url)
    checks, requests = {}, []
    for identity, url in sorted(urls.items()):
        check = checks[identity] = {
            "status": "request_error",
            "usd": "",
            "currency": "",
            "quote": "",
            "checked_at": "",
            "source": "listing_grid",
            "basis": "Per-ticket listing price as returned; fees not independently verified",
            "listing_id": "",
            "listing_count": "",
            "observations": [],
            "requests": [],
        }
        seen, quotes = set(), []
        expected = None
        quote_error = None
        try:
            for page in range(1, config.listing_max_pages + 1):
                raw = output / "raw" / f"listings_{identity}_p{page}.json"
                body = listing_request(page, config.listing_page_size)
                record = {"url": url, "method": "POST", "body": body, "raw_file": str(raw)}
                check["requests"].append(len(requests))
                requests.append(record)
                try:
                    payload = session.fetch(url, json_data=body, response_path=raw)
                    items, total, pages = listing_page(
                        payload, identity, page, config.listing_page_size
                    )
                    if expected is None:
                        expected = (total, pages)
                    elif (total, pages) != expected:
                        raise ResponseError("Listing totals changed during pagination")
                    for item in items:
                        listing_id = str(field(item, "Id"))
                        if listing_id in seen:
                            raise ResponseError("Listing IDs repeat across pages")
                        seen.add(listing_id)
                        try:
                            quotes.append(listing_quote(item, payload, currencies))
                        except ValueError as exc:
                            quote_error = str(exc)
                    check["observations"].append(
                        {"raw_file": str(raw), "page": page, "total": total, "pages": pages}
                    )
                except (ResponseError, OSError) as exc:
                    record["error"] = str(exc)
                    raise
                finally:
                    record["checked_at"] = check["checked_at"] = utc_now()
                if page >= pages:
                    if total is not None and len(seen) != total:
                        raise ResponseError("Unique listing count does not equal TotalCount")
                    check["listing_count"] = len(seen)
                    if not seen:
                        check["status"] = "no_listings"
                    elif quote_error:
                        check["status"] = quote_error
                    else:
                        check.update(min(quotes, key=lambda item: Decimal(item["usd_unrounded"])))
                        check["status"] = "priced"
                    break
            else:
                check["status"] = "incomplete"
                check["error"] = "Listing page limit reached; observed minimum is not a floor"
        except (ResponseError, OSError) as exc:
            check["error"] = str(exc)
            logger.warning("Listing price lookup failed for %s: %s", identity, exc)
        logger.info("Listing price %s: %s", identity, check["status"])
    return checks, requests
