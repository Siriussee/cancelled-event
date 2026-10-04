"""Event identity, explicit currencies and precise price conversion."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DEFAULT_SYMBOLS = {
    "USD": {"US$", "USD"},
    "CAD": {"C$", "CA$", "CAD"},
    "EUR": {"€", "EUR"},
    "GBP": {"£", "GBP"},
}


def event_id(row):
    identity = row.get("SH Event ID") or row.get("stubhub_eventId") or row.get("eventId")
    if not identity:
        match = re.search(
            r"/event/([0-9]+)(?:/|$)", row.get("StubHub URL") or row.get("stubhub_url") or ""
        )
        identity = match.group(1) if match else ""
    if not re.fullmatch(r"[0-9]+", identity):
        raise ValueError("Each input row requires a numeric SH event ID or StubHub event URL")
    return identity


def parse_price(value, currencies):
    """Accept explicit currency tokens and English number grouping; never infer bare $."""
    tokens = {}
    for code, symbols in DEFAULT_SYMBOLS.items():
        for symbol in symbols:
            tokens.setdefault(symbol, set()).add(code)
    for item in currencies:
        code = item.get("code", "")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code):
            continue
        for symbol in (code, item.get("symbol"), item.get("extendedSymbol")):
            if isinstance(symbol, str) and symbol:
                tokens.setdefault(symbol, set()).add(code)
    text = re.sub(r"^from\s+", "", value.strip(), flags=re.IGNORECASE)
    number = r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)"
    for token in sorted(tokens, key=len, reverse=True):
        escaped = re.escape(token)
        match = re.fullmatch(rf"{escaped}\s*{number}|{number}\s*{escaped}", text)
        if not match:
            continue
        if token == "$" or len(tokens[token]) != 1:
            raise ValueError("ambiguous_currency")
        amount = Decimal((match[1] or match[2]).replace(",", ""))
        if amount <= 0:
            raise ValueError("invalid_price")
        return amount, next(iter(tokens[token]))
    if re.fullmatch(rf"\$\s*{number}", text):
        raise ValueError("ambiguous_currency")
    raise ValueError("invalid_price")


def usd_amount(amount, currency, currencies):
    """Keep precision for comparing listing prices before cent rounding."""
    if currency == "USD":
        return amount
    rates = {item.get("code"): item.get("currentRate") for item in currencies}
    try:
        usd_rate, source_rate = Decimal(str(rates["USD"])), Decimal(str(rates[currency]))
        if not all(rate.is_finite() and rate > 0 for rate in (usd_rate, source_rate)):
            raise ValueError("Invalid exchange rate")
        return amount * usd_rate / source_rate
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ValueError("fx_error") from exc


def to_usd(amount, currency, currencies):
    return usd_amount(amount, currency, currencies).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
