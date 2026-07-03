"""Weekly review — deterministic, read-only reporting over the paper ledger.

The report answers one question above all others: how did the bot do against
just buying the same 60/40 allocation on day one and never touching it? That
comparison is the point of the whole project, so a report that cannot make it
(no benchmark snapshot, no current price) refuses to ship rather than guess.

Rules this module lives by (CLAUDE.md + reporting agent brief):
  * Read-only. Nothing here mutates trades, positions, cash, or runs.
  * All figures in GBP. Fees and FX costs are always visible — venue fees and
    FX are cash actually paid out; spread is reported separately because it is
    embedded in execution prices rather than charged on top.
  * The benchmark is fee-free buy-and-hold, and the rendered copy says so
    ("before costs") — an honest comparison names its own handicap.
  * Computation is plain Python; the render is a deterministic template. The
    optional LLM prose-polish arrives with Phase 2 infrastructure — the
    numeric template below must always stand alone (NOTES.md, milestone 6).
  * Honest framing: a record of what happened, never a forecast.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .config import Config, load_config
from .data import alpaca, coingecko, fx
from .money import gbp, to_decimal
from .paper_ledger import PaperLedger
from .risk import PortfolioSnapshot, check_drift

ZERO = Decimal("0")
HUNDRED = Decimal("100")
TWO_DP = Decimal("0.01")


class ReportError(Exception):
    pass


# -- report shape ---------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    asset: str
    sleeve: str
    quantity: Decimal
    market_value_gbp: Decimal
    book_cost_gbp: Decimal  # all-in (venue fee + FX included), per the ledger


@dataclass(frozen=True)
class CostBreakdown:
    """Friction split the way it is actually incurred: venue fees and FX are
    cash paid out on top of gross value; spread is implicit in the execution
    price, so it is reported alongside but never added to 'paid'."""

    fee_gbp: Decimal
    fx_gbp: Decimal
    spread_gbp: Decimal

    @property
    def paid_gbp(self) -> Decimal:
        return gbp(self.fee_gbp + self.fx_gbp)


@dataclass(frozen=True)
class TradeLine:
    run_date: str
    sleeve: str
    asset: str
    side: str
    quantity: Decimal
    gross_gbp: Decimal
    cash_delta_gbp: Decimal  # negative for buys, positive for sells
    rationale: str | None


@dataclass(frozen=True)
class RunSummary:
    total: int
    success: int
    no_action: int
    failure: int
    failures: tuple[str, ...]  # "run_date: detail", one per failed/crashed run


@dataclass(frozen=True)
class WeeklyReport:
    week_start: dt.date  # Monday of week_ending's ISO week
    week_ending: dt.date
    starting_capital_gbp: Decimal
    allocation_label: str  # e.g. "60/40 crypto/stocks"
    portfolio_value_gbp: Decimal
    cash_gbp: Decimal
    holdings: tuple[Holding, ...]
    pnl_gbp: Decimal
    pnl_pct: Decimal
    benchmark_value_gbp: Decimal
    vs_benchmark_gbp: Decimal  # portfolio minus benchmark; negative = trailing
    week_costs: CostBreakdown
    cumulative_costs: CostBreakdown
    trades: tuple[TradeLine, ...]
    runs: RunSummary
    drift_flags: tuple[str, ...]


# -- computation ------------------------------------------------------------------


def _allocation_frac(sleeve: str, config: Config) -> Decimal:
    if sleeve == "crypto":
        return config.portfolio.allocation_crypto_frac
    if sleeve == "stocks":
        return config.portfolio.allocation_stocks_frac
    raise ReportError(f"unknown sleeve {sleeve!r}")


def _pct_label(frac: Decimal) -> str:
    """Decimal('0.6') -> '60' for the allocation label."""
    return format((frac * HUNDRED).normalize(), "f")


def _benchmark_value_gbp(
    led: PaperLedger, config: Config, prices_gbp: dict[str, Decimal]
) -> Decimal:
    """Value today of the untouched day-one allocation: each sleeve's share of
    starting capital converted to units at the snapshotted phase1 price, held
    ever since. Fee-free by construction — the render says 'before costs'."""
    rows = [r for r in led.benchmark_snapshots() if r["phase"] == "phase1"]
    if not rows:
        raise ReportError(
            "no phase1 benchmark snapshot in the ledger — the buy-and-hold "
            "comparison is impossible and no report ships without it"
        )
    day_one = min(r["snapshot_date"] for r in rows)
    day_one_rows = [r for r in rows if r["snapshot_date"] == day_one]

    seen_sleeves: set[str] = set()
    total = ZERO
    for row in day_one_rows:
        asset = row["asset"]
        asset_cfg = config.assets.get(asset)
        if asset_cfg is None:
            raise ReportError(
                f"benchmark asset {asset!r} is not in config — cannot determine "
                f"its sleeve allocation"
            )
        if asset_cfg.sleeve in seen_sleeves:
            # v1 puts one asset per sleeve; two would double-count the stake.
            raise ReportError(
                f"multiple benchmark assets in the {asset_cfg.sleeve!r} sleeve — "
                f"multi-asset benchmarks are unimplemented in v1"
            )
        seen_sleeves.add(asset_cfg.sleeve)
        if asset not in prices_gbp:
            raise ReportError(f"no current GBP price for benchmark asset {asset!r}")
        stake = config.portfolio.starting_capital_gbp * _allocation_frac(
            asset_cfg.sleeve, config
        )
        units = stake / to_decimal(row["price_gbp"])
        total += units * to_decimal(prices_gbp[asset])
    return gbp(total)


def _sum_costs(trade_rows) -> CostBreakdown:
    fee = fx_cost = spread = ZERO
    for r in trade_rows:
        fee += to_decimal(r["fee_gbp"])
        fx_cost += to_decimal(r["fx_cost_gbp"])
        spread += to_decimal(r["spread_cost_gbp"])
    return CostBreakdown(fee_gbp=gbp(fee), fx_gbp=gbp(fx_cost), spread_gbp=gbp(spread))


def _run_summary(run_rows) -> RunSummary:
    success = no_action = failure = 0
    failures: list[str] = []
    for r in run_rows:
        outcome = r["outcome"]
        if outcome == "success":
            success += 1
        elif outcome == "no-action":
            no_action += 1
        else:
            # 'failure', or NULL — a run that started and never finished is a
            # failure for reliability purposes; hiding it would be dishonest.
            failure += 1
            if outcome is None:
                detail = "started but never finished (crashed mid-run)"
            else:
                detail = r["detail"] or "no detail recorded"
            failures.append(f"{r['run_date']}: {detail}")
    return RunSummary(
        total=len(run_rows),
        success=success,
        no_action=no_action,
        failure=failure,
        failures=tuple(failures),
    )


def build_weekly_report(
    led: PaperLedger,
    config: Config,
    prices_gbp: dict[str, Decimal],
    week_ending: dt.date,
) -> WeeklyReport:
    """Compute the week's numbers from the ledger. Pure given its inputs:
    same ledger contents + same prices -> the identical report."""
    week_start = week_ending - dt.timedelta(days=week_ending.weekday())

    cash = led.cash_balance_gbp()
    holdings: list[Holding] = []
    sleeve_values: dict[str, Decimal] = {"crypto": ZERO, "stocks": ZERO}
    for pos in led.positions():
        if pos.quantity <= 0:
            continue
        if pos.asset not in prices_gbp:
            raise ReportError(f"no current GBP price for held asset {pos.asset!r}")
        value = gbp(pos.quantity * to_decimal(prices_gbp[pos.asset]))
        sleeve_values[pos.sleeve] = sleeve_values.get(pos.sleeve, ZERO) + value
        holdings.append(
            Holding(
                asset=pos.asset,
                sleeve=pos.sleeve,
                quantity=pos.quantity,
                market_value_gbp=value,
                book_cost_gbp=pos.book_cost_gbp,
            )
        )

    portfolio_value = gbp(
        cash + sum((h.market_value_gbp for h in holdings), start=ZERO)
    )
    starting = config.portfolio.starting_capital_gbp
    pnl = gbp(portfolio_value - starting)
    pnl_pct = (pnl / starting * HUNDRED).quantize(TWO_DP)

    benchmark_value = _benchmark_value_gbp(led, config, prices_gbp)
    vs_benchmark = gbp(portfolio_value - benchmark_value)

    week_trade_rows = led.trades_between(week_start, week_ending)
    trades = tuple(
        TradeLine(
            run_date=r["run_date"],
            sleeve=r["sleeve"],
            asset=r["asset"],
            side=r["side"],
            quantity=to_decimal(r["quantity"]),
            gross_gbp=to_decimal(r["gross_gbp"]),
            cash_delta_gbp=to_decimal(r["cash_delta_gbp"]),
            rationale=r["rationale"],
        )
        for r in week_trade_rows
    )

    allocation_label = (
        f"{_pct_label(config.portfolio.allocation_crypto_frac)}/"
        f"{_pct_label(config.portfolio.allocation_stocks_frac)} crypto/stocks"
    )

    return WeeklyReport(
        week_start=week_start,
        week_ending=week_ending,
        starting_capital_gbp=gbp(starting),
        allocation_label=allocation_label,
        portfolio_value_gbp=portfolio_value,
        cash_gbp=cash,
        holdings=tuple(holdings),
        pnl_gbp=pnl,
        pnl_pct=pnl_pct,
        benchmark_value_gbp=benchmark_value,
        vs_benchmark_gbp=vs_benchmark,
        week_costs=_sum_costs(week_trade_rows),
        cumulative_costs=_sum_costs(led.trades()),
        trades=trades,
        runs=_run_summary(led.runs_between(week_start, week_ending)),
        drift_flags=check_drift(
            PortfolioSnapshot(cash_gbp=cash, holdings_value_gbp=sleeve_values),
            config,
        ),
    )


# -- rendering ---------------------------------------------------------------------


def _signed_gbp(amount: Decimal) -> str:
    return f"+£{amount}" if amount >= 0 else f"-£{-amount}"


def _benchmark_verdict(report: WeeklyReport) -> str:
    gap = report.vs_benchmark_gbp
    if gap < 0:
        return (
            f"The bot is £{-gap} behind buy-and-hold — doing nothing would have "
            f"done better so far. Part of that gap is the "
            f"£{report.cumulative_costs.paid_gbp} of costs the benchmark "
            f"doesn't pay; the rest is timing."
        )
    if gap > 0:
        return (
            f"The bot is £{gap} ahead of buy-and-hold — and since the benchmark "
            f"pays no costs, that lead is genuine."
        )
    return "The bot is exactly level with buy-and-hold."


def render_weekly_report(report: WeeklyReport) -> str:
    """One plain-English section for the household finance review. Same report
    in, same string out — there is deliberately nothing generative here."""
    lines: list[str] = []
    a = lines.append

    a(
        f"## Ledger — paper trading, week {report.week_start.isoformat()} "
        f"to {report.week_ending.isoformat()}"
    )
    a("")
    a(
        f"Portfolio value: £{report.portfolio_value_gbp} "
        f"(cash £{report.cash_gbp} + holdings "
        f"£{gbp(report.portfolio_value_gbp - report.cash_gbp)})."
    )
    a(
        f"P&L since day one: {_signed_gbp(report.pnl_gbp)} ({report.pnl_pct:+}%) "
        f"on the £{report.starting_capital_gbp} start, net of all costs."
    )
    a("")
    a(
        f"vs. just holding the same {report.allocation_label} from day one, "
        f"untouched (before costs): that buy-and-hold pot would be worth "
        f"£{report.benchmark_value_gbp}."
    )
    a(_benchmark_verdict(report))
    a("")
    if report.holdings:
        a("Holdings:")
        for h in report.holdings:
            a(
                f"  - {h.asset} ({h.sleeve}): {h.quantity} units, worth "
                f"£{h.market_value_gbp} (cost £{h.book_cost_gbp} all-in)."
            )
    else:
        a("Holdings: none yet — the pot is all cash.")
    a("")
    wc, cc = report.week_costs, report.cumulative_costs
    a(
        f"Costs this week: £{wc.paid_gbp} paid (venue fees £{wc.fee_gbp} + "
        f"FX £{wc.fx_gbp}), plus £{wc.spread_gbp} lost to spread inside "
        f"execution prices."
    )
    a(
        f"Costs since day one: £{cc.paid_gbp} paid (venue fees £{cc.fee_gbp} + "
        f"FX £{cc.fx_gbp}), plus £{cc.spread_gbp} spread."
    )
    a("")
    if report.trades:
        n = len(report.trades)
        a(f"Activity this week: {n} trade{'' if n == 1 else 's'}.")
        for t in report.trades:
            if t.side == "buy":
                money_part = (
                    f"£{t.gross_gbp} at market, £{-t.cash_delta_gbp} cash out "
                    f"after costs"
                )
                verb = "bought"
            else:
                money_part = (
                    f"£{t.gross_gbp} at market, £{t.cash_delta_gbp} banked "
                    f"after costs"
                )
                verb = "sold"
            a(f"  - {t.run_date}: {verb} {t.quantity} {t.asset} — {money_part}.")
            a(f"    why: {t.rationale or 'no rationale recorded'}")
    else:
        a("Activity this week: no trades.")
    a("")
    r = report.runs
    a(
        f"Reliability: {r.total} run{'' if r.total == 1 else 's'} this week — "
        f"{r.success} success / {r.no_action} no-action / {r.failure} failure."
    )
    for failure in r.failures:
        a(f"  - failed {failure}")
    a("")
    if report.drift_flags:
        a("Allocation drift:")
        for flag in report.drift_flags:
            a(f"  - {flag}")
    else:
        a("Allocation drift: no flags.")
    a("")
    a(
        "Ledger is a discipline and learning tool. The numbers above are a "
        "record of what happened, not a forecast — the benchmark line is the "
        "only scoreboard that matters."
    )
    return "\n".join(lines)


# -- live entry point ----------------------------------------------------------------


def _fetch_prices_gbp(config: Config) -> dict[str, Decimal]:
    """Current GBP price per configured asset: BTC straight from CoinGecko in
    GBP; stocks from Alpaca in USD, converted at the ECB reference rate."""
    fx_rate: Decimal | None = None
    prices: dict[str, Decimal] = {}
    for asset in config.assets.values():
        if asset.sleeve == "crypto":
            series = coingecko.fetch_daily_closes(asset.symbol, asset.coingecko_id)
        else:
            series = alpaca.fetch_daily_closes(asset.symbol)
        close = series.latest.close
        if series.currency == "GBP":
            prices[asset.symbol] = close
        elif series.currency == "USD":
            if fx_rate is None:
                fx_rate = fx.fetch_gbp_usd()
            prices[asset.symbol] = close / fx_rate
        else:
            raise ReportError(
                f"{asset.symbol}: no GBP conversion for prices quoted in "
                f"{series.currency}"
            )
    return prices


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        config = load_config(root / "config.toml")
        led = PaperLedger(root / config.runtime.db_path)
        try:
            prices_gbp = _fetch_prices_gbp(config)
            report = build_weekly_report(led, config, prices_gbp, dt.date.today())
        finally:
            led.close()
    except Exception as e:
        print(f"REPORT FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(render_weekly_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
