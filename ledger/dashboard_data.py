"""Dashboard data exporter — everything the React dashboard renders, as JSON.

Pure and deterministic: the builder takes the ledger, config, price series and
FX rate, and returns a JSON-serializable dict. Money travels as strings (the
frontend never does arithmetic); percentages as rounded floats (display only).

Unlike the weekly report (which refuses to render without a benchmark), the
dashboard is a live view and degrades gracefully: before day one it shows the
un-opened state rather than failing.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from .config import Config
from .data.types import PriceSeries
from .money import gbp, to_decimal
from .paper_ledger import PaperLedger
from .strategies import signals

MA_DAYS_FOR_CHART = 50


def _m(value: Decimal | None) -> str | None:
    return None if value is None else str(gbp(value))


def _pct(fraction: Decimal, digits: int = 2) -> float:
    return round(float(fraction * 100), digits)


def _gbp_closes(series: PriceSeries, fx_rate: Decimal) -> list[tuple[dt.date, Decimal]]:
    """Closes converted to GBP. USD series convert at today's rate for every
    point — a documented approximation (historical FX would need another API;
    at sparkline scale the error is invisible)."""
    if series.currency == "GBP":
        return [(c.date, c.close) for c in series.closes]
    return [(c.date, c.close / fx_rate) for c in series.closes]


def _sparkline(led: PaperLedger, gbp_closes_by_symbol: dict[str, list]) -> list[dict]:
    """Reconstruct daily portfolio value from trade history: for each day since
    opening, cash after that day's trades plus holdings at that day's closes
    (carry-forward over market holidays/weekends)."""
    opened = led.opened_on()
    if opened is None:
        return []
    opened_date = dt.date.fromisoformat(opened)
    opening_cash = led.opening_balance_gbp()

    spine: set[dt.date] = set()
    for closes in gbp_closes_by_symbol.values():
        spine.update(d for d, _ in closes if d >= opened_date)
    if not spine:
        return []

    trades = [
        (dt.date.fromisoformat(t["run_date"]), t["asset"], t["side"],
         to_decimal(t["quantity"]), to_decimal(t["cash_delta_gbp"]))
        for t in led.trades()
    ]

    points = []
    for day in sorted(spine):
        cash = opening_cash
        qty: dict[str, Decimal] = {}
        for t_day, asset, side, quantity, cash_delta in trades:
            if t_day > day:
                continue
            cash += cash_delta
            qty[asset] = qty.get(asset, Decimal("0")) + (
                quantity if side == "buy" else -quantity
            )
        value = cash
        for asset, q in qty.items():
            if q == 0:
                continue
            close = _close_on_or_before(gbp_closes_by_symbol[asset], day)
            if close is None:
                continue
            value += q * close
        points.append({"date": day.isoformat(), "value_gbp": _m(value)})
    return points


def _close_on_or_before(closes: list[tuple[dt.date, Decimal]], day: dt.date):
    best = None
    for d, c in closes:
        if d <= day:
            best = c
        else:
            break
    return best


def _asset_block(series: PriceSeries, gbp_closes, led: PaperLedger, config: Config,
                 asset_cfg) -> dict:
    values = series.values()
    latest = series.latest.close
    ma_days = config.strategy.ma_days

    chart = []
    for i, close in enumerate(series.closes):
        ma = (
            str(signals.sma(values[: i + 1], ma_days)) if i + 1 >= ma_days else None
        )
        chart.append({"date": close.date.isoformat(), "close": str(close.close),
                      "ma": ma})

    trend = None
    sessions_above = 0
    if len(values) >= ma_days:
        ma_now = signals.sma(values, ma_days)
        trend = "bullish" if latest >= ma_now else "bearish"
        for i in range(len(values) - 1, ma_days - 2, -1):
            if values[i] >= signals.sma(values[: i + 1], ma_days):
                sessions_above += 1
            else:
                break

    rsi_value = (
        round(float(signals.rsi(values, config.strategy.rsi_period)), 1)
        if len(values) > config.strategy.rsi_period
        else None
    )

    change_pct = None
    if len(values) >= 2 and values[-2] > 0:
        change_pct = _pct((latest - values[-2]) / values[-2])

    buys = [
        {"date": t["run_date"], "notional_gbp": t["gross_gbp"]}
        for t in led.trades()
        if t["asset"] == series.symbol and t["side"] == "buy"
    ]

    return {
        "symbol": series.symbol,
        "sleeve": asset_cfg.sleeve,
        "currency": series.currency,
        "latest_close": str(latest),
        "change_today_pct": change_pct,
        "trend": trend,
        "sessions_above_ma": sessions_above,
        "rsi": rsi_value,
        "rsi_oversold": float(config.strategy.rsi_oversold),
        "rsi_overbought": float(config.strategy.rsi_overbought),
        "chart": chart,
        "buys": buys,
    }


def build_dashboard_data(
    led: PaperLedger,
    config: Config,
    series_by_symbol: dict[str, PriceSeries],
    fx_rate: Decimal,
    kill_switch_engaged: bool = False,
    generated_at: dt.datetime | None = None,
) -> dict:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    cash = led.cash_balance_gbp()
    starting = config.portfolio.starting_capital_gbp

    gbp_closes_by_symbol = {
        sym: _gbp_closes(s, fx_rate) for sym, s in series_by_symbol.items()
    }
    prices_gbp = {sym: closes[-1][1] for sym, closes in gbp_closes_by_symbol.items()}

    holdings_by_sleeve = {"crypto": Decimal("0"), "stocks": Decimal("0")}
    for pos in led.positions():
        if pos.quantity > 0 and pos.asset in prices_gbp:
            holdings_by_sleeve[pos.sleeve] += pos.quantity * prices_gbp[pos.asset]
    invested = sum(holdings_by_sleeve.values())
    total = cash + invested

    opened = led.opened_on()
    pnl_gbp = gbp(total - starting) if opened else None
    pnl_pct = _pct((total - starting) / starting) if opened else None

    allocation = {
        "target": {
            "crypto": _pct(config.portfolio.allocation_crypto_frac),
            "stocks": _pct(config.portfolio.allocation_stocks_frac),
        },
        "actual": {
            sleeve: (_pct(value / invested) if invested else None)
            for sleeve, value in holdings_by_sleeve.items()
        },
        "invested_gbp": _m(invested),
        "cash_gbp": _m(cash),
    }

    benchmark = None
    snapshots = [r for r in led.benchmark_snapshots() if r["phase"] == "phase1"]
    if snapshots and opened:
        fracs = {
            "crypto": config.portfolio.allocation_crypto_frac,
            "stocks": config.portfolio.allocation_stocks_frac,
        }
        bench_value = Decimal("0")
        for row in snapshots:
            asset_cfg = config.assets.get(row["asset"])
            if asset_cfg is None or row["asset"] not in prices_gbp:
                continue
            units = (starting * fracs[asset_cfg.sleeve]) / to_decimal(row["price_gbp"])
            bench_value += units * prices_gbp[row["asset"]]
        benchmark = {
            "ledger_pct": pnl_pct,
            "benchmark_pct": _pct((bench_value - starting) / starting),
            "note": "before costs",
        }

    activity = []
    for t in led.trades():
        activity.append({
            "kind": "trade",
            "date": t["run_date"],
            "sleeve": t["sleeve"],
            "title": f"Bought {t['asset']}",
            "detail": f"£{t['gross_gbp']} + £{t['fee_gbp']} fees",
            "amount_gbp": str(gbp(-to_decimal(t["cash_delta_gbp"]))),
            "rationale": t["rationale"] or "",
        })
    far_past = dt.date(2000, 1, 1)
    for r in led.runs_between(far_past, generated_at.date()):
        if r["outcome"] in ("no-action", "failure"):
            activity.append({
                "kind": "run",
                "date": r["run_date"],
                "sleeve": None,
                "title": "Run failed" if r["outcome"] == "failure" else "No action",
                "detail": (r["detail"] or "")[:80],
                "amount_gbp": None,
                "rationale": r["detail"] or "",
            })
    activity.sort(key=lambda a: a["date"], reverse=True)

    runs = led.runs_between(far_past, generated_at.date())
    last_run = None
    if runs:
        last = runs[-1]
        last_run = {"date": last["run_date"], "outcome": last["outcome"],
                    "detail": last["detail"]}

    assets = {
        sym: _asset_block(series, gbp_closes_by_symbol[sym], led, config,
                          config.assets[sym])
        for sym, series in series_by_symbol.items()
        if sym in config.assets
    }

    return {
        "generated_at": generated_at.isoformat(),
        "mode": "paper",
        "opened": opened,
        "kill_switch_engaged": kill_switch_engaged,
        "hero": {
            "total_balance_gbp": _m(total),
            "pnl_gbp": _m(pnl_gbp),
            "pnl_pct": pnl_pct,
            "since": opened,
            "sparkline": _sparkline(led, gbp_closes_by_symbol),
        },
        "allocation": allocation,
        "benchmark": benchmark,
        "pending_proposal": None,  # Phase 1 is autonomous; Phase 2 fills this
        "activity": activity[:15],
        "last_run": last_run,
        "assets": assets,
    }


def write_dashboard_data(data: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(data, indent=1))
