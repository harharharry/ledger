"""The Proposal — the only thing a strategist is allowed to produce.

Strategists emit proposals; the risk manager blocks or resizes them; the
orchestrator turns survivors into fills. Nothing here touches the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class ProposalError(Exception):
    pass


@dataclass(frozen=True)
class Proposal:
    sleeve: str  # 'crypto' | 'stocks'
    asset: str
    venue: str
    side: str  # v1 strategies only ever propose 'buy'
    notional_gbp: Decimal
    rationale: str  # plain English; Harry reads this in one glance

    def __post_init__(self) -> None:
        if self.sleeve not in ("crypto", "stocks"):
            raise ProposalError(f"invalid sleeve {self.sleeve!r}")
        if self.side not in ("buy", "sell"):
            raise ProposalError(f"invalid side {self.side!r}")
        if self.notional_gbp <= 0:
            raise ProposalError("notional_gbp must be positive")
        if not self.rationale.strip():
            raise ProposalError("a proposal without a rationale is not reviewable")
