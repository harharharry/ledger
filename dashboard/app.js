// Ledger dashboard — no-build React (ES modules via CDN, htm for templates).
// Read-only over data.json except the kill switch, which POSTs to the local
// serve command. All money arrives as strings; this file never does money math.

import { createElement, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import htm from "htm";

const html = htm.bind(createElement);

const SLEEVE_COLOR = { crypto: "var(--indigo)", stocks: "var(--teal)" };
const ASSET_NAMES = { BTC: "Bitcoin", QQQ: "Invesco QQQ" };

function fmtGBP(s) {
  if (s === null || s === undefined) return "—";
  const [whole, pence] = String(s).split(".");
  return "£" + Number(whole).toLocaleString("en-GB") + (pence ? "." + pence : "");
}

function fmtPct(p) {
  if (p === null || p === undefined) return "—";
  return (p >= 0 ? "▲ " : "▼ ") + Math.abs(p).toFixed(2) + "%";
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", {
    day: "numeric", month: "short",
  });
}

function Sparkline({ points }) {
  if (!points || points.length < 2) return null;
  const values = points.map((p) => parseFloat(p.value_gbp));
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const up = values[values.length - 1] >= values[0];
  const pts = values
    .map((v, i) => `${(i / (values.length - 1)) * 600},${(1 - (v - min) / range) * 56 + 4}`)
    .join(" ");
  return html`<svg class="sparkline" viewBox="0 0 600 64" preserveAspectRatio="none"
    role="img" aria-label="Portfolio value history">
    <polyline points=${pts} fill="none" stroke=${up ? "#34C759" : "#FF3B30"}
      stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
  </svg>`;
}

function Hero({ hero, opened }) {
  const pct = hero.pnl_pct;
  return html`<section class="hero">
    <div class="hero-label">Total balance</div>
    <div class="hero-value tabular">${fmtGBP(hero.total_balance_gbp)}</div>
    <div class="hero-sub">
      ${pct !== null && html`<span class=${"chip " + (pct >= 0 ? "up" : "down")}>${fmtPct(pct)}</span>`}
      <span class="muted">
        ${opened
          ? `${fmtGBP(hero.pnl_gbp)} since ${fmtDate(hero.since)} · Paper mode`
          : "Not yet opened — the first daily run records the £500 opening balance"}
      </span>
    </div>
    <${Sparkline} points=${hero.sparkline} />
  </section>`;
}

function Ring({ allocation }) {
  const actual = allocation.actual;
  const crypto = actual.crypto ?? allocation.target.crypto;
  const stocks = actual.stocks ?? allocation.target.stocks;
  const c1 = 2 * Math.PI * 52, c2 = 2 * Math.PI * 38;
  return html`<svg width="120" height="120" viewBox="0 0 120 120" role="img"
    aria-label="Allocation rings: crypto ${crypto}%, stocks ${stocks}%">
    <circle cx="60" cy="60" r="52" fill="none" stroke="rgba(94,92,230,0.15)" stroke-width="10" />
    <circle cx="60" cy="60" r="52" fill="none" stroke="var(--indigo)" stroke-width="10"
      stroke-dasharray=${c1} stroke-dashoffset=${c1 * (1 - crypto / 100)}
      stroke-linecap="round" transform="rotate(-90 60 60)" />
    <circle cx="60" cy="60" r="38" fill="none" stroke="rgba(48,176,199,0.15)" stroke-width="10" />
    <circle cx="60" cy="60" r="38" fill="none" stroke="var(--teal)" stroke-width="10"
      stroke-dasharray=${c2} stroke-dashoffset=${c2 * (1 - stocks / 100)}
      stroke-linecap="round" transform="rotate(-90 60 60)" />
  </svg>`;
}

function AllocationCard({ allocation, onSelectAsset, assets }) {
  const { target, actual } = allocation;
  const tilt =
    actual.crypto === null ? "not yet invested" :
    Math.abs(actual.crypto - target.crypto) < 0.5 ? "on target" :
    `crypto ${actual.crypto > target.crypto ? "tilted +" : "tilted −"}${Math.abs(actual.crypto - target.crypto).toFixed(0)}pts`;
  const sleeves = [
    { key: "crypto", name: "Crypto", color: "var(--indigo)" },
    { key: "stocks", name: "Stocks / ETFs", color: "var(--teal)" },
  ];
  return html`<div class="card">
    <h3>Allocation</h3>
    <p class="card-sub">Target ${target.crypto.toFixed(0)} / ${target.stocks.toFixed(0)} · ${tilt}</p>
    <div class="rings-row">
      <${Ring} allocation=${allocation} />
      <div class="ring-legend">
        ${sleeves.map((s) => {
          const symbol = Object.keys(assets).find((k) => assets[k].sleeve === s.key);
          return html`<div key=${s.key}
            class=${"legend-item" + (symbol ? " clickable" : "")}
            onClick=${() => symbol && onSelectAsset(symbol)}>
            <span class="swatch" style=${{ background: s.color }}></span>
            <div class="legend-text">
              <span class="name">${s.name}${symbol ? " →" : ""}</span>
              <span class="detail tabular">
                ${actual[s.key] === null ? "0" : actual[s.key].toFixed(0)}% actual · ${target[s.key].toFixed(0)}% target
              </span>
            </div>
          </div>`;
        })}
      </div>
    </div>
  </div>`;
}

function BenchmarkCard({ benchmark }) {
  if (!benchmark) {
    return html`<div class="card">
      <h3>Vs. just holding</h3>
      <p class="card-sub">Same start date, untouched allocation</p>
      <p class="empty-note">Starts with the first run — day-one prices are snapshotted
        into the ledger and every report compares against holding them untouched.</p>
    </div>`;
  }
  const rows = [
    { name: "Ledger (active)", pct: benchmark.ledger_pct, color: benchmark.ledger_pct >= 0 ? "var(--green)" : "var(--red)", valColor: benchmark.ledger_pct >= 0 ? "var(--green)" : "var(--red)" },
    { name: "Buy & hold benchmark", pct: benchmark.benchmark_pct, color: "#C7C7CC", valColor: "var(--ink-soft)" },
  ];
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.pct)), 1);
  return html`<div class="card">
    <h3>Vs. just holding</h3>
    <p class="card-sub">Same start date, untouched allocation (${benchmark.note})</p>
    <div class="bench-row">
      ${rows.map((r) => html`<div class="bench-item" key=${r.name}>
        <div class="top">
          <span class="name">${r.name}</span>
          <span class="val tabular" style=${{ color: r.valColor }}>
            ${(r.pct >= 0 ? "+" : "") + r.pct.toFixed(2)}%</span>
        </div>
        <div class="bar-track">
          <div class="bar-fill" style=${{ width: (Math.abs(r.pct) / maxAbs) * 100 + "%", background: r.color }}></div>
        </div>
      </div>`)}
    </div>
  </div>`;
}

function ProposalCard({ proposal }) {
  if (!proposal) return null;
  return html`<div class="proposal">
    <div class="proposal-head"><span class="dot"></span><span>Pending approval · ${proposal.sent}</span></div>
    <div class="proposal-body">
      <div>
        <div class="proposal-title">${proposal.title}</div>
        <div class="proposal-reason">${proposal.rationale}</div>
      </div>
      <div class="proposal-actions">
        <button class="btn btn-decline">Decline</button>
        <button class="btn btn-approve">Approve</button>
      </div>
    </div>
  </div>`;
}

function ActivityCard({ activity }) {
  const [open, setOpen] = useState(null);
  return html`<div class="card activity-card">
    <h3>Recent activity</h3>
    <p class="card-sub">Tap any row for the full rationale</p>
    <div class="activity-list">
      ${activity.length === 0 && html`<p class="empty-note">Nothing yet — activity appears after the first daily run.</p>`}
      ${activity.map((a, i) => {
        const iconBg = a.kind === "trade"
          ? SLEEVE_COLOR[a.sleeve]
          : a.title === "Run failed" ? "var(--red)" : "#C7C7CC";
        const initial = a.kind === "trade" ? a.title.slice(-3, -2) : a.title === "Run failed" ? "!" : "–";
        return html`<div key=${i} class=${"activity-item" + (open === i ? " open" : "")}
          onClick=${() => setOpen(open === i ? null : i)}>
          <div class="a-icon" style=${{ background: iconBg }}>${initial}</div>
          <div class="a-wrap">
            <div class="a-main">
              <div class="a-title">${a.title}</div>
              <div class="a-detail">${a.detail}</div>
            </div>
            ${a.rationale && html`<div class="a-rationale">${a.rationale}</div>`}
          </div>
          <div class="a-amount tabular" style=${a.amount_gbp ? {} : { color: "var(--ink-soft)" }}>
            ${a.amount_gbp ? fmtGBP(a.amount_gbp) : a.title === "Run failed" ? "Failed" : "—"}
            <span class="time">${fmtDate(a.date)}</span>
          </div>
        </div>`;
      })}
    </div>
  </div>`;
}

function Footer({ data, killEngaged, onToggleKill }) {
  const last = data.last_run;
  return html`<footer>
    <span class="updated">
      ${last ? `Last run ${fmtDate(last.date)} · ${last.outcome ?? "in progress"}` : "No runs yet"}
      · data refreshed ${new Date(data.generated_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}
    </span>
    <div class="kill-switch">
      ${killEngaged ? "Automation paused" : "Pause automation"}
      <div class=${"toggle" + (killEngaged ? " on" : "")} onClick=${onToggleKill}
        role="switch" aria-checked=${killEngaged} aria-label="Kill switch"></div>
    </div>
  </footer>`;
}

function PriceChart({ asset }) {
  const closes = asset.chart.map((c) => parseFloat(c.close));
  const mas = asset.chart.map((c) => (c.ma === null ? null : parseFloat(c.ma)));
  const all = closes.concat(mas.filter((v) => v !== null));
  const min = Math.min(...all), max = Math.max(...all);
  const range = max - min || 1;
  const w = 600, h = 220, padTop = 16, padBottom = 16;
  const x = (i) => (i / (closes.length - 1)) * w;
  const y = (v) => padTop + (1 - (v - min) / range) * (h - padTop - padBottom);
  const pricePts = closes.map((v, i) => `${x(i)},${y(v).toFixed(1)}`).join(" ");
  const maPts = mas
    .map((v, i) => (v === null ? null : `${x(i)},${y(v).toFixed(1)}`))
    .filter(Boolean).join(" ");
  const buyDates = new Set(asset.buys.map((b) => b.date));
  const markers = asset.chart
    .map((c, i) => (buyDates.has(c.date) ? { i, v: closes[i] } : null))
    .filter(Boolean);
  return html`<svg class="price-chart" viewBox="0 0 600 220" preserveAspectRatio="none"
    role="img" aria-label="${asset.symbol} price and 50-day moving average">
    <polyline points=${maPts} fill="none" stroke="#C7C7CC" stroke-width="2" stroke-dasharray="5,5" />
    <polyline points=${pricePts} fill="none" stroke="var(--indigo)" stroke-width="2.5"
      stroke-linecap="round" stroke-linejoin="round" />
    ${markers.map((m) => html`<circle key=${m.i} cx=${x(m.i)} cy=${y(m.v)} r="5"
      fill="#5E5CE6" stroke="#fff" stroke-width="2" />`)}
  </svg>`;
}

function AssetDetail({ asset }) {
  const name = ASSET_NAMES[asset.symbol] || asset.symbol;
  const color = SLEEVE_COLOR[asset.sleeve];
  const ccy = asset.currency === "GBP" ? "£" : "$";
  const bullish = asset.trend === "bullish";
  const rsi = asset.rsi;
  const overbought = rsi !== null && rsi >= asset.rsi_overbought;
  const oversold = rsi !== null && rsi <= asset.rsi_oversold;
  const synthesis = bullish
    ? `Price is above its 50-day average (${asset.sessions_above_ma} session${asset.sessions_above_ma === 1 ? "" : "s"} and counting), so scheduled accumulation is on. ` +
      (oversold
        ? `RSI ${rsi} is in the oversold zone, so the next buy is tilted up 1.5x — a dip inside an uptrend is the setup this strategy accumulates into.`
        : overbought
        ? `RSI ${rsi} is overbought, so sizing is halved — at £60 base that lands under the £50 fee floor, meaning no trade until momentum cools.`
        : `RSI ${rsi} is neutral, so the standard base size applies. No momentum override in play.`)
    : `Price is below its 50-day average, so the trend gate is closed and no buys will fire regardless of momentum. This is the discipline doing its job — the bot does not buy into downtrends.`;
  return html`<div>
    <div class="asset-title">
      <span class="asset-icon" style=${{ background: color }}>${asset.symbol[0]}</span>
      <div>
        <div class="asset-name">${name} <span class="asset-ticker">· ${asset.symbol}</span></div>
        <div class="asset-price tabular">
          ${ccy}${parseFloat(asset.latest_close).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          ${asset.change_today_pct !== null && html` <span class=${"chip " + (asset.change_today_pct >= 0 ? "up" : "down")}>${fmtPct(asset.change_today_pct)} today</span>`}
        </div>
      </div>
    </div>

    <div class="card chart-card">
      <div class="chart-card-head">
        <h3>Price & trend filter</h3>
        <div class="chart-legend">
          <span><i style=${{ background: "var(--indigo)" }}></i> Price (${asset.currency})</span>
          <span><i class="dashed"></i> 50-day MA</span>
          <span><i class="dot-i" style=${{ background: "var(--indigo)" }}></i> Bot buy</span>
        </div>
      </div>
      <${PriceChart} asset=${asset} />
    </div>

    <div class="grid" style=${{ marginTop: "20px" }}>
      <div class="card">
        <h3>Trend</h3>
        <p class="card-sub">50-day moving average filter</p>
        <div class="indicator">
          <span class="dot" style=${{ background: bullish ? "var(--green)" : "var(--red)" }}></span>
          <div>
            <div class="indicator-title">${bullish ? "Bullish" : "Bearish"}</div>
            <div class="indicator-detail">
              ${bullish
                ? `Price has closed above the 50-day MA for ${asset.sessions_above_ma} straight session${asset.sessions_above_ma === 1 ? "" : "s"}. This gate is currently open — scheduled accumulation can fire.`
                : "Price is below the 50-day MA. The trend gate is closed: no buys fire until price recovers the average."}
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>Momentum</h3>
        <p class="card-sub">${"14-period RSI"}</p>
        <div class="rsi-track">
          <div class="rsi-zone rsi-low" style=${{ width: asset.rsi_oversold + "%" }}></div>
          <div class="rsi-zone rsi-mid" style=${{ width: (asset.rsi_overbought - asset.rsi_oversold) + "%" }}></div>
          <div class="rsi-zone rsi-high" style=${{ width: (100 - asset.rsi_overbought) + "%" }}></div>
          ${rsi !== null && html`<div class="rsi-marker" style=${{ left: rsi + "%" }}></div>`}
        </div>
        <div class="rsi-labels"><span>Oversold ≤${asset.rsi_oversold.toFixed(0)}</span><span>Neutral</span><span>Overbought ≥${asset.rsi_overbought.toFixed(0)}</span></div>
        <div class="indicator-detail" style=${{ marginTop: "12px" }}>
          ${rsi === null ? "Not enough history yet." :
            `${rsi} — ${oversold ? "oversold: buys tilt up 1.5x while the trend gate is open." : overbought ? "overbought: sizing halves, which lands under the fee floor at the current base." : "neutral: standard sizing applies."}`}
        </div>
      </div>
    </div>

    <div class="card" style=${{ marginTop: "20px" }}>
      <h3>What the strategy is reading</h3>
      <p class="card-sub">Deterministic synthesis from today's data — the same rules the bot runs, in plain English</p>
      <p class="synthesis">${synthesis}</p>
    </div>
  </div>`;
}

function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [view, setView] = useState("overview");
  const [killEngaged, setKillEngaged] = useState(false);

  useEffect(() => {
    fetch("data.json")
      .then((r) => { if (!r.ok) throw new Error("data.json missing — run: python -m ledger.dashboard"); return r.json(); })
      .then((d) => { setData(d); setKillEngaged(d.kill_switch_engaged); })
      .catch((e) => setError(String(e)));
  }, []);

  const toggleKill = () => {
    fetch("/api/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engaged: !killEngaged }),
    })
      .then((r) => r.json())
      .then((d) => setKillEngaged(d.engaged))
      .catch(() => alert("Kill switch needs the serve command: python -m ledger.dashboard"));
  };

  if (error) return html`<main><p class="empty-note">${error}</p></main>`;
  if (!data) return html`<main><p class="empty-note">Loading…</p></main>`;

  const assetSymbols = Object.keys(data.assets);
  return html`<div>
    <header>
      <div class="header-inner">
        <div class="brand"><span class=${"brand-dot" + (killEngaged ? " paused" : "")}></span> Ledger</div>
        <div class="segmented">
          <button class="active">Paper</button>
          <button disabled title="Phase 2 — after the observation run">Live</button>
        </div>
        <div class="preview-badge">Phase 1 · autonomous paper</div>
      </div>
    </header>

    <div class="tabbar">
      <button class=${view === "overview" ? "active" : ""} onClick=${() => setView("overview")}>Overview</button>
      ${assetSymbols.map((sym) => html`<button key=${sym}
        class=${view === sym ? "active" : ""} onClick=${() => setView(sym)}>
        ${(ASSET_NAMES[sym] || sym)} · Technical</button>`)}
    </div>

    <main>
      ${view === "overview"
        ? html`<div>
            <${Hero} hero=${data.hero} opened=${data.opened} />
            <div class="grid">
              <${AllocationCard} allocation=${data.allocation} assets=${data.assets} onSelectAsset=${setView} />
              <${BenchmarkCard} benchmark=${data.benchmark} />
            </div>
            <${ProposalCard} proposal=${data.pending_proposal} />
            <${ActivityCard} activity=${data.activity} />
            <${Footer} data=${data} killEngaged=${killEngaged} onToggleKill=${toggleKill} />
          </div>`
        : html`<${AssetDetail} asset=${data.assets[view]} />`}
    </main>
  </div>`;
}

createRoot(document.getElementById("root")).render(html`<${App} />`);
