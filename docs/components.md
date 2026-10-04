# Component contracts

| Component | File | Contract |
| --- | --- | --- |
| City filter | `filter_worldcities_by_geonames_feature.py` | GeoNames via Open-Meteo; require same country, nearest exact name within 25 km or a name variant within 2 km. Keep PPLC/PPLA/PPLA2/PPL; persist an audit for resume. |
| StubHub discovery | `event_scraper.py` | City CSV → append-only event CSV, raw pages and coordinate checkpoints; deduplicate within each seed city and restore written IDs on resume. |
| Venue enrichment | `venue_map_fetcher.py` | Unique numeric event/category pairs → atomic JSON; validate existing cache before skipping. |
| Ticketmaster collection | `ticketmaster_cancelled_event_scraper.py` | Country/date/category pages → deduplicated cancelled-event snapshot; validate pagination before publishing. |
| Matching | `cancelled_event_intersection.py` | Two CSVs → strict date/location and fuzzy venue/title intersection; scores retained for audit. |
| Price refresh | `price_fetcher.py`, `event_page_prices.py`, `listing_prices.py` | Matches → warmed Playwright GET of event page one with currency=USD, validated USD listing minimum, explicit coverage/scope, original links and raw HTML. Confirm a floor through complete coverage or agreement with declared event minimum; otherwise publish a page-only minimum. Optional native POST and Explore sources retain fresh FX conversion. |
| Cancellation workflow | `cancelled_workflow.py` | Existing SH/TM snapshots → intersection + enriched prices in a fresh directory; input hashes, stage status and evidence paths in a manifest; nonzero exit for incomplete/failed prices. |
| Configuration | `config.py` | Environment defaults read at instance construction; validate limits, finite delays and booleans. No dotenv loader. |
| HTTP | `http.py` | Shared StubHub curl transport; HTTP/JSON checks, timeout and exponential retry, capped at 60 seconds; optional JSON POST, raw failure response capture, session cookies and per-attempt pacing. |
| Runtime | `runtime.py` | CLI-only logging, bounded worker queue and atomic CSV/JSON publication. |
| Official API check | `scripts/verify_ticketmaster_api.py` | Two small official API access probes; private local key file, redacted output; no feed download. |

Python component paths are relative to `src/stubhub_all_event_scraper/`.

The matcher output uses `ticketmaster_discovery_id` for the website API identifier
(the old `ticketmaster_graphql_id` label is retired).

A StubHub checkpoint stores `lat,lng,next_page`; `-1` marks a completed discovery
slice. Old positive-page checkpoints remain readable. Checkpoints require the
corresponding event CSV. Changing coordinates, sort settings or date scope
requires a fresh crawl. Use both fresh paths, for example:

```bash
stubhub-scrape-events \
  --events-csv output/new-run/events.csv \
  --progress-log log/new-run/progress.log
```

Clear the city audit when changing filtering policy. Successful cached rows are
reused; network errors are retried on the next run. Venue files include category
IDs; old event-only venue files are kept locally but are not reused.
