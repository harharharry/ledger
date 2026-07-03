"""Tests for the stocks sleeve wrapper.

The wrapper contract only: signal maths (sma, rsi, propose_accumulation) is
shared with the crypto sleeve and exhaustively covered in
test_strategy_crypto.py — no point testing it twice. Price series are
synthetic and deterministic, mirroring the crypto test fixtures. No network,
no randomness.
"""

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from ledger.config import AssetConfig
from ledger.data.types import DailyClose, PriceSeries
from ledger.strategies import stocks
from ledger.strategies.signals import StrategyError

D = Decimal


def make_series(deltas, start="100", symbol="QQQ", currency="USD") -> PriceSeries:
    """Build a PriceSeries from a start price and a list of daily deltas."""
    values = [D(start)]
    for delta in deltas:
        values.append(values[-1] + D(str(delta)))
    first_day = dt.date(2026, 1, 1)
    closes = tuple(
        DailyClose(date=first_day + dt.timedelta(days=i), close=v)
        for i, v in enumerate(values)
    )
    return PriceSeries(symbol=symbol, currency=currency, closes=closes)


# Regime fixtures, all 51 closes (the minimum for the 50-day trend filter).
# Same shapes as the crypto tests, quoted in USD as Alpaca would deliver them.

def uptrend_neutral_series() -> PriceSeries:
    """Alternating +2/-1: net uptrend (125 > MA 113.5), RSI ~65 (neutral)."""
    return make_series([2 if i % 2 == 0 else -1 for i in range(50)])


def downtrend_series() -> PriceSeries:
    """Steady -2/day from 300: latest 200 < MA 249."""
    return make_series([-2] * 50, start="300")


PLENTY_OF_CASH = D("300")


def test_stocks_propose_delegates_for_the_configured_asset(config):
    proposal = stocks.propose(uptrend_neutral_series(), config, PLENTY_OF_CASH)
    assert proposal is not None
    assert proposal.sleeve == "stocks"
    assert proposal.asset == "QQQ"
    assert proposal.venue == "alpaca"
    assert proposal.side == "buy"
    assert proposal.notional_gbp == D("60.00")
    assert proposal.rationale.strip()


def test_stocks_propose_sits_out_a_downtrend(config):
    assert stocks.propose(downtrend_series(), config, PLENTY_OF_CASH) is None


def test_stocks_propose_rejects_unconfigured_symbol(config):
    series = make_series(
        [2 if i % 2 == 0 else -1 for i in range(50)], symbol="BTC", currency="GBP"
    )
    with pytest.raises(StrategyError, match="BTC"):
        stocks.propose(series, config, PLENTY_OF_CASH)


def test_stocks_propose_refuses_multi_asset_config(config):
    voo = AssetConfig(symbol="VOO", sleeve="stocks", venue="alpaca")
    cfg = replace(config, assets={**config.assets, "VOO": voo})
    with pytest.raises(StrategyError, match="not implemented"):
        stocks.propose(uptrend_neutral_series(), cfg, PLENTY_OF_CASH)


def test_stocks_propose_sub_floor_cash_proposes_nothing(config):
    # £40 available < £50 floor: fee drag dominates, so nothing is proposed.
    assert stocks.propose(uptrend_neutral_series(), config, D("40")) is None
