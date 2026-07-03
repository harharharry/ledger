"""Stocks sleeve strategy — a thin, single-asset wrapper over signals.py.

v1 tracks exactly one stocks/ETF asset (QQQ, per config). All the actual
signal logic lives in signals.propose_accumulation so the crypto sleeve
computes identical maths; this module only checks that the price series it
was handed matches the configured stocks asset. Output is a Proposal or None
— never a trade.

Stocks-specific note: QQQ trades on a USD venue (Alpaca), so every fill
carries GBP↔USD FX conversion cost on top of the notional. That cost is
priced by the fill engine, not here — the strategy sizes in GBP and lets the
fee-aware trade floor keep proposals large enough to absorb the drag.
"""

from __future__ import annotations

from decimal import Decimal

from ..config import Config
from ..data.types import PriceSeries
from ..proposals import Proposal
from .signals import StrategyError, propose_accumulation

SLEEVE = "stocks"


def propose(
    series: PriceSeries,
    config: Config,
    available_cash_gbp: Decimal,
) -> Proposal | None:
    """Run strategy v1 for the stocks sleeve against the given price series."""
    assets = config.sleeve_assets(SLEEVE)
    if len(assets) != 1:
        # Silently picking one would trade an asset nobody reviewed. If the
        # sleeve ever grows, extend this module deliberately (per-asset cash
        # split, proposal cadence) rather than looping here.
        raise StrategyError(
            f"stocks sleeve v1 handles exactly one configured asset, found "
            f"{sorted(a.symbol for a in assets)} — multi-asset stocks is not implemented"
        )
    asset = assets[0]
    if series.symbol != asset.symbol:
        raise StrategyError(
            f"price series is for {series.symbol!r} but the configured stocks "
            f"asset is {asset.symbol!r}"
        )
    return propose_accumulation(series, asset, config, available_cash_gbp)
