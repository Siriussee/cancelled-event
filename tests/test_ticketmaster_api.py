"""Security checks for the local Ticketmaster API verification helper."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from cancelled_event.diagnostics import ticketmaster_api as tm

FAKE_KEY = "synthetic_test_key_1234567890"


class TicketmasterApiVerificationTests(unittest.TestCase):
    def test_key_file_permissions_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.env"
            path.write_text(f"TICKETMASTER_API_KEY={FAKE_KEY}\n")
            path.chmod(0o600)
            self.assertEqual(tm.read_key(path), FAKE_KEY)
            path.chmod(0o644)
            with self.assertRaises(tm.VerificationError):
                tm.read_key(path)
            path.chmod(0o600)
            link = Path(directory) / "link.env"
            link.symlink_to(path)
            with self.assertRaises(tm.VerificationError):
                tm.read_key(link)

    def test_empty_key_stops_before_any_network_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.env"
            path.write_text("TICKETMASTER_API_KEY=\n")
            path.chmod(0o600)
            with patch.object(tm, "get_json") as request, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tm.main(["--key-file", str(path)]), 2)
            request.assert_not_called()

    def test_http_and_network_errors_do_not_expose_key_or_url(self) -> None:
        url = f"https://app.ticketmaster.com/test?apikey={FAKE_KEY}"
        errors = (
            HTTPError(url, 403, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode())),
            URLError(url),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(tm, "build_opener", return_value=opener):
                    with self.assertRaises(tm.VerificationError) as caught:
                        tm.get_json("/discovery-feed/v2/events", FAKE_KEY)
                self.assertNotIn(FAKE_KEY, str(caught.exception))
                self.assertNotIn("https://", str(caught.exception))

    def test_feed_summary_drops_echoed_keys_and_download_links(self) -> None:
        metadata = {
            "uri": f"https://example.invalid/download?apikey={FAKE_KEY}",
            "num_events": 15,
            "compressed_size_bytes": 1234,
            "last_updated": FAKE_KEY,
        }
        payload = {
            "countries": {"US": {"CSV": metadata}, "CA": {"CSV": metadata}},
            "echoed_key": FAKE_KEY,
        }
        result = tm.feed_summary(payload)
        self.assertTrue(result["ok"])
        serialized = json.dumps(result)
        self.assertNotIn(FAKE_KEY, serialized)
        self.assertNotIn("https://", serialized)
        metadata["num_events"] = FAKE_KEY
        self.assertNotIn(FAKE_KEY, json.dumps(tm.feed_summary(payload)))

    def test_missing_country_is_not_reported_as_success(self) -> None:
        result = tm.feed_summary({"countries": {"US": {}, "CA": {}}})
        self.assertFalse(result["ok"])
        with self.assertRaises(tm.VerificationError):
            tm.discovery_summary({"fault": {"faultstring": FAKE_KEY}})

    def test_transport_uses_fixed_host_disables_proxy_and_blocks_redirects(self) -> None:
        response = Mock(status=200)
        response.read.return_value = b'{"countries": {}}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(tm, "build_opener", return_value=opener) as build:
            tm.get_json("/discovery-feed/v2/events", FAKE_KEY)
        request = opener.open.call_args.args[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual((parsed.scheme, parsed.hostname), ("https", "app.ticketmaster.com"))
        self.assertEqual(parse_qs(parsed.query)["apikey"], [FAKE_KEY])
        proxy_handler, redirect_handler = build.call_args.args
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsNone(
            redirect_handler.redirect_request(None, None, 302, "", {}, "https://example.invalid")
        )
        with self.assertRaises(tm.VerificationError):
            tm.get_json("https://example.invalid", FAKE_KEY)

    def test_unexpected_exception_is_hidden_at_command_boundary(self) -> None:
        output = io.StringIO()
        with (
            patch.object(tm, "read_key", return_value=FAKE_KEY),
            patch.object(tm, "verify", side_effect=RuntimeError(FAKE_KEY)),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(tm.main([]), 2)
        self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_final_output_redacts_key_even_if_summary_accidentally_includes_it(self) -> None:
        output = io.StringIO()
        report = {"checks": {"discovery_api": {"ok": True}}, "note": FAKE_KEY}
        with (
            patch.object(tm, "read_key", return_value=FAKE_KEY),
            patch.object(tm, "verify", return_value=report),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(tm.main([]), 0)
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
