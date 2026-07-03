# Ledger

A personal, small-scale (£500) investment assistant: paper-trades autonomously first
(Phase 1), then proposes real trades for human approval (Phase 2). It is a **discipline and
learning tool, not a market-prediction system**.

Design: `trading-assistant-spec.md` (v1.1). Standing build rules: `CLAUDE.md`.
Decision log: `NOTES.md`.

## Status

- [x] Milestone 1 — paper ledger + fee-realistic fill engine (fees, spread, FX) + tests
- [x] Milestone 2 — data ingestion (CoinGecko crypto + Alpaca stocks + ECB FX)
- [ ] Milestone 3 — strategist modules
- [ ] Milestone 4 — risk manager
- [ ] Milestone 5 — orchestrator + scheduler + run logging
- [ ] Milestone 6 — reporting + weekly summary
- [ ] Milestone 7 — dashboard
- [ ] Milestone 8 — Phase 1 observation run (4–6 weeks minimum)
- [ ] Milestone 9 — Phase 2 (propose-and-approve; nothing executes without human approval)

## Layout

- `config.toml` — every tunable (allocation, caps, floors, venue fees). Config over code.
- `ledger/` — the Python package: `money` (Decimal rules), `config`, `fill_engine`,
  `paper_ledger`, `kill_switch`
- `ledger/data/` — price ingestion: `coingecko` (crypto, GBP), `alpaca` (stocks, USD,
  needs `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` env vars), `fx` (GBPUSD via ECB).
  Live check: `.venv/bin/python -m ledger.data.smoke`
- `tests/` — pytest suite
- `.claude/agents/` — build-time subagents (crypto-strategist, stocks-strategist,
  risk-manager, reporting). The *deployed* bot is deterministic Python; no LLM calls in the
  trading decision path.

## Running tests

```sh
python3 -m venv .venv && .venv/bin/pip install pytest   # once
.venv/bin/python -m pytest
```

## Kill switch

`touch KILL_SWITCH` in the repo root pauses all bot activity; delete the file to resume.
Checked before any action, every run.

## Credentials (Phase 2, not needed yet)

API keys are read from environment variables only — never hardcoded, logged, or committed.
All exchange/broker keys must be created **trade-only with withdrawals disabled**, and
IP-restricted where supported. The bot fails loudly if a required key is missing.
