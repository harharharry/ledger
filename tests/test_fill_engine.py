from decimal import Decimal

import pytest

from ledger.fill_engine import FillError, Order, simulate_fill


def btc_buy(config, notional="100.00", mid="50000"):
    order = Order(
        venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal(mid), notional_gbp=Decimal(notional),
    )
    return simulate_fill(
        order, config.venue("kraken"), config.asset("BTC"),
        config.fx.conversion_cost_rate,
    )


def hype_buy(config, notional="100.00", mid="50", fx="1.27"):
    order = Order(
        venue="kraken", asset="HYPE", side="buy",
        mid_price=Decimal(mid), fx_rate=Decimal(fx), notional_gbp=Decimal(notional),
    )
    return simulate_fill(
        order, config.venue("kraken"), config.asset("HYPE"),
        config.fx.conversion_cost_rate,
    )


# -- buys ---------------------------------------------------------------------


def test_gbp_pair_buy_charges_taker_fee_no_fx(config):
    fill = btc_buy(config)
    # 0.40% taker on ~£100 gross
    assert fill.fee_gbp == Decimal("0.40")
    assert fill.fx_cost_gbp == Decimal("0")
    # BTC pair spread: 0.10% full, half per side, on ~£100
    assert fill.spread_cost_gbp == Decimal("0.05")
    assert fill.gross_gbp == Decimal("100.00")
    assert fill.cash_delta_gbp == Decimal("-100.40")


def test_buy_executes_above_mid(config):
    fill = btc_buy(config, mid="50000")
    assert fill.exec_price == Decimal("50025")  # mid * (1 + 0.10%/2)


def test_usd_pair_buy_charges_fx_and_pair_spread(config):
    fill = hype_buy(config)
    # HYPE/USD: kraken taker 0.40%, 0.30% pair spread, 0.50% FX
    assert fill.fee_gbp == Decimal("0.40")
    assert fill.fx_cost_gbp == Decimal("0.50")
    assert fill.spread_cost_gbp == Decimal("0.15")
    assert fill.cash_delta_gbp == Decimal("-100.90")
    assert fill.quote_currency == "USD"


def test_buy_quantity_math(config):
    fill = hype_buy(config, notional="100.00", mid="50", fx="1.27")
    # £100 -> $127; exec price 50 * 1.0015 = 50.075; qty = 127/50.075
    assert fill.quantity == Decimal("2.53619571")


# -- sells --------------------------------------------------------------------


def test_sell_executes_below_mid_and_nets_fees(config):
    order = Order(
        venue="kraken", asset="BTC", side="sell",
        mid_price=Decimal("50000"), quantity=Decimal("0.002"),
    )
    fill = simulate_fill(
        order, config.venue("kraken"), config.asset("BTC"),
        config.fx.conversion_cost_rate,
    )
    assert fill.exec_price == Decimal("49975")  # mid * (1 - 0.05%)
    assert fill.gross_gbp == Decimal("99.95")
    assert fill.fee_gbp == Decimal("0.40")
    assert fill.cash_delta_gbp == Decimal("99.55")  # proceeds minus fee


def test_usd_pair_sell_charges_fx(config):
    order = Order(
        venue="kraken", asset="HYPE", side="sell",
        mid_price=Decimal("50"), fx_rate=Decimal("1.27"),
        quantity=Decimal("2.53619571"),
    )
    fill = simulate_fill(
        order, config.venue("kraken"), config.asset("HYPE"),
        config.fx.conversion_cost_rate,
    )
    assert fill.fx_cost_gbp > 0
    assert fill.cash_delta_gbp < fill.gross_gbp


# -- round trip: fees must bite -------------------------------------------------


def test_round_trip_loses_money_at_flat_price(config):
    """Buy then immediately sell at unchanged mid: the loss equals total
    friction. This is the property the whole fill engine exists to enforce."""
    buy = btc_buy(config, notional="100.00")
    sell_order = Order(
        venue="kraken", asset="BTC", side="sell",
        mid_price=Decimal("50000"), quantity=buy.quantity,
    )
    sell = simulate_fill(
        sell_order, config.venue("kraken"), config.asset("BTC"),
        config.fx.conversion_cost_rate,
    )
    round_trip = buy.cash_delta_gbp + sell.cash_delta_gbp
    assert round_trip < 0
    # ~0.4% + 0.4% fees + 0.1% spread on £100 => roughly £0.90 lost
    assert Decimal("-1.00") < round_trip < Decimal("-0.80")


def test_satellite_round_trip_costs_more_than_btc(config):
    """SUI's 0.40% spread vs BTC's 0.10%: the fill engine must show satellite
    friction honestly — this is why the universe is capped at five."""
    sui_order = Order(
        venue="kraken", asset="SUI", side="buy",
        mid_price=Decimal("3"), notional_gbp=Decimal("100.00"),
    )
    sui = simulate_fill(
        sui_order, config.venue("kraken"), config.asset("SUI"),
        config.fx.conversion_cost_rate,
    )
    btc = btc_buy(config)
    assert sui.total_friction_gbp > btc.total_friction_gbp


# -- validation ------------------------------------------------------------------


def test_buy_requires_notional(config):
    order = Order(venue="kraken", asset="BTC", side="buy", mid_price=Decimal("50000"))
    with pytest.raises(FillError, match="notional_gbp"):
        simulate_fill(
            order, config.venue("kraken"), config.asset("BTC"),
            config.fx.conversion_cost_rate,
        )


def test_sell_requires_quantity(config):
    order = Order(venue="kraken", asset="BTC", side="sell", mid_price=Decimal("50000"))
    with pytest.raises(FillError, match="quantity"):
        simulate_fill(
            order, config.venue("kraken"), config.asset("BTC"),
            config.fx.conversion_cost_rate,
        )


def test_gbp_pair_rejects_nontrivial_fx_rate(config):
    order = Order(
        venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("50000"), fx_rate=Decimal("1.27"),
        notional_gbp=Decimal("100"),
    )
    with pytest.raises(FillError, match="fx_rate must be 1"):
        simulate_fill(
            order, config.venue("kraken"), config.asset("BTC"),
            config.fx.conversion_cost_rate,
        )


def test_order_listing_mismatch_rejected(config):
    order = Order(
        venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("50000"), notional_gbp=Decimal("100"),
    )
    with pytest.raises(FillError, match="does not match"):
        simulate_fill(
            order, config.venue("kraken"), config.asset("ETH"),
            config.fx.conversion_cost_rate,
        )


def test_invalid_side_rejected(config):
    order = Order(
        venue="kraken", asset="BTC", side="short",
        mid_price=Decimal("50000"), notional_gbp=Decimal("100"),
    )
    with pytest.raises(FillError, match="side"):
        simulate_fill(
            order, config.venue("kraken"), config.asset("BTC"),
            config.fx.conversion_cost_rate,
        )


def test_zero_mid_price_rejected(config):
    order = Order(
        venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("0"), notional_gbp=Decimal("100"),
    )
    with pytest.raises(FillError, match="mid_price"):
        simulate_fill(
            order, config.venue("kraken"), config.asset("BTC"),
            config.fx.conversion_cost_rate,
        )


def test_maker_liquidity_uses_maker_fee(config):
    order = Order(
        venue="kraken", asset="BTC", side="buy",
        mid_price=Decimal("50000"), notional_gbp=Decimal("100"),
    )
    fill = simulate_fill(
        order, config.venue("kraken"), config.asset("BTC"),
        config.fx.conversion_cost_rate, liquidity="maker",
    )
    assert fill.fee_gbp == Decimal("0.25")  # 0.25% maker vs 0.40% taker
