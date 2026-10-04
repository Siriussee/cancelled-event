# Floor-price path investigation — October 4, 2026

A warmed Playwright browser session successfully reads real listings from the
original event HTML. This is now the default price source and is integrated
with cancellation matching. The previous HTTP 403 limitation applies to direct
requests and the older POST adapter, not to the validated warmed-browser path.

## Paths explored

| Path | Live observation | Decision |
| --- | --- | --- |
| Explore display quote | 55 `no_quote`, one `not_found` across 56 targets | Insufficient for these cancelled events. |
| Direct event HTML / native listing POST | HTTP 403, including an active basketball control | Keep failures as unknown; retain POST as an optional legacy adapter. |
| Browser navigating directly to an event | HTTP 403 / CAPTCHA | Direct navigation alone was insufficient in the initial probe. |
| Corrected browser listing POST with current frontend parameters | Timed out / challenge response | No successful POST listing evidence; do not require POST for the working path. |
| Chromium homepage + Explore warmup, then sorted event GET | HTTP 200 with `app-context` and `index-data.grid` | Working path: original embedded listings, page one only. |
| SEO `AggregateOffer.lowPrice` | Chad Gray showed CAD 89.56 while actual grid listings were CAD 162.37 | Exclude SEO metadata from current listing floors. |
| Browser `GetLocationSettings` | HTTP 200 with current CAD/USD rates | Convert observed buyer-currency amounts with fresh rates. |

The browser uses a regular Chrome user agent, `en-US`, a Los Angeles timezone,
and a 1440×900 viewport. It visits the homepage and Explore, waiting eight
seconds on each, then navigates to the event with `quantity=0`,
`sortBy=NEWPRICE`, `sortDirection=0`, `page=1`, and `estimatedFees=true`.
No extra listing pages or listing POST are requested by the adapter.
The returned grid's sort-direction metadata can differ from the requested
value; minimum selection uses actual amounts rather than trusting order.

## Real examples

Chad Gray, event `161331875`, The Bellwether, Los Angeles, October 4, 2026:

- Final workflow capture: `2026-10-04T19:15:54.574948+00:00`.
- Two actual listings: `13367389979` (Accessible) and `13367390032`
  (General Admission), both `rawPrice=162.37` in buyer currency CAD.
- `totalCount=totalListingsCount=2`, `itemsRemaining=0`, `quantity=0`.
  Entire active inventory fits on the first page.
- Floor CAD 162.37; fresh rates CAD `1.603729000` and USD `1.125700000`
  produce normalized USD **113.97**. The UI rounds the quote to `C$162`.
- Seller `listingCurrencyCode=USD` must not be applied to buyer `rawPrice`.
  The page's fee disclosure says `incl. fees`; checkout is not inspected.

A separate session at 19:12 UTC returned CAD 179.52 for the same listing IDs.
These are time- and session-specific displayed observations, not a fixed quote.
Both original responses remain in the local evidence directories.

NBA preseason Lakers at Warriors, event `161659240`, October 6, 2026,
provided the active-event control. At 18:55 UTC the sorted quantity-zero page
returned 10 of 311 grouped listings, from 3,598 total listings. Its first-page
minimum CAD 47.33 (listing `14119150534`, available quantity `[1]`) matched
`grid.minPrice`. Default quantity two had shown CAD 47.70, demonstrating why
quantity filtering changes the observed minimum. The later implemented-module
smoke at 19:08 UTC returned CAD 47.70 with quantity zero; inventory had changed.

## Complete workflow validation

Fresh final run: `output/cancelled_browser_floor_workflow_20261004_validated/`.
It used the collected StubHub shortlist and normalized Ticketmaster/TicketWeb
cancellations, and completed at `2026-10-04T19:17:00.082646+00:00`:

- 58 match pairs, 56 unique StubHub events; original rows and links preserved.
- 56 event-document GETs, each page one; all original HTML bodies retained.
- 55 `priced`: 19 with complete first-page coverage, 36 with partial coverage
  whose exact observed minimum matched the declared event minimum.
- One `no_listings`: Drift Long Island, event `161949052`. Its correctly
  identified parent document contained an empty grid with `eventId=0`, both
  explicit listing counts zero and no items. The zero-ID sentinel is accepted
  only under those conditions; it is not a general identity-validation bypass.
- Zero transport/schema errors; complete price stage and manifest; exit code 0.
- Current FX settings, UTC timestamps, input hashes and request URLs retained.
  The ephemeral browser session was closed after publication.

The first browser batch remains at `output/cancelled_browser_floor_workflow_20261004/`;
it returned 55 prices and rejected the zero-ID empty grid. Its preserved response
provided the evidence for the narrowly validated empty-grid rule.
The older native POST batch remains at `output/cancelled_floor_workflow_20261004/`:
all 56 event POSTs received 403 and no numeric prices were invented.
The successful two-event smoke is `output/browser_floor_smoke_20261004/`.

## Implementation and verification

`event_page_prices.py` implements warmed browser document capture, identity /
filter checks and first-page scope. `price_fetcher.py` handles currency conversion
and publication; `cancelled_workflow.py` uses the browser source by default.
A partial minimum without matching event metadata is explicitly `page_priced`;
its global `SH Floor Price` stays blank. Recommended-grid and inventory totals
are recorded separately. See [price refresh](price_refresh.md) for the contract.

Sanitized real document extracts for Chad Gray, basketball and empty inventory
are committed in `tests/fixtures/`; session tokens and unrelated fields are omitted.
All 124 offline tests, Ruff lint/format checks and source/wheel builds passed.
Playwright/Chromium were installed in the local project environment. Browser
access can still fail in a different environment; such failures preserve the
matches and produce unknown prices instead of empty-inventory claims.
