# Price refresh

`stubhub-fetch-prices` follows matching. It accepts the matcher CSV or a table
with `SH Event ID`/`StubHub URL`, preserves every row/link and adds `Price (USD)`,
`Price Status`, original quote/currency and UTC observation time.

```bash
stubhub-find-cancelled-overlap
stubhub-fetch-prices --output-dir output/prices/new-run
```

For an existing snapshot, provide its exact seeds and discovery files:

```bash
stubhub-fetch-prices \
  --input-csv output/my-run/intersection.csv \
  --events-csv output/my-run/events.csv \
  --cities-csv data/worldcities.csv \
  --source-raw-dir output/my-run/raw \
  --output-dir output/my-run/prices
```

The output directory must be empty. Results are `enriched.csv`, `report.md`,
`checks.json`, `location_settings.json` and `raw/`. Inputs stay untouched;
generated outputs remain ignored by Git. The JSON records input hashes, seed
coordinates, attempted pages, response paths and individual observations.

The algorithm refreshes SH Explore at the lowest source page, then searches up
to four pages either side if necessary. Targets share page requests and stop
after their first fresh observation. Use the same `EXPLORE_SORT_TYPE` and
`EXPLORE_PAGE_SIZE` as discovery. Raw lookup supports current filenames and old
coordinate filenames; coordinates always come from the seed list. Without raw
files, same-named seed cities are rejected rather than guessed.

SH's `formattedFromPrice` is a displayed per-ticket starting price. USD amounts
use fresh `GetLocationSettings` rates: `amount × USD rate / source rate`, rounded
to cents with decimal arithmetic. Explicit codes/symbols are required; bare `$`
is ambiguous. Fees, checkout totals and individual listings are not independently
verified. `hasActiveListings=False` does not discard a returned quote.

| Status | Meaning |
| --- | --- |
| `priced` | A displayed quote was converted to USD. |
| `no_quote` | Event found; Explore omitted/emptied its price field. Inventory remains unknown. |
| `not_found` | Event absent from the successfully queried bounded page range. |
| `source_missing` | No discovery seed/page recovered. |
| `request_error` | An incomplete lookup had a transport or schema failure. |
| `fx_error` | Quote found but conversion rates unavailable/invalid. |
| `ambiguous_currency` / `invalid_price` | Quote cannot safely be parsed. |

Requests share temporary anonymous cookies and run serially, two seconds apart,
including retries. Cookies are removed when the run ends. Exit status is nonzero
for source/request/rate/parsing errors; valid `no_quote` and `not_found` results
are retained as unknown. A bounded lookup is not a complete inventory search.

Adapted from the temporary Explore price script in
[the earlier cancelled-events chat](codex://threads/019fbb24-90d1-74b3-b1ea-4e555e02bd93?hostId=remote-ssh-discovered%3Aubuntu).
