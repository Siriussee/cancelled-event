# Price refresh

`stubhub-fetch-prices` defaults to `--source listings`. It accepts the matcher
CSV or a table with `SH Event ID`/`StubHub URL`, preserves every row/link, and
adds `Price (USD)`, status, original floor/currency, listing ID/count, source,
price basis and UTC observation time. Discovery seeds and raw Explore pages
are unnecessary for this mode.

**Live validation limitation:** on October 4, 2026, the tested environment's
event-page GET and listing POST requests received HTTP 403. Successful live
listing responses and numeric floors have not been verified. The adapter's
price and pagination contracts are tested offline; blocked responses remain
`request_error`, with blank prices and raw evidence. This is not evidence of
no inventory. See [path investigation](floor_price_path.md).

## Run after matching

```bash
stubhub-find-cancelled-overlap
stubhub-fetch-prices --output-dir output/prices/new-run
```

Or combine matching and price collection for existing snapshots:

```bash
stubhub-cancelled-workflow \
  --stubhub-csv output/my-run/events.csv \
  --ticketmaster-csv output/my-run/ticketmaster_cancelled_events.csv \
  --output-dir output/cancelled/new-run
```

The workflow creates `intersection.csv`, `prices/enriched.csv`,
`prices/report.md`, `prices/checks.json`, `prices/location_settings.json`,
`prices/raw/` and `manifest.json`. The manifest records source hashes, match
and price stage outcomes and evidence paths. A failed price lookup preserves
matches and produces `needs_attention` with a nonzero exit code. It never
recollects or overwrites the source snapshots. Upstream collection remains
`stubhub-scrape-events` and `ticketmaster-scrape-cancelled-events`.

Both commands require an empty output directory. Inputs stay untouched and
generated outputs remain ignored by Git. Empty intersections produce a CSV
with headers and make no price requests.

## Listing floor contract

The adapter posts JSON to each canonical StubHub event URL with
`ShowAllTickets=true`, `HideDuplicateTickets=false`, `SortBy=PRICE`,
`SortDirection=0`, `EstimatedFees=true` and explicit page/size. No quantity,
seat or section filter is applied by the client. Numeric IDs without URLs use
`https://www.stubhub.com/event/<id>/`. Source URLs remain in the output, and
URLs must be on StubHub and match the event ID before listing requests run.

Accept an `Items`/`items` list and `TotalCount`/`totalCount` or
`NumPages`/`numPages` metadata. Follow every declared page, require stable
counts and unique listing IDs, and verify the final total when provided.
An explicit wrong event ID/page, conflicting fields, missing pages, repeated
IDs, or changing totals invalidates the floor. `PRICE_LISTING_MAX_PAGES`
(default 100) bounds work: reaching it leaves the floor unknown. A minimum
observed on only part of an event is never promoted to a floor.

Amounts come from positive, finite `RawPrice` with explicit `CurrencyCode`,
or a displayed `Price` with an unambiguous currency token. A bare `$` needs
explicit response currency metadata. Do not infer currency from venue country
or requested URL. If any listing's amount/currency/conversion is unknown,
the full event floor stays unknown. USD conversion uses fresh
`GetLocationSettings` rates: `amount × USD rate / source rate`, rounded to cents
with decimal arithmetic. Listed amounts are per ticket as returned;
`EstimatedFees=true` requests displayed fee estimates, but inclusion of every
fee and the checkout total are not independently verified.

Duplicate event rows share one lookup. `hasActiveListings=False` in discovery
does not skip the lookup. `SH Floor Price` and `Price Source=listing_grid`
identify the listing result; `SH From Price` stays blank in this mode. All
requests, bodies, page response paths and observations are retained in
`checks.json`. The transport saves the final raw response per page even on
HTTP or JSON failures, so a `.json` evidence file may contain HTML.

## Optional Explore display quotes

```bash
stubhub-fetch-prices --source explore \
  --input-csv output/my-run/intersection.csv \
  --events-csv output/my-run/events.csv \
  --cities-csv data/worldcities.csv \
  --source-raw-dir output/my-run/raw \
  --output-dir output/my-run/display-quotes
```

Explore refreshes the lowest source page, then searches up to four pages
either side. Targets share requests and stop after their first fresh
observation. Use the same `EXPLORE_SORT_TYPE` and `EXPLORE_PAGE_SIZE` as
discovery. Raw lookup supports current and old coordinate filenames, with
coordinates from the seed list. Without raw files, same-named seed cities
are rejected rather than guessed.

`formattedFromPrice` is a displayed starting price, not a verified listing
floor. It goes into `SH From Price` with `Price Source=explore`; `SH Floor
Price` stays blank. Missing quotes and bounded search misses remain unknown.
This retains the earlier Explore refresh behavior behind an explicit source.

| Status | Meaning |
| --- | --- |
| `priced` | Complete listing minimum, or optional Explore quote, converted to USD; inspect `Price Source`. |
| `no_listings` | Listing response explicitly declares zero listings. Price is blank. |
| `incomplete` | Listing page limit reached. Floor is unknown. |
| `request_error` | Transport, schema or pagination validation failed. Inventory is unknown. |
| `fx_error` | A listing/quote cannot be converted with fresh rates. |
| `ambiguous_currency` / `invalid_price` | A price cannot safely be interpreted. |
| `no_quote` | Explore found the event but omitted/emptied its price field. |
| `not_found` | Explore did not find the event in the successfully queried bounded range. |
| `source_missing` | Explore source seed/page could not be recovered. |

Requests share temporary anonymous cookies and run serially two seconds
apart, including retries. Cookies are removed when the run ends. Error and
incomplete statuses produce a nonzero exit; valid `no_listings`, `no_quote`
and `not_found` results remain blank. Website endpoints can change without
notice, and the listing-page responses are observations over a time interval,
not an atomic inventory snapshot.
