"""Weekly report tests. Ledgers are built for real (PaperLedger + the real fill
engine + the real config.toml) so every asserted penny is a penny the deployed
system would actually report. No network anywhere."""

import datetime as dt
import re
from decimal import Decimal

import pytest

from ledger.fill_engine import Order, simulate_fill
from ledger.paper_ledger import PaperLedger
from ledger.reporting import ReportError, build_weekly_report, render_weekly_report

D = Decimal

WEEK_ENDING = dt.date(2026, 7, 3)  # a Friday
WEEK_START = dt.date(2026, 6, 29)  # the Monday of that ISO week
LAST_WEEK_FRIDAY = dt.date(2026, 6, 26)  # previous ISO week

DAY_ONE_PRICES = {"BTC": D("50000"), "QQQ": D("400")}
MOVED_PRICES = {"BTC": D("55000"), "QQQ": D("380")}

BTC_RATIONALE = "uptrend: price above the 50-day MA, RSI neutral — scheduled buy"


@pytest.fixture
def led(tmp_path):
    with PaperLedger(tmp_path / "test.db") as ledger:
        yield ledger


def open_and_snapshot(led, config, day_one="2026-06-29"):
    led.open(config.portfolio.starting_capital_gbp, ts=f"{day_one}T07:30:00+00:00")
    led.snapshot_benchmark("phase1", DAY_ONE_PRICES, snapshot_date=day_one)


def buy_btc(led, config, run_date, rationale=BTC_RATIONALE):
    """£60 BTC buy on Kraken at mid £50,000 — the happy-path trade.

    Hand-derivation of the fill (kraken: 0.40% taker, 0.10% full spread, GBP so
    no FX): exec price = 50000 x 1.0005 = 50025; quantity = 60/50025 =
    0.00119940 (8dp); gross = 0.00119940 x 50025 = £60.00; fee = £0.24;
    spread cost = 0.00119940 x 50000 x 0.0005 = £0.03; cash out = £60.24."""
    fill = simulate_fill(
        Order(
            sleeve="crypto", venue="kraken", asset="BTC", side="buy",
            mid_price=D("50000"), notional_gbp=D("60"),
        ),
        config.venue("kraken"),
        config.fx.conversion_cost_rate,
    )
    led.record_fill(
        fill,
        trade_key=f"{run_date}:crypto:BTC:buy",
        ts=f"{run_date}T08:00:00+00:00",
        rationale=rationale,
        run_date=run_date,
    )
    return fill


def buy_qqq(led, config, run_date):
    """£60 QQQ buy on Alpaca at mid $500, GBPUSD 1.25 — carries FX cost.

    Hand-derivation (alpaca: 0% fee, 0.05% full spread, 0.50% FX cost):
    gross = £60.00; fee = £0.00; FX cost = £0.30; spread = £0.01;
    cash out = £60.30."""
    fill = simulate_fill(
        Order(
            sleeve="stocks", venue="alpaca", asset="QQQ", side="buy",
            mid_price=D("500"), fx_rate=D("1.25"), notional_gbp=D("60"),
        ),
        config.venue("alpaca"),
        config.fx.conversion_cost_rate,
    )
    led.record_fill(
        fill,
        trade_key=f"{run_date}:stocks:QQQ:buy",
        ts=f"{run_date}T08:00:00+00:00",
        rationale="stocks sleeve accumulation",
        run_date=run_date,
    )
    return fill


# -- happy path, to the penny -----------------------------------------------------


def test_happy_path_numbers(config, led):
    """Open £500, snapshot BTC £50k / QQQ £400, buy £60 of BTC, then report
    with BTC at £55k and QQQ at £380. Every figure hand-computed:

      cash       = 500.00 - 60.24                       = £439.76
      holdings   = 0.00119940 x 55000                   = £65.97
      portfolio  = 439.76 + 65.97                       = £505.73
      pnl        = 505.73 - 500.00                      = +£5.73 (+1.15%)
      benchmark  = (300/50000) x 55000 + (200/400) x 380
                 = 0.006 x 55000 + 0.5 x 380 = 330 + 190 = £520.00
      vs bench   = 505.73 - 520.00                      = -£14.27
    """
    open_and_snapshot(led, config)
    buy_btc(led, config, run_date="2026-07-01")

    report = build_weekly_report(led, config, MOVED_PRICES, WEEK_ENDING)

    assert report.week_start == WEEK_START
    assert report.week_ending == WEEK_ENDING
    assert report.cash_gbp == D("439.76")
    assert report.portfolio_value_gbp == D("505.73")
    assert report.pnl_gbp == D("5.73")
    assert report.pnl_pct == D("1.15")
    assert report.benchmark_value_gbp == D("520.00")
    assert report.vs_benchmark_gbp == D("-14.27")

    assert len(report.holdings) == 1
    holding = report.holdings[0]
    assert holding.asset == "BTC" and holding.sleeve == "crypto"
    assert holding.quantity == D("0.00119940")
    assert holding.market_value_gbp == D("65.97")
    assert holding.book_cost_gbp == D("60.24")  # all-in, fee included

    assert report.week_costs.fee_gbp == D("0.24")
    assert report.week_costs.fx_gbp == D("0.00")
    assert report.week_costs.spread_gbp == D("0.03")
    assert report.week_costs.paid_gbp == D("0.24")

    assert len(report.trades) == 1
    assert report.trades[0].rationale == BTC_RATIONALE


def test_reporting_is_read_only(config, led):
    open_and_snapshot(led, config)
    buy_btc(led, config, run_date="2026-07-01")
    cash_before = led.cash_balance_gbp()
    trades_before = len(led.trades())

    build_weekly_report(led, config, MOVED_PRICES, WEEK_ENDING)

    assert led.cash_balance_gbp() == cash_before
    assert len(led.trades()) == trades_before
    assert led.position("BTC").quantity == D("0.00119940")


def test_week_start_is_monday_of_iso_week(config, led):
    open_and_snapshot(led, config)
    sunday = dt.date(2026, 7, 5)
    report = build_weekly_report(led, config, DAY_ONE_PRICES, sunday)
    assert report.week_start == dt.date(2026, 6, 29)  # Monday of the same ISO week


# -- benchmark comparison wording ---------------------------------------------------


def test_trailing_benchmark_is_said_plainly(config, led):
    # Bot is mostly cash while BTC rockets: portfolio £505.73 vs benchmark
    # £520.00 (see happy-path derivation) — the bot trails and the copy says so.
    open_and_snapshot(led, config)
    buy_btc(led, config, run_date="2026-07-01")
    report = build_weekly_report(led, config, MOVED_PRICES, WEEK_ENDING)
    assert report.vs_benchmark_gbp < 0
    text = render_weekly_report(report)
    assert "£14.27 behind buy-and-hold" in text
    assert "doing nothing would have done better" in text


def test_ahead_of_benchmark_wording(config, led):
    # Prices collapse: benchmark = 0.006 x 30000 + 0.5 x 300 = £330.00, while
    # the mostly-cash bot is at 439.76 + 0.00119940 x 30000 = £475.74.
    open_and_snapshot(led, config)
    buy_btc(led, config, run_date="2026-07-01")
    crashed = {"BTC": D("30000"), "QQQ": D("300")}
    report = build_weekly_report(led, config, crashed, WEEK_ENDING)
    assert report.benchmark_value_gbp == D("330.00")
    assert report.portfolio_value_gbp == D("475.74")
    text = render_weekly_report(report)
    assert "ahead of buy-and-hold" in text
    # honest even when winning: the benchmark's fee-free handicap is named
    assert "before costs" in text


def test_quiet_week_still_reports_benchmark_and_level_wording(config, led):
    open_and_snapshot(led, config)
    report = build_weekly_report(led, config, DAY_ONE_PRICES, WEEK_ENDING)
    # nothing traded, prices unmoved: pot and benchmark both £500.00
    assert report.portfolio_value_gbp == D("500.00")
    assert report.benchmark_value_gbp == D("500.00")
    assert report.vs_benchmark_gbp == D("0.00")
    text = render_weekly_report(report)
    assert "exactly level with buy-and-hold" in text
    assert "Activity this week: no trades." in text
    assert "all cash" in text


# -- costs: week vs cumulative ------------------------------------------------------


def test_costs_split_week_vs_cumulative(config, led):
    """A QQQ buy landed last ISO week; a BTC buy this week. Week costs count
    only the BTC trade; cumulative counts both — including the QQQ trade's
    30p FX cost, which must stay visible forever."""
    open_and_snapshot(led, config, day_one="2026-06-26")
    buy_qqq(led, config, run_date=str(LAST_WEEK_FRIDAY))  # fees £0, FX £0.30, spread £0.01
    buy_btc(led, config, run_date="2026-07-01")  # fees £0.24, FX £0, spread £0.03

    report = build_weekly_report(led, config, MOVED_PRICES, WEEK_ENDING)

    assert report.week_costs.fee_gbp == D("0.24")
    assert report.week_costs.fx_gbp == D("0.00")
    assert report.week_costs.spread_gbp == D("0.03")
    assert report.week_costs.paid_gbp == D("0.24")

    assert report.cumulative_costs.fee_gbp == D("0.24")
    assert report.cumulative_costs.fx_gbp == D("0.30")
    assert report.cumulative_costs.spread_gbp == D("0.04")
    assert report.cumulative_costs.paid_gbp == D("0.54")

    # last week's trade is in the cumulative costs but not the week's activity
    assert [t.asset for t in report.trades] == ["BTC"]


# -- refusal to fabricate -------------------------------------------------------------


def test_missing_benchmark_snapshot_raises(config, led):
    led.open(config.portfolio.starting_capital_gbp)
    with pytest.raises(ReportError, match="benchmark"):
        build_weekly_report(led, config, DAY_ONE_PRICES, WEEK_ENDING)


def test_missing_price_for_held_asset_raises(config, led):
    open_and_snapshot(led, config)
    buy_btc(led, config, run_date="2026-07-01")
    with pytest.raises(ReportError, match="BTC"):
        build_weekly_report(led, config, {"QQQ": D("380")}, WEEK_ENDING)


def test_missing_price_for_benchmark_asset_raises(config, led):
    # nothing held, so the held-asset check passes; the benchmark still needs QQQ
    open_and_snapshot(led, config)
    with pytest.raises(ReportError, match="QQQ"):
        build_weekly_report(led, config, {"BTC": D("55000")}, WEEK_ENDING)


# -- rendered content ---------------------------------------------------------------


def test_render_contains_the_non_negotiables(config, led):
    open_and_snapshot(led, config)
    buy_btc(led, config, run_date="2026-07-01")

    led.record_run_start(dt.date(2026, 6, 29))
    led.finish_run(dt.date(2026, 6, 29), "success", "bought BTC")
    led.record_run_start(dt.date(2026, 6, 30))
    led.finish_run(dt.date(2026, 6, 30), "no-action", "gates closed")
    led.record_run_start(dt.date(2026, 7, 1))
    led.finish_run(dt.date(2026, 7, 1), "failure", "DataError: FX feed down")

    report = build_weekly_report(led, config, MOVED_PRICES, WEEK_ENDING)
    text = render_weekly_report(report)

    # 1. the benchmark comparison, named as fee-free — the point of the project
    assert (
        "vs. just holding the same 60/40 crypto/stocks from day one, "
        "untouched (before costs)" in text
    )
    assert "£520.00" in text
    # 2. costs always visible, spread separate from cash paid
    assert "venue fees £0.24 + FX £0.00" in text
    assert "£0.03 lost to spread" in text
    # 3. what fired and why, rationale verbatim
    assert "bought 0.00119940 BTC" in text
    assert BTC_RATIONALE in text
    # 4. run reliability, failure detail included
    assert "3 runs this week — 1 success / 1 no-action / 1 failure" in text
    assert "failed 2026-07-01: DataError: FX feed down" in text
    # 5. drift flags verbatim (only BTC held -> crypto is 100% of invested value)
    assert report.drift_flags  # the setup really does drift
    for flag in report.drift_flags:
        assert flag in text
    # 6. honest framing, no prediction language ("edge" as a whole word only —
    #    the project is literally called Ledger)
    assert "not a forecast" in text
    for banned in (r"predict", r"\bedge\b", r"expected to", r"should rise", r"will rise"):
        assert not re.search(banned, text.lower())


def test_crashed_run_counts_as_failure(config, led):
    open_and_snapshot(led, config)
    led.record_run_start(dt.date(2026, 7, 1))  # started, never finished
    report = build_weekly_report(led, config, DAY_ONE_PRICES, WEEK_ENDING)
    assert report.runs.failure == 1
    assert "crashed mid-run" in report.runs.failures[0]
    assert "crashed mid-run" in render_weekly_report(report)


# -- determinism ---------------------------------------------------------------------


def test_report_and_render_are_deterministic(config, led):
    open_and_snapshot(led, config)
    buy_btc(led, config, run_date="2026-07-01")
    led.record_run_start(dt.date(2026, 7, 1))
    led.finish_run(dt.date(2026, 7, 1), "success", "bought BTC")

    first = build_weekly_report(led, config, MOVED_PRICES, WEEK_ENDING)
    second = build_weekly_report(led, config, MOVED_PRICES, WEEK_ENDING)
    assert first == second
    assert render_weekly_report(first) == render_weekly_report(second)
