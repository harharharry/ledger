# Project Spec: Hands-Off Investment Assistant (working title: "Ledger")

**Owner:** Harry
**Drafted by:** Claude (Sonnet 5), for build/QA by Claude Fable 5 in Claude Code
**Status:** v1.1 — reviewed and revised by Fable 5 (see §14 for review findings)
**Starting capital:** £500 (separate from ISA / long-term holdings)

---

## 1. Purpose & Honest Framing

This is a personal tool to help Harry manage a small, higher-risk pot of money with more
discipline and less emotion than manual trading — **not** a system that predicts or reliably
beats the market. No model, strategy, or piece of software can guarantee that. The value this
tool aims to deliver is:

- Systematic, rules-based decision-making instead of impulse trades
- Enforced risk management (position sizing, allocation limits)
- A low-effort review cadence (daily/weekly) instead of constant price-checking
- A genuine before/after test: does an automated approach actually beat just holding?

This document is a spec for a personal tool, not financial advice. Asset selection, allocation
targets, and risk tolerance are Harry's decisions — the software should surface information and
enforce rules he sets, not make unilateral judgment calls about what's "right."

**Ask for Fable 5:** Read this whole spec critically. Flag anything that's ambiguous,
underspecified, technically unrealistic, or where the risk controls have a gap. Don't just
implement — sanity-check the design first.

---

## 2. Phases

### Phase 1 — Paper Trading Sandbox (build first)
- Virtual ledger, starting balance £500 (or whatever's configured)
- Real, live market data; simulated fills (no real broker connection required)
- **Fills must be realistic (v1.1):** every simulated trade is charged the actual fee schedule
  of the venue it would execute on live (Kraken/Coinbase/Alpaca), plus an estimated spread and
  FX conversion cost. A paper result that ignores costs is worse than useless — it's misleading.
- Fully autonomous — the bot trades against the virtual ledger with no approval step, since
  nothing real is at risk. This is deliberately "hands-off" so Harry can observe behavior over
  weeks before trusting it with money.
- Runs for a minimum observation period (suggest 4–6 weeks) before Phase 2 is unlocked.

### Phase 2 — Live, Propose-and-Approve
- Same strategy engine, now connected to real accounts (see §6) with real funds.
- The bot does **not** place trades unilaterally. It generates a proposal (asset, direction,
  size, rationale, risk parameters) and sends a notification. Harry approves or rejects.
- Target cadence: roughly once/day to 5x/week, not intraday or high-frequency. This is a
  deliberate constraint to keep it swing-trade-paced and low-intrusion, not a scalping bot.
- If no response within a set window (e.g. 24h), the proposal expires — it does not execute by
  default. (Confirm this default with Harry; some users prefer auto-expire, others prefer
  auto-execute-if-no-objection. Default to the safer option: expire.)

**Hard constraint, not negotiable in the build:** nothing in this system should be able to place
a live order without an explicit human approval step in Phase 2. Automation ends at "propose."

---

## 3. Portfolio & Allocation

- Two "sleeves": Crypto and Stocks/ETFs. Starting reference split ~60/40 crypto/stocks, but
  **this must be a configurable parameter, not hardcoded** — Harry may revise it during reviews.
- Stocks sleeve: initially focused on tech/AI exposure (individual names and/or thematic ETFs),
  via US-listed instruments.
- Crypto sleeve: BTC-led, with the tool explicitly *not* assuming an "alt season" thesis anytime
  soon per Harry's current view — but again, adjustable at review time, not hardcoded.
- Rebalancing: the risk manager (see §5) should flag allocation drift beyond a configurable
  threshold (e.g. ±10 percentage points from target) at each review rather than auto-rebalancing
  silently.

---

## 4. Data & Broker/Exchange Integration

| Need | Recommendation | Notes |
|---|---|---|
| Crypto price data | CoinGecko public API or CCXT library | Free tier sufficient for daily-cadence strategy, no account needed |
| Crypto live execution (Phase 2) | Kraken and/or Coinbase Advanced Trade API | Harry already holds accounts on both |
| Stock/ETF price data | Alpaca Market Data API | Free with an Alpaca account |
| Stock/ETF paper + live execution | Alpaca | UK-accessible, commission-free, has a proper free paper-trading sandbox covering both stocks/ETFs and crypto — useful even if crypto execution ultimately goes through Kraken/Coinbase |
| Notifications (Phase 2) | Email to start (simplest, reliable); Telegram bot as a nice-to-have upgrade | Avoid anything that requires new paid infrastructure for v1 |

**Credentials handling (important):** API keys/secrets are never entered into or handled by
Claude chat. They live in environment variables / a local secrets file on Harry's own machine,
managed by Harry directly. The build should read credentials from env vars and fail loudly if
missing — never hardcode or log them.

**Key permissions (non-negotiable):** all exchange/broker API keys must be created trade-only,
with **withdrawal permissions disabled**, and IP-restricted where the platform supports it. A
compromised key must never be able to move funds out of the account.

**Currency & FX:** the pot is GBP but BTC and US stocks are USD-denominated. The ledger must
track GBP as base currency, apply a realistic FX conversion cost on every cross-currency trade,
and report all P&L in GBP. Prefer GBP pairs where they exist (Kraken offers BTC/GBP).

---

## 5. System Architecture — Agents

This is designed explicitly as a multi-agent build, both because it's the right shape for the
problem and because Harry wants to use this project to understand how Claude subagents work in
practice.

- **Orchestrator** — runs on a schedule (e.g. daily cron). Calls the strategist agents, passes
  their output to the risk manager, and (Phase 2) assembles the proposal notification. Owns the
  overall run log.
- **Crypto Strategist** — ingests crypto price/on-chain data, applies the configured strategy
  logic (see §7) to the crypto sleeve, outputs a proposed action + rationale.
- **Stocks Strategist** — same, for the stocks/ETF sleeve.
- **Risk Manager** — the veto layer. Checks proposed actions against position-size limits,
  allocation drift limits, and any hard rules (e.g. max % of pot in a single asset). Can block
  or resize a proposal; cannot originate one.
- **Reporting Agent** — compiles the daily log into the weekly review: performance vs
  benchmark, what fired and why, any flagged allocation drift, and a plain-English summary.

Each agent should be a distinct subagent definition (in Claude Code terms: separate
`.claude/agents/*.md` files with scoped tool access) rather than one monolithic prompt — that's
what makes the "isolated context, single responsibility" model actually work, and it's the part
worth Harry paying attention to as a working example of the architecture.

**Build-time vs runtime (v1.1 clarification):** the subagent structure above describes how the
system is *built and maintained* in Claude Code. The *deployed* daily bot must be deterministic
Python — the strategist/risk/orchestrator roles become code modules, not live LLM calls. Reasons:
(a) daily LLM calls would consume a meaningful share of a £500 pot's plausible returns in API
costs; (b) LLM decisions aren't reproducible, which breaks backtesting and auditability. The
only runtime LLM usage permitted in v1 is generating the weekly plain-English report (and
optionally proposal rationale text), on a cheap model, with strict cost logging.

---

## 6. Strategy Logic — Starting Point

Keep the v1 strategy deliberately simple and legible — Harry should be able to read a proposal
and understand *why* in one glance, not trust a black box. Suggested v1 baseline (to be reviewed
and adjusted, not treated as gospel):

- Trend/momentum filter (e.g. price relative to a moving average) to avoid buying into strong
  downtrends
- Scheduled accumulation (DCA-style) as the default behavior, with momentum signals used to
  *tilt* timing/size rather than trigger all-or-nothing entries
- No leverage, no shorting, no derivatives in v1
- No single trade larger than a configurable % of the relevant sleeve (suggest starting at 20%)
- **Fee-aware minimum trade size (v1.1):** no trade smaller than a configurable floor (suggest
  £50–75). At this pot size, fees and spread on small trades are the dominant cost — fewer,
  larger accumulations beat frequent small ones. The paper engine must prove this holds.

This is intentionally boring. Complexity (multiple signal types, ML-driven signals, etc.) can be
layered on in v2 once there's a track record from v1 to compare against.

---

## 7. Risk Management Rules (hard limits, enforced in code)

- Max position size per trade: configurable, suggested default 20% of sleeve
- Max sleeve drift before flagging: configurable, suggested default ±10 percentage points
- No live order execution without human approval (Phase 2)
- Daily/weekly proposal cap: configurable, suggested default 1/day, 5/week max
- Kill switch: a manual override that pauses all bot activity immediately, checked before any
  action is taken

---

## 8. Benchmarking

Every performance report must show the bot's result **against a simple benchmark**: e.g. holding
the same starting allocation (60/40 or whatever's configured) in BTC and a broad tech/AI ETF,
untouched, over the same period. Without this comparison there's no way to know if the active
management is adding anything over passive holding — which is the actual question this whole
project is testing.

---

## 9. Dashboard Requirements (for next design step)

To be designed in detail next, but should at minimum show:
- Current portfolio value, allocation vs target, P&L (absolute and vs benchmark)
- Trade log with rationale for each entry
- Pending proposal (Phase 2) with clear approve/reject action
- Weekly review summary view
- Simple mode toggle: Paper vs Live, with Live visually distinct (no accidental confusion)

---

## 10. Tech Stack (suggested, for Fable 5 to confirm/challenge)

- Backend: Python (rich data/finance library ecosystem, easy scheduling)
- Ledger storage: SQLite for MVP (simple, file-based, no infra to manage) — Postgres later if
  needed
- Scheduler: cron or a simple in-process scheduler for the daily run
- Frontend: React dashboard (matches Harry's existing tooling preferences)
- Notifications: SMTP email to start

---

## 11. Build Milestones

1. Paper ledger + simulated execution engine
2. Data ingestion (crypto + stocks)
3. Strategist agents (crypto, stocks) — simple baseline logic
4. Risk manager agent
5. Orchestrator wiring it together on a schedule
6. Reporting agent + weekly summary
7. Dashboard (separate design pass, see §9)
8. Phase 1 observation run (4–6 weeks minimum)
9. Phase 2: real broker/exchange connections, notification + approval flow
10. Go-live with real funds, small size first

---

## 12. Open Questions — Resolved (Fable 5, v1.1)

- **SQLite:** yes, sufficient. Single-writer daily-cadence workload; the runtime bot is one
  process (see §5 clarification), so there's no concurrent-write pattern to worry about.
- **Cron vs event-driven:** cron is right. One scheduled run per day evaluating "should I
  propose today?" against the weekly cap is simpler and fully auditable. Event-driven adds
  nothing at this cadence.
- **Risk rule gaps:** three added — fee-aware minimum trade size (§6), idempotent runs with
  missed-run alerting (§14.3), and no-withdrawal API keys (§4).
- **Email for approvals:** email as *notification* is fine; email as *approval mechanism* was
  never actually specified and doesn't work without a receiving endpoint. v1: approval happens
  in the dashboard, the email just links to it. v2: Telegram bot with inline approve/decline
  buttons.
- **Subagent breakdown:** correct for build-time; see §5 for the build-time/runtime split,
  which is the important correction.

---

## 13. Explicitly Out of Scope (v1)

- Leverage, shorting, derivatives, options
- High-frequency/intraday trading
- Multi-exchange arbitrage
- Full tax automation (but see §14.5 — the trade log must export a CGT-ready CSV from day one)

---

## 14. Fable 5 Review Findings (v1.1)

Issues found on review of the v1 spec and design thread, now folded into the sections above or
recorded here.

### 14.1 Cost realism (critical — fixed in §2, §4, §6)
v1 ignored fees, spread, and FX entirely. At £28–42 trade sizes (as shown in the dashboard
mockup's sample data), venue fees plus spread plus GBP→USD conversion can consume 1–3% per
round trip — enough to erase the plausible edge of any daily-cadence strategy. Fixed via
realistic fill simulation, fee-aware minimum trade size, and GBP-base FX accounting. **The
dashboard's sample trade sizes should be treated as illustrative only; real trades will be
fewer and larger.**

### 14.2 Runtime architecture (critical — fixed in §5)
v1 implied the deployed bot runs LLM agents daily. Corrected: deterministic Python at runtime,
LLM only for weekly report generation. Subagents remain the build-time structure.

### 14.3 Hosting & failure handling (new requirement)
The bot needs an always-on home: a small VPS (~£5/mo), a Raspberry Pi, or a scheduled GitHub
Actions workflow (free tier is adequate for one daily run). Requirements: every run logs an
outcome (success, no-action, or failure); runs are idempotent (re-running a day never
double-trades); a missed or failed run sends a notification rather than failing silently.

### 14.4 Approval mechanism (fixed in §12)
Dashboard-based approval in v1; email is notification-only with a deep link. Telegram inline
buttons are the planned v2 upgrade.

### 14.5 UK tax record-keeping (new requirement)
Gains on a £500 pot will almost certainly fall under the CGT annual exemption, but HMRC
share-pooling rules apply to crypto disposals regardless. The trade log schema must capture
enough (date, asset, quantity, GBP value, fees) to export a CGT-ready CSV at any time. Cheap
to build now, painful to reconstruct later.

### 14.6 Alpaca crypto for UK accounts (verify before Phase 2)
Alpaca's stock/ETF offering and paper sandbox are fine for UK residents; crypto availability on
international accounts is not guaranteed and must be verified at Phase 2. Default plan: stocks
via Alpaca, crypto via Kraken (which offers GBP pairs, reducing FX drag).

### 14.7 Benchmark integrity (new requirement)
Benchmark starting prices (BTC and the chosen tech/AI ETF) must be snapshotted into the ledger
on day one of Phase 1 and again at Phase 2 go-live. Without the snapshot, the "vs. just
holding" comparison can't be computed honestly.

### 14.8 Integration with existing reviews (design note)
The weekly report should be formatted to drop into Harry's existing household finance review
cadence as one section, not become a separate ritual. Success criterion: the whole weekly
touchpoint takes under ten minutes.

### 14.9 Expectation check (unchanged, restated)
Even an excellent year on £500 is a few hundred pounds. The realistic ROI of this project is
the system, the discipline, and the agent-architecture learning — with the benchmark in §8
existing precisely to test, honestly, whether the automation earns its keep against doing
nothing.
