import datetime as dt
from decimal import Decimal

import pytest

from ledger.data.http import DataError
from ledger.data.types import DailyClose, PriceSeries


def close(day: str, price: str) -> DailyClose:
    return DailyClose(date=dt.date.fromisoformat(day), close=Decimal(price))


def test_series_basics():
    s = PriceSeries(
        symbol="BTC", currency="GBP",
        closes=(close("2026-07-01", "78000"), close("2026-07-02", "78500")),
    )
    assert s.latest.close == Decimal("78500")
    assert s.values() == [Decimal("78000"), Decimal("78500")]
    assert len(s) == 2


def test_empty_series_rejected():
    with pytest.raises(DataError, match="empty"):
        PriceSeries(symbol="BTC", currency="GBP", closes=())


def test_unsorted_dates_rejected():
    with pytest.raises(DataError, match="ascending"):
        PriceSeries(
            symbol="BTC", currency="GBP",
            closes=(close("2026-07-02", "78500"), close("2026-07-01", "78000")),
        )


def test_duplicate_dates_rejected():
    with pytest.raises(DataError, match="ascending"):
        PriceSeries(
            symbol="BTC", currency="GBP",
            closes=(close("2026-07-01", "78000"), close("2026-07-01", "78500")),
        )


def test_nonpositive_close_rejected():
    with pytest.raises(DataError, match="non-positive"):
        PriceSeries(symbol="BTC", currency="GBP", closes=(close("2026-07-01", "0"),))
