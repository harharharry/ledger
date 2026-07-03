from decimal import Decimal

import pytest

from ledger.fill_engine import FillError, Order, simulate_fill


def kraken_buy(config, notional="100.00", mid="50000"):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal(mid), notional_gbp=Decimal(notional),
    )
    return simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def alpaca_buy(config, notional="100.00", mid="500", fx="1.27"):
    order = Order(
        sleeve="stocks", venue="alpaca", asset="QQQ", side="buy",
        mid_price=Decimal(mid), fx_rate=Decimal(fx), notional_gbp=Decimal(notional),
    )
    return simulate_fill(order, config.venue("alpaca"), config.fx.conversion_cost_rate)


# -- buys ---------------------------------------------------------------------


def test_kraken_buy_charges_taker_fee_no_fx(config):
    fill = kraken_buy(config)
    # 0.40% taker on ~£100 gross
    assert fill.fee_gbp == Decimal("0.40")
    assert fill.fx_cost_gbp == Decimal("0")
    # spread: 0.10% full, half per side, on ~£100
    assert fill.spread_cost_gbp == Decimal("0.05")
    assert fill.gross_gbp == Decimal("100.00")


def test_buy_executes_above_mid(config):
    fill = kraken_buy(config, mid="50000")
    assert fill.exec_price == Decimal("50025")  # mid * (1 + 0.10%/2)


def test_buy_cash_delta_exceeds_notional(config):
    fill = kraken_buy(config)
    # fees on top of the notional: total cash out > £100
    assert fill.cash_delta_gbp == Decimal("-100.40")


def test_alpaca_buy_charges_fx_but_no_commission(config):
    fill = alpaca_buy(config)
    assert fill.fee_gbp == Decimal("0")
    assert fill.fx_cost_gbp == Decimal("0.50")  # 0.50% of £100
    assert fill.cash_delta_gbp == Decimal("-100.50")


def test_buy_quantity_math(config):
    fill = alpaca_buy(config, notional="100.00", mid="500", fx="1.27")
    # £100 -> $127; exec price 500 * 1.00025 = 500.125; qty = 127/500.125
    assert fill.quantity == Decimal("0.25393652")


# -- sells --------------------------------------------------------------------


def test_sell_executes_below_mid_and_nets_fees(config):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="sell",
        mid_price=Decimal("50000"), quantity=Decimal("0.002"),
    )
    fill = simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)
    assert fill.exec_price == Decimal("49975")  # mid * (1 - 0.05%)
    assert fill.gross_gbp == Decimal("99.95")
    assert fill.fee_gbp == Decimal("0.40")
    assert fill.cash_delta_gbp == Decimal("99.55")  # proceeds minus fee


def test_sell_on_alpaca_charges_regulatory_fee_and_fx(config):
    # At pot-sized trades the SEC fee rounds to £0.00 (it's $27.80 per $1M),
    # so use a $10k sale to see it: 10000 * 0.00278% = $0.278 -> £0.22.
    order = Order(
        sleeve="stocks", venue="alpaca", asset="QQQ", side="sell",
        mid_price=Decimal("500"), fx_rate=Decimal("1.27"),
        quantity=Decimal("20"),
    )
    fill = simulate_fill(order, config.venue("alpaca"), config.fx.conversion_cost_rate)
    assert fill.fee_gbp == Decimal("0.22")
    assert fill.fx_cost_gbp > 0
    assert fill.cash_delta_gbp < fill.gross_gbp


# -- round trip: fees must bite -------------------------------------------------


def test_round_trip_loses_money_at_flat_price(config):
    """Buy then immediately sell at unchanged mid: the loss equals total
    friction. This is the property the whole milestone exists to enforce."""
    buy = kraken_buy(config, notional="100.00")
    sell_order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="sell",
        mid_price=Decimal("50000"), quantity=buy.quantity,
    )
    sell = simulate_fill(sell_order, config.venue("kraken"), config.fx.conversion_cost_rate)
    round_trip = buy.cash_delta_gbp + sell.cash_delta_gbp
    assert round_trip < 0
    # ~0.4% + 0.4% fees + 0.1% spread on £100 => roughly £0.90 lost
    assert Decimal("-1.00") < round_trip < Decimal("-0.80")


def test_small_trade_friction_pct_documents_fee_drag(config):
    """At £25 (below the £50 floor) friction on a USD venue round trip is ~1%+,
    which is why the floor exists."""
    buy = alpaca_buy(config, notional="25.00")
    friction_pct = buy.total_friction_gbp / Decimal("25.00")
    assert friction_pct > Decimal("0.005")  # >0.5% one-way before the sell leg


# -- validation ------------------------------------------------------------------


def test_buy_requires_notional(config):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("50000"),
    )
    with pytest.raises(FillError, match="notional_gbp"):
        simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def test_sell_requires_quantity(config):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="sell",
        mid_price=Decimal("50000"),
    )
    with pytest.raises(FillError, match="quantity"):
        simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def test_gbp_venue_rejects_nontrivial_fx_rate(config):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("50000"), fx_rate=Decimal("1.27"),
        notional_gbp=Decimal("100"),
    )
    with pytest.raises(FillError, match="fx_rate must be 1"):
        simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def test_invalid_side_rejected(config):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="short",
        mid_price=Decimal("50000"), notional_gbp=Decimal("100"),
    )
    with pytest.raises(FillError, match="side"):
        simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def test_zero_mid_price_rejected(config):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("0"), notional_gbp=Decimal("100"),
    )
    with pytest.raises(FillError, match="mid_price"):
        simulate_fill(order, config.venue("kraken"), config.fx.conversion_cost_rate)


def test_maker_liquidity_uses_maker_fee(config):
    order = Order(
        sleeve="crypto", venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("50000"), notional_gbp=Decimal("100"),
    )
    fill = simulate_fill(
        order, config.venue("kraken"), config.fx.conversion_cost_rate, liquidity="maker"
    )
    assert fill.fee_gbp == Decimal("0.25")  # 0.25% maker vs 0.40% taker
