# CLAUDE.md — Ledger Project

Standing instructions for every Claude Code session in this repo. Read this fully before doing
anything. The full design is in `trading-assistant-spec.md` (v1.1); the dashboard reference is
`dashboard-mockup.html`. If this file and the spec ever conflict, this file wins.

## What this project is

A personal, small-scale (£500) investment assistant for Harry: paper-trades autonomously first
(Phase 1), then proposes real trades for human approval (Phase 2). It is a discipline and
learning tool, not a market-prediction system. Never claim or imply otherwise in code comments,
UI copy, reports, or notifications.

## Non-negotiables (never change these, never "improve" around them)

1. **No live order is ever placed without explicit human approval.** Automation ends at
   "propose." There is no config flag, env var, or code path that bypasses this. If you find
   yourself building one, stop.
2. **Credentials:** read from environment variables only. Never hardcode, log, echo, or commit
   keys. Fail loudly if missing. All exchange/broker keys must be documented as trade-only /
   withdrawal-disabled in the setup README.
3. **Fees first.** The paper fill engine charges realistic venue fees, spread, and GBP↔USD FX
   cost on every simulated trade. No strategy code gets written or evaluated against a
   fee-free ledger. This is build milestone 1 for a reason.
4. **Deterministic runtime.** The deployed daily bot is plain Python — no LLM calls in the
   trading decision path. LLM usage at runtime is limited to weekly report / rationale text
   generation on a cheap model, with costs logged.
5. **Kill switch is checked before any action**, every run.
6. **Every run logs an outcome** (success / no-action / failure), runs are idempotent
   (re-running a day never double-trades), and failures notify rather than fail silently.

## Architecture summary

- **Runtime:** Python 3.12+, SQLite ledger, one daily cron-style run. GBP is the base currency
  for all accounting and reporting.
- **Build-time subagents** (define in `.claude/agents/`, keep scoped tool access):
  - `crypto-strategist` — crypto sleeve strategy logic and its tests
  - `stocks-strategist` — stocks/ETF sleeve strategy logic and its tests
  - `risk-manager` — position/allocation limit enforcement; veto layer; can resize/block,
    never originate
  - `reporting` — weekly review generation, benchmark comparison
  These are how the system is *built and maintained*. The deployed bot compiles their output
  into deterministic modules (see non-negotiable 4).
- **Strategy v1 (keep it boring):** trend filter (price vs 50-day MA) gating DCA-style
  scheduled accumulation, momentum (RSI) tilting size. No leverage, no shorting, no
  derivatives. Minimum trade size floor (default £50) — fee drag dominates below it.
- **Config over code:** allocation split (default 60/40 crypto/stocks), per-trade cap (20% of
  sleeve), drift threshold (±10pts), proposal cadence caps (1/day, 5/week), trade floor — all
  live in a config file, never hardcoded.
- **Benchmark:** snapshot starting prices for BTC + chosen tech/AI ETF into the ledger on day
  one. Every report shows performance vs. untouched buy-and-hold of the same allocation.
- **Tax:** trade log schema captures date, asset, quantity, GBP value, fees — sufficient to
  export a CGT-ready CSV at any time.

## Build order (from spec §11 — do not reorder)

1. Paper ledger + realistic fill engine (fees/spread/FX) + tests
2. Data ingestion: crypto (CoinGecko or CCXT) + stocks (Alpaca market data)
3. Strategist modules (baseline logic above) + tests
4. Risk manager module + tests
5. Orchestrator + scheduler wiring + run logging
6. Reporting + weekly summary
7. Dashboard (React, match `dashboard-mockup.html` — Apple-style, light, SF system font,
   tabular numerals; keep the pending-proposal card as the only loud element)
8. Phase 1 observation run scaffolding (4–6 weeks minimum before Phase 2 is considered)
9. Phase 2: broker/exchange connections, notification (email link → dashboard approval), the
   approve/decline flow

## Working style

- Test as you go; every module lands with tests. Prefer boring, legible code over clever code —
  Harry should be able to read a strategy file and understand it.
- Don't add features, strategies, or abstractions beyond the spec without asking. In
  particular: no new indicators, no ML, no additional assets in v1.
- When something in the spec turns out to be wrong or unbuildable, say so and propose the fix —
  don't silently work around it.
- Verify externally-dependent assumptions when you reach them (Alpaca crypto availability for
  UK accounts at Phase 2; current fee schedules when building the fill engine).
- Keep a `NOTES.md` of decisions made during the build so future sessions don't re-litigate
  them.
