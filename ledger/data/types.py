"""Common shapes returned by every data client."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from .http import DataError


@dataclass(frozen=True)
class DailyClose:
    date: dt.date
    close: Decimal


@dataclass(frozen=True)
class PriceSeries:
    symbol: str
    currency: str  # ISO code of the price quotes, e.g. 'GBP', 'USD'
    closes: tuple[DailyClose, ...]  # oldest first

    def __post_init__(self) -> None:
        if not self.closes:
            raise DataError(f"{self.symbol}: empty price series")
        dates = [c.date for c in self.closes]
        if dates != sorted(set(dates)):
            raise DataError(f"{self.symbol}: price series dates must be strictly ascending")
        if any(c.close <= 0 for c in self.closes):
            raise DataError(f"{self.symbol}: non-positive close in price series")

    @property
    def latest(self) -> DailyClose:
        return self.closes[-1]

    def values(self) -> list[Decimal]:
        return [c.close for c in self.closes]

    def __len__(self) -> int:
        return len(self.closes)
