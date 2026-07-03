"""The paper ledger: SQLite-backed cash, positions, trades, and benchmark snapshots.

Design notes (see NOTES.md):
  * GBP is the base currency for everything. All money is Decimal, stored as
    TEXT so nothing is ever a float.
  * Cash is the sum of an append-only cash_events table — auditable by eye.
  * Every trade needs a caller-supplied ``trade_key``; a UNIQUE constraint
    makes re-running a day incapable of double-trading (non-negotiable 6).
  * Position book cost is all-in (fees + FX included), reduced proportionally
    on sells — average-cost basis, which matches HMRC section 104 pooling.
  * The trades table captures date, asset, quantity, GBP value, and fees:
    sufficient for a CGT-ready CSV at any time (spec §14.5).
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .fill_engine import Fill
from .money import gbp, to_decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    trade_key TEXT NOT NULL UNIQUE,
    run_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    sleeve TEXT NOT NULL CHECK (sleeve IN ('crypto', 'stocks')),
    venue TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity TEXT NOT NULL,
    exec_price TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    fx_rate TEXT NOT NULL,
    gross_gbp TEXT NOT NULL,
    fee_gbp TEXT NOT NULL,
    spread_cost_gbp TEXT NOT NULL,
    fx_cost_gbp TEXT NOT NULL,
    cash_delta_gbp TEXT NOT NULL,
    rationale TEXT
);

CREATE TABLE IF NOT EXISTS cash_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('opening_balance', 'trade')),
    amount_gbp TEXT NOT NULL,
    trade_id INTEGER REFERENCES trades(id),
    note TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    asset TEXT PRIMARY KEY,
    sleeve TEXT NOT NULL,
    quantity TEXT NOT NULL,
    book_cost_gbp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_snapshots (
    id INTEGER PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    phase TEXT NOT NULL,
    asset TEXT NOT NULL,
    price_gbp TEXT NOT NULL,
    UNIQUE (snapshot_date, phase, asset)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    run_date TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT CHECK (outcome IN ('success', 'no-action', 'failure')),
    detail TEXT
);
"""


class LedgerError(Exception):
    pass


class InsufficientCashError(LedgerError):
    pass


class InsufficientPositionError(LedgerError):
    pass


class DuplicateTradeError(LedgerError):
    pass


@dataclass(frozen=True)
class Position:
    asset: str
    sleeve: str
    quantity: Decimal
    book_cost_gbp: Decimal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperLedger:
    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    # -- lifecycle ---------------------------------------------------------

    def open(self, starting_capital_gbp: Decimal, ts: str | None = None) -> None:
        """Record the opening balance. Idempotent; re-opening with a different
        amount is an error, not a silent overwrite."""
        existing = self._meta("opening_balance_gbp")
        if existing is not None:
            if to_decimal(existing) != gbp(starting_capital_gbp):
                raise LedgerError(
                    f"ledger already opened with £{existing}; "
                    f"refusing to re-open with £{gbp(starting_capital_gbp)}"
                )
            return
        amount = gbp(starting_capital_gbp)
        if amount <= 0:
            raise LedgerError("opening balance must be positive")
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('opening_balance_gbp', ?)",
                (str(amount),),
            )
            self._conn.execute(
                "INSERT INTO cash_events (ts, kind, amount_gbp, note) "
                "VALUES (?, 'opening_balance', ?, 'starting capital')",
                (ts or _now_iso(), str(amount)),
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PaperLedger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- balances & positions ----------------------------------------------

    def cash_balance_gbp(self) -> Decimal:
        rows = self._conn.execute("SELECT amount_gbp FROM cash_events").fetchall()
        return gbp(sum((to_decimal(r["amount_gbp"]) for r in rows), Decimal("0")))

    def position(self, asset: str) -> Position | None:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE asset = ?", (asset,)
        ).fetchone()
        if row is None:
            return None
        return Position(
            asset=row["asset"],
            sleeve=row["sleeve"],
            quantity=to_decimal(row["quantity"]),
            book_cost_gbp=to_decimal(row["book_cost_gbp"]),
        )

    def positions(self) -> list[Position]:
        rows = self._conn.execute("SELECT asset FROM positions ORDER BY asset").fetchall()
        return [self.position(r["asset"]) for r in rows]

    # -- trades --------------------------------------------------------------

    def record_fill(
        self,
        fill: Fill,
        trade_key: str,
        ts: str | None = None,
        rationale: str | None = None,
        run_date=None,
    ) -> int:
        """Apply a simulated fill to the ledger atomically. Raises
        DuplicateTradeError if trade_key was already recorded — this is the
        idempotency guard, so callers treat it as 'already done', not failure.

        run_date is the logical run day the cadence caps count against; the
        orchestrator passes its run date explicitly (which may differ from the
        wall-clock ts in tests and backfills). Defaults to the date of ts."""
        dupe = self._conn.execute(
            "SELECT id FROM trades WHERE trade_key = ?", (trade_key,)
        ).fetchone()
        if dupe is not None:
            raise DuplicateTradeError(f"trade_key {trade_key!r} already recorded")

        pos = self.position(fill.asset)
        if fill.side == "buy":
            cost = -fill.cash_delta_gbp
            balance = self.cash_balance_gbp()
            if cost > balance:
                raise InsufficientCashError(
                    f"buy needs £{cost} but cash is £{balance}"
                )
            if pos is not None and pos.sleeve != fill.sleeve:
                raise LedgerError(
                    f"{fill.asset} already held in sleeve {pos.sleeve!r}"
                )
            new_qty = (pos.quantity if pos else Decimal("0")) + fill.quantity
            new_cost = (pos.book_cost_gbp if pos else Decimal("0")) + cost
        else:
            if pos is None or pos.quantity < fill.quantity:
                held = pos.quantity if pos else Decimal("0")
                raise InsufficientPositionError(
                    f"sell of {fill.quantity} {fill.asset} but holding {held}"
                )
            new_qty = pos.quantity - fill.quantity
            if new_qty == 0:
                new_cost = Decimal("0")
            else:
                sold_frac = fill.quantity / pos.quantity
                new_cost = pos.book_cost_gbp - gbp(pos.book_cost_gbp * sold_frac)

        ts = ts or _now_iso()
        run_date = str(run_date) if run_date is not None else ts[:10]
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO trades (trade_key, run_date, ts, sleeve, venue, asset, "
                "side, quantity, exec_price, quote_currency, fx_rate, gross_gbp, "
                "fee_gbp, spread_cost_gbp, fx_cost_gbp, cash_delta_gbp, rationale) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_key, run_date, ts, fill.sleeve, fill.venue, fill.asset, fill.side,
                    str(fill.quantity), str(fill.exec_price), fill.quote_currency,
                    str(fill.fx_rate), str(fill.gross_gbp), str(fill.fee_gbp),
                    str(fill.spread_cost_gbp), str(fill.fx_cost_gbp),
                    str(fill.cash_delta_gbp), rationale,
                ),
            )
            trade_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO cash_events (ts, kind, amount_gbp, trade_id) "
                "VALUES (?, 'trade', ?, ?)",
                (ts, str(fill.cash_delta_gbp), trade_id),
            )
            self._conn.execute(
                "INSERT INTO positions (asset, sleeve, quantity, book_cost_gbp) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (asset) DO UPDATE SET quantity = ?, book_cost_gbp = ?",
                (
                    fill.asset, fill.sleeve, str(new_qty), str(gbp(new_cost)),
                    str(new_qty), str(gbp(new_cost)),
                ),
            )
        return trade_id

    def trades(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM trades ORDER BY id").fetchall()

    def trades_between(self, start, end) -> list[sqlite3.Row]:
        """Trades with a logical run day in [start, end], inclusive."""
        return self._conn.execute(
            "SELECT * FROM trades WHERE run_date BETWEEN ? AND ? ORDER BY id",
            (str(start), str(end)),
        ).fetchall()

    def runs_between(self, start, end) -> list[sqlite3.Row]:
        """Run log rows with run_date in [start, end], inclusive."""
        return self._conn.execute(
            "SELECT * FROM runs WHERE run_date BETWEEN ? AND ? ORDER BY run_date",
            (str(start), str(end)),
        ).fetchall()

    # -- benchmark ------------------------------------------------------------

    def snapshot_benchmark(
        self, phase: str, prices_gbp: dict[str, Decimal], snapshot_date: str | None = None
    ) -> None:
        """Record day-one benchmark prices. Idempotent for the same
        (date, phase, asset); a conflicting re-snapshot with a different price
        raises rather than silently rewriting history."""
        snapshot_date = snapshot_date or datetime.now(timezone.utc).date().isoformat()
        with self._conn:
            for asset, p in prices_gbp.items():
                existing = self._conn.execute(
                    "SELECT price_gbp FROM benchmark_snapshots "
                    "WHERE snapshot_date = ? AND phase = ? AND asset = ?",
                    (snapshot_date, phase, asset),
                ).fetchone()
                if existing is not None:
                    if to_decimal(existing["price_gbp"]) != gbp(p):
                        raise LedgerError(
                            f"benchmark for {asset} on {snapshot_date} already "
                            f"snapshotted at £{existing['price_gbp']}"
                        )
                    continue
                self._conn.execute(
                    "INSERT INTO benchmark_snapshots (snapshot_date, phase, asset, price_gbp) "
                    "VALUES (?, ?, ?, ?)",
                    (snapshot_date, phase, asset, str(gbp(p))),
                )

    def benchmark_snapshots(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM benchmark_snapshots ORDER BY snapshot_date, asset"
        ).fetchall()

    # -- run log ----------------------------------------------------------------

    def run_outcome(self, run_date) -> str | None:
        """Outcome of the day's run, or None if it hasn't run (or crashed
        mid-run without finishing)."""
        row = self._conn.execute(
            "SELECT outcome FROM runs WHERE run_date = ?", (str(run_date),)
        ).fetchone()
        return row["outcome"] if row else None

    def record_run_start(self, run_date, ts: str | None = None) -> None:
        """Start (or restart) the day's run row. A re-start clears the previous
        outcome — callers must check run_outcome first; only crashed (NULL) or
        'failure' runs should be retried."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_date, started_at) VALUES (?, ?) "
                "ON CONFLICT (run_date) DO UPDATE SET "
                "started_at = excluded.started_at, finished_at = NULL, "
                "outcome = NULL, detail = NULL",
                (str(run_date), ts or _now_iso()),
            )

    def finish_run(self, run_date, outcome: str, detail: str | None = None,
                   ts: str | None = None) -> None:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET finished_at = ?, outcome = ?, detail = ? "
                "WHERE run_date = ?",
                (ts or _now_iso(), outcome, detail, str(run_date)),
            )
        if cur.rowcount == 0:
            raise LedgerError(f"no run started for {run_date}")

    def trades_count_on(self, day) -> int:
        """Trades counted against a logical run day (cadence caps key off
        run_date, not wall-clock ts)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE run_date = ?", (str(day),)
        ).fetchone()
        return row["n"]

    def trades_count_between(self, start, end) -> int:
        """Trades with a logical run day in [start, end], inclusive."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE run_date BETWEEN ? AND ?",
            (str(start), str(end)),
        ).fetchone()
        return row["n"]

    # -- tax export -----------------------------------------------------------

    def export_cgt_csv(self, path: str | Path) -> int:
        """Write every trade as a CGT-ready CSV row; returns the row count.
        Fees column is total friction (venue fee + FX cost) — allowable costs."""
        rows = self.trades()
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["date", "asset", "side", "quantity", "gross_value_gbp", "fees_gbp"]
            )
            for r in rows:
                fees = gbp(to_decimal(r["fee_gbp"]) + to_decimal(r["fx_cost_gbp"]))
                writer.writerow(
                    [r["ts"], r["asset"], r["side"], r["quantity"], r["gross_gbp"], str(fees)]
                )
        return len(rows)

    # -- internals -------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
