"""Risk manager — the veto layer between the strategy and the orchestrator.

Every proposal passes through ``evaluate`` before anything else happens to it.
This module can block a proposal or resize it *down*; it never originates a
trade, never increases one, and never converts a block into a different trade.
Rebalancing is deliberately outside its power: allocation drift is flagged in
plain English for Harry to act on manually.

Rule order matters and is fixed: kill switch first (CLAUDE.md non-negotiable
5 — no code path or config value may reorder or skip it), then cadence caps,
then the per-trade asset cap, then the minimum trade floor. First block wins.
All limits come from config; nothing is hardcoded here.

v1.2: per-asset target weights replace the old crypto/stocks sleeves; every
sleeve-level rule below became the same rule at asset level. The per-trade cap
is the *greater* of the percentage cap and the trade floor: when an asset's
allocation is small enough that its percentage cap falls below the floor, the
floor becomes the binding cap (otherwise the two rules deadlock the asset —
see NOTES.md, milestone 5, resolved 2026-07-03; at v1.2 weights every asset
except BTC needs this). Sub-floor arrivals are still blocked outright; the
floor-block rule itself never softens.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from . import kill_switch
from .config import Config
from .money import gbp
from .proposals import Proposal

ZERO = Decimal("0")
HUNDRED = Decimal("100")
ONE_PT = Decimal("0.1")


@dataclass(frozen=True)
class PortfolioSnapshot:
    """What the portfolio looks like right now, valued at market in GBP."""

    cash_gbp: Decimal
    holdings_value_gbp: dict[str, Decimal]  # {asset symbol: market value}


@dataclass(frozen=True)
class RiskDecision:
    proposal: Proposal | None  # surviving (possibly resized) proposal; None when blocked
    verdict: str  # 'approved' | 'resized' | 'blocked'
    reasons: tuple[str, ...]  # plain English, one per rule that acted
    drift_flags: tuple[str, ...]


def _pct_label(frac: Decimal) -> str:
    """Render a fraction as its human percentage: Decimal('0.2') -> '20'."""
    return format((frac * HUNDRED).normalize(), "f")


def _pts_label(pts: Decimal) -> str:
    """Render percentage points to one decimal place for drift messages."""
    return format(pts.quantize(ONE_PT), "f")


def asset_value_gbp(snapshot: PortfolioSnapshot, symbol: str, config: Config) -> Decimal:
    """An asset's holdings at market PLUS its target share of uninvested cash.

    Cash is allocated by target weight because on day one there are no
    holdings at all: if an asset were only worth what it holds, everything
    would be worth £0 and the per-trade cap (a percentage of the asset's
    allocation) would forbid all trading forever. Counting the asset's share
    of cash gives the cap something real to bite on — e.g. £500 cash and no
    holdings puts BTC (40% weight) at £200, so the 20% cap allows £40 (which
    the trade floor then lifts to £50 — see evaluate).
    """
    holdings = snapshot.holdings_value_gbp.get(symbol, ZERO)
    return holdings + snapshot.cash_gbp * config.asset(symbol).target_weight_frac


def check_drift(snapshot: PortfolioSnapshot, config: Config) -> tuple[str, ...]:
    """Flag assets whose share of INVESTED value has drifted past the threshold.

    Cash is excluded here (unlike ``asset_value_gbp``): drift is about what
    the market has done to the positions we actually hold, and uninvested
    cash is by definition still at the target split. With no holdings there
    is nothing to compare, so no flags.

    A deviation is flagged only when it is strictly greater than the
    configured threshold — exactly-at-threshold is tolerated. Flags are
    information for Harry, nothing more: this function never touches,
    resizes, or emits proposals, because auto-rebalancing is exactly the
    kind of unsupervised trading this project forbids.
    """
    total = sum(snapshot.holdings_value_gbp.values(), start=ZERO)
    if total == 0:
        return ()

    flags: list[str] = []
    for symbol, asset in config.assets.items():
        actual_pts = snapshot.holdings_value_gbp.get(symbol, ZERO) / total * HUNDRED
        target_pts = asset.target_weight_frac * HUNDRED
        deviation = abs(actual_pts - target_pts)
        if deviation > config.portfolio.drift_threshold_pts:
            flags.append(
                f"{symbol} is {_pts_label(actual_pts)}% of invested value "
                f"vs {_pts_label(target_pts)}% target — off by "
                f"{_pts_label(deviation)}pts (threshold "
                f"±{format(config.portfolio.drift_threshold_pts, 'f')}pts); "
                f"rebalancing is Harry's decision, not the bot's"
            )
    return tuple(flags)


def evaluate(
    proposal: Proposal,
    snapshot: PortfolioSnapshot,
    config: Config,
    proposals_today: int,
    proposals_this_week: int,
    kill_switch_path: str | Path | None = None,
) -> RiskDecision:
    """Run one proposal through every risk rule, in fixed order.

    ``kill_switch_path`` defaults to the configured path; the override exists
    only so tests can point at a temp directory. Drift flags are computed
    up front and attached to every decision — including blocks — because a
    drifted book is worth knowing about regardless of what happened to
    today's proposal.
    """
    drift_flags = check_drift(snapshot, config)
    trading = config.trading

    # 1. Kill switch. First, always, no matter what else is wrong.
    path = kill_switch_path if kill_switch_path is not None else config.runtime.kill_switch_path
    if kill_switch.is_engaged(path):
        return RiskDecision(
            proposal=None,
            verdict="blocked",
            reasons=("kill switch is engaged — all bot activity is paused",),
            drift_flags=drift_flags,
        )

    # 2. Cadence caps. The counts come from the caller (the run log); the
    # risk layer just enforces the ceiling.
    if proposals_today >= trading.max_proposals_per_day:
        return RiskDecision(
            proposal=None,
            verdict="blocked",
            reasons=(
                f"daily proposal cap reached: {proposals_today} already today "
                f"(max {trading.max_proposals_per_day}/day)",
            ),
            drift_flags=drift_flags,
        )
    if proposals_this_week >= trading.max_proposals_per_week:
        return RiskDecision(
            proposal=None,
            verdict="blocked",
            reasons=(
                f"weekly proposal cap reached: {proposals_this_week} already this week "
                f"(max {trading.max_proposals_per_week}/week)",
            ),
            drift_flags=drift_flags,
        )

    reasons: list[str] = []
    surviving = proposal
    verdict = "approved"

    # 3. Per-trade asset cap. Oversize proposals are resized DOWN to the
    # cap — never up, and an at-cap or under-cap proposal passes untouched.
    #
    # The effective cap is max(percentage cap, trade floor) — the floor wins
    # a cap/floor conflict (Harry's decision, 2026-07-03). Without this, a
    # small allocation deadlocks: at v1.2 weights a 10% satellite is ~£50 of
    # a £500 pot, so the 20% cap is ~£10 — far below the £50 floor — and
    # every proposal would be resized-then-blocked forever. Letting the floor
    # act as the binding minimum cap means every asset can always make at
    # least one floor-sized trade, which is exactly the minimum the fee-drag
    # rule already demands; as the allocation grows past floor/cap_frac the
    # percentage cap dominates again and both rules keep their intent.
    pct_cap = gbp(trading.per_trade_cap_frac_of_asset * asset_value_gbp(snapshot, proposal.asset, config))
    floor = gbp(trading.min_trade_gbp)
    cap = max(pct_cap, floor)
    floor_is_cap = pct_cap < floor
    cap_pct = _pct_label(trading.per_trade_cap_frac_of_asset)
    if proposal.notional_gbp > cap:
        original = gbp(proposal.notional_gbp)
        if floor_is_cap:
            # Honest audit note: the percentage wasn't what bound here.
            note = (
                f" [risk: resized from £{original} to £{cap} — trade floor is "
                f"the effective cap at this allocation size]"
            )
            reason = (
                f"notional £{original} exceeds the effective per-trade cap of "
                f"£{cap} for {proposal.asset} (the {cap_pct}% cap "
                f"of £{pct_cap} is below the £{floor} trade floor, so the "
                f"floor is the binding cap); resized down to £{cap}"
            )
        else:
            note = f" [risk: resized from £{original} to £{cap} — {cap_pct}% asset cap]"
            reason = (
                f"notional £{original} exceeds the {cap_pct}% per-trade cap "
                f"of £{cap} for {proposal.asset}; resized down to £{cap}"
            )
        surviving = replace(
            proposal,
            notional_gbp=cap,
            rationale=proposal.rationale + note,
        )
        verdict = "resized"
        reasons.append(reason)

    # 4. Fee floor. Applied to the surviving notional. Because the effective
    # cap above is never below the floor, a cap resize can no longer land
    # under it — but a proposal that *arrives* below the floor is still
    # blocked, unsoftened: the strategy shouldn't emit those, and the veto
    # layer doesn't trust upstream. Never raised to the floor: this layer
    # does not increase trades.
    if surviving.notional_gbp < trading.min_trade_gbp:
        reasons.append(
            f"notional £{gbp(surviving.notional_gbp)} is below the "
            f"£{gbp(trading.min_trade_gbp)} minimum trade floor "
            f"(fee drag dominates); blocked"
        )
        return RiskDecision(
            proposal=None,
            verdict="blocked",
            reasons=tuple(reasons),
            drift_flags=drift_flags,
        )

    return RiskDecision(
        proposal=surviving,
        verdict=verdict,
        reasons=tuple(reasons),
        drift_flags=drift_flags,
    )
