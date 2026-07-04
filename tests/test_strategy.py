import datetime as dt
from decimal import Decimal

import pytest

from ledger.data.types import DailyClose, PriceSeries
from ledger.strategies.signals import StrategyError, propose_accumulation, rsi, sma

D = Decimal
TODAY = dt.date(2026, 7, 3)
PLENTY = D("1000")


def make_series(symbol: str, values: list[Decimal], currency: str = "GBP") -> PriceSeries:
    start = TODAY - dt.timedelta(days=len(values) - 1)
    return PriceSeries(
        symbol=symbol, currency=currency,
        closes=tuple(
            DailyClose(date=start + dt.timedelta(days=i), close=v)
            for i, v in enumerate(values)
        ),
    )


def sawtooth(start: str, up: str, down: str, n: int = 60) -> list[Decimal]:
    """+up/-down alternating from start — a controllable trend/RSI fixture."""
    values, level = [], D(start)
    for i in range(n):
        level += D(up) if i % 2 == 0 else -D(down)
        values.append(level)
    return values


def uptrend_neutral(symbol="BTC"):
    # +2/-1: rising, above the 50-day MA, RSI ~66.7 (neutral at 40/70 bounds)
    return make_series(symbol, sawtooth("100", "2", "1"))


def downtrend(symbol="BTC"):
    return make_series(symbol, sawtooth("200", "-2", "-1"))


def dip_in_uptrend(symbol="BTC"):
    """Long climb (+4/-1), then eight -3 sessions: the pullback stays above
    the 50-day MA (gate open) but drags RSI(14) to ~39.5, under the 40 bound."""
    values = sawtooth("100", "4", "1", n=50)
    for _ in range(8):
        values.append(values[-1] - D("3"))
    series = make_series(symbol, values)
    assert series.latest.close >= sma(series.values(), 50)  # regime check
    assert rsi(series.values(), 14) <= D("40")
    return series


# -- indicators ----------------------------------------------------------------


def test_sma():
    assert sma([D(1), D(2), D(3), D(4)], 2) == D("3.5")


def test_sma_thin_data_raises():
    with pytest.raises(StrategyError, match="at least 3"):
        sma([D(1), D(2)], 3)


def test_rsi_all_gains_is_100():
    assert rsi([D(i) for i in range(1, 20)], 14) == 100


def test_rsi_hand_computed_wilder_case():
    # period 2 over [10, 11, 10, 11]: seed avg gain/loss = 0.5/0.5, then the
    # final +1 smooths to 0.75/0.25 -> RS 3 -> RSI 75
    assert rsi([D(10), D(11), D(10), D(11)], 2) == D("75")


def test_rsi_thin_data_raises():
    with pytest.raises(StrategyError, match="at least 15"):
        rsi([D(1)] * 14, 14)


# -- proposal logic ----------------------------------------------------------------


def test_uptrend_neutral_proposes_base_size(config):
    p = propose_accumulation(uptrend_neutral(), config.asset("BTC"), config, PLENTY)
    assert p is not None
    assert p.side == "buy" and p.asset == "BTC" and p.venue == "kraken"
    assert p.notional_gbp == D("60.00")
    assert "uptrend" in p.rationale and "neutral" in p.rationale


def test_downtrend_proposes_nothing(config):
    assert propose_accumulation(downtrend(), config.asset("BTC"), config, PLENTY) is None


def test_oversold_dip_tilts_up(config):
    p = propose_accumulation(dip_in_uptrend(), config.asset("BTC"), config, PLENTY)
    assert p is not None
    assert p.notional_gbp == D("90.00")  # £60 * 1.5
    assert "oversold" in p.rationale


def test_overbought_reduction_lands_under_floor(config):
    # all-gains RSI 100 -> 0.5x of £60 = £30 < £50 floor -> nothing proposed
    all_gains = make_series("BTC", [D(100 + i) for i in range(60)])
    assert propose_accumulation(all_gains, config.asset("BTC"), config, PLENTY) is None


def test_cash_cap_resizes_then_floors(config):
    p = propose_accumulation(uptrend_neutral(), config.asset("BTC"), config, D("55"))
    assert p.notional_gbp == D("55.00")
    assert "capped" in p.rationale
    assert propose_accumulation(uptrend_neutral(), config.asset("BTC"), config, D("40")) is None


def test_thin_series_raises(config):
    thin = make_series("BTC", [D(100 + i) for i in range(30)])
    with pytest.raises(StrategyError, match="refusing to guess"):
        propose_accumulation(thin, config.asset("BTC"), config, PLENTY)


def test_symbol_mismatch_raises(config):
    with pytest.raises(StrategyError, match="ETH"):
        propose_accumulation(uptrend_neutral("BTC"), config.asset("ETH"), config, PLENTY)


def test_usd_priced_asset_rationale_uses_dollar_sign(config):
    p = propose_accumulation(
        make_series("HYPE", sawtooth("30", "1", "0.5"), currency="USD"),
        config.asset("HYPE"), config, PLENTY,
    )
    assert p is not None
    assert "$" in p.rationale
