# Ticketmaster cancellation collection

```bash
ticketmaster-scrape-cancelled-events --start-date 2026-10-04 --days 30
```

Source: `GET https://www.ticketmaster.com/api/search/events/category/{categoryId}`.
Default scope: US, Concerts + Sports, today plus 29 dates; two-second serial
request pacing. Dates use the venue timezone, with a URL-date fallback; “today”
uses the machine's calendar date, so set `--start-date` for reproducible runs.

The website API returns all statuses. Keep `cancelled=true` or
`eventChangeStatus=eventCancelled`, then restrict records to the requested local
date. Pages have 20 records; the implementation assumes accessible pages 0–48.
Fail on excess pages, missing records, repeated IDs or changing totals. An
explicit `--max-pages-per-query` debug cap allows partial output and logs it.

Publish the deduplicated CSV only after every slice succeeds. Raw pages are
written atomically. `--resume-raw` reuses validated saved responses, which may
contain stale statuses; use a fresh directory for a new snapshot. HTTP 403/429
backoff is at least 30 seconds; retry sleeps are capped at 60 seconds.

The endpoint is an internal website API and its contract is unverified by the
offline test suite. The supported bulk alternative is the official Discovery
Feed (`https://app.ticketmaster.com/discovery-feed/v2/events`), requiring a
developer key. The repository's [API check](ticketmaster_api_verification.md)
verifies access only; it does not implement bulk ingestion.
