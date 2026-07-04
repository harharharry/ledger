"""Live API smoke tests — skipped unless RUN_INTEGRATION=1 is set.

Run with: RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/test_data_integration.py -v
The Alpaca test additionally needs APCA_API_KEY_ID / APCA_API_SECRET_KEY.
"""

import os
from decimal import Decimal

import pytest

from ledger.data import coingecko, fx

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to hit live APIs",
)


def test_coingecko_live():
    series = coingecko.fetch_daily_closes("BTC", "bitcoin", days=10)
    assert series.currency == "GBP"
    assert len(series) >= 8
    assert series.latest.close > Decimal("1000")  # BTC above £1k, sanity only


def test_frankfurter_live():
    rate = fx.fetch_gbp_usd()
    assert Decimal("1.0") < rate < Decimal("2.0")


def test_coingecko_usd_pair_live():
    # HYPE trades on Kraken's USD pair, so its series is fetched in USD
    series = coingecko.fetch_daily_closes("HYPE", "hyperliquid", days=10, vs_currency="usd")
    assert series.currency == "USD"
    assert len(series) >= 8
