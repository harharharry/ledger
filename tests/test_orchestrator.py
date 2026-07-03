import datetime as dt
from decimal import Decimal

import pytest

from ledger import kill_switch
from ledger.data.http import DataError
from ledger.data.types import DailyClose, PriceSeries
from ledger.orchestrator import RunResult, affordable_notional, run_daily
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
    """Rising +2/-1 sawtooth: latest above the 50-day MA, RSI ~67 (neutral —
    below the 70 overbought bound, above the 40 oversold bound)."""
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


@pytest.fixture
def led(tmp_path):
    with PaperLedger(tmp_path / "test.db") as ledger:
        yield ledger


@pytest.fixture
def switch(tmp_path):
    return tmp_path / "KILL_SWITCH"


def run(config, led, switch, crypto_series, stocks_series, today=TODAY):
    return run_daily(
        config, led,
        fetch_crypto=lambda a: crypto_series,
        fetch_stocks=lambda a: stocks_series,
        fetch_fx=lambda: D("1.25"),
        today=today,
        kill_switch_path=switch,
    )


def never_called(*args):
    raise AssertionError("fetcher called despite engaged kill switch")


# -- first run -------------------------------------------------------------------


def test_day_one_opens_snapshots_and_trades(config, led, switch):
    result = run(config, led, switch, uptrend("BTC"), downtrend("QQQ", "USD"))
    assert result.outcome == "success"
    assert led.run_outcome(TODAY) == "success"
    # benchmark snapshotted for both assets, QQQ converted at 1.25
    rows = led.benchmark_snapshots()
    assert {r["asset"] for r in rows} == {"BTC", "QQQ"}
    # one trade: crypto uptrend fired, stocks gated off
    assert led.trades_count_on(TODAY) == 1
    trade = led.trades()[0]
    assert trade["asset"] == "BTC" and trade["side"] == "buy"
    assert led.cash_balance_gbp() < D("500.00")
    pos = led.position("BTC")
    assert pos.quantity > 0


def test_all_gates_closed_is_no_action(config, led, switch):
    result = run(config, led, switch, downtrend("BTC"), downtrend("QQQ", "USD"))
    assert result.outcome == "no-action"
    assert led.trades_count_on(TODAY) == 0
    assert led.run_outcome(TODAY) == "no-action"
    assert any("no proposal" in e for e in result.events)


# -- idempotency ---------------------------------------------------------------


def test_rerun_same_day_never_double_trades(config, led, switch):
    run(config, led, switch, uptrend("BTC"), downtrend("QQQ", "USD"))
    result = run(config, led, switch, uptrend("BTC"), downtrend("QQQ", "USD"))
    assert result.outcome == "no-action"
    assert "idempotent" in result.events[0]
    assert led.trades_count_on(TODAY) == 1


# -- kill switch ----------------------------------------------------------------


def test_kill_switch_stops_everything_before_data_fetch(config, led, switch):
    kill_switch.engage(switch, reason="test")
    result = run_daily(
        config, led,
        fetch_crypto=never_called, fetch_stocks=never_called, fetch_fx=never_called,
        today=TODAY, kill_switch_path=switch,
    )
    assert result.outcome == "no-action"
    assert "kill switch" in result.events[0]
    assert led.run_outcome(TODAY) == "no-action"


# -- cadence --------------------------------------------------------------------


def test_daily_cap_allows_one_trade_and_crypto_goes_first(config, led, switch):
    # both sleeves signal; zero holdings means crypto (60% target) is the more
    # under-allocated and claims the single daily slot; stocks is blocked
    result = run(config, led, switch, uptrend("BTC"), uptrend("QQQ", "USD"))
    assert result.outcome == "success"
    assert led.trades_count_on(TODAY) == 1
    assert led.trades()[0]["sleeve"] == "crypto"
    assert any("stocks: blocked" in e for e in result.events)


def test_next_day_stocks_gets_the_slot_at_floor_size(config, led, switch):
    """Day 2: stocks is most under target and gets first claim on the slot.
    20% of its sleeve (~£176 of allocated cash) is ~£35, below the £50 floor —
    under the floor-wins rule (Harry's decision, 2026-07-03, NOTES.md) the
    effective cap is the floor, so the £60 proposal is resized to £50 and
    trades instead of deadlocking."""
    run(config, led, switch, uptrend("BTC"), uptrend("QQQ", "USD"))
    day2 = TODAY + dt.timedelta(days=1)
    result = run(config, led, switch, uptrend("BTC"), uptrend("QQQ", "USD"), today=day2)
    assert result.outcome == "success"
    trade = led.trades()[-1]
    assert trade["sleeve"] == "stocks" and trade["asset"] == "QQQ"
    assert D(trade["gross_gbp"]) == D("50.00")
    assert "trade floor is the effective cap" in trade["rationale"]
    # the daily slot is spent; crypto reports blocked
    assert any(e.startswith("crypto: blocked") for e in result.events)
    assert led.trades_count_between(TODAY, day2) == 2


# -- failure handling ---------------------------------------------------------------


def test_failure_is_recorded_and_raised_then_retryable(config, led, switch):
    def broken_fx():
        raise DataError("FX feed down")

    with pytest.raises(DataError):
        run_daily(
            config, led,
            fetch_crypto=lambda a: uptrend("BTC"),
            fetch_stocks=lambda a: downtrend("QQQ", "USD"),
            fetch_fx=broken_fx, today=TODAY, kill_switch_path=switch,
        )
    assert led.run_outcome(TODAY) == "failure"
    # a failed day may retry — and the retry works end to end
    result = run(config, led, switch, uptrend("BTC"), downtrend("QQQ", "USD"))
    assert result.outcome == "success"
    assert led.trades_count_on(TODAY) == 1


# -- fee headroom ------------------------------------------------------------------


def test_affordable_notional_never_breaches_cash(config):
    kraken = config.venue("kraken")
    alpaca_v = config.venue("alpaca")
    for venue in (kraken, alpaca_v):
        cash = D("100.00")
        notional = affordable_notional(cash, venue, config.fx.conversion_cost_rate)
        friction = venue.taker_fee_rate + venue.spread_frac / 2
        if venue.quote_currency != "GBP":
            friction += config.fx.conversion_cost_rate
        worst_case_cost = notional * (1 + friction)
        assert worst_case_cost <= cash
        assert notional > D("95")  # and it isn't absurdly conservative


def test_executed_buy_cost_fits_available_cash(config, led, switch):
    # end-to-end: the recorded trade's all-in cost fits the crypto sleeve share
    run(config, led, switch, uptrend("BTC"), downtrend("QQQ", "USD"))
    trade = led.trades()[0]
    cost = -D(trade["cash_delta_gbp"])
    assert cost <= D("500.00") * config.portfolio.allocation_crypto_frac


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
