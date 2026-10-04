"""Validated environment settings, read when a configuration is constructed."""

import math
import os
from dataclasses import dataclass, field


def env_field(name, default, convert=str):
    """Read one environment variable at instance creation, never at import."""
    return field(default_factory=lambda: convert(os.getenv(name, str(default))))


def env_bool(value: str) -> bool:
    """Parse explicit boolean values; reject misspellings."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def validate_number(name: str, value: float, minimum: float = 0) -> None:
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")


@dataclass
class RequestConfig:
    """Shared StubHub request and logging settings."""

    max_retries: int = env_field("MAX_RETRIES", 3, int)
    retry_delay: float = env_field("RETRY_DELAY", 2.0, float)
    request_timeout: int = env_field("REQUEST_TIMEOUT", 30, int)
    log_level: str = env_field("LOG_LEVEL", "INFO")
    user_agent: str = env_field(
        "USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36",
    )

    def __post_init__(self) -> None:
        validate_number("max_retries", self.max_retries, 1)
        validate_number("request_timeout", self.request_timeout, 1)
        validate_number("retry_delay", self.retry_delay)
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")


@dataclass
class ScraperConfig(RequestConfig):
    """City discovery inputs, output paths and pagination limits."""

    input_csv: str = env_field("INPUT_CSV", "data/worldcities.csv")
    combined_csv: str = env_field("EVENTS_CSV", "output/events.csv")
    progress_log: str = env_field("PROGRESS_LOG_FILE", "log/progress_log_event.log")
    out_dir: str = env_field("WGET_OUTPUT_DIR", "output/wget_output")
    log_file: str = env_field("SCRAPER_LOG_FILE", "log/scraper.log")
    concurrent_cities: int = env_field("CONCURRENT_CITIES", 5, int)
    wait_seconds: float = env_field("WAIT_SECONDS", 1.0, float)
    max_pages_per_city: int = env_field("MAX_PAGES_PER_CITY", 100, int)
    duplicate_stop_ratio: float = env_field("DUPLICATE_STOP_RATIO", 0.75, float)
    explore_sort_type: str = env_field("EXPLORE_SORT_TYPE", "Distance")
    explore_page_size: int = env_field("EXPLORE_PAGE_SIZE", 100, int)

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in ("concurrent_cities", "max_pages_per_city", "explore_page_size"):
            validate_number(name, getattr(self, name), 1)
        validate_number("wait_seconds", self.wait_seconds)
        if not 0 < self.duplicate_stop_ratio <= 1:
            raise ValueError("duplicate_stop_ratio must be greater than 0 and at most 1")


@dataclass
class VenueFetcherConfig(RequestConfig):
    """Venue enrichment inputs, output paths and concurrency."""

    events_csv: str = env_field("EVENTS_CSV", "output/events.csv")
    venue_dir: str = env_field("VENUE_OUTPUT_DIR", "output/venues")
    log_file: str = env_field("VENUE_FETCHER_LOG_FILE", "log/venue_fetcher.log")
    concurrent_venues: int = env_field("CONCURRENT_VENUES", 10, int)
    wait_seconds: float = env_field("WAIT_SECONDS", 1.0, float)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_number("concurrent_venues", self.concurrent_venues, 1)
        validate_number("wait_seconds", self.wait_seconds)
