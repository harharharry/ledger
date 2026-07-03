"""Crypto daily closes from the CoinGecko public API.

Free tier, no account required. An optional demo API key (env
COINGECKO_API_KEY) raises the rate limit; absent is fine at one run per day.
CoinGecko may return finer-than-daily granularity depending on tier, so the
parser normalises to one close per UTC day (last point of the day wins — for
today that is the most recent snapshot, which is what a daily run wants).
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

from ..money import to_decimal
from .http import DataError, get_json
from .types import DailyClose, PriceSeries

BASE_URL = "https://api.coingecko.com/api/v3"
API_KEY_ENV = "COINGECKO_API_KEY"  # optional


def fetch_daily_closes(
    symbol: str,
    coingecko_id: str,
    days: int = 100,
    vs_currency: str = "gbp",
    get=get_json,
) -> PriceSeries:
    url = (
        f"{BASE_URL}/coins/{coingecko_id}/market_chart"
        f"?vs_currency={vs_currency}&days={days}&interval=daily"
    )
    headers = {}
    api_key = os.environ.get(API_KEY_ENV)
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    payload = get(url, headers=headers)
    return parse_market_chart(payload, symbol=symbol, currency=vs_currency.upper())


def parse_market_chart(payload: object, symbol: str, currency: str) -> PriceSeries:
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise DataError(f"{symbol}: unexpected CoinGecko payload shape")
    points = payload["prices"]
    if not points:
        raise DataError(f"{symbol}: CoinGecko returned no prices")

    by_day: dict[dt.date, Decimal] = {}
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise DataError(f"{symbol}: malformed CoinGecko price point {point!r}")
        ms_epoch, price = point
        seconds = int(to_decimal(ms_epoch) / 1000)
        day = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).date()
        by_day[day] = to_decimal(price)  # last point of the day wins

    closes = tuple(DailyClose(date=d, close=by_day[d]) for d in sorted(by_day))
    return PriceSeries(symbol=symbol, currency=currency, closes=closes)
