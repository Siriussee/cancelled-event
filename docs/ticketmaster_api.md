# Optional official Ticketmaster API access check

Create a private file in the current working directory named `.env.ticketmaster.local`,
containing only `TICKETMASTER_API_KEY=<your key>` (comments are allowed), then:

```bash
chmod 600 .env.ticketmaster.local
cancelled-event verify-ticketmaster
```

Use `--key-file /absolute/path/to/key.env` to select a file explicitly, including
when running from an installed package outside the checkout. The default is
resolved from the working directory when the command runs.

Linux-only helper using the standard library. It checks the official Discovery
API and US/CA Discovery Feed file metadata with two HTTPS requests, one second
apart; no retry, feed download or cancellation ingestion. Exit codes: 0 success,
1 access failure, 2 local/unexpected error, 130 interruption.

The key file must be owned by the current user, private and not a symlink. The
helper blocks redirects, bypasses inherited proxies and verifies TLS. Output
contains only typed summaries; keys, authenticated URLs and raw responses are
not printed or saved. `.env.*` files are ignored by Git.
