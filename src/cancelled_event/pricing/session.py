"""Paced HTTP session for optional price sources and currency settings."""

import time

from ..http import get_json

SETTINGS_URL = "https://www.stubhub.com/secure/Browse/DefaultMaster/GetLocationSettings"


class PriceSession:
    """Share cookies for settings and Explore; pace all requests, including retries."""

    def __init__(self, config, output):
        self.config = config
        self.cookie_file = output / ".session.cookies"
        self.last_request = None

    def pace(self):
        if self.last_request is not None:
            time.sleep(
                max(0, self.config.request_interval - (time.monotonic() - self.last_request))
            )
        self.last_request = time.monotonic()

    def fetch(self, url, **kwargs):
        return get_json(
            url, self.config, cookie_file=self.cookie_file, before_request=self.pace, **kwargs
        )

    def close(self):
        self.cookie_file.unlink(missing_ok=True)
