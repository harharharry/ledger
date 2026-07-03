import datetime as dt
import json
from decimal import Decimal

import pytest

from ledger.dashboard_data import build_dashboard_data, write_dashboard_data
from ledger.fill_engine import Order, simulate_fill
from ledger.paper_ledger import PaperLedger

# reuse the deterministic series builders from the orchestrator tests
from test_orchestrator import TODAY, downtrend, uptrend

D = Decimal


@pytest.fixture
def led(tmp_path):
    with PaperLedger(tmp_path / "test.db") as ledger:
        yield ledger


def btc_buy(config, notional="60.00"):
    series = uptrend("BTC")
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="buy",
        mid_price=series.latest.close, notional_gbp=D(notional),
    )
    return simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def build(config, led, **kwargs):
    return build_dashboard_data(
        led, config,
        series_by_symbol={"BTC": uptrend("BTC"), "QQQ": downtrend("QQQ", "USD")},
        fx_rate=D("1.25"),
        **kwargs,
    )


def test_unopened_ledger_degrades_gracefully(config, led):
    data = build(config, led)
    assert data["opened"] is None
    assert data["hero"]["total_balance_gbp"] == "0.00"
    assert data["hero"]["pnl_pct"] is None
    assert data["hero"]["sparkline"] == []
    assert data["benchmark"] is None
    assert data["pending_proposal"] is None


def test_opened_with_trade_hero_and_allocation(config, led):
    led.open(D("500.00"), ts=f"{TODAY}T08:00:00+00:00")
    fill = btc_buy(config)
    led.record_fill(fill, "k1", ts=f"{TODAY}T08:05:00+00:00", run_date=TODAY)
    data = build(config, led)

    # hero: cash + BTC at latest close; position was bought at the same close,
    # so value = 500 - friction
    cash = D("500.00") + fill.cash_delta_gbp
    holding = fill.quantity * uptrend("BTC").latest.close
    expected_total = cash + holding
    assert data["hero"]["total_balance_gbp"] == str(
        expected_total.quantize(D("0.01"))
    )
    assert data["allocation"]["actual"]["crypto"] == 100.0
    assert data["allocation"]["cash_gbp"] == str(cash.quantize(D("0.01")))
    assert data["opened"] == str(TODAY)


def test_benchmark_pct_math(config, led):
    led.open(D("500.00"), ts=f"{TODAY}T08:00:00+00:00")
    # snapshot at half of today's closes: benchmark exactly doubles the
    # allocated capital of both sleeves -> +100%
    btc_now = uptrend("BTC").latest.close
    qqq_now_gbp = downtrend("QQQ", "USD").latest.close / D("1.25")
    led.snapshot_benchmark(
        "phase1",
        {"BTC": btc_now / 2, "QQQ": qqq_now_gbp / 2},
        snapshot_date=str(TODAY),
    )
    data = build(config, led)
    assert data["benchmark"]["benchmark_pct"] == pytest.approx(100.0, abs=0.1)
    assert data["benchmark"]["note"] == "before costs"


def test_sparkline_reconstructs_history(config, led):
    led.open(D("500.00"), ts=f"{TODAY}T08:00:00+00:00")
    fill = btc_buy(config)
    led.record_fill(fill, "k1", ts=f"{TODAY}T08:05:00+00:00", run_date=TODAY)
    data = build(config, led)
    spark = data["hero"]["sparkline"]
    # opening day is the only spine day on/after opening in these series
    assert len(spark) == 1
    assert spark[0]["date"] == str(TODAY)
    assert spark[0]["value_gbp"] == data["hero"]["total_balance_gbp"]


def test_asset_block_signals(config, led):
    data = build(config, led)
    btc = data["assets"]["BTC"]
    assert btc["trend"] == "bullish"
    assert btc["sessions_above_ma"] >= 1
    assert 0 < btc["rsi"] < 100
    assert btc["rsi_oversold"] == 40.0
    # chart has an MA value from day 50 onward, null before
    assert btc["chart"][10]["ma"] is None
    assert btc["chart"][-1]["ma"] is not None
    qqq = data["assets"]["QQQ"]
    assert qqq["trend"] == "bearish"
    assert qqq["sessions_above_ma"] == 0


def test_activity_includes_trades_and_bad_runs(config, led):
    led.open(D("500.00"), ts=f"{TODAY}T08:00:00+00:00")
    led.record_fill(btc_buy(config), "k1", ts=f"{TODAY}T08:05:00+00:00", run_date=TODAY)
    led.record_run_start(TODAY)
    led.finish_run(TODAY, "failure", "DataError: FX feed down")
    data = build(config, led)
    kinds = {a["kind"] for a in data["activity"]}
    assert kinds == {"trade", "run"}
    run_item = next(a for a in data["activity"] if a["kind"] == "run")
    assert run_item["title"] == "Run failed"
    assert "FX feed down" in run_item["rationale"]
    trade_item = next(a for a in data["activity"] if a["kind"] == "trade")
    assert trade_item["title"] == "Bought BTC"
    assert trade_item["rationale"] == ""  # none passed on record_fill


def test_output_is_json_serializable_and_writes(config, led, tmp_path):
    led.open(D("500.00"), ts=f"{TODAY}T08:00:00+00:00")
    data = build(config, led, kill_switch_engaged=True)
    out = tmp_path / "data.json"
    write_dashboard_data(data, out)
    round_tripped = json.loads(out.read_text())
    assert round_tripped["kill_switch_engaged"] is True
    assert round_tripped["mode"] == "paper"
    # money is strings everywhere, never floats
    assert isinstance(round_tripped["hero"]["total_balance_gbp"], str)
