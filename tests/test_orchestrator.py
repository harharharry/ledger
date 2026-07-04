import datetime as dt
from decimal import Decimal

import pytest

from ledger import kill_switch
from ledger.data.http import DataError
from ledger.data.types import DailyClose, PriceSeries
from ledger.orchestrator import affordable_notional, run_daily
from ledger.paper_ledger import PaperLedger

D = Decimal
TODAY = dt.date(2026, 7, 3)


def make_series(symbol: str, currency: str, closes: list[Decimal]) -> PriceSeries:
    start = TODAY - dt.timedelta(days=len(closes) - 1)
    return PriceSeries(
        symbol=symbol, currency=currency,
        closes=tuple(
            DailyClose(date=start + dt.timedelta(days=i), close=c)
            for i, c in enumerate(closes)
        ),
    )


def uptrend(symbol: str, currency: str = "GBP") -> PriceSeries:
    """Rising +2/-1 sawtooth: latest above the 50-day MA, RSI ~67 (neutral)."""
    values, price = [], D("100")
    for i in range(60):
        price += D("2") if i % 2 == 0 else D("-1")
        values.append(price)
    return make_series(symbol, currency, values)


def downtrend(symbol: str, currency: str = "GBP") -> PriceSeries:
    values, price = [], D("200")
    for i in range(60):
        price -= D("2") if i % 2 == 0 else D("-1")
        values.append(price)
    return make_series(symbol, currency, values)


def series_map(config, up: set[str] = frozenset()) -> dict:
    """One series per configured asset — uptrend for `up`, downtrend otherwise.
    Currencies follow each pair's quote (HYPE is USD)."""
    out = {}
    for symbol, asset in config.assets.items():
        build = uptrend if symbol in up else downtrend
        out[symbol] = build(symbol, asset.quote_currency)
    return out


@pytest.fixture
def led(tmp_path):
    with PaperLedger(tmp_path / "test.db") as ledger:
        yield ledger


@pytest.fixture
def switch(tmp_path):
    return tmp_path / "KILL_SWITCH"


def run(config, led, switch, up: set[str], today=TODAY):
    series = series_map(config, up)
    return run_daily(
        config, led,
        fetch_series=lambda a: series[a.symbol],
        fetch_fx=lambda: D("1.25"),
        today=today,
        kill_switch_path=switch,
    )


def never_called(*args):
    raise AssertionError("fetcher called despite engaged kill switch")


# -- first run -------------------------------------------------------------------


def test_day_one_snapshots_all_assets_and_trades_btc_at_floor(config, led, switch):
    result = run(config, led, switch, up={"BTC"})
    assert result.outcome == "success"
    # benchmark: one snapshot row per configured asset
    rows = led.benchmark_snapshots()
    assert {r["asset"] for r in rows} == set(config.assets)
    # BTC (highest under-allocation) trades; day-one cap is the £50 floor
    # (20% of its £200 allocation is £40 < £50 — floor-wins rule)
    assert led.trades_count_on(TODAY) == 1
    trade = led.trades()[0]
    assert trade["asset"] == "BTC" and trade["side"] == "buy"
    assert D(trade["gross_gbp"]) == D("50.00")
    assert "trade floor is the effective cap" in trade["rationale"]
    assert led.cash_balance_gbp() < D("500.00")


def test_all_gates_closed_is_no_action(config, led, switch):
    result = run(config, led, switch, up=set())
    assert result.outcome == "no-action"
    assert led.trades_count_on(TODAY) == 0
    assert any("no proposal" in e for e in result.events)


# -- idempotency ---------------------------------------------------------------


def test_rerun_same_day_never_double_trades(config, led, switch):
    run(config, led, switch, up={"BTC"})
    result = run(config, led, switch, up={"BTC"})
    assert result.outcome == "no-action"
    assert "idempotent" in result.events[0]
    assert led.trades_count_on(TODAY) == 1


# -- kill switch ----------------------------------------------------------------


def test_kill_switch_stops_everything_before_data_fetch(config, led, switch):
    kill_switch.engage(switch, reason="test")
    result = run_daily(
        config, led,
        fetch_series=never_called, fetch_fx=never_called,
        today=TODAY, kill_switch_path=switch,
    )
    assert result.outcome == "no-action"
    assert "kill switch" in result.events[0]
    assert led.run_outcome(TODAY) == "no-action"


# -- cadence + asset ordering ------------------------------------------------------


def test_daily_cap_gives_slot_to_most_under_target(config, led, switch):
    # every gate open; zero holdings means under-allocation equals target
    # weight, so BTC (40%) wins the single daily slot; the rest are blocked
    result = run(config, led, switch, up=set(config.assets))
    assert result.outcome == "success"
    assert led.trades_count_on(TODAY) == 1
    assert led.trades()[0]["asset"] == "BTC"
    assert any(e.startswith("ETH: blocked") for e in result.events)


def test_next_day_rotates_to_most_under_target(config, led, switch):
    run(config, led, switch, up=set(config.assets))
    day2 = TODAY + dt.timedelta(days=1)
    result = run(config, led, switch, up=set(config.assets), today=day2)
    assert result.outcome == "success"
    # BTC holds 100% of invested value, so ETH (25pts under) is next
    assert led.trades()[-1]["asset"] == "ETH"
    assert led.trades_count_between(TODAY, day2) == 2


def test_gated_asset_passes_slot_to_next_in_line(config, led, switch):
    # BTC's gate is closed; ETH is the most under-target asset with an open gate
    result = run(config, led, switch, up=set(config.assets) - {"BTC"})
    assert result.outcome == "success"
    assert led.trades()[0]["asset"] == "ETH"
    assert any(e.startswith("BTC: no proposal") for e in result.events)


def test_usd_pair_fill_carries_fx_cost(config, led, switch):
    # only HYPE's gate is open: it takes the slot and its fill pays FX
    result = run(config, led, switch, up={"HYPE"})
    assert result.outcome == "success"
    trade = led.trades()[0]
    assert trade["asset"] == "HYPE"
    assert trade["quote_currency"] == "USD"
    assert D(trade["fx_cost_gbp"]) > 0


# -- failure handling ---------------------------------------------------------------


def test_failure_is_recorded_and_raised_then_retryable(config, led, switch):
    def broken_fx():
        raise DataError("FX feed down")

    series = series_map(config, up={"BTC"})
    with pytest.raises(DataError):
        run_daily(
            config, led,
            fetch_series=lambda a: series[a.symbol],
            fetch_fx=broken_fx, today=TODAY, kill_switch_path=switch,
        )
    assert led.run_outcome(TODAY) == "failure"
    result = run(config, led, switch, up={"BTC"})
    assert result.outcome == "success"
    assert led.trades_count_on(TODAY) == 1


# -- fee headroom ------------------------------------------------------------------


def test_affordable_notional_never_breaches_cash(config):
    for symbol in ("BTC", "SUI", "HYPE"):
        listing = config.asset(symbol)
        venue = config.venue(listing.venue)
        cash = D("100.00")
        notional = affordable_notional(cash, venue, listing, config.fx.conversion_cost_rate)
        friction = venue.taker_fee_rate + listing.spread_frac / 2
        if listing.quote_currency != "GBP":
            friction += config.fx.conversion_cost_rate
        assert notional * (1 + friction) <= cash
        assert notional > D("95")  # and it isn't absurdly conservative


def test_executed_buy_cost_never_breaches_cash(config, led, switch):
    run(config, led, switch, up={"BTC"})
    trade = led.trades()[0]
    cost = -D(trade["cash_delta_gbp"])
    assert cost <= D("500.00")
    # and the risk cap kept the trade floor-sized, not cash-sized
    assert D(trade["gross_gbp"]) == D("50.00")


# -- run log helpers ------------------------------------------------------------------


def test_run_restart_clears_previous_outcome(led):
    led.record_run_start(TODAY)
    led.finish_run(TODAY, "failure", "boom")
    led.record_run_start(TODAY)
    assert led.run_outcome(TODAY) is None


def test_finish_without_start_raises(led):
    from ledger.paper_ledger import LedgerError

    with pytest.raises(LedgerError, match="no run started"):
        led.finish_run(TODAY, "success")
