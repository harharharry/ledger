"""Simulated fill engine — the paper ledger's price of admission.

Every simulated trade is charged:
  * the venue's real fee schedule (taker by default — a daily-cadence bot
    effectively pays taker),
  * half the estimated bid-ask spread (buys execute above mid, sells below),
  * a one-way GBP<->quote-currency FX conversion cost on non-GBP venues,
  * any regulatory fee on sells (SEC section 31 for US stocks).

Conventions:
  * Buys are sized in GBP notional (the strategy thinks in GBP); fees and FX
    cost are charged on top, so total cash out exceeds the notional.
  * Sells are sized in asset quantity; fees and FX cost come out of proceeds.
  * ``fx_rate`` is quote-currency units per 1 GBP (e.g. GBPUSD ~ 1.27).
    GBP-quoted venues must pass fx_rate == 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import VenueConfig
from .money import gbp, price, qty, to_decimal

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")


class FillError(Exception):
    pass


@dataclass(frozen=True)
class Order:
    sleeve: str  # 'crypto' | 'stocks'
    venue: str
    asset: str
    side: str  # 'buy' | 'sell'
    mid_price: Decimal  # in the venue's quote currency
    fx_rate: Decimal = ONE  # quote units per 1 GBP
    notional_gbp: Decimal | None = None  # required for buys
    quantity: Decimal | None = None  # required for sells


@dataclass(frozen=True)
class Fill:
    sleeve: str
    venue: str
    asset: str
    side: str
    quantity: Decimal
    exec_price: Decimal  # in quote currency, spread already applied
    quote_currency: str
    fx_rate: Decimal
    gross_gbp: Decimal  # value at exec price, before fees/FX
    fee_gbp: Decimal
    spread_cost_gbp: Decimal  # already embedded in exec_price; reported for honesty
    fx_cost_gbp: Decimal
    cash_delta_gbp: Decimal  # negative for buys, positive for sells

    @property
    def total_friction_gbp(self) -> Decimal:
        return self.fee_gbp + self.spread_cost_gbp + self.fx_cost_gbp


def _validate(order: Order, venue: VenueConfig) -> None:
    if order.side not in ("buy", "sell"):
        raise FillError(f"invalid side {order.side!r}")
    if order.sleeve not in ("crypto", "stocks"):
        raise FillError(f"invalid sleeve {order.sleeve!r}")
    if to_decimal(order.mid_price) <= 0:
        raise FillError("mid_price must be positive")
    if to_decimal(order.fx_rate) <= 0:
        raise FillError("fx_rate must be positive")
    if venue.quote_currency == "GBP" and to_decimal(order.fx_rate) != 1:
        raise FillError(f"venue {venue.name!r} is GBP-quoted; fx_rate must be 1")
    if order.side == "buy":
        if order.notional_gbp is None or to_decimal(order.notional_gbp) <= 0:
            raise FillError("buy orders need a positive notional_gbp")
    else:
        if order.quantity is None or to_decimal(order.quantity) <= 0:
            raise FillError("sell orders need a positive quantity")


def simulate_fill(
    order: Order,
    venue: VenueConfig,
    fx_conversion_cost_rate: Decimal,
    liquidity: str = "taker",
) -> Fill:
    _validate(order, venue)
    if liquidity not in ("taker", "maker"):
        raise FillError(f"invalid liquidity {liquidity!r}")

    fee_rate = venue.taker_fee_rate if liquidity == "taker" else venue.maker_fee_rate
    half_spread = venue.spread_frac / TWO
    mid = to_decimal(order.mid_price)
    fx = to_decimal(order.fx_rate)
    cross_currency = venue.quote_currency != "GBP"
    fx_cost_rate = fx_conversion_cost_rate if cross_currency else ZERO

    if order.side == "buy":
        exec_price = price(mid * (ONE + half_spread))
        notional_quote = to_decimal(order.notional_gbp) * fx
        quantity = qty(notional_quote / exec_price)
        if quantity <= 0:
            raise FillError("order too small: fills to zero quantity")
        gross_quote = quantity * exec_price
        fee_quote = gross_quote * fee_rate
        gross_gbp = gbp(gross_quote / fx)
        fee_gbp = gbp(fee_quote / fx)
        fx_cost_gbp = gbp(gross_gbp * fx_cost_rate)
        spread_cost_gbp = gbp(quantity * mid * half_spread / fx)
        cash_delta_gbp = -(gross_gbp + fee_gbp + fx_cost_gbp)
    else:
        exec_price = price(mid * (ONE - half_spread))
        quantity = qty(order.quantity)
        gross_quote = quantity * exec_price
        fee_quote = gross_quote * (fee_rate + venue.sell_regulatory_fee_rate)
        gross_gbp = gbp(gross_quote / fx)
        fee_gbp = gbp(fee_quote / fx)
        fx_cost_gbp = gbp(gross_gbp * fx_cost_rate)
        spread_cost_gbp = gbp(quantity * mid * half_spread / fx)
        cash_delta_gbp = gross_gbp - fee_gbp - fx_cost_gbp

    return Fill(
        sleeve=order.sleeve,
        venue=venue.name,
        asset=order.asset,
        side=order.side,
        quantity=quantity,
        exec_price=exec_price,
        quote_currency=venue.quote_currency,
        fx_rate=fx,
        gross_gbp=gross_gbp,
        fee_gbp=fee_gbp,
        spread_cost_gbp=spread_cost_gbp,
        fx_cost_gbp=fx_cost_gbp,
        cash_delta_gbp=cash_delta_gbp,
    )
