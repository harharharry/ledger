# NOTES.md — Build decisions

Decision log so future sessions don't re-litigate. Newest entries at the bottom of each
section. Spec is `trading-assistant-spec.md` v1.1; standing rules in `CLAUDE.md`.

## 2026-07-03 — Milestone 1: paper ledger + fill engine

### Fee schedules (verified via web search 2026-07-03)

- **Kraken Pro** base tier (<$10k 30-day volume): **0.25% maker / 0.40% taker**. Kraken's
  July 2026 change (tiering by Assets-on-Platform as well as volume) doesn't move a £500 pot
  off the base tier. Source: kraken.com/features/fee-schedule.
- **Coinbase Advanced** base tier: **0.40% maker / 0.60% taker**. Kept in config as the
  alternative crypto venue; Kraken is the default (cheaper, has BTC/GBP so no FX drag).
- **Alpaca** stocks/ETFs: **$0 commission**; SEC section 31 fee on sells ($27.80 per $1M of
  proceeds, config as 0.00278%) — rounds to £0.00 at pot-sized trades but modelled anyway.
  FINRA TAF ignored (per-share, negligible at this size).
- **Default liquidity assumption: taker.** A daily-cadence bot placing marketable orders pays
  taker; using maker rates would flatter the paper results. `simulate_fill` accepts
  `liquidity="maker"` for later experimentation but taker is the default everywhere.
- Spread estimates (full bid-ask, config `spread_pct`): Kraken BTC/GBP 0.10%, Coinbase 0.15%,
  Alpaca liquid ETFs 0.05%. These are estimates, revisit if milestone 2's live data shows
  they're off.
- FX one-way conversion cost: 0.50% (retail Wise/bank margin estimate). Applied on every
  trade on a non-GBP venue, both directions. Revisit at Phase 2 when the real rail is known.

### Conventions

- **All money is `Decimal`, stored as TEXT in SQLite.** GBP quantized to pence (banker's
  rounding), quantities/prices to 8dp. Floats never touch the ledger.
- **Percent units:** `config.toml` uses human-readable percents (`0.40` = 0.40%). The loader
  converts to fractions once; code fields are named `*_rate` / `*_frac` and are fractions.
- **Fill semantics:** buys are sized in GBP notional with fees/FX charged on top (total cash
  out > notional); sells are sized in quantity with fees/FX out of proceeds. Half the
  configured spread is applied to the execution price per side; the implied cost is also
  reported explicitly as `spread_cost_gbp`. `fx_rate` = quote-currency units per 1 GBP;
  GBP-quoted venues must pass exactly 1 (validated).
- **Idempotency:** every trade requires a caller-supplied `trade_key` with a UNIQUE
  constraint; replaying a day raises `DuplicateTradeError` and changes nothing. Suggested key
  shape: `YYYY-MM-DD:sleeve:asset:side`.
- **Cash is an append-only `cash_events` table** summed on read — auditable, and plenty fast
  at daily cadence.
- **Position book cost is all-in** (venue fee + FX included) and reduced proportionally on
  sells — average-cost basis, consistent with HMRC section 104 pooling. CGT CSV export
  (`export_cgt_csv`) reports fees = venue fee + FX cost as allowable costs.
- **Benchmark snapshots** are idempotent per (date, phase, asset); re-snapshotting the same
  key with a *different* price raises — history is never silently rewritten.
- **Kill switch is a file** (`KILL_SWITCH` at repo root, path in config): Harry can engage it
  with `touch` even if Python is broken. Helper in `ledger/kill_switch.py`; orchestrator
  (milestone 5) must check it first, every run.
- **`runs` table** already in the schema (run_date UNIQUE, outcome success/no-action/failure)
  so milestone 5 doesn't need a migration.

### Environment

- Python 3.14 via Homebrew; project targets ≥3.12, stdlib only at runtime so far (sqlite3,
  tomllib, decimal). Dev deps: pytest, in `.venv/`. Run tests with
  `.venv/bin/python -m pytest`.

### Numbers worth remembering

- Flat-price round trip on Kraken at £100: **~£0.90 lost** (0.4% + 0.4% taker + 0.1% spread).
  Locked in as a test (`test_round_trip_loses_money_at_flat_price`).
- A £25 trade on Alpaca loses >0.5% one-way before the sell leg — the £50 floor is justified.
