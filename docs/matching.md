# Cancellation matching

```bash
cancelled-event match \
  --stubhub-csv output/events.csv \
  --ticketmaster-csv output/ticketmaster_cancelled_events.csv
```

Deduplicate StubHub by `eventId`, Ticketmaster by `tmId` (falling back to `id`).
Require a cancelled/canceled Ticketmaster status and reject explicit
`allowPublicPurchase=False`. Match exact venue country, URL-derived full date
and normalized city; require US state agreement when both states are present.
A recognized venue country overrides the StubHub seed city's country.

To match existing snapshots and fetch listing floor prices in one run:

```bash
cancelled-event run \
  --stubhub-csv output/events.csv \
  --ticketmaster-csv output/ticketmaster_cancelled_events.csv \
  --output-dir output/cancelled/new-run
```

This uses the same matcher and keeps every match pair/link in the priced CSV.
The default price source uses Playwright and fetches only event page one.
See [price refresh](price_refresh.md) for browser setup and minimum-price scope rules.

After accent/punctuation normalization, require venue similarity ≥ 0.86 and
title similarity ≥ 0.80. Whole-phrase title containment also qualifies when the
shorter title has at least two words and eight characters. Attraction scores
are evidence only. Missing dates and weak matches are excluded.

The output is an atomic CSV with match scores and source IDs/URLs; there is no
manual-review list. Matches show representation in the collected StubHub feed,
not active inventory: `hasActiveListings=True` is not required. Missing states,
URL dates, internal API omissions and similar titles can affect precision or
recall; a live inventory claim needs separate verification.
