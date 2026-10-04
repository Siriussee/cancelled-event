# Listing floor path investigation — October 4, 2026

The listing-grid adapter is implemented and integrated with cancellation
matching. Its successful live response contract is **not yet verified** in
this environment: StubHub's event routes return HTTP 403. It must not be
described as a proven working live price source.

## Observations

| Path | Observed result | Consequence |
| --- | --- | --- |
| Existing Explore refresh | 55 `no_quote`, one `not_found` across 56 target events | No floor can be recovered from this snapshot's display quotes. |
| `GetLocationSettings` | HTTP 200 with currency rates | FX rates are available independently of listing access. |
| Canonical event HTML and numeric `/event/<id>/` | HTTP 403, challenge response | HTML or structured offer parsing cannot be validated here. |
| Standard Chromium with JavaScript and a 15-second wait | HTTP 403 followed by a CAPTCHA frame | Normal browser loading does not resolve access in this environment. |
| JSON POST to event URLs | HTTP 403 for all 56 matched events | Record request errors and retain response bodies; never infer no tickets. |
| NBA preseason Lakers at Warriors, event `161659240` | Listing POST also returned HTTP 403 | An expected active-event control is blocked too; it cannot validate the success schema. |
| Guessed `/Browse/Events/GetListings` route | HTTP 404 | Do not adopt this route as a fallback. |

Explore's `hasActiveListings=False` also appeared throughout the collected
discovery pages. It is not used to skip requests or to establish empty
inventory. The event page adapter follows the published event-page JSON POST
pattern, but its response schema is currently supported by offline contract
fixtures rather than a successful fresh site response. A public example of
this POST pattern is [StubHubScraper](https://github.com/robertocommit/StubHubScraper/blob/main/main.py);
it is a reference, not proof that the site's current contract is unchanged.

## Complete workflow observation

The local run `output/cancelled_floor_workflow_20261004/` used the collected
StubHub shortlist and normalized Ticketmaster/TicketWeb cancellations:

- 58 match pairs, representing 56 unique StubHub events.
- All 58 rows and both source URLs preserved in `prices/enriched.csv`.
- 56 listing POSTs and 56 final raw page responses preserved.
- 56 `request_error` results; zero numeric prices and zero `no_listings` claims.
- Price stage and manifest marked `needs_attention`; process exit code 1.
- Source hashes retained; temporary session cookies removed.

These are ignored local run artifacts, not bundled fixture data. The
workflow makes the limitation reviewable without replacing input snapshots.
The separate control output is `control-prices/`, and the browser observation
is retained in `browser_observation.html` under the same run directory.

## Implementation validation

Offline tests cover complete multi-page minimum selection, cross-currency
conversion, minimum selection before cent rounding, duplicate match rows,
explicit empty inventory, ambiguous currency, malformed/nonfinite prices,
wrong event/page identities, changing totals, repeated listing IDs,
truncation, partial failures, retained HTTP failure bodies and the combined
matching/price workflow. Existing Explore behavior remains available with
`--source explore`.
All 110 tests, Ruff lint/format checks and source/wheel builds passed.

Live success still needs a successful listing response, including a known
priced event as a positive control, before this adapter can be considered
operationally verified. A 403 response from a cancelled event cannot serve
as an empty-inventory test.
