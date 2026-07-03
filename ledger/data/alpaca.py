"""Stock/ETF daily bars from the Alpaca Market Data API (free IEX feed).

Credentials come from environment variables only and the client fails loudly
if they are missing (CLAUDE.md non-negotiable 2). Data-only usage still uses
the account's API keys — they must be created trade-only with withdrawals
disabled, like every key in this project.

Bars are split-adjusted so a stock split never looks like a crash to the
50-day MA.
"""

from __future__ import annotations

import datetime as dt
import os

from ..money import to_decimal
from .http import DataError, get_json
from .types import DailyClose, PriceSeries

DATA_URL = "https://data.alpaca.markets/v2"
KEY_ENV = "APCA_API_KEY_ID"
SECRET_ENV = "APCA_API_SECRET_KEY"


class MissingCredentialsError(DataError):
    pass


def _credentials() -> tuple[str, str]:
    missing = [name for name in (KEY_ENV, SECRET_ENV) if not os.environ.get(name)]
    if missing:
        raise MissingCredentialsError(
            f"missing environment variable(s): {', '.join(missing)}. "
            "Create Alpaca keys trade-only with withdrawals disabled."
        )
    return os.environ[KEY_ENV], os.environ[SECRET_ENV]


def fetch_daily_closes(
    symbol: str,
    days: int = 100,
    feed: str = "iex",
    get=get_json,
) -> PriceSeries:
    key, secret = _credentials()
    # Ask for double the window in calendar days: markets are shut roughly
    # 2 days in 7 plus holidays, and we trim to the newest `days` bars anyway.
    start = (dt.date.today() - dt.timedelta(days=days * 2)).isoformat()
    url = (
        f"{DATA_URL}/stocks/{symbol}/bars"
        f"?timeframe=1Day&adjustment=split&feed={feed}&start={start}&limit=1000"
    )
    payload = get(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    series = parse_bars(payload, symbol=symbol)
    if len(series) > days:
        series = PriceSeries(
            symbol=series.symbol, currency=series.currency, closes=series.closes[-days:]
        )
    return series


def parse_bars(payload: object, symbol: str) -> PriceSeries:
    if not isinstance(payload, dict) or not isinstance(payload.get("bars"), list):
        raise DataError(f"{symbol}: unexpected Alpaca payload shape")
    bars = payload["bars"]
    if not bars:
        raise DataError(f"{symbol}: Alpaca returned no bars — check the symbol")
    closes = []
    for bar in bars:
        try:
            day = dt.date.fromisoformat(str(bar["t"])[:10])
            closes.append(DailyClose(date=day, close=to_decimal(bar["c"])))
        except (KeyError, TypeError, ValueError) as e:
            raise DataError(f"{symbol}: malformed Alpaca bar {bar!r}") from e
    return PriceSeries(symbol=symbol, currency="USD", closes=tuple(closes))
