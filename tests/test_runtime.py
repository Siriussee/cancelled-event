"""Checks for environment parsing, atomic files, HTTP retries and bounded scheduling."""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from stubhub_all_event_scraper import http, runtime
from stubhub_all_event_scraper.config import ScraperConfig, env_bool


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(http.logger, "disabled", True))

    def test_environment_is_read_per_instance(self):
        first = ScraperConfig(concurrent_cities=2)
        with patch.dict(os.environ, {"CONCURRENT_CITIES": "7", "WAIT_SECONDS": "0.25"}):
            second = ScraperConfig()
        self.assertEqual(first.concurrent_cities, 2)
        self.assertEqual(second.concurrent_cities, 7)
        self.assertEqual(second.wait_seconds, 0.25)

    def test_boolean_false_is_false_and_invalid_input_is_rejected(self):
        self.assertFalse(env_bool("false"))
        self.assertTrue(env_bool("TRUE"))
        with self.assertRaises(ValueError):
            env_bool("maybe")

    def test_invalid_settings_fail_before_io(self):
        for settings in (
            {"max_retries": 0},
            {"wait_seconds": -1},
            {"retry_delay": float("nan")},
            {"request_timeout": 0},
            {"duplicate_stop_ratio": 0},
            {"log_level": "oops"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ScraperConfig(**settings)

    def test_atomic_writer_preserves_previous_file_on_failure_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.csv"
            path.write_text("original")
            with self.assertRaises(RuntimeError), runtime.atomic_output(path) as handle:
                handle.write("partial")
                raise RuntimeError("disk failure")
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [path])
            with runtime.atomic_output(path) as handle:
                handle.write("complete")
            self.assertEqual(path.read_text(), "complete")

    def test_atomic_writer_handles_parallel_distinct_outputs(self):
        with tempfile.TemporaryDirectory() as directory:

            def write(index):
                with runtime.atomic_output(Path(directory) / f"{index}.json") as handle:
                    json.dump({"id": index}, handle)

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(20)))
            self.assertEqual(len(list(Path(directory).iterdir())), 20)

    def test_http_errors_and_invalid_json_are_retried_then_fail(self):
        config = ScraperConfig(max_retries=2, retry_delay=0, request_timeout=9)
        for result in (
            Mock(returncode=22, stderr="HTTP 403", stdout="{}"),
            Mock(returncode=0, stderr="", stdout="<html>"),
            Mock(returncode=0, stderr="", stdout='{"error":"blocked"}'),
        ):
            with (
                self.subTest(result=result),
                patch.object(http.subprocess, "run", return_value=result) as run,
                patch.object(http.time, "sleep"),
            ):
                with self.assertRaises(http.ResponseError):
                    http.get_json("https://www.stubhub.com/test", config)
                self.assertEqual(run.call_count, 2)
                self.assertIn("--fail-with-body", run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs["timeout"], 14)

    def test_http_timeout_is_retried_and_successful_json_is_returned(self):
        result = Mock(returncode=0, stderr="", stdout='{"events":[],"total":0}')
        with (
            patch.object(
                http.subprocess, "run", side_effect=[subprocess.TimeoutExpired("curl", 1), result]
            ) as run,
            patch.object(http.time, "sleep"),
        ):
            self.assertEqual(
                http.get_json("https://www.stubhub.com/test", ScraperConfig(max_retries=2)),
                {"events": [], "total": 0},
            )
        self.assertEqual(run.call_count, 2)

    def test_worker_scheduler_does_not_eagerly_consume_all_items(self):
        gate = threading.Event()
        consumed = []

        def items():
            for item in range(30):
                consumed.append(item)
                yield item

        def work(item):
            gate.wait(2)
            return item

        # Unblock after workers are running without leaving a blocked thread on assertion failures.
        timer = threading.Timer(0.05, gate.set)
        timer.start()
        results = runtime.worker_results(work, items(), 2)
        next(results)
        self.assertLessEqual(len(consumed), 4)
        remaining = list(results)
        self.assertEqual(len(remaining), 29)
        timer.join()
