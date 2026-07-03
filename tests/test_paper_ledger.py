from decimal import Decimal

import pytest

from ledger.fill_engine import Order, simulate_fill
from ledger.paper_ledger import (
    DuplicateTradeError,
    InsufficientCashError,
    InsufficientPositionError,
    LedgerError,
    PaperLedger,
)


@pytest.fixture
def ledger(tmp_path):
    with PaperLedger(tmp_path / "test.db") as led:
        led.open(Decimal("500.00"), ts="2026-07-03T08:00:00+00:00")
        yield led


def btc_buy_fill(config, notional="100.00", mid="50000"):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal(mid), notional_gbp=Decimal(notional),
    )
    return simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def btc_sell_fill(config, quantity, mid="50000"):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="sell",
        mid_price=Decimal(mid), quantity=quantity,
    )
    return simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


# -- opening ------------------------------------------------------------------


def test_open_sets_cash(ledger):
    assert ledger.cash_balance_gbp() == Decimal("500.00")


def test_open_is_idempotent(ledger):
    ledger.open(Decimal("500.00"))
    assert ledger.cash_balance_gbp() == Decimal("500.00")


def test_reopen_with_different_amount_raises(ledger):
    with pytest.raises(LedgerError, match="already opened"):
        ledger.open(Decimal("1000.00"))


# -- buys -----------------------------------------------------------------------


def test_buy_moves_cash_and_creates_position(ledger, config):
    fill = btc_buy_fill(config)
    ledger.record_fill(fill, trade_key="2026-07-03:crypto:BTC:buy")
    assert ledger.cash_balance_gbp() == Decimal("399.60")  # 500 - 100.40
    pos = ledger.position("BTC")
    assert pos.quantity == fill.quantity
    assert pos.book_cost_gbp == Decimal("100.40")  # all-in cost incl. fees
    assert pos.sleeve == "crypto"


def test_buy_beyond_cash_raises_and_changes_nothing(ledger, config):
    fill = btc_buy_fill(config, notional="600.00")
    with pytest.raises(InsufficientCashError):
        ledger.record_fill(fill, trade_key="k1")
    assert ledger.cash_balance_gbp() == Decimal("500.00")
    assert ledger.position("BTC") is None
    assert ledger.trades() == []


def test_repeat_buy_accumulates_position(ledger, config):
    fill = btc_buy_fill(config)
    ledger.record_fill(fill, trade_key="day1")
    ledger.record_fill(fill, trade_key="day2")
    pos = ledger.position("BTC")
    assert pos.quantity == fill.quantity * 2
    assert pos.book_cost_gbp == Decimal("200.80")


# -- idempotency -----------------------------------------------------------------


def test_duplicate_trade_key_never_double_trades(ledger, config):
    fill = btc_buy_fill(config)
    ledger.record_fill(fill, trade_key="2026-07-03:crypto:BTC:buy")
    with pytest.raises(DuplicateTradeError):
        ledger.record_fill(fill, trade_key="2026-07-03:crypto:BTC:buy")
    assert len(ledger.trades()) == 1
    assert ledger.cash_balance_gbp() == Decimal("399.60")


# -- sells --------------------------------------------------------------------------


def test_sell_returns_cash_and_reduces_position(ledger, config):
    buy = btc_buy_fill(config)
    ledger.record_fill(buy, trade_key="b1")
    half = buy.quantity / 2
    sell = btc_sell_fill(config, quantity=half)
    ledger.record_fill(sell, trade_key="s1")
    pos = ledger.position("BTC")
    assert pos.quantity == buy.quantity - half
    # average-cost basis: half the book cost left
    assert pos.book_cost_gbp == Decimal("50.20")
    assert ledger.cash_balance_gbp() == Decimal("399.60") + sell.cash_delta_gbp


def test_full_sell_zeroes_position(ledger, config):
    buy = btc_buy_fill(config)
    ledger.record_fill(buy, trade_key="b1")
    sell = btc_sell_fill(config, quantity=buy.quantity)
    ledger.record_fill(sell, trade_key="s1")
    pos = ledger.position("BTC")
    assert pos.quantity == 0
    assert pos.book_cost_gbp == 0


def test_selling_more_than_held_raises(ledger, config):
    buy = btc_buy_fill(config)
    ledger.record_fill(buy, trade_key="b1")
    sell = btc_sell_fill(config, quantity=buy.quantity * 2)
    with pytest.raises(InsufficientPositionError):
        ledger.record_fill(sell, trade_key="s1")


def test_selling_asset_never_held_raises(ledger, config):
    sell = btc_sell_fill(config, quantity=Decimal("0.001"))
    with pytest.raises(InsufficientPositionError):
        ledger.record_fill(sell, trade_key="s1")


# -- round trip against the ledger ------------------------------------------------


def test_flat_price_round_trip_ends_below_starting_cash(ledger, config):
    """Buy and fully sell at unchanged prices: cash must end lower than £500
    by exactly the friction. The ledger can never launder fees away."""
    buy = btc_buy_fill(config)
    ledger.record_fill(buy, trade_key="b1")
    sell = btc_sell_fill(config, quantity=buy.quantity)
    ledger.record_fill(sell, trade_key="s1")
    assert ledger.position("BTC").quantity == 0
    assert ledger.cash_balance_gbp() < Decimal("500.00")
    assert ledger.cash_balance_gbp() == Decimal("500.00") + buy.cash_delta_gbp + sell.cash_delta_gbp


# -- benchmark ---------------------------------------------------------------------


def test_benchmark_snapshot_and_idempotency(ledger):
    prices = {"BTC": Decimal("78123.45"), "QQQ": Decimal("437.10")}
    ledger.snapshot_benchmark("phase1", prices, snapshot_date="2026-07-03")
    ledger.snapshot_benchmark("phase1", prices, snapshot_date="2026-07-03")  # no-op
    rows = ledger.benchmark_snapshots()
    assert len(rows) == 2
    assert {r["asset"] for r in rows} == {"BTC", "QQQ"}


def test_benchmark_conflicting_resnapshot_raises(ledger):
    ledger.snapshot_benchmark("phase1", {"BTC": Decimal("78123.45")}, snapshot_date="2026-07-03")
    with pytest.raises(LedgerError, match="already"):
        ledger.snapshot_benchmark(
            "phase1", {"BTC": Decimal("80000.00")}, snapshot_date="2026-07-03"
        )


# -- CGT export ----------------------------------------------------------------------


def test_cgt_csv_export(ledger, config, tmp_path):
    buy = btc_buy_fill(config)
    ledger.record_fill(buy, trade_key="b1", ts="2026-07-03T09:00:00+00:00")
    out = tmp_path / "cgt.csv"
    count = ledger.export_cgt_csv(out)
    assert count == 1
    lines = out.read_text().strip().splitlines()
    assert lines[0] == "date,asset,side,quantity,gross_value_gbp,fees_gbp"
    assert "BTC" in lines[1] and "buy" in lines[1] and "0.40" in lines[1]


# -- persistence ----------------------------------------------------------------------


def test_ledger_survives_reopen(tmp_path, config):
    db = tmp_path / "test.db"
    with PaperLedger(db) as led:
        led.open(Decimal("500.00"))
        led.record_fill(btc_buy_fill(config), trade_key="b1")
    with PaperLedger(db) as led:
        assert led.cash_balance_gbp() == Decimal("399.60")
        assert led.position("BTC") is not None
        assert len(led.trades()) == 1
