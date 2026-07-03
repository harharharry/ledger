"""Tiny stdlib HTTP helper for the data clients.

JSON numbers are parsed straight to Decimal (parse_float) so float artifacts
never enter the pipeline. Transient failures (429, 5xx, network errors) retry
with backoff; anything else fails loudly. Error messages carry the URL but
never the request headers — headers hold API keys.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from decimal import Decimal


class DataError(Exception):
    pass


RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Some providers (Cloudflare in front of Frankfurter, for one) 403 the default
# Python urllib User-Agent.
USER_AGENT = "ledger/0.1 (personal paper-trading tool)"


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    retries: int = 3,
    backoff_seconds: float = 2.0,
) -> object:
    last_error: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(backoff_seconds * attempt)
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_STATUS:
                raise DataError(f"GET {url} failed: HTTP {e.code}") from e
            last_error = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_error = e
    raise DataError(f"GET {url} failed after {retries} attempts: {last_error}")
