"""The daily run — deterministic Python end to end, no LLM anywhere.

One cron invocation per day. Order of operations:

  1. Idempotency guard: a day that already finished success/no-action never
     runs again (and can never double-trade regardless, thanks to trade keys).
     Crashed or failed runs may be retried.
  2. Kill switch — checked before any action, every run (non-negotiable 5).
  3. Fetch prices + FX, snapshot the benchmark on day one (spec §14.7).
  4. Assets are evaluated most-under-target-weight first; the risk manager
     has veto over everything; survivors become paper fills (Phase 1 is
     fully autonomous against the paper ledger — nothing real is at risk; in
     Phase 2 execution is replaced by propose-and-approve).
  5. Every run finishes with a logged outcome: success / no-action / failure
     (non-negotiable 6). Failures record, then re-raise so cron exits nonzero.

v1.2: crypto-only, multi-asset. Each asset runs the same strategy against its
own series; the daily cadence cap (1/day) means the ordering in step 4 decides
who gets the slot, so accumulation naturally pulls the book toward target
weights without ever selling to rebalance.

Phase 1 cadence note: the proposal caps (1/day, 5/week) are enforced against
*executed paper trades*, which is what "proposal" means while the bot trades
autonomously. In Phase 2 the same counts come from proposals sent to Harry.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from . import kill_switch, risk
from .config import AssetConfig, Config, load_config
from .data import coingecko, fx
from .data.types import PriceSeries
from .fill_engine import Order, simulate_fill
from .money import gbp
from .paper_ledger import DuplicateTradeError, PaperLedger
from .risk import PortfolioSnapshot
from .strategies import signals

PENNY = Decimal("0.01")
ZERO = Decimal("0")


@dataclass(frozen=True)
class RunResult:
    run_date: dt.date
    outcome: str  # 'success' | 'no-action' | 'failure' (failure also raises)
    events: tuple[str, ...]


def affordable_notional(
    available_cash_gbp: Decimal, venue, listing: AssetConfig, fx_cost_rate: Decimal
) -> Decimal:
    """The largest buy notional whose all-in cost (fees + FX on top) still fits
    in cash. The strategy caps size at this figure so a fill can never bounce
    off InsufficientCashError. A penny is shaved to absorb rounding at the
    pence quantization boundary.

    Affordability is checked against TOTAL cash, not the asset's target-weight
    slice: its only job is preventing overdraw, and with the 1/day cadence cap
    there is at most one fill per run anyway. Per-asset sizing discipline is
    the risk manager's cap (with the floor-wins rule) — checking the slice
    here would deadlock 10%-weight satellites forever, since 10% of the pot
    can never cover a floor-sized trade plus fees at £500."""
    friction = venue.taker_fee_rate + listing.spread_frac / 2
    if listing.quote_currency != "GBP":
        friction += fx_cost_rate
    return gbp(available_cash_gbp / (1 + friction)) - PENNY


def _week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _asset_order(snapshot: PortfolioSnapshot, config: Config) -> list[str]:
    """Most-under-target asset gets first claim on the daily proposal slot.
    A fixed order would systematically starve whatever came last; this way
    accumulation pulls the book toward target weights by construction."""
    total = sum(snapshot.holdings_value_gbp.values(), start=ZERO)

    def under_allocation(symbol: str) -> Decimal:
        share = (
            snapshot.holdings_value_gbp.get(symbol, ZERO) / total if total else ZERO
        )
        return config.assets[symbol].target_weight_frac - share

    return sorted(config.assets, key=lambda s: (-under_allocation(s), s))


def run_daily(
    config: Config,
    led: PaperLedger,
    fetch_series,
    fetch_fx,
    today: dt.date | None = None,
    kill_switch_path: str | Path | None = None,
) -> RunResult:
    today = today or dt.date.today()
    ks_path = kill_switch_path or config.runtime.kill_switch_path

    prior = led.run_outcome(today)
    if prior in ("success", "no-action"):
        return RunResult(
            today, "no-action",
            (f"already ran today (outcome: {prior}); runs are idempotent",),
        )

    led.record_run_start(today)

    if kill_switch.is_engaged(ks_path):
        led.finish_run(today, "no-action", "kill switch engaged")
        return RunResult(today, "no-action", ("kill switch engaged — no action taken",))

    try:
        events: list[str] = []
        led.open(config.portfolio.starting_capital_gbp)

        fx_rate = fetch_fx()
        series_by_symbol: dict[str, PriceSeries] = {
            symbol: fetch_series(asset) for symbol, asset in config.assets.items()
        }

        prices_gbp: dict[str, Decimal] = {}
        for symbol, series in series_by_symbol.items():
            close = series.latest.close
            prices_gbp[symbol] = close if series.currency == "GBP" else close / fx_rate

        if not led.benchmark_snapshots():
            led.snapshot_benchmark(
                "phase1",
                {sym: gbp(p) for sym, p in prices_gbp.items()},
                snapshot_date=today.isoformat(),
            )
            events.append("day one: benchmark starting prices snapshotted")

        holdings: dict[str, Decimal] = {}
        for pos in led.positions():
            if pos.quantity > 0:
                holdings[pos.asset] = pos.quantity * prices_gbp[pos.asset]
        snapshot = PortfolioSnapshot(
            cash_gbp=led.cash_balance_gbp(), holdings_value_gbp=holdings
        )
        for flag in risk.check_drift(snapshot, config):
            events.append(f"drift: {flag}")

        trades = 0
        for symbol in _asset_order(snapshot, config):
            asset = config.assets[symbol]
            series = series_by_symbol[symbol]
            venue = config.venue(asset.venue)
            affordable = affordable_notional(
                led.cash_balance_gbp(), venue, asset, config.fx.conversion_cost_rate
            )

            proposal = signals.propose_accumulation(series, asset, config, affordable)
            if proposal is None:
                events.append(
                    f"{symbol}: no proposal (trend gate closed, or size below "
                    f"the fee floor)"
                )
                continue

            decision = risk.evaluate(
                proposal, snapshot, config,
                proposals_today=led.trades_count_on(today),
                proposals_this_week=led.trades_count_between(_week_start(today), today),
                kill_switch_path=ks_path,
            )
            if decision.proposal is None:
                events.append(f"{symbol}: blocked — {'; '.join(decision.reasons)}")
                continue
            if decision.verdict == "resized":
                events.append(f"{symbol}: {'; '.join(decision.reasons)}")

            approved = decision.proposal
            order = Order(
                venue=approved.venue, asset=approved.asset,
                side=approved.side, mid_price=series.latest.close,
                fx_rate=Decimal("1") if asset.quote_currency == "GBP" else fx_rate,
                notional_gbp=approved.notional_gbp,
            )
            fill = simulate_fill(order, venue, asset, config.fx.conversion_cost_rate)
            trade_key = f"{today}:{approved.asset}:{approved.side}"
            try:
                led.record_fill(
                    fill, trade_key, rationale=approved.rationale, run_date=today
                )
            except DuplicateTradeError:
                events.append(f"{symbol}: {trade_key} already recorded — skipped")
                continue
            trades += 1
            events.append(
                f"{symbol}: bought {fill.quantity} for "
                f"£{-fill.cash_delta_gbp} all-in (£{fill.total_friction_gbp} "
                f"friction) — {approved.rationale}"
            )

        outcome = "success" if trades else "no-action"
        led.finish_run(today, outcome, "; ".join(events))
        return RunResult(today, outcome, tuple(events))
    except Exception as e:
        led.finish_run(today, "failure", f"{type(e).__name__}: {e}")
        raise


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config.toml")
    led = PaperLedger(root / config.runtime.db_path)
    try:
        result = run_daily(
            config, led,
            # each asset is priced in its pair's quote currency, so paper
            # fills execute in the currency the real Kraken pair trades
            fetch_series=lambda a: coingecko.fetch_daily_closes(
                a.symbol, a.coingecko_id, vs_currency=a.quote_currency.lower()
            ),
            fetch_fx=fx.fetch_gbp_usd,
            kill_switch_path=root / config.runtime.kill_switch_path,
        )
    except Exception as e:
        # The failure outcome is already in the run log; nonzero exit is what
        # the scheduler's alerting hooks onto.
        print(f"RUN FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        led.close()
    for event in result.events:
        print(event)
    print(f"outcome: {result.outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
