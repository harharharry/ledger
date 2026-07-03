import datetime as dt
from decimal import Decimal

import pytest

from ledger.data.coingecko import fetch_daily_closes, parse_market_chart
from ledger.data.http import DataError

# 2026-07-01 00:00 UTC and 2026-07-02 00:00 UTC in ms
DAY1 = 1782864000000
DAY2 = 1782950400000
HOUR = 3600 * 1000


def test_parse_daily_points():
    payload = {"prices": [[DAY1, Decimal("78000.5")], [DAY2, Decimal("78500")]]}
    series = parse_market_chart(payload, symbol="BTC", currency="GBP")
    assert len(series) == 2
    assert series.closes[0].date == dt.date(2026, 7, 1)
    assert series.closes[0].close == Decimal("78000.5")
    assert series.latest.close == Decimal("78500")
    assert series.currency == "GBP"


def test_hourly_granularity_downsamples_to_last_point_per_day():
    payload = {
        "prices": [
            [DAY1, Decimal("78000")],
            [DAY1 + HOUR, Decimal("78100")],
            [DAY1 + 2 * HOUR, Decimal("78200")],  # last point of day 1
            [DAY2, Decimal("79000")],
        ]
    }
    series = parse_market_chart(payload, symbol="BTC", currency="GBP")
    assert len(series) == 2
    assert series.closes[0].close == Decimal("78200")


def test_empty_prices_fails_loudly():
    with pytest.raises(DataError, match="no prices"):
        parse_market_chart({"prices": []}, symbol="BTC", currency="GBP")


def test_wrong_shape_fails_loudly():
    with pytest.raises(DataError, match="payload shape"):
        parse_market_chart({"error": "rate limited"}, symbol="BTC", currency="GBP")


def test_malformed_point_fails_loudly():
    with pytest.raises(DataError, match="malformed"):
        parse_market_chart({"prices": [[DAY1]]}, symbol="BTC", currency="GBP")


def test_fetch_builds_url_and_optional_key(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, **kw):
        seen["url"] = url
        seen["headers"] = headers
        return {"prices": [[DAY1, Decimal("78000")]]}

    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    fetch_daily_closes("BTC", "bitcoin", days=100, get=fake_get)
    assert "coins/bitcoin/market_chart" in seen["url"]
    assert "vs_currency=gbp" in seen["url"]
    assert "interval=daily" in seen["url"]
    assert seen["headers"] == {}

    monkeypatch.setenv("COINGECKO_API_KEY", "demo-key")
    fetch_daily_closes("BTC", "bitcoin", get=fake_get)
    assert seen["headers"] == {"x-cg-demo-api-key": "demo-key"}
