# Repository review

## Purpose and boundaries

The project turns location-based StubHub discovery and date-based Ticketmaster
cancellations into reproducible local artifacts and conservative matches.
Venue maps are optional enrichment. It is not a full-market inventory service;
no live ticket availability or complete worldwide coverage is established.
The flow is in [README](../README.md); contracts are in [components](components.md).

## Findings and changes

| Finding | Resolution |
| --- | --- |
| Runtime dependencies declared but unused | Standard-library runtime; remove requirements.txt. Keep Ruff and build as development tools. |
| Duplicated configuration and retry logic | Shared config/HTTP/runtime helpers; environment read on construction, finite values validated. |
| .env copy instructions suggested automatic loading | Document explicit shell export; remove unsupported rate-limit, debug and SSL switches. |
| Import-time logging / missing command help | Configure logging only on execution; all subcommands support --help. |
| Failures swallowed and reported as success | StubHub workers propagate failures to a nonzero command result; preserve valid checkpoints. |
| Resume could duplicate a committed page | Recover written IDs; synchronize CSV/checkpoint writes; retain explicit completed-city markers. |
| Coordinate-derived names lost signs or Unicode distinctions | Slug plus a digest of full city/country/coordinates. |
| Venue workers raced on counters and event-only filenames | Aggregate results in the main thread; filenames include event/category, cache is validated. |
| Unlimited queued futures | Bound the queue to twice the worker count. |
| Partial snapshots could replace valid files | Atomic CSV/JSON publication; Ticketmaster detects missing/repeated records and unstable totals. |
| GeoNames could select a distant or cross-border namesake | Validate country, coordinates and distance; do not cache failed network audits. |
| Matching used seed country for cross-border discoveries | Prefer recognized venue country; preserve non-Latin text and accept both cancellation spellings. |
| Docs described retired GraphQL endpoints and old run counts | Replace with concise current contracts; official Feed probing is documented separately from ingestion. |
| Temporary browser probe / incompatible unused city table | Remove both; preserve the existing US/Canada seed input. |

## Package and command structure

Business code lives in `src/cancelled_event/`. The `cancelled-event` command and
`python -m cancelled_event` share one dispatcher. Source adapters are in
`sources/`, price adapters and shared parsing are in `pricing/`, and the official
API check is an installed component in `diagnostics/`. Price adapters depend on
shared grid/currency parsing rather than on the price orchestrator. Generic CSV,
JSON and timestamp helpers live in `runtime.py`.

New workflow runs publish `matches.csv`; standalone matching defaults to
`output/matches.csv`. Existing environment variable names and CSV columns remain
supported. Existing local artifacts are retained at their original paths.

## Git policy

Track source, offline tests, concise docs, CI, package metadata, the MIT license,
`.env.example`, LF normalization rules, the city seed CSV and empty output/log placeholders. Ignore local
`.env` variants, generated city lists/audits, output/log data, caches, virtual
environments and build artifacts. Existing local scrape results are retained.
No secrets or scraped responses are staged; no history rewrite is required.

Cancelled Event is maintained independently at
`https://github.com/Siriussee/cancelled-event`. Keep that repository as `origin`;
there is no `upstream` remote. Preserve the original commit history and MIT
copyright attribution.

## Validation and remaining limits

Offline regression tests cover transport/retry errors, crash resume, concurrent
writes, venue cache collisions, pagination completeness, conservative matching,
key redaction, packaging entry points and Git ignores. Ruff checks formatting
and lint; release artifacts are built and their CLI help is checked outside the
checkout. A synthetic collection-to-match run also passes. Local validation uses
Python 3.14; Python 3.11 syntax is checked and CI covers 3.11, 3.13 and 3.14.

No full scrape or authenticated API verification is performed by this review.
Internal endpoint stability, live cancellation coverage and inventory remain
unverified. Reusing raw Ticketmaster pages can reuse old statuses. Use a fresh
run for a new snapshot, and one process per output/checkpoint set. The seed
CSV's original external provenance is not recorded in the repository.
