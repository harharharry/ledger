"""Tests for the shared strategy signals and the crypto sleeve wrapper.

All price series are synthetic and deterministic — signal regimes (uptrend,
downtrend, neutral/oversold/overbought RSI) are constructed by hand so every
expectation here is checkable on paper. No network, no randomness.
"""

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from ledger.config import AssetConfig
from ledger.data.types import DailyClose, PriceSeries
from ledger.strategies import crypto
from ledger.strategies.signals import StrategyError, propose_accumulation, rsi, sma

D = Decimal


def make_series(deltas, start="100", symbol="BTC", currency="GBP") -> PriceSeries:
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
# Signal values are asserted in the tests below so a change to the maths
# can't silently repurpose a fixture.

def uptrend_neutral_series() -> PriceSeries:
    """Alternating +2/-1: net uptrend (125 > MA 113.5), RSI ~65 (neutral)."""
    return make_series([2 if i % 2 == 0 else -1 for i in range(50)])


def downtrend_series() -> PriceSeries:
    """Steady -2/day from 300: latest 200 < MA 249."""
    return make_series([-2] * 50, start="300")


def all_gains_series() -> PriceSeries:
    """Steady +1/day: latest 150 > MA 125.5, RSI exactly 100 (overbought)."""
    return make_series([1] * 50)


def dip_in_uptrend_series() -> PriceSeries:
    """+2/day for 40 days then -3/day for 10: latest 150 > MA 145.5, RSI ~37.8.

    Note this is the *deepest* kind of dip that can exist above the 50-day MA:
    exhaustive search puts the floor of Wilder RSI(14) in an uptrend at ~36,
    which is why the oversold tests below need a custom rsi_oversold — the
    default threshold of 30 is unreachable once the trend filter has passed.
    """
    return make_series([2] * 40 + [-3] * 10)


BTC = AssetConfig(symbol="BTC", sleeve="crypto", venue="kraken", coingecko_id="bitcoin")

PLENTY_OF_CASH = D("300")


# --- sma ---------------------------------------------------------------


def test_sma_averages_last_n_values():
    values = [D(x) for x in (1, 2, 3, 4, 5)]
    assert sma(values, 3) == D(4)  # (3+4+5)/3
    assert sma(values, 5) == D(3)


def test_sma_rejects_short_input_and_bad_window():
    with pytest.raises(StrategyError):
        sma([D(1), D(2)], 3)
    with pytest.raises(StrategyError):
        sma([D(1)], 0)


# --- rsi ---------------------------------------------------------------


def test_rsi_is_100_when_there_are_no_losses():
    values = [D(100 + i) for i in range(20)]
    assert rsi(values, 14) == D(100)


def test_rsi_is_0_when_there_are_no_gains():
    values = [D(100 - i) for i in range(20)]
    assert rsi(values, 14) == D(0)


def test_rsi_matches_hand_computed_wilder_value():
    # Deltas: +1, -0.5, +1, +0.5 with period 3.
    # Seed: avg_gain = (1+0+1)/3 = 2/3, avg_loss = (0+0.5+0)/3 = 1/6.
    # Smooth +0.5: avg_gain = (2/3*2 + 0.5)/3 = 11/18, avg_loss = (1/6*2)/3 = 1/9.
    # RS = 5.5 -> RSI = 100 - 100/6.5 = 1100/13 = 84.6153...
    values = [D(x) for x in ("10", "11", "10.5", "11.5", "12")]
    expected = (D(1100) / D(13)).quantize(D("0.000001"))
    assert rsi(values, 3).quantize(D("0.000001")) == expected


def test_rsi_rejects_thin_input():
    with pytest.raises(StrategyError):
        rsi([D(1)] * 14, 14)  # needs period + 1 values


# --- propose_accumulation: trend gate and sizing -----------------------


def test_thin_series_raises_rather_than_guessing(config):
    fifty_closes = make_series([1] * 49)  # one short of ma_days + 1
    with pytest.raises(StrategyError, match="thin data"):
        propose_accumulation(fifty_closes, BTC, config, PLENTY_OF_CASH)


def test_downtrend_proposes_nothing(config):
    assert propose_accumulation(downtrend_series(), BTC, config, PLENTY_OF_CASH) is None


def test_uptrend_with_neutral_rsi_proposes_base_size_buy(config):
    series = uptrend_neutral_series()
    assert series.latest.close > sma(series.values(), 50)  # regime sanity
    assert D(30) < rsi(series.values(), 14) < D(70)

    proposal = propose_accumulation(series, BTC, config, PLENTY_OF_CASH)
    assert proposal is not None
    assert proposal.side == "buy"
    assert proposal.sleeve == "crypto"
    assert proposal.asset == "BTC"
    assert proposal.venue == "kraken"
    assert proposal.notional_gbp == D("60.00")
    assert proposal.rationale.strip()
    assert "neutral" in proposal.rationale
    assert "50-day MA" in proposal.rationale


def test_oversold_dip_in_uptrend_tilts_size_up(config):
    # The default rsi_oversold=30 cannot co-occur with an uptrend (see fixture
    # docstring), so exercise the tilt with a reachable threshold. Base size
    # and tilt stay at the real config values: £60 * 1.5 = £90.
    series = dip_in_uptrend_series()
    assert series.latest.close > sma(series.values(), 50)
    assert D(30) < rsi(series.values(), 14) <= D(45)

    cfg = replace(config, strategy=replace(config.strategy, rsi_oversold=D(45)))
    proposal = propose_accumulation(series, BTC, cfg, PLENTY_OF_CASH)
    assert proposal is not None
    assert proposal.notional_gbp == D("90.00")
    assert "oversold" in proposal.rationale


def test_dip_below_custom_oversold_threshold_is_neutral_at_default(config):
    # Same series under the real config: RSI ~37.8 is above the default
    # oversold threshold of 30, so no tilt applies.
    proposal = propose_accumulation(dip_in_uptrend_series(), BTC, config, PLENTY_OF_CASH)
    assert proposal is not None
    assert proposal.notional_gbp == D("60.00")


def test_overbought_tilt_lands_below_floor_and_proposes_nothing(config):
    # £60 * 0.5 = £30 < £50 floor: the fee-drag rule wins and nothing is
    # proposed, even though the trend filter and RSI both produced a signal.
    assert propose_accumulation(all_gains_series(), BTC, config, PLENTY_OF_CASH) is None


def test_overbought_tilt_produces_reduced_proposal_above_floor(config):
    # With a larger base the same 0.5x tilt clears the floor: £200 * 0.5 = £100.
    cfg = replace(config, strategy=replace(config.strategy, base_trade_gbp=D("200")))
    proposal = propose_accumulation(all_gains_series(), BTC, cfg, PLENTY_OF_CASH)
    assert proposal is not None
    assert proposal.notional_gbp == D("100.00")
    assert "overbought" in proposal.rationale


# --- propose_accumulation: cash cap and fee floor ----------------------


def test_size_is_capped_at_available_cash(config):
    proposal = propose_accumulation(uptrend_neutral_series(), BTC, config, D("55"))
    assert proposal is not None
    assert proposal.notional_gbp == D("55.00")
    assert "capped" in proposal.rationale


def test_cash_below_floor_proposes_nothing(config):
    assert propose_accumulation(uptrend_neutral_series(), BTC, config, D("40")) is None


# --- crypto sleeve wrapper ---------------------------------------------


def test_crypto_propose_delegates_for_the_configured_asset(config):
    proposal = crypto.propose(uptrend_neutral_series(), config, PLENTY_OF_CASH)
    assert proposal is not None
    assert proposal.sleeve == "crypto"
    assert proposal.asset == "BTC"
    assert proposal.venue == "kraken"
    assert proposal.side == "buy"
    assert proposal.notional_gbp == D("60.00")


def test_crypto_propose_rejects_unconfigured_symbol(config):
    series = make_series([2 if i % 2 == 0 else -1 for i in range(50)], symbol="ETH")
    with pytest.raises(StrategyError, match="ETH"):
        crypto.propose(series, config, PLENTY_OF_CASH)


def test_crypto_propose_refuses_multi_asset_config(config):
    eth = AssetConfig(symbol="ETH", sleeve="crypto", venue="kraken", coingecko_id="ethereum")
    cfg = replace(config, assets={**config.assets, "ETH": eth})
    with pytest.raises(StrategyError, match="not implemented"):
        crypto.propose(uptrend_neutral_series(), cfg, PLENTY_OF_CASH)
