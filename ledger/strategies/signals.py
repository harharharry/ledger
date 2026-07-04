"""Strategy signals — the whole of strategy v1, applied per asset.

Spec §6, kept deliberately boring: a moving-average trend filter gates
DCA-style accumulation (no buying into a downtrend), and RSI tilts the size —
bigger on oversold dips within an uptrend, smaller when frothy. Buys only.

v1.2: the universe is multiple crypto assets, each evaluated independently by
this same function — the orchestrator decides which asset gets the daily slot
(most under its target weight goes first). Everything is deterministic Decimal
arithmetic: no floats, no randomness, no network, no LLMs (CLAUDE.md
non-negotiable 4). All tunables come from config. Output is a Proposal — this
module never touches the ledger and never executes anything.
"""

from __future__ import annotations

from decimal import Decimal

from ..config import AssetConfig, Config
from ..data.types import PriceSeries
from ..money import gbp
from ..proposals import Proposal

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

# Currency prefixes for rationale text only — accounting stays GBP throughout.
_CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$"}


class StrategyError(Exception):
    pass


def sma(values: list[Decimal], n: int) -> Decimal:
    """Simple moving average of the last n values."""
    if n < 1:
        raise StrategyError(f"sma window must be at least 1, got {n}")
    if len(values) < n:
        raise StrategyError(f"sma needs at least {n} values, got {len(values)}")
    return sum(values[-n:], ZERO) / n


def rsi(values: list[Decimal], period: int) -> Decimal:
    """Wilder's RSI over the full series, 0-100 scale.

    Seeded with the plain mean of the first `period` gains and losses, then
    Wilder-smoothed for every later close: avg = (prev*(period-1) + current)
    / period. Computed over the whole series rather than a trailing window
    because the smoothing carries history — truncating the input changes the
    answer, so callers pass everything they have.
    """
    if period < 1:
        raise StrategyError(f"rsi period must be at least 1, got {period}")
    if len(values) < period + 1:
        raise StrategyError(
            f"rsi({period}) needs at least {period + 1} values, got {len(values)}"
        )
    deltas = [later - earlier for earlier, later in zip(values, values[1:])]
    gains = [max(d, ZERO) for d in deltas]
    losses = [max(-d, ZERO) for d in deltas]

    avg_gain = sum(gains[:period], ZERO) / period
    avg_loss = sum(losses[:period], ZERO) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == ZERO:
        return HUNDRED  # nothing but gains: maximally overbought by convention
    relative_strength = avg_gain / avg_loss
    return HUNDRED - HUNDRED / (ONE + relative_strength)


def propose_accumulation(
    series: PriceSeries,
    asset: AssetConfig,
    config: Config,
    available_cash_gbp: Decimal,
) -> Proposal | None:
    """Apply strategy v1 to one asset: trend filter, RSI tilt, fee floor.

    Returns a buy Proposal, or None when the strategy chooses to sit out
    (downtrend, or the sized trade would be below the fee-aware floor).
    Raises StrategyError on thin data — guessing on a partial history would
    make the trend filter meaningless, so we fail loudly instead.
    """
    if series.symbol != asset.symbol:
        raise StrategyError(
            f"series is for {series.symbol!r} but asset config is {asset.symbol!r}"
        )
    strat = config.strategy
    if len(series) < strat.ma_days + 1:
        raise StrategyError(
            f"{series.symbol}: the {strat.ma_days}-day trend filter needs at least "
            f"{strat.ma_days + 1} daily closes, got {len(series)} — refusing to "
            "guess on thin data"
        )

    values = series.values()
    latest = series.latest.close
    moving_average = sma(values, strat.ma_days)
    if latest < moving_average:
        return None  # downtrend: DCA is gated off entirely

    rsi_value = rsi(values, strat.rsi_period)
    size = strat.base_trade_gbp
    if rsi_value <= strat.rsi_oversold:
        size *= strat.oversold_tilt
        rsi_note = (
            f"RSI {_whole(rsi_value)} is oversold (<= {_whole(strat.rsi_oversold)}), "
            f"so a {strat.oversold_tilt}x-tilted £{gbp(size)} accumulation"
        )
    elif rsi_value >= strat.rsi_overbought:
        size *= strat.overbought_tilt
        rsi_note = (
            f"RSI {_whole(rsi_value)} is overbought (>= {_whole(strat.rsi_overbought)}), "
            f"so a {strat.overbought_tilt}x-reduced £{gbp(size)} accumulation"
        )
    else:
        rsi_note = (
            f"RSI {_whole(rsi_value)} is neutral, "
            f"so standard £{gbp(size)} accumulation"
        )

    capped = min(size, available_cash_gbp)
    if capped < config.trading.min_trade_gbp:
        return None  # fee drag dominates below the floor — never propose sub-floor
    notional = gbp(capped)

    cash_note = (
        f", capped to £{notional} by available cash" if capped < size else ""
    )
    symbol = _CURRENCY_SYMBOLS.get(series.currency, f"{series.currency} ")
    rationale = (
        f"{series.symbol} {symbol}{_two_dp(latest)} is above its {strat.ma_days}-day "
        f"MA {symbol}{_two_dp(moving_average)} (uptrend); {rsi_note}{cash_note}."
    )
    return Proposal(
        asset=asset.symbol,
        venue=asset.venue,
        side="buy",
        notional_gbp=notional,
        rationale=rationale,
    )


def _two_dp(value: Decimal) -> Decimal:
    """Prices in rationale text read best at 2dp; accounting precision is elsewhere."""
    return value.quantize(Decimal("0.01"))


def _whole(value: Decimal) -> Decimal:
    """RSI readings in rationale text as whole numbers — 56, not 56.1837."""
    return value.quantize(Decimal("1"))
