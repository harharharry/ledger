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

## 2026-07-03 — Milestone 2: data ingestion

- **Crypto prices: CoinGecko** public API (chose it over CCXT: no dependency, no exchange
  account, GBP quotes directly). Optional demo key via env `COINGECKO_API_KEY` if rate
  limits ever bite; not needed at one run/day. Parser normalises any granularity to one
  close per UTC day (last point wins). Verified live 2026-07-03.
- **Stocks prices: Alpaca** Market Data v2, free IEX feed, split-adjusted daily bars.
  Keys from env `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`, fail-loudly. **Not yet verified
  live — needs Harry's Alpaca account keys (create trade-only, withdrawals disabled).**
- **FX: Frankfurter** (`api.frankfurter.dev`, ECB reference rates, free, no key) for GBPUSD.
  One rate per business day matches the daily cadence. Note: the spot *rate* comes from
  here; the conversion *cost* stays a config estimate. Cloudflare 403s the default Python
  urllib User-Agent, so `data/http.py` always sends its own. Verified live 2026-07-03
  (GBPUSD 1.3306).
- **HTTP:** stdlib urllib only (still zero runtime dependencies). JSON numbers parse
  straight to Decimal. Retries with backoff on 429/5xx/network errors; other statuses fail
  loudly. Error messages never include headers (keys live there).
- **History window: 100 days** — enough for the 50-day MA plus RSI(14) with slack.
- **Assets moved into config** (`[assets.crypto.BTC]`, `[assets.stocks.QQQ]`). QQQ is a
  **placeholder** — Harry must confirm the tech/AI ETF before Phase 1 day one, because the
  benchmark snapshot locks it in.
- **Tests:** parsers are unit-tested offline with fixture payloads; live API tests exist in
  `tests/test_data_integration.py` behind `RUN_INTEGRATION=1`. Manual check:
  `.venv/bin/python -m ledger.data.smoke`.

## 2026-07-03 — Milestone 3: strategist modules

- Built via the crypto-strategist and stocks-strategist subagents (general-purpose agents
  running those role definitions — .claude/agents/*.md files created mid-session aren't
  registered as first-class agent types until the next session).
- **Shared core in `ledger/strategies/signals.py`** (SMA, Wilder RSI, `propose_accumulation`);
  `crypto.py` / `stocks.py` are thin per-sleeve wrappers. Both sleeves run identical v1 logic
  by design.
- **Proposal shape** lives in `ledger/proposals.py`: (sleeve, asset, venue, side, notional_gbp,
  rationale). Strategies only emit 'buy' in v1 — selling only happens via Harry acting on a
  drift flag, never from strategy code.
- **Strategy tunables** in config `[strategy]`: ma_days=50, rsi_period=14, base_trade_gbp=60,
  RSI bounds 30/70, tilts 1.5x/0.5x.
- **Sizing order:** base → RSI tilt → cap at available cash → fee floor check (below £50 →
  no proposal). Thin history (< ma_days+1 closes) raises StrategyError — fail loudly, never
  guess.
- **⚠ Finding (crypto-strategist agent, verified analytically + by search): `rsi_oversold=30`
  is unreachable dead config under the trend filter.** Wilder RSI(14) can't get below ~36
  while price is still above its 50-day MA, so the 1.5x dip tilt never fires; the strategy is
  effectively plain gated DCA with only the overbought reduction. If the dip tilt should be
  live, raise `rsi_oversold` to ~40–45 in config. **Resolved 2026-07-03: Harry raised
  `rsi_oversold` to 40.**
- Related: at defaults, the overbought tilt (£60 × 0.5 = £30) always lands below the £50
  floor, so overbought days propose nothing at all. Treated as intentional (fee-drag rule).
- Multi-asset sleeves are explicitly unimplemented: a second asset in either sleeve raises
  StrategyError rather than silently picking one.

## 2026-07-03 — Milestone 4: risk manager

- **Sleeve value for the per-trade cap = holdings at market + the sleeve's target share of
  cash.** Without allocating cash, day one gives every sleeve £0 and the 20% cap forbids all
  trading forever. Day one: crypto sleeve £300 → cap £60 (deliberately equal to
  base_trade_gbp).
- **Drift is measured on invested value only** (holdings shares vs target split, cash
  excluded); no flags while holdings are zero. Early flags (first buy → 100% one sleeve) are
  accepted noise — it's a flag in a report, never a trade. Convention: flagged only when
  strictly beyond the ±10pt threshold.
- **Check order:** kill switch → cadence caps → per-trade cap (resize down) → fee floor
  (block). Drift flags attach to every decision, including blocks. Resizes append an audit
  note to the proposal rationale.
- **Boundary conventions (risk-manager agent):** cap resize fires strictly above the
  pence-quantized cap; the floor blocks strictly below £50 (exactly £50 passes); at/under-cap
  proposals pass through as the identical object.
- **For milestone 5:** `evaluate` trusts the caller for proposals_today / proposals_this_week
  — the orchestrator must source them from the runs/proposals log, and should pass an
  absolute kill_switch_path (the config value is repo-relative).
- **Known edge for milestone 5 (orchestrator):** strategies cap size at available cash, but
  fill fees are charged on top, so a proposal at exactly available-cash will fail at
  record_fill with InsufficientCashError. The orchestrator should pass strategies a slightly
  haircut cash figure (or catch and log the failure). Decide there, not in risk.py.

## 2026-07-03 — Milestone 5: orchestrator

- `ledger/orchestrator.py`, built in the main session (the orchestrator wires the agents'
  modules together; no subagent owns it). Entry point: `python -m ledger.orchestrator`;
  suggested cron: `30 7 * * *` from the repo root.
- **Run flow:** idempotency guard (success/no-action days never re-run; crashed/failed days
  may retry) → run row → kill switch → fetch prices+FX → day-one benchmark snapshot →
  sleeves in most-under-allocated-first order → strategy → risk veto → paper fill. Every run
  finishes success / no-action / failure; failures record then re-raise (nonzero exit for
  cron alerting — actual email/notification wiring lands with Phase 2 infra, noted §14.3).
- **Phase 1 cadence source:** executed paper trades count as "proposals" for the 1/day,
  5/week caps (weeks are ISO, Mon–Sun). Phase 2 will count proposals sent instead.
- **Fee headroom:** strategies receive `affordable_notional()` — sleeve cash ÷ (1 + taker
  fee + half spread + FX cost), minus a safety penny — so a fill can never bounce off
  InsufficientCashError. Closes the milestone-4 known edge.
- **Sleeve cash = target share of total cash** (0.6/0.4), consistent with risk.py's sleeve
  valuation.
- **⚠ Structural conflict found (test-documented in test_orchestrator.py): at a £500 pot the
  20% per-trade cap and the £50 fee floor deadlock the stocks sleeve.** Stocks sleeve value
  ≈ £200 at best (40% of pot) → cap £40 < £50 floor → every stocks proposal is
  resized-then-blocked, forever. The sleeve needs ≥ £250 of value for the cap to clear the
  floor. Crypto clears it only barely (£300 → £60 cap). Options: (a) floor wins when they
  conflict (effective cap = max(20% of sleeve, £50)); (b) raise per_trade_cap_pct; (c) leave
  strict — stocks sleeve stays dormant and drift flags accumulate. **Decision pending —
  Harry's call.**

### Numbers worth remembering

- Flat-price round trip on Kraken at £100: **~£0.90 lost** (0.4% + 0.4% taker + 0.1% spread).
  Locked in as a test (`test_round_trip_loses_money_at_flat_price`).
- A £25 trade on Alpaca loses >0.5% one-way before the sell leg — the £50 floor is justified.
