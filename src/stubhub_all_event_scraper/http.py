"""StubHub JSON transport with HTTP failure checks and bounded retries."""

import json
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .config import RequestConfig
from .runtime import atomic_output

logger = logging.getLogger(__name__)


class ResponseError(RuntimeError):
    """A request or JSON response cannot safely be used."""


def get_json(
    url: str,
    config: RequestConfig,
    *,
    data: str | None = None,
    json_data: dict | None = None,
    cookie_file: Path | None = None,
    before_request: Callable[[], None] | None = None,
    response_path: Path | None = None,
) -> dict:
    """Retry transport and JSON failures, using curl for compressed responses."""
    if data is not None and json_data is not None:
        raise ValueError("Choose either form data or JSON data")
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--compressed",
        "--fail-with-body",
        "--max-time",
        str(config.request_timeout),
        "-H",
        f"User-Agent: {config.user_agent}",
        "-H",
        "Accept: application/json",
        "-H",
        "Referer: https://www.stubhub.com/",
        "-H",
        "Origin: https://www.stubhub.com",
    ]
    if data is not None:
        command.extend(["--data", data])
    if json_data is not None:
        command.extend(["-H", "Content-Type: application/json", "--data", json.dumps(json_data)])
    if cookie_file is not None:
        command.extend(["--cookie", str(cookie_file), "--cookie-jar", str(cookie_file)])
    command.append(url)
    for attempt in range(config.max_retries):
        try:
            if before_request is not None:
                before_request()
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=config.request_timeout + 5,
            )
            # Keep the final response even when HTTP or JSON validation fails.
            if response_path is not None:
                with atomic_output(response_path) as handle:
                    handle.write(result.stdout)
            if result.returncode:
                raise ResponseError(f"curl exited {result.returncode}: {result.stderr.strip()}")
            payload = json.loads(result.stdout)
            if (
                not isinstance(payload, dict)
                or not payload
                or payload.get("error")
                or payload.get("errors")
            ):
                raise ResponseError("Expected a non-empty JSON object without API errors")
            return payload
        except (ResponseError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            if attempt == config.max_retries - 1:
                raise ResponseError(
                    f"Request failed after {config.max_retries} attempts: {exc}"
                ) from exc
            delay = min(config.retry_delay * 2**attempt, 60)
            logger.warning("Request failed: %s; retrying in %.1fs", exc, delay)
            time.sleep(delay)
    raise AssertionError("max_retries must be positive")
