/* =========================================================
   Agentic Trading Desk — Frontend JS
   All state lives here. Zero dependencies.
   ========================================================= */

const state = {
  results: {},          // symbol → scorecard from /api/score
  activeTicker: null,
  pendingOrder: null,   // {symbol, card}
  planResult: null,
};

// ── Security helpers ────────────────────────────────────────
// All data from user input or API responses MUST be passed
// through esc() before embedding in HTML strings.

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ESC_MAP[c]);
}

function setText(el, text) {
  if (el) el.textContent = text;
}

function appendDomMsg(feed, nodes) {
  const div = document.createElement('div');
  nodes.forEach(n => div.appendChild(n));
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

function makeEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

// ── Utilities ──────────────────────────────────────────────

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

function ts() {
  return new Date().toLocaleTimeString('en-US', { hour12: false }) + ' UTC';
}

function scoreColor(s) {
  if (s == null) return '#888';
  return s > 0 ? '#00ff41' : s < 0 ? '#ff4444' : '#888';
}

function actionClass(action) {
  if (!action) return 'hold';
  const a = action.toUpperCase();
  if (a.startsWith('EXIT')) return 'exit';
  if (a.includes('RE-ENTRY') || a.includes('TACTICAL')) return 'enter';
  if (a.startsWith('HOLD')) return 'hold';
  return 'wait';
}

function actionShort(action) {
  if (!action) return '—';
  const map = {
    'EXIT / TRIM': 'EXIT/TRIM', 'EXIT': 'EXIT',
    'RE-ENTRY (new cycle)': 'ENTER',
    'TACTICAL REBOUND (counter-trend)': 'TACTICAL',
    'HOLD (ride the cycle)': 'HOLD', 'HOLD (under review)': 'REVIEW',
    'WAIT (do not chase)': 'WAIT', 'STAY OUT / AVOID': 'AVOID',
    'HOLD / OBSERVE': 'OBSERVE', 'OBSERVE': 'OBSERVE',
  };
  return map[action] || action.slice(0, 8);
}

// ── Sparkline SVG ──────────────────────────────────────────
// All values are numbers we generate; no user strings here.

function sparkline(closes, w = 64, h = 26) {
  if (!closes || closes.length < 3) return '';
  const pts = closes.slice(-20);
  const mn = Math.min(...pts), mx = Math.max(...pts);
  const rng = mx - mn || 1;
  const coords = pts.map((c, i) => {
    const x = (i / (pts.length - 1)) * (w - 2) + 1;
    const y = h - 2 - ((c - mn) / rng) * (h - 4);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const up = pts[pts.length - 1] >= pts[0];
  const color = up ? '#00ff41' : '#ff6b6b';
  // SVG is built entirely from safe numeric strings — no user data
  const gid = `g${Math.random().toString(36).slice(2, 8)}`;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block" aria-hidden="true">`
    + `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="1" y2="0">`
    + `<stop offset="0%" stop-color="${color}" stop-opacity="0.3"/>`
    + `<stop offset="100%" stop-color="${color}" stop-opacity="1"/>`
    + `</linearGradient></defs>`
    + `<polyline points="${coords}" fill="none" stroke="url(#${gid})" stroke-width="1.5" stroke-linejoin="round"/>`
    + `</svg>`;
}

// ── Flag Badges ─────────────────────────────────────────────
// Badge labels are hardcoded strings — no API data interpolated.

function flagBadges(flags) {
  if (!flags) return '';
  const parts = [];
  const exh = flags.exhaustion || [];
  const bear = flags.bearish || [];
  const reb = flags.rebound || [];

  if (exh.some(f => f.toLowerCase().includes('rsi')))      parts.push('<span class="badge amber">OVERBOUGHT</span>');
  if (exh.some(f => f.toLowerCase().includes('bollinger'))) parts.push('<span class="badge amber">BB UPPER</span>');
  if (exh.some(f => f.toLowerCase().includes('atr') || f.toLowerCase().includes('stretch'))) parts.push('<span class="badge amber">STRETCHED</span>');
  if (exh.some(f => f.toLowerCase().includes('z-score')))  parts.push('<span class="badge amber">STAT EXT</span>');
  if (exh.some(f => f.toLowerCase().includes('macd')))     parts.push('<span class="badge orange">MACD SHRINK</span>');
  if (reb.length > 0)                                       parts.push('<span class="badge green">REBOUND</span>');
  if (bear.some(f => f.toLowerCase().includes('macd')))    parts.push('<span class="badge red">MACD DEEP</span>');
  if (flags.death_cross)                                    parts.push('<span class="badge red">DEATH×</span>');
  if (bear.length >= 2 && !flags.death_cross)              parts.push('<span class="badge orange">VOLATILE</span>');
  if (parts.length === 0)                                   parts.push('<span class="badge dim">NEUTRAL</span>');
  return parts.slice(0, 3).join('');
}

// ── Scanner Grid ────────────────────────────────────────────

function addOrUpdateRow(card) {
  const sym     = card.symbol || '?';
  const price   = card.indicators?.close;
  const score   = card.pillar_total;
  const action  = card.decision?.action || '';
  const closes  = card._closes || [];

  const ac = actionClass(action);
  const sc = scoreColor(score);
  const scoreStr = score != null ? (score > 0 ? `+${score}` : `${score}`) : '?';

  const existing = document.getElementById(`row-${esc(sym)}`);
  const tr = existing || document.createElement('tr');
  if (!existing) {
    tr.id = `row-${esc(sym)}`;
    tr.className = 'scanner-row';
    document.getElementById('scanner-tbody').insertBefore(tr, document.getElementById('scanner-tbody').firstChild);
  }

  // Clear and rebuild cells via DOM — no innerHTML with dynamic data in text cells
  tr.textContent = '';

  // TICKER cell
  const tdSym = document.createElement('td');
  tdSym.className = 'td-ticker';
  tdSym.dataset.sym = sym;
  const symSpan = makeEl('span', 'sym-name', sym);
  tdSym.appendChild(symSpan);

  // PRICE cell
  const tdPrice = document.createElement('td');
  tdPrice.className = 'td-price';
  tdPrice.style.color = (price != null && price > 0) ? '#00ff41' : '#ff4444';
  tdPrice.textContent = price != null ? price.toFixed(2) : '—';

  // SCORE cell
  const tdScore = document.createElement('td');
  tdScore.className = 'td-score';
  tdScore.style.color = sc;
  tdScore.style.fontWeight = '700';
  tdScore.textContent = scoreStr;

  // TREND cell — sparkline is pure numeric SVG, safe for innerHTML
  const tdTrend = document.createElement('td');
  tdTrend.className = 'td-trend';
  tdTrend.innerHTML = sparkline(closes);

  // FLAGS cell — badge HTML uses only hardcoded label strings
  const tdFlags = document.createElement('td');
  tdFlags.className = 'td-flags';
  tdFlags.innerHTML = flagBadges(card.decision?.flags);

  // ACTION cell — button uses data-sym, not inline onclick
  const tdAction = document.createElement('td');
  tdAction.className = 'td-action';
  const btn = makeEl('button', `action-btn ac-${ac}`, actionShort(action));
  btn.dataset.sym = sym;
  btn.dataset.role = 'open-order';
  tdAction.appendChild(btn);

  tr.append(tdSym, tdPrice, tdScore, tdTrend, tdFlags, tdAction);
}

function selectTicker(sym) {
  state.activeTicker = sym;
  document.querySelectorAll('.scanner-row').forEach(r => r.classList.remove('active'));
  const row = document.getElementById(`row-${sym}`);
  if (row) row.classList.add('active');
  const card = state.results[sym];
  if (card) updateAnalystPanel(card);
}

// ── Analyst Panel ───────────────────────────────────────────
// Built via DOM to keep user/API strings out of HTML contexts.

function updateAnalystPanel(card) {
  const sym = card.symbol || '?';
  setText(document.getElementById('analyst-symbol'), sym);

  const feed = document.getElementById('analyst-feed');
  feed.textContent = '';  // clear

  const d   = card.decision  || {};
  const p   = card.pillars   || {};
  const r   = card.risk;
  const ind = card.indicators || {};

  // ── Timestamp + framing message ──
  const msgDiv = makeEl('div', 'feed-msg system');
  msgDiv.appendChild(makeEl('div', 'feed-ts', ts()));
  msgDiv.appendChild(makeEl('div', 'feed-text', d.framing || d.rationale || ''));

  // Pillar summary line (numbers only — textContent safe)
  const pillarsLine = [
    `Trend ${pillarText(p.trend?.score)}`,
    `Momentum ${pillarText(p.momentum?.score)}`,
    `Macro ${pillarText(p.macro_sentiment?.score)}`,
  ].join('  ·  ');
  msgDiv.appendChild(makeEl('div', 'feed-dim', pillarsLine));
  feed.appendChild(msgDiv);

  // ── Terminal block ──
  const termDiv = makeEl('div', 'feed-terminal');
  const termLines = [];
  termLines.push(`> SCORE: ${card.pillar_total != null ? (card.pillar_total > 0 ? '+' : '') + card.pillar_total : '?'} / ±6`);
  termLines.push(`> ACTION: ${d.action || '?'}`);
  if (r?.suggested_stop != null)
    termLines.push(`> STOP: $${r.suggested_stop.toFixed(2)} (${r.stop_distance_pct}% away, ${r.stop_type || ''})`);
  if (r?.vol_target_fraction != null) {
    const fv = r.forecast_vol_annual;
    termLines.push(`> SIZE: ${Math.round(r.vol_target_fraction * 100)}% of sleeve` + (fv ? ` (forecast vol ${(fv * 100).toFixed(1)}%)` : ''));
  }
  if (r?.zscore_20 != null)
    termLines.push(`> Z-SCORE(20d): ${r.zscore_20 > 0 ? '+' : ''}${r.zscore_20.toFixed(2)}σ`);
  if (r?.vol_ratio != null)
    termLines.push(`> VOL_RATIO: ${r.vol_ratio.toFixed(2)}× long-run${r.vol_ratio >= 1.3 ? ' ⚠ EXPANDING' : ''}`);
  termLines.forEach(l => termDiv.appendChild(makeEl('div', 'feed-term-line', l)));
  feed.appendChild(termDiv);

  // ── Flags block ──
  const f = d.flags || {};
  const flagsDiv = makeEl('div', 'feed-flags-block');
  (f.exhaustion || []).forEach(x => flagsDiv.appendChild(makeEl('span', 'feed-flag exh', `▲ exhaustion — ${x}`)));
  (f.bearish    || []).forEach(x => flagsDiv.appendChild(makeEl('span', 'feed-flag bear', `▼ bearish — ${x}`)));
  (f.rebound    || []).forEach(x => flagsDiv.appendChild(makeEl('span', 'feed-flag reb', `↑ rebound — ${x}`)));
  if (f.death_cross) flagsDiv.appendChild(makeEl('span', 'feed-flag bear', '✖ structure — active death-cross'));
  if (flagsDiv.children.length) feed.appendChild(flagsDiv);

  // ── Indicator event line ──
  if (ind.rsi14 != null) {
    const evDiv = makeEl('div', 'feed-event');
    const dot = makeEl('span', 'feed-dot');
    const txt = makeEl('span', null,
      ` RSI ${ind.rsi14.toFixed(0)}  ·  MACD hist ${ind.macd_hist != null ? (ind.macd_hist > 0 ? '+' : '') + ind.macd_hist.toFixed(4) : '—'}  ·  %B ${ind.percent_b != null ? ind.percent_b.toFixed(2) : '—'}`
    );
    evDiv.append(dot, txt);
    feed.appendChild(evDiv);
  }
}

function pillarText(s) {
  if (s == null) return '?';
  return (s > 0 ? '+' : '') + s;
}

// appendUserMessage — pure user input, use textContent only
function appendUserMessage(text) {
  const feed = document.getElementById('analyst-feed');
  const outer = makeEl('div', 'feed-msg user');
  outer.appendChild(makeEl('div', 'feed-text', text));
  feed.appendChild(outer);
  feed.scrollTop = feed.scrollHeight;
}

// appendSystemMessage — text is constructed by our code only.
// Any API/user data embedded MUST be pre-escaped by the caller.
function appendSystemMessage(safeHtml) {
  const feed = document.getElementById('analyst-feed');
  const outer = makeEl('div', 'feed-msg system');
  outer.appendChild(makeEl('div', 'feed-ts', ts()));
  const body = document.createElement('div');
  body.className = 'feed-text';
  body.innerHTML = safeHtml;          // caller guarantees this is safe HTML
  outer.appendChild(body);
  feed.appendChild(outer);
  feed.scrollTop = feed.scrollHeight;
}

// ── Scan Form ───────────────────────────────────────────────

async function handleScan() {
  const sym      = document.getElementById('scan-symbol').value.trim().toUpperCase();
  const closesRaw = document.getElementById('scan-closes').value.trim();
  const macroScore = parseInt(document.getElementById('scan-macro').value) || 0;
  const holding  = document.getElementById('scan-holding').checked;

  if (!sym)      { showToast('Enter a ticker symbol', 'error'); return; }
  if (!closesRaw){ showToast('Paste close prices (JSON array or comma-separated)', 'error'); return; }

  let closes;
  try { closes = JSON.parse(closesRaw); }
  catch { closes = closesRaw.split(',').map(s => parseFloat(s.trim())).filter(n => !isNaN(n)); }
  if (!Array.isArray(closes) || closes.length < 30) {
    showToast('Need at least 30 close prices', 'error'); return;
  }

  const btn = document.getElementById('scan-btn');
  btn.textContent = 'SCANNING...';
  btn.disabled = true;

  const card = await post('/api/score', { symbol: sym, close: closes, macro_score: macroScore, holding });
  btn.textContent = 'SCAN';
  btn.disabled = false;

  if (card.error) { showToast(card.error, 'error'); return; }
  card._closes = closes;
  state.results[sym] = card;
  addOrUpdateRow(card);
  selectTicker(sym);
  showToast(`${sym} scored: ${card.pillar_total > 0 ? '+' : ''}${card.pillar_total}`, 'success');
}

// ── Order Modal ─────────────────────────────────────────────

function openOrderModal(sym) {
  const card = state.results[sym];
  if (!card) return;
  state.pendingOrder = { symbol: sym, card };
  state.planResult   = null;

  // All text set via textContent — no innerHTML with sym
  setText(document.getElementById('modal-symbol'), sym);
  const actionEl = document.getElementById('modal-action');
  setText(actionEl, card.decision?.action || '?');
  actionEl.className = 'modal-action-val ac-' + actionClass(card.decision?.action);
  setText(document.getElementById('modal-price'), card.indicators?.close?.toFixed(2) ?? '—');
  const score = card.pillar_total;
  setText(document.getElementById('modal-score'), score != null ? (score > 0 ? '+' : '') + score : '?');

  const r = card.risk;
  setText(document.getElementById('modal-stop'),
    r?.suggested_stop != null ? `$${r.suggested_stop.toFixed(2)} (${r.stop_distance_pct}% away)` : '—');
  setText(document.getElementById('modal-size-hint'),
    r?.vol_target_fraction != null ? `Vol-target: ${Math.round(r.vol_target_fraction * 100)}% of sleeve` : '');

  document.getElementById('plan-output').textContent = '';
  const confirmBtn = document.getElementById('confirm-trade-btn');
  confirmBtn.disabled = true;
  confirmBtn.textContent = 'BUILD PLAN FIRST';
  document.getElementById('approval-modal').classList.remove('hidden');
}

function closeModal() {
  document.getElementById('approval-modal').classList.add('hidden');
  state.pendingOrder = null;
  state.planResult   = null;
}

async function buildPlan() {
  const o = state.pendingOrder;
  if (!o) return;

  const qty     = parseInt(document.getElementById('modal-qty').value);
  const urgency = document.getElementById('modal-urgency').value;
  const bid     = parseFloat(document.getElementById('modal-bid').value);
  const ask     = parseFloat(document.getElementById('modal-ask').value);
  const age     = parseFloat(document.getElementById('modal-age').value || '2.0');
  const horizon = parseInt(document.getElementById('modal-horizon').value) || 30;

  if (!qty || qty < 1)           { showToast('Enter quantity', 'error'); return; }
  if (isNaN(bid) || isNaN(ask))  { showToast('Enter bid/ask quote', 'error'); return; }
  if (ask <= bid)                { showToast('Ask must be > bid', 'error'); return; }

  const action = (o.card.decision?.action || '').toUpperCase();
  const side   = (action.includes('EXIT') || action.includes('TRIM')) ? 'sell' : 'buy';

  const order = {
    symbol: o.symbol, side, qty,
    quote: { bid, ask, last: o.card.indicators?.close || bid, age_sec: age },
    urgency, horizon_min: horizon,
  };

  const btn = document.getElementById('build-plan-btn');
  btn.textContent = 'BUILDING...';
  btn.disabled = true;
  const plan = await post('/api/plan', order);
  btn.textContent = 'BUILD PLAN';
  btn.disabled = false;

  if (plan.error) { showToast(plan.error, 'error'); return; }
  state.planResult = plan;
  renderPlanOutput(plan);

  const blocked = plan.status === 'BLOCKED';
  const confirmBtn = document.getElementById('confirm-trade-btn');
  confirmBtn.disabled = blocked;
  confirmBtn.textContent = blocked ? 'BLOCKED — CHECK WARNINGS' : 'CONFIRM TRADE';
}

function renderPlanOutput(plan) {
  const el = document.getElementById('plan-output');
  el.textContent = '';

  const q  = plan.quote    || {};
  const pr = plan.pricing  || {};
  const sl = plan.slicing  || {};

  // Status line
  const statusLine = makeEl('div', 'plan-status');
  statusLine.textContent = `● ${plan.status}`;
  statusLine.style.color = plan.status === 'BLOCKED' ? '#ff4444' : '#00ff41';
  el.appendChild(statusLine);

  // Key-value rows — all values via textContent
  const kvs = [
    ['Side',     `${(plan.side || '').toUpperCase()} ${plan.qty} ${plan.symbol}`],
    ['Quote',    `${q.bid?.toFixed(2)} / ${q.ask?.toFixed(2)} · spread ${q.spread_bps} bps · age ${q.age_sec}s`],
    ['Limit',    `$${pr.limit?.toFixed(2)} · ${pr.style}`],
    ['Est. cost',`≤${plan.est_all_in_cost_bps} bps all-in`],
    ['Algo',     `${sl.algo} · ${sl.n_slices} slice(s) / ${sl.horizon_min}min`],
  ];
  kvs.forEach(([k, v]) => {
    const row = makeEl('div', 'plan-row');
    row.appendChild(makeEl('span', 'dim-text', k));
    row.appendChild(makeEl('span', null, ` ${v}`));
    el.appendChild(row);
  });

  // Slices table — all numeric data
  const sliceTable = buildTable(
    ['#', 'Qty', 'Limit', 'Time'],
    (sl.slices || []).map(s => [`#${s.slice}`, `${s.qty} sh`, `$${s.limit}`, `t+${s.at_min}min`])
  );
  el.appendChild(sliceTable);

  // Checks table — status + detail are safe strings from our Python scripts
  const checkTable = buildTable(
    ['Check', 'Status', 'Detail'],
    (plan.checks || []).map(c => {
      const icon = { PASS: '✓', WARN: '⚠', BLOCK: '✗', SKIP: '·' }[c.status];
      return [c.name, `${icon} ${c.status}`, c.detail];
    }),
    (tr, row, i) => {
      const statusCell = tr.cells[1];
      const cls = { PASS: '#00ff41', WARN: '#f59e0b', BLOCK: '#ff4444', SKIP: '#555' };
      statusCell.style.color = cls[(plan.checks || [])[i]?.status] || '#888';
    }
  );
  checkTable.style.marginTop = '8px';
  el.appendChild(checkTable);
}

function buildTable(headers, rows, rowCallback) {
  const table = document.createElement('table');
  table.className = 'plan-table';
  const thead = table.createTHead();
  const hrow  = thead.insertRow();
  headers.forEach(h => { const th = document.createElement('th'); th.textContent = h; hrow.appendChild(th); });
  const tbody = table.createTBody();
  rows.forEach((cells, i) => {
    const tr = tbody.insertRow();
    cells.forEach(c => { tr.insertCell().textContent = c; });
    if (rowCallback) rowCallback(tr, cells, i);
  });
  return table;
}

function confirmTrade() {
  if (!state.planResult || state.planResult.status === 'BLOCKED') return;
  const p = state.planResult;
  // Only safe numeric/fixed strings embedded — esc() as defence-in-depth
  appendSystemMessage(
    `<strong style="color:#00ff41">ORDER CONFIRMED (demo)</strong><br>
     ${esc(p.side?.toUpperCase())} ${esc(String(p.qty))} ${esc(p.symbol)}
     · limit $${esc(p.pricing?.limit?.toFixed(2))} · ${esc(sl(p)?.algo)}<br>
     <span class="dim-text">In production: submit via Robinhood MCP
     <code>review_equity_order</code> → <code>place_equity_order</code>
     after explicit user approval.</span>`
  );
  closeModal();
  showToast(`Trade confirmed: ${p.side?.toUpperCase()} ${p.qty} ${p.symbol}`, 'success');
}
function sl(p) { return p.slicing || {}; }

// ── Backtest Tab ─────────────────────────────────────────────

async function runBacktest() {
  const closesRaw = document.getElementById('bt-closes').value.trim();
  if (!closesRaw) { showToast('Paste close prices for backtest', 'error'); return; }

  let close;
  try { close = JSON.parse(closesRaw); }
  catch { close = closesRaw.split(',').map(s => parseFloat(s.trim())).filter(n => !isNaN(n)); }
  if (!Array.isArray(close) || close.length < 250) {
    showToast('Backtest needs ≥250 bars (ideally 400+)', 'error'); return;
  }

  const btn = document.getElementById('bt-btn');
  btn.textContent = 'RUNNING...';
  btn.disabled = true;

  const payload = {
    close,
    lag:         parseInt(document.getElementById('bt-lag').value)    || 1,
    cost_bps:    parseFloat(document.getElementById('bt-cost').value)  || 5,
    warmup:      parseInt(document.getElementById('bt-warmup').value)  || 220,
    splits:      parseInt(document.getElementById('bt-splits').value)  || 0,
    sensitivity: document.getElementById('bt-sensitivity').checked,
    no_stop:     document.getElementById('bt-nostop').checked,
  };

  const res = await post('/api/backtest', payload);
  btn.textContent = 'RUN BACKTEST';
  btn.disabled = false;

  if (res.error) { showToast(res.error, 'error'); return; }
  renderBacktestResults(res);
}

function renderBacktestResults(res) {
  const el = document.getElementById('bt-results');
  el.textContent = '';

  const m = res.metrics || {};
  const c = res.config  || {};

  // Header
  const hdr = makeEl('div', 'bt-header-row');
  hdr.appendChild(makeEl('div', 'bt-period',
    `${res.period?.start || ''} → ${res.period?.end || ''}  ·  ${res.bars_tested} bars`));
  hdr.appendChild(makeEl('div', 'dim-text',
    `lag=${c.lag}b · ${c.cost_bps}bps/side · stop ${c.atr_stop ? 'ON' : 'OFF'}`));
  el.appendChild(hdr);

  // Metrics grid
  const grid = makeEl('div', 'bt-metrics-grid');
  const retPct = m.total_return_pct;
  const bh     = res.buy_hold_return_pct;
  const rows = [
    ['Total Return', `${retPct > 0 ? '+' : ''}${retPct}% vs B&H ${bh > 0 ? '+' : ''}${bh}%`, retPct > 0 ? '#00ff41' : '#ff4444'],
    ['CAGR',         m.cagr_pct != null ? `${m.cagr_pct > 0 ? '+' : ''}${m.cagr_pct}%` : '—', null],
    ['Sharpe',       m.sharpe, m.sharpe > 1 ? '#00ff41' : m.sharpe > 0 ? '#f59e0b' : '#ff4444'],
    ['Sortino',      m.sortino, null],
    ['Max Drawdown', `${m.max_drawdown_pct}%`, '#ff4444'],
    ['Trades',       m.n_trades, null],
    ['Win Rate',     m.win_rate_pct != null ? `${m.win_rate_pct}%` : '—', null],
    ['Profit Factor',m.profit_factor, null],
    ['Avg Trade',    m.avg_trade_pct != null ? `${m.avg_trade_pct > 0 ? '+' : ''}${m.avg_trade_pct}%` : '—', null],
    ['Avg Hold',     m.avg_bars_held != null ? `${m.avg_bars_held} bars` : '—', null],
    ['Exposure',     m.exposure_pct != null ? `${m.exposure_pct}%` : '—', null],
  ];
  rows.forEach(([k, v, color]) => {
    const row = makeEl('div', 'bt-metric');
    row.appendChild(makeEl('span', 'dim-text', k));
    const vspan = makeEl('span', null, v != null ? String(v) : '—');
    if (color) vspan.style.color = color;
    row.appendChild(vspan);
    grid.appendChild(row);
  });
  el.appendChild(grid);

  // Walk-forward
  if (res.walk_forward?.length) {
    el.appendChild(makeEl('div', 'bt-section-title', 'WALK-FORWARD SEGMENTS'));
    el.appendChild(buildTable(
      ['#', 'Period', 'Return', 'B&H', 'Sharpe', 'Max DD', 'Trades'],
      res.walk_forward.map(s => {
        const mm = s.metrics || {};
        return [
          `#${s.segment}`,
          `${s.period?.start || ''}→${s.period?.end || ''}`,
          `${mm.total_return_pct > 0 ? '+' : ''}${mm.total_return_pct}%`,
          `${s.buy_hold_return_pct > 0 ? '+' : ''}${s.buy_hold_return_pct}%`,
          String(mm.sharpe), `${mm.max_drawdown_pct}%`, String(mm.n_trades),
        ];
      }),
      (tr, _, i) => {
        const mm = res.walk_forward[i].metrics;
        tr.cells[2].style.color = mm.total_return_pct > 0 ? '#00ff41' : '#ff4444';
        tr.cells[3].style.color = '#666';
      }
    ));
  }

  // Sensitivity
  if (res.sensitivity?.length) {
    const st = makeEl('div', 'bt-section-title', 'SENSITIVITY — LAG × COST');
    st.style.marginTop = '16px';
    el.appendChild(st);
    el.appendChild(buildTable(
      ['Lag', 'Cost', 'Return', 'Sharpe', 'Max DD', 'Trades'],
      res.sensitivity.map(g => [
        `lag=${g.lag}`, `${g.cost_bps} bps`,
        `${g.total_return_pct > 0 ? '+' : ''}${g.total_return_pct}%`,
        String(g.sharpe), `${g.max_drawdown_pct}%`, String(g.n_trades),
      ]),
      (tr, _, i) => {
        tr.cells[2].style.color = res.sensitivity[i].total_return_pct > 0 ? '#00ff41' : '#ff4444';
      }
    ));
  }

  // Trade list
  if (res.trades?.length) {
    const st2 = makeEl('div', 'bt-section-title', `TRADES (${Math.min(res.trades.length, 30)} shown)`);
    st2.style.marginTop = '16px';
    el.appendChild(st2);
    el.appendChild(buildTable(
      ['Entry', 'Exit', 'Entry $', 'Exit $', 'Return', 'Hold', 'Reason'],
      res.trades.slice(0, 30).map(t => [
        t.entry_date, t.exit_date,
        `$${t.entry_px?.toFixed(2)}`, `$${t.exit_px?.toFixed(2)}`,
        `${t.return_pct > 0 ? '+' : ''}${t.return_pct}%`,
        `${t.bars_held}b`, t.exit_reason,
      ]),
      (tr, _, i) => {
        tr.cells[4].style.color = res.trades[i].return_pct > 0 ? '#00ff41' : '#ff4444';
        tr.cells[0].style.color = '#666';
        tr.cells[1].style.color = '#666';
        tr.cells[6].style.color = '#666';
      }
    ));
  }
}

// ── RiskEngine Tab ───────────────────────────────────────────

async function runVolatility() {
  const closesRaw = document.getElementById('vol-closes').value.trim();
  if (!closesRaw) { showToast('Paste close prices', 'error'); return; }
  let close;
  try { close = JSON.parse(closesRaw); }
  catch { close = closesRaw.split(',').map(s => parseFloat(s.trim())).filter(n => !isNaN(n)); }

  const btn = document.getElementById('vol-btn');
  btn.textContent = 'COMPUTING...';
  btn.disabled = true;
  const res = await post('/api/volatility', { close });
  btn.textContent = 'COMPUTE';
  btn.disabled = false;

  if (res.error) { showToast(res.error, 'error'); return; }
  renderVolResults(res);
}

function renderVolResults(v) {
  const el = document.getElementById('vol-results');
  el.textContent = '';
  const g  = v.garch || {};
  const ar = v.ar1   || {};

  function section(title) {
    const d = makeEl('div', 'vol-section', title);
    el.appendChild(d);
  }
  function volRow(k, valStr, note) {
    const row = makeEl('div', 'vol-row');
    row.appendChild(makeEl('span', 'vol-key', k));
    row.appendChild(makeEl('span', 'vol-val', valStr ?? '—'));
    if (note) row.appendChild(makeEl('span', 'dim-text vol-note', note));
    el.appendChild(row);
  }

  section('ATR / STOP');
  volRow('ATR-14', v.atr?.toFixed(4), v.atr_pct ? `${v.atr_pct.toFixed(2)}% of price` : '');
  volRow('Chandelier Stop', v.chandelier_stop?.toFixed(4), '3×ATR, 22-bar lookback');

  section('VOLATILITY');
  volRow('EWMA Vol (ann.)', v.ewma_vol_annual ? `${(v.ewma_vol_annual * 100).toFixed(2)}%` : null, 'λ=0.94 RiskMetrics');
  volRow('GARCH Forecast (ann.)', g.forecast_vol_annual ? `${(g.forecast_vol_annual * 100).toFixed(2)}%` : null, 'next-day conditional');
  volRow('GARCH Long-run (ann.)', g.long_run_vol_annual ? `${(g.long_run_vol_annual * 100).toFixed(2)}%` : null);
  volRow('GARCH Persistence', g.persistence?.toFixed(4), g.persistence >= 0.95 ? '⚠ near-integrated' : '');
  volRow('Vol Ratio', g.vol_ratio?.toFixed(3), g.vol_ratio >= 1.3 ? '⚠ vol expanding' : 'ratio to long-run');

  section('MEAN REVERSION');
  volRow('20d Z-score', v.zscore_20 != null ? `${v.zscore_20 > 0 ? '+' : ''}${v.zscore_20.toFixed(3)}σ` : null);
  volRow('AR(1) β', ar.ar1_beta?.toFixed(5), ar.ar1_beta < 0 ? 'mean-reverting' : 'trending');
  volRow('Half-life', ar.half_life_bars ? `${ar.half_life_bars.toFixed(1)} bars` : 'n/a', ar.mean_reverting ? '✓ confirmed' : '');

  section('SIZING');
  volRow('Vol-target Fraction', v.vol_target_fraction != null ? `${(v.vol_target_fraction * 100).toFixed(1)}% of sleeve` : null, 'target 15% ann. vol');
}

// ── Tab Switching ────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  const btn   = document.querySelector(`.tab-btn[data-tab="${name}"]`);
  const panel = document.getElementById(`tab-${name}`);
  if (btn)   btn.classList.add('active');
  if (panel) panel.classList.add('active');
}

// ── Toast ────────────────────────────────────────────────────

function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;        // textContent — no XSS
  el.className = `toast toast-${type} show`;
  setTimeout(() => el.classList.remove('show'), 3500);
}

// ── Init ─────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Tab switching
  document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

  // Scan form
  document.getElementById('scan-btn').addEventListener('click', handleScan);

  // Scanner grid — event delegation for row select and order button
  document.getElementById('scanner-tbody').addEventListener('click', e => {
    const orderBtn = e.target.closest('[data-role="open-order"]');
    if (orderBtn) { openOrderModal(orderBtn.dataset.sym); return; }
    const row = e.target.closest('.scanner-row');
    if (row) {
      const symEl = row.querySelector('.sym-name');
      if (symEl) selectTicker(symEl.textContent);
    }
  });

  // Analyst chat
  const ainput = document.getElementById('analyst-input');
  const doQuery = () => {
    const v = ainput.value.trim();
    if (!v) return;
    appendUserMessage(v);
    ainput.value = '';
    const card = state.activeTicker ? state.results[state.activeTicker] : null;
    if (card) {
      setTimeout(() => {
        const score = card.pillar_total;
        const scoreStr = esc(score != null ? (score > 0 ? '+' : '') + score : '?');
        const action  = esc(card.decision?.action || '?');
        const framing = esc(card.decision?.framing || '');
        const sym     = esc(state.activeTicker);
        appendSystemMessage(
          `Based on the current ${sym} scorecard (${scoreStr}/±6), the recommended action is: <strong>${action}</strong>. ${framing}`
        );
      }, 400);
    }
  };
  document.getElementById('analyst-send').addEventListener('click', doQuery);
  ainput.addEventListener('keydown', e => { if (e.key === 'Enter') doQuery(); });

  // Backtest
  document.getElementById('bt-btn').addEventListener('click', runBacktest);

  // Risk engine
  document.getElementById('vol-btn').addEventListener('click', runVolatility);

  // Modal
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('cancel-trade-btn').addEventListener('click', closeModal);
  document.getElementById('build-plan-btn').addEventListener('click', buildPlan);
  document.getElementById('confirm-trade-btn').addEventListener('click', confirmTrade);
  document.getElementById('approval-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });

  switchTab('portfolio');
});
