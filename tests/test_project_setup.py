#!/usr/bin/env python3
"""Project setup checks for packaging and Git hygiene."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


class ProjectSetupTests(unittest.TestCase):
    def test_pyproject_defines_importable_commands(self) -> None:
        metadata_path = PROJECT_ROOT / "pyproject.toml"

        metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        project = metadata["project"]
        scripts = project["scripts"]

        self.assertEqual(project["name"], "cancelled-event")
        self.assertIn(">=3.11", project["requires-python"])
        self.assertEqual(
            set(scripts),
            {"cancelled-event"},
        )

        for entry_point in scripts.values():
            module_name, function_name = entry_point.split(":", maxsplit=1)
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, function_name)))

    def test_gitignore_excludes_generated_artifacts(self) -> None:
        if not (PROJECT_ROOT / ".git").exists():
            self.skipTest("Git-specific check requires a checkout")
        generated_paths = [
            "src/cancelled_event/sources/__pycache__/stubhub_events.cpython-314.pyc",
            ".venv/bin/python",
            ".env",
            ".env.ticketmaster.local",
            "dist/package.whl",
            "src/cancelled_event.egg-info/PKG-INFO",
            "output/events.csv",
            "log/progress_log_event.log",
            "log/scraper.log",
            "log/venue_fetcher.log",
            "output/wget_output/toronto_p0.json",
            "output/venues/123_venue.json",
            "data/worldcities.filtered.csv",
            "data/worldcities_feature_audit.csv",
        ]

        result = subprocess.run(
            ["git", "check-ignore", *generated_paths],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), generated_paths)

    def test_console_modules_import_without_output_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC_ROOT)

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import cancelled_event.sources.stubhub_events; "
                        "import cancelled_event.sources.venue_maps; "
                        "import cancelled_event.sources.ticketmaster_cancellations; "
                        "import cancelled_event.matching; "
                        "import cancelled_event.sources.city_seeds; "
                        "import cancelled_event.pricing.refresh; "
                        "import cancelled_event.workflow"
                    ),
                ],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(tmpdir) / "output" / "wget_output").exists())
            self.assertFalse((Path(tmpdir) / "output" / "venues").exists())
            self.assertFalse((Path(tmpdir) / "log" / "scraper.log").exists())
            self.assertFalse((Path(tmpdir) / "log" / "venue_fetcher.log").exists())
            self.assertEqual(list(Path(tmpdir).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
