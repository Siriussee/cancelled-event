# Price refresh

`stubhub-fetch-prices` and `stubhub-cancelled-workflow` default to
`--source event-page`. They accept matcher CSVs or tables with `SH Event ID` /
`StubHub URL`, preserve every row and source link, and add USD prices,
original currency amounts, observation time, listing IDs, counts and scope.
No discovery seed or Explore snapshot is needed for browser prices.
Browser prices explicitly request USD and validate the returned currency;
they do not fetch exchange rates or convert the amount locally.

## Browser setup and workflow

```bash
python -m pip install -e '.[browser]'
python -m playwright install chromium

stubhub-cancelled-workflow \
  --stubhub-csv output/my-run/events.csv \
  --ticketmaster-csv output/my-run/ticketmaster_cancelled_events.csv \
  --output-dir output/cancelled/new-run
```

To refresh an existing intersection, run:

```bash
stubhub-fetch-prices \
  --input-csv output/my-run/intersection.csv \
  --output-dir output/prices/new-run
```

Use `--browser-executable /absolute/path/to/chrome` for an existing Chromium
installation. `PRICE_BROWSER_EXECUTABLE` and `PRICE_BROWSER_WAIT_SECONDS`
(default 8 seconds per warmup page) provide the same settings.

The workflow writes `intersection.csv`, `prices/enriched.csv`, `prices/report.md`,
`prices/checks.json`, original HTML in
`prices/raw/`, and `manifest.json`. The manifest includes input hashes and
stage results. Inputs remain untouched; both commands require a fresh empty
output directory. Empty intersections produce headers without starting a browser.

## Event page one contract

A shared ephemeral headless Chromium session visits the homepage and Explore
page, allowing normal site JavaScript to establish an anonymous session. It
then navigates to each event with:

```text
?quantity=0&sortBy=NEWPRICE&sortDirection=0&page=1&estimatedFees=true&currency=USD
```

Only page one is requested for each distinct event; there is no listing POST,
scrolling or pagination in the adapter. Numeric IDs without URLs use
`https://www.stubhub.com/event/<id>/`. URLs must belong to StubHub and match
the event ID. Requests are serial and paced two seconds apart. The browser
and its temporary cookies are discarded at the end.

Read `app-context` and `index-data.grid` JSON from the **original HTTP document**,
not hydrated DOM text or SEO `AggregateOffer.lowPrice`. Validate the event IDs,
page number, `quantity=0`, absence of seat/section/other explicit filters,
unique listing IDs and consistent counts. `showRecentlySold` / `isSold` items
are excluded from active prices. Unknown active listing prices invalidate the
minimum; HTTP errors and challenge pages never mean zero inventory.
The site's zero-ID empty-grid sentinel is accepted only inside a correctly
identified event document with an empty item array and both explicit totals zero.
The parent document must confirm `currencyCode=USD`; any active listing's
explicit buyer currency must agree. An ignored USD parameter is an error,
not a reason to relabel another currency as USD.

The site's recommended-ticket grouping can reduce `totalCount` below
`totalListingsCount`. Keep the original inventory count in `SH Listing Count`,
the grid count in `SH Grid Listing Count`, and the active page count in
`SH Listings Observed`. Coverage and price scope are separate:

- All active inventory fits on page one: `priced`, `coverage=complete`, scope
  `event (all listings on page 1)`.
- Partial inventory but the exact observed minimum equals `grid.minPrice`:
  `priced`, `coverage=partial`, scope `event (page 1 matches declared minimum)`.
  This corroborates the site's declared event minimum with a real listing;
  it does not claim to have inspected all inventory.
- Partial inventory without that agreement: `page_priced`, scope `page 1 only`.
  `SH Floor Price` is blank; `SH Page Min Price` and `Price (USD)` contain only
  the observed page minimum. This is an accepted first-page result, exit code 0.

`SH Page Min Price` is retained for every successful event-page quote.
`Price Source=event_page_grid` distinguishes this source. A contradictory
minimum in a complete grid is a validation error, rather than a guessed floor.

## Currency, precision and fees

Use the positive finite `rawPrice` with `buyerCurrencyCode` or explicit page
`currencyCode`. `listingCurrencyCode` identifies the seller currency and must
not be applied to `rawPrice`. With the default `currency=USD`, a live Chad Gray
document returned `rawPrice=113.97`, `buyerCurrencyCode=USD`, and the rounded
UI `price="$114"`. The exact floor is **USD 113.97**. The site's own currency
selection frontend uses this query parameter too.

The browser source records `currency_mode=direct_usd` and skips `GetLocationSettings`.
`SH Floor Price` and `Price (USD)` come directly from the USD quote, preserving
the raw amount before final cent rounding. Do not infer currency from venue
or hostname. The previous CAD capture had `rawPrice=162.37` despite seller
`listingCurrencyCode=USD`; that amount was CAD and remains a regression fixture.
`estimatedFees` and the site's fee disclosure are retained in `checks.json`.
Amounts are per ticket as returned; final checkout totals are not verified.

A real Chad Gray capture at 19:24 UTC on October 4, 2026 returned two actual
USD 113.97 listings, equal to the grid minimum, without an FX request.
See [path investigation](floor_price_path.md) for live evidence and alternatives.

## Other sources

Native POST and Explore sources retain fresh `GetLocationSettings` rates,
saved as `location_settings.json`: `amount × USD rate / source rate`, using
decimal arithmetic and rounding only the final USD amount to cents.

`--source listings` retains the older native JSON POST adapter. It requests
`ShowAllTickets=true`, `HideDuplicateTickets=false`, `SortBy=PRICE`,
`SortDirection=0`, `EstimatedFees=true`, and explicit page/size. This legacy
contract remains offline-tested but received HTTP 403 in the live environment;
it is not the recommended working path. It requires every declared page,
stable counts and unique IDs before publishing a floor. `PRICE_LISTING_MAX_PAGES`
(default 100) limits work; truncation leaves prices unknown. Raw failure bodies
are retained even when the `.json` file actually contains HTML.

`--source explore` refreshes the displayed `formattedFromPrice` using discovery
seed/page lookup and a bounded search (default four pages either side). Set
`--events-csv`, `--cities-csv`, and optionally `--source-raw-dir` to the original
discovery artifacts. Use matching Explore sort/page-size settings. These are
display quotes: `SH From Price` is populated and `SH Floor Price` stays blank.

| Status | Meaning |
| --- | --- |
| `priced` | Confirmed/corroborated event-page floor, complete POST floor, or Explore quote; inspect source and scope. |
| `page_priced` | Observed page-one minimum only; global floor is blank. |
| `no_listings` | Explicit zero inventory, with blank price. |
| `incomplete` | No active page price or native POST page limit reached. |
| `request_error` | Transport, challenge, schema, identity or count validation failed. |
| `fx_error` | Fresh rates cannot convert a quote to USD. |
| `ambiguous_currency` / `invalid_price` | An active listing cannot safely be interpreted. |
| `no_quote` / `not_found` / `source_missing` | Explore quote absent, bounded lookup missed, or discovery seed unavailable. |

Errors and incomplete results preserve matches and produce `needs_attention`
with a nonzero workflow exit. Successful observations can change between
requests, and a working browser session does not guarantee future access.
