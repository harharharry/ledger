"""GBP/USD spot rate from Frankfurter (ECB reference rates, free, no key).

ECB publishes one rate per business day — exactly the granularity a daily
bot needs. The returned rate is USD per 1 GBP, the fill engine's fx_rate
convention. The *cost* of converting is separate (config [fx]).
"""

from __future__ import annotations

from decimal import Decimal

from ..money import to_decimal
from .http import DataError, get_json

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base=GBP&symbols=USD"


def fetch_gbp_usd(get=get_json) -> Decimal:
    payload = get(FRANKFURTER_URL)
    return parse_gbp_usd(payload)


def parse_gbp_usd(payload: object) -> Decimal:
    if not isinstance(payload, dict) or "rates" not in payload:
        raise DataError("unexpected Frankfurter payload shape")
    rate = payload["rates"].get("USD")
    if rate is None:
        raise DataError("Frankfurter payload missing USD rate")
    rate = to_decimal(rate)
    # GBPUSD has spent the last half-century between ~1.0 and ~2.7;
    # anything outside that is a broken feed, not a market move.
    if not (Decimal("0.8") < rate < Decimal("3")):
        raise DataError(f"implausible GBPUSD rate {rate}")
    return rate
