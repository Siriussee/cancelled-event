"""Validated settings for browser and optional HTTP price sources."""

import os
from dataclasses import dataclass

from ..config import RequestConfig, env_field, validate_number


@dataclass
class PriceConfig(RequestConfig):
    """Serial, bounded price refresh; inputs remain untouched."""

    input_csv: str = env_field("PRICE_INPUT_CSV", "")
    events_csv: str = env_field("STUBHUB_EVENTS_CSV", "")
    cities_csv: str = env_field("INPUT_CSV", "data/worldcities.csv")
    source_raw_dir: str = env_field("PRICE_SOURCE_RAW_DIR", "")
    output_dir: str = env_field("PRICE_OUTPUT_DIR", "output/prices")
    request_interval: float = env_field("PRICE_REQUEST_INTERVAL", 2.0, float)
    page_radius: int = env_field("PRICE_PAGE_RADIUS", 4, int)
    log_file: str = env_field("PRICE_LOG_FILE", "log/prices.log")
    explore_sort_type: str = env_field("EXPLORE_SORT_TYPE", "Distance")
    explore_page_size: int = env_field("EXPLORE_PAGE_SIZE", 100, int)
    source: str = env_field("PRICE_SOURCE", "event-page")
    listing_page_size: int = env_field("PRICE_LISTING_PAGE_SIZE", 50, int)
    listing_max_pages: int = env_field("PRICE_LISTING_MAX_PAGES", 100, int)
    browser_executable: str = env_field("PRICE_BROWSER_EXECUTABLE", "")
    browser_wait_seconds: float = env_field("PRICE_BROWSER_WAIT_SECONDS", 8.0, float)

    def __post_init__(self):
        super().__post_init__()
        if not self.input_csv:
            self.input_csv = os.getenv(
                "CANCELLED_EVENT_INTERSECTION_CSV",
                "output/matches.csv",
            )
        if not self.events_csv:
            self.events_csv = os.getenv("EVENTS_CSV", "output/events.csv")
        validate_number("request_interval", self.request_interval)
        validate_number("page_radius", self.page_radius)
        validate_number("explore_page_size", self.explore_page_size, 1)
        validate_number("listing_page_size", self.listing_page_size, 1)
        validate_number("listing_max_pages", self.listing_max_pages, 1)
        validate_number("browser_wait_seconds", self.browser_wait_seconds)
        if self.source not in {"listings", "explore", "event-page"}:
            raise ValueError("Price source must be event-page, listings or explore")
