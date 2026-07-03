import datetime as dt
from decimal import Decimal

import pytest

from ledger.data.alpaca import (
    KEY_ENV,
    SECRET_ENV,
    MissingCredentialsError,
    fetch_daily_closes,
    parse_bars,
)
from ledger.data.http import DataError


def bar(day: str, close: str) -> dict:
    return {"t": f"{day}T04:00:00Z", "o": close, "h": close, "l": close,
            "c": Decimal(close), "v": 1000}


def test_parse_bars():
    payload = {"bars": [bar("2026-07-01", "436.20"), bar("2026-07-02", "437.10")]}
    series = parse_bars(payload, symbol="QQQ")
    assert series.currency == "USD"
    assert series.closes[0].date == dt.date(2026, 7, 1)
    assert series.latest.close == Decimal("437.10")


def test_no_bars_fails_loudly():
    with pytest.raises(DataError, match="no bars"):
        parse_bars({"bars": []}, symbol="TYPO")


def test_wrong_shape_fails_loudly():
    with pytest.raises(DataError, match="payload shape"):
        parse_bars({"message": "forbidden"}, symbol="QQQ")


def test_malformed_bar_fails_loudly():
    with pytest.raises(DataError, match="malformed"):
        parse_bars({"bars": [{"t": "not-a-date", "c": "1"}]}, symbol="QQQ")


def test_missing_credentials_fail_loudly(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(MissingCredentialsError, match=KEY_ENV):
        fetch_daily_closes("QQQ")


def test_fetch_sends_keys_and_trims(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "key-id")
    monkeypatch.setenv(SECRET_ENV, "secret")
    seen = {}
    many_bars = {
        "bars": [
            bar((dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(), "400")
            for i in range(150)
        ]
    }

    def fake_get(url, headers=None, **kw):
        seen["url"] = url
        seen["headers"] = headers
        return many_bars

    series = fetch_daily_closes("QQQ", days=100, get=fake_get)
    assert seen["headers"] == {"APCA-API-KEY-ID": "key-id", "APCA-API-SECRET-KEY": "secret"}
    assert "stocks/QQQ/bars" in seen["url"]
    assert "timeframe=1Day" in seen["url"]
    assert "adjustment=split" in seen["url"]
    assert "feed=iex" in seen["url"]
    assert len(series) == 100  # trimmed to the newest `days` bars
