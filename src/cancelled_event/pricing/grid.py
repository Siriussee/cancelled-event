"""Shared validation and price parsing for StubHub listing grids."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

from ..http import ResponseError
from .common import event_id, parse_price, to_usd, usd_amount


def field(payload, pascal_name):
    """Support the site's PascalCase and camelCase grid schemas without guessing."""
    camel_name = pascal_name[0].lower() + pascal_name[1:]
    if pascal_name in payload and camel_name in payload:
        if payload[pascal_name] != payload[camel_name]:
            raise ResponseError(f"Conflicting listing fields: {pascal_name}")
    return payload.get(pascal_name, payload.get(camel_name))


def listing_url(row):
    identity = event_id(row)
    value = row.get("StubHub URL") or row.get("stubhub_url") or row.get("url")
    if not value:
        return f"https://www.stubhub.com/event/{identity}/"
    parts = urlsplit(value)
    match = re.search(r"/event/([0-9]+)/?$", parts.path)
    if (
        parts.scheme != "https"
        or parts.netloc not in {"www.stubhub.com", "stubhub.com", "www.stubhub.ca", "stubhub.ca"}
        or not match
        or match[1] != identity
    ):
        raise ValueError(f"Expected a StubHub event URL matching event ID {identity}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def listing_page(payload, identity, page, requested_size):
    if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
        raise ResponseError("Expected a listing grid without API errors")
    owner = field(payload, "EventId")
    if owner is not None and str(owner) != identity:
        raise ResponseError("Listing grid belongs to a different event")
    items = field(payload, "Items")
    total = field(payload, "TotalCount")
    pages = field(payload, "NumPages")
    size = field(payload, "PageSize")
    size = requested_size if size is None else size
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ResponseError("Listing response requires an Items list")
    for name, value in (("TotalCount", total), ("NumPages", pages)):
        if value is not None and (type(value) is not int or value < 0):
            raise ResponseError(f"Listing {name} must be a nonnegative integer")
    if type(size) is not int or size < 1:
        raise ResponseError("Listing PageSize must be a positive integer")
    if total is None and pages is None:
        raise ResponseError("Listing response requires TotalCount or NumPages")
    if pages is None:
        pages = (total + size - 1) // size
    if total is not None and ((total == 0 and items) or (total > 0 and not items)):
        raise ResponseError("Listing count disagrees with returned items")
    if (pages == 0 and items) or (not items and total != 0 and pages != 0):
        raise ResponseError("Listing page count disagrees with returned items")
    if total == 0 and pages > 1:
        raise ResponseError("Empty listing result declares multiple pages")
    if total and pages < (total + size - 1) // size:
        raise ResponseError("Listing page count cannot cover TotalCount")
    current_page = field(payload, "CurrentPage")
    if current_page is not None and (type(current_page) is not int or current_page != page):
        raise ResponseError("Listing response returned the wrong page")
    for item in items:
        if not re.fullmatch(r"[0-9]+", str(field(item, "Id") or "")):
            raise ResponseError("Every listing requires a numeric Id")
        owner = field(item, "EventId")
        if owner is not None and str(owner) != identity:
            raise ResponseError("Listing belongs to a different event")
    return items, total, pages


def listing_quote(item, payload, currencies):
    # rawPrice is in the buyer's display currency, not listingCurrencyCode (seller currency).
    code = field(item, "BuyerCurrencyCode") or field(item, "CurrencyCode")
    response_code = field(payload, "CurrencyCode")
    if code and response_code and code != response_code:
        raise ValueError("ambiguous_currency")
    code = code or response_code
    if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code)):
        raise ValueError("ambiguous_currency")
    raw = field(item, "RawPrice")
    quote = field(item, "Price")
    if code and isinstance(quote, str) and quote:
        try:
            _, displayed_code = parse_price(quote, currencies)
        except ValueError:
            pass
        else:
            if code != displayed_code:
                raise ValueError("ambiguous_currency")
    if raw is not None and code is not None:
        try:
            amount = Decimal(str(raw))
        except InvalidOperation as exc:
            raise ValueError("invalid_price") from exc
        if isinstance(raw, bool) or not amount.is_finite() or amount <= 0:
            raise ValueError("invalid_price")
        quote = f"{code} {amount}"
    elif isinstance(quote, str) and quote:
        # A bare dollar symbol still requires explicit response currency metadata.
        if code and re.fullmatch(r"\$\s*[0-9,.]+", quote.strip()):
            quote = code + " " + quote.strip().removeprefix("$")
        amount, code = parse_price(quote, currencies)
    else:
        raise ValueError("invalid_price")
    return {
        "amount": str(amount),
        "usd": str(to_usd(amount, code, currencies)),
        "usd_unrounded": str(usd_amount(amount, code, currencies)),
        "currency": code,
        "quote": quote,
        "listing_id": str(field(item, "Id")),
    }
