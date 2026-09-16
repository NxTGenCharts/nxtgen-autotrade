const API = "/api";
let state = {
  token: localStorage.getItem("nxtgen_token") || null,
  user: null,
  route: location.hash || "#/dashboard",
  mode: "demo",
  accounts: [],
  symbolsByAccount: {},
  strategies: [],
  pollTimer: null,
};

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
function money(n) { n = Number(n || 0); const s = n < 0 ? "-" : "+"; return `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`; }
function pct(n) { n = Number(n || 0); return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`; }
function plClass(n) { return Number(n) >= 0 ? "pos" : "neg"; }

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const res = await fetch(API + path, Object.assign({}, opts, { headers }));
  let body = null;
  try { body = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((body && body.error) || `Request failed (${res.status})`);
  return body;
}

function setRoute(r) { location.hash = r; }
window.addEventListener("hashchange", () => { state.route = location.hash; render(); });

function logout() {
  disconnectStream();
  state.token = null; state.user = null;
  localStorage.removeItem("nxtgen_token");
  setRoute("#/login"); render();
}

async function boot() {
  if (state.token) {
    try { state.user = await api("/auth/me"); }
    catch (e) { state.token = null; localStorage.removeItem("nxtgen_token"); }
  }
  if (!state.token && !["#/login", "#/register", "#/"].includes(state.route)) {
    state.route = "#/";
  }
  render();
  if (state.token) connectStream();
}

// --------------------------------------------------------- real-time SSE
let sse = null;
function connectStream() {
  if (sse) return;
  try {
    sse = new EventSource(`${API}/stream?token=${encodeURIComponent(state.token)}`);
  } catch (e) { return; }
  const onEvent = () => {
    // Live push received — refresh whatever's currently on screen rather than
    // guessing which piece changed. Cheap: these are small JSON GETs.
    const route = state.route.replace("#/", "");
    const main = document.getElementById("main");
    if (!main) return;
    if (route.startsWith("dashboard") || route === "") renderDashboard(main).catch(() => {});
    else if (route.startsWith("bots/")) renderBotDetail(main, route.split("/")[1]).catch(() => {});
    else if (route.startsWith("bots")) renderBots(main).catch(() => {});
    else if (route.startsWith("accounts")) renderAccounts(main).catch(() => {});
  };
  ["account:update", "bot:update", "bot:trade", "bot:log", "risk:event"].forEach(ch => sse.addEventListener(ch, onEvent));
  sse.onerror = () => { sse.close(); sse = null; setTimeout(() => { if (state.token) connectStream(); }, 4000); };
}
function disconnectStream() { if (sse) { sse.close(); sse = null; } }

// ---------------------------------------------------------------- shell --
function render() {
  const app = document.getElementById("app");
  app.innerHTML = "";
  if (!state.token) {
    if (state.route === "#/login") return app.appendChild(renderAuth("login"));
    if (state.route === "#/register") return app.appendChild(renderAuth("register"));
    return app.appendChild(renderLanding());
  }
  app.appendChild(renderShell());
}

function renderLanding() {
  const wrap = el(`<div class="landing">
    <div class="landing-nav">
      <div class="brand"><div class="mark">N</div><div class="name">NxTGen<br><small>AutoTrade</small></div></div>
      <div style="display:flex;gap:10px">
        <button class="btn" id="nav-login">Log In</button>
        <button class="btn primary" id="nav-signup">Start Trading Free</button>
      </div>
    </div>
    <div class="landing-hero">
      <h1>Automate Your Trading.<br>Your Strategy. Your Accounts.</h1>
      <p>Connect your broker, choose a strategy, allocate capital and let NxTGen automate the execution — with hard risk limits built in from day one. Start with a free realistic demo, no broker required.</p>
      <div class="landing-cta">
        <button class="btn primary" id="hero-signup">Start Trading Free</button>
        <button class="btn" id="hero-demo">Explore Demo</button>
      </div>
    </div>
    <div class="grid cols-3" style="padding:0 48px 60px;max-width:900px">
      <div class="card"><b>Connect → Choose → Allocate → Start</b><p class="tiny" style="margin-top:8px">A beginner can launch a bot in under a minute. Simple Mode hides the complexity; Advanced Mode exposes every parameter.</p></div>
      <div class="card"><b>Real risk controls</b><p class="tiny" style="margin-top:8px">Per-bot exposure caps, drawdown kill switches, and a global emergency stop across every bot on your account.</p></div>
      <div class="card"><b>Honest demo trading</b><p class="tiny" style="margin-top:8px">Simulated spread, slippage, commission and margin — no inflated win rates, ever.</p></div>
    </div>
  </div>`);
  wrap.querySelector("#nav-login").onclick = () => setRoute("#/login");
  wrap.querySelector("#nav-signup").onclick = () => setRoute("#/register");
  wrap.querySelector("#hero-signup").onclick = () => setRoute("#/register");
  wrap.querySelector("#hero-demo").onclick = () => setRoute("#/register");
  return wrap;
}

function renderAuth(mode) {
  const isLogin = mode === "login";
  const wrap = el(`<div class="auth-wrap"><div class="auth-card">
    <h2>${isLogin ? "Welcome back" : "Create your account"}</h2>
    <p class="sub">${isLogin ? "Log in to NxTGen AutoTrade" : "You'll get a free $10,000 demo account instantly."}</p>
    <div id="auth-error"></div>
    <form id="auth-form">
      ${isLogin ? "" : `<div class="field"><label>Display name</label><input name="display_name" placeholder="Optional"></div>`}
      <div class="field"><label>Email</label><input name="email" type="email" required></div>
      <div class="field"><label>Password</label><input name="password" type="password" required minlength="8"></div>
      <button class="btn primary" style="width:100%;padding:11px" type="submit">${isLogin ? "Log In" : "Create Free Account"}</button>
    </form>
    <div class="switch-link">${isLogin ? `New here? <a href="#/register">Create an account</a>` : `Already have an account? <a href="#/login">Log in</a>`}</div>
  </div></div>`);
  wrap.querySelector("#auth-form").onsubmit = async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target).entries());
    try {
      const res = await api(isLogin ? "/auth/login" : "/auth/register", { method: "POST", body: JSON.stringify(data) });
      state.token = res.token; state.user = res.user;
      localStorage.setItem("nxtgen_token", res.token);
      connectStream();
      setRoute("#/dashboard");
    } catch (err) {
      wrap.querySelector("#auth-error").innerHTML = `<div class="error-box">${err.message}</div>`;
    }
  };
  return wrap;
}

function renderShell() {
  const root = el(`<div style="display:flex;width:100%">
    <div class="sidebar">
      <div class="brand"><div class="mark">N</div><div class="name">NxTGen<br><small>AutoTrade</small></div></div>
      ${navItem("#/dashboard", "Dashboard")}
      ${navItem("#/accounts", "Accounts")}
      ${navItem("#/bots", "My Bots")}
      ${navItem("#/history", "History")}
      ${navItem("#/settings", "Settings")}
      ${state.user && state.user.role === "admin" ? navItem("#/admin", "Admin") : ""}
      <div class="nav-spacer"></div>
      <div class="nav-foot">${state.user ? state.user.email : ""}<br><a href="#" id="logout-link">Log out</a></div>
    </div>
    <div class="main" id="main"></div>
  </div>`);
  root.querySelector("#logout-link").onclick = (e) => { e.preventDefault(); logout(); };
  const main = root.querySelector("#main");
  const route = state.route.replace("#/", "");
  if (route.startsWith("dashboard")) renderDashboard(main);
  else if (route.startsWith("accounts")) renderAccounts(main);
  else if (route.startsWith("bots/")) renderBotDetail(main, route.split("/")[1]);
  else if (route.startsWith("bots")) renderBots(main);
  else if (route.startsWith("history")) renderHistory(main);
  else if (route.startsWith("settings")) renderSettings(main);
  else if (route.startsWith("admin")) renderAdmin(main);
  else renderDashboard(main);
  return root;
}

function navItem(route, label) {
  const active = state.route.startsWith(route) || (route === "#/dashboard" && state.route === "#/");
  return `<a class="nav-item ${active ? "active" : ""}" href="${route}">${label}</a>`;
}

// ------------------------------------------------------------ dashboard --
async function renderDashboard(main) {
  main.innerHTML = `<div class="topbar"><h1>Dashboard</h1></div><div id="dash-body">Loading…</div>`;
  try {
    const [d, bots] = await Promise.all([api("/dashboard"), api("/bots?mode=demo")]);
    const body = main.querySelector("#dash-body");
    body.innerHTML = `
      <div class="grid cols-4">
        ${statCard("Total Balance", money(d.total_balance))}
        ${statCard("Equity", money(d.equity))}
        ${statCard("Free Margin", money(d.free_margin))}
        ${statCard("Used Margin", money(d.used_margin))}
        ${statCard("Unrealized P/L", money(d.unrealized_pl), d.unrealized_pl)}
        ${statCard("Today's P/L", money(d.today_pl), d.today_pl)}
        ${statCard("Total P/L", money(d.total_pl), d.total_pl)}
        ${statCard("Active Bots", d.active_bots)}
        ${statCard("Connected Accounts", d.connected_accounts)}
        ${statCard("Win Rate", d.win_rate + "%")}
        ${statCard("Total Trades", d.total_trades)}
      </div>
      <div class="section-title">Active Bots</div>
      <div id="dash-bots"></div>
    `;
    const list = body.querySelector("#dash-bots");
    const active = bots.filter(b => b.state === "running" || b.state === "paused");
    if (!active.length) {
      list.innerHTML = `<div class="empty-state">No active bots yet. <a href="#/bots" style="color:var(--accent2)">Create your first bot</a> to get started.</div>`;
    } else {
      active.forEach(b => list.appendChild(botCard(b)));
    }
  } catch (e) {
    main.querySelector("#dash-body").innerHTML = `<div class="error-box">${e.message}</div>`;
  }
}

function statCard(label, value, plValue) {
  const cls = plValue !== undefined ? plClass(plValue) : "";
  return `<div class="card"><div class="stat-label">${label}</div><div class="stat-value ${cls}">${value}</div></div>`;
}

function botCard(b) {
  const card = el(`<div class="bot-card">
    <div class="row">
      <div style="display:flex;gap:8px;align-items:center">
        <b>${b.symbol}</b>
        <span class="tag strategy">${b.strategy === "dca_trend" ? "DCA" : "GRID"}</span>
        <span class="badge-state ${b.state}">${b.state.replace("_"," ")}</span>
      </div>
      <div class="${plClass(b.total_pl)}" style="font-weight:700">${money(b.total_pl)} · ${pct(b.total_pl_pct)}</div>
    </div>
    <div style="margin-top:6px;color:var(--muted);font-size:13px">${b.name} · ${b.account_label} · ${b.risk_profile}</div>
    <div class="metrics">
      <div class="metric"><div class="l">Capital</div><div class="v">${money(b.capital)}</div></div>
      <div class="metric"><div class="l">Trades</div><div class="v">${b.trades}</div></div>
      <div class="metric"><div class="l">Win Rate</div><div class="v">${b.win_rate}%</div></div>
      <div class="metric"><div class="l">Drawdown</div><div class="v">${b.max_drawdown_pct}%</div></div>
      <div class="metric"><div class="l">Open Positions</div><div class="v">${b.open_positions}</div></div>
    </div>
    <div class="btn-row">
      <button class="btn small" data-act="view">View</button>
      ${b.state === "running" ? `<button class="btn small" data-act="pause">Pause</button>` : ""}
      ${b.state === "paused" ? `<button class="btn small" data-act="resume">Resume</button>` : ""}
      ${b.state === "created" || b.state === "stopped" ? `<button class="btn small primary" data-act="start">Start</button>` : ""}
      ${["running","paused"].includes(b.state) ? `<button class="btn small danger" data-act="stop">Stop</button>` : ""}
    </div>
  </div>`);
  card.querySelector('[data-act="view"]').onclick = () => setRoute(`#/bots/${b.id}`);
  const bind = (act, fn) => { const btn = card.querySelector(`[data-act="${act}"]`); if (btn) btn.onclick = fn; };
  bind("pause", async () => { await api(`/bots/${b.id}/pause`, { method: "POST" }); render(); });
  bind("resume", async () => { await api(`/bots/${b.id}/resume`, { method: "POST" }); render(); });
  bind("start", async () => { await api(`/bots/${b.id}/start`, { method: "POST" }); render(); });
  bind("stop", async () => { await api(`/bots/${b.id}/stop`, { method: "POST" }); render(); });
  return card;
}

// -------------------------------------------------------------- accounts -
async function renderAccounts(main) {
  main.innerHTML = `<div class="topbar"><h1>Accounts</h1></div><div id="acc-body">Loading…</div>`;
  const accounts = await api("/accounts");
  const body = main.querySelector("#acc-body");
  body.innerHTML = "";
  accounts.forEach(a => {
    body.appendChild(el(`<div class="acct-card">
      <div class="acct-head">
        <div>
          <div class="label"><span class="dot-status"></span>${a.label}</div>
          <div class="sub">${a.broker_name} · ${a.account_type.toUpperCase()} · Leverage 1:${a.leverage}</div>
        </div>
        <div style="text-align:right">
          <div class="stat-value">${money(a.balance)}</div>
          <div class="tiny">Equity ${money(a.equity)}</div>
        </div>
      </div>
      <div class="metrics">
        <div class="metric"><div class="l">Free Margin</div><div class="v">${money(a.free_margin)}</div></div>
        <div class="metric"><div class="l">Used Margin</div><div class="v">${money(a.used_margin)}</div></div>
        <div class="metric"><div class="l">Margin Level</div><div class="v">${a.margin_level_pct}%</div></div>
        <div class="metric"><div class="l">Unrealized P/L</div><div class="v ${plClass(a.unrealized_pl)}">${money(a.unrealized_pl)}</div></div>
      </div>
    </div>`));
  });
  const addCard = el(`<div class="acct-card">
    <b>Connect Deriv Account</b>
    <p class="tiny" style="margin:8px 0 14px">Trade your own Deriv demo or real account with a bot, alongside the free built-in demo account above.</p>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn small primary" data-broker="Deriv">Connect Deriv</button>
    </div>
    <div id="connect-msg" style="margin-top:12px"></div>
  </div>`);
  addCard.querySelectorAll("[data-broker]").forEach(btn => {
    btn.onclick = async () => {
      const app_id = prompt("Deriv App ID (register one free at api.deriv.com):");
      if (!app_id) return;
      const api_token = prompt("Deriv API Token (create one in your Deriv account under API Token, with Read + Trade scopes):");
      if (!api_token) return;
      try {
        const res = await api("/accounts/connect", { method: "POST", body: JSON.stringify({ broker_id: "deriv", app_id, api_token }) });
        addCard.querySelector("#connect-msg").innerHTML = `<div class="tiny" style="color:var(--green)">Connected: ${res.label}</div>`;
        renderAccounts(main);
      } catch (e) {
        addCard.querySelector("#connect-msg").innerHTML = `<div class="error-box">${e.message}</div>`;
      }
    };
  });
  body.appendChild(addCard);
}

// ------------------------------------------------------------------ bots -
async function renderBots(main) {
  main.innerHTML = `<div class="topbar"><h1>My Bots</h1>
    <div style="display:flex;gap:10px"><button class="btn danger" id="kill-switch">Stop All Bots</button><button class="btn primary" id="new-bot">+ Create New Bot</button></div>
  </div><div id="bots-body">Loading…</div>`;
  main.querySelector("#kill-switch").onclick = async () => {
    if (!confirm("Stop every running bot on your account?")) return;
    await api("/bots/stop-all", { method: "POST" });
    renderBots(main);
  };
  main.querySelector("#new-bot").onclick = () => openWizard(() => renderBots(main));
  const bots = await api("/bots?mode=demo");
  const body = main.querySelector("#bots-body");
  body.innerHTML = "";
  if (!bots.length) {
    body.innerHTML = `<div class="empty-state">No bots yet. Create your first one — it takes under a minute.</div>`;
    return;
  }
  bots.forEach(b => body.appendChild(botCard(b)));
}

// wizard --------------------------------------------------------------
async function openWizard(onDone) {
  const accounts = await api("/accounts");
  const strategies = await api("/strategies");
  let step = 0;
  const data = { account_id: accounts[0] && accounts[0].id, market: "all", symbol: null, strategy: null, risk_profile: "balanced", capital: 500 };
  let symbols = [];

  const overlay = el(`<div class="wizard-overlay"><div class="wizard">
    <div class="wizard-head"><b>Create New Bot</b><button class="btn small" id="wiz-close">✕</button></div>
    <div class="wizard-steps">${[0,1,2,3,4].map(()=>`<div class="dot"></div>`).join("")}</div>
    <div id="wiz-body"></div>
  </div></div>`);
  overlay.querySelector("#wiz-close").onclick = () => overlay.remove();
  document.body.appendChild(overlay);

  async function loadSymbols() {
    symbols = await api(`/accounts/${data.account_id}/symbols`);
  }

  function updateDots() {
    overlay.querySelectorAll(".dot").forEach((d, i) => d.classList.toggle("done", i <= step));
  }

  async function renderStep() {
    updateDots();
    const body = overlay.querySelector("#wiz-body");
    if (step === 0) {
      body.innerHTML = `<div class="section-title">Step 1 · Select Market</div>`;
      const cats = [["all","All Markets"],["forex","Forex"],["crypto","Crypto"],["commodities","Commodities"],["indices","Indices"],["synthetic","Synthetic Indices"]];
      const catWrap = el(`<div></div>`);
      cats.forEach(([key,label]) => {
        const c = el(`<div class="option-card ${data.market===key?"selected":""}"><div class="icon">◆</div><div class="body"><b>${label}</b></div></div>`);
        c.onclick = () => { data.market = key; renderStep(); };
        catWrap.appendChild(c);
      });
      body.appendChild(catWrap);
      body.appendChild(navButtons(false, true));
    } else if (step === 1) {
      body.innerHTML = `<div class="section-title">Step 2 · Select Asset</div><div id="sym-list">Loading available instruments…</div>`;
      await loadSymbols();
      const filtered = symbols.filter(s => data.market === "all" || s.category === data.market);
      const list = body.querySelector("#sym-list");
      list.innerHTML = "";
      filtered.forEach(s => {
        const c = el(`<div class="option-card ${data.symbol===s.broker_symbol?"selected":""}"><div class="icon">${s.display_name[0]}</div><div class="body"><b>${s.display_name}</b><span>${s.broker_symbol} · ${s.category}</span></div></div>`);
        c.onclick = () => { data.symbol = s.broker_symbol; renderStep(); };
        list.appendChild(c);
      });
      body.appendChild(navButtons(true, !!data.symbol));
    } else if (step === 2) {
      body.innerHTML = `<div class="section-title">Step 3 · Select Strategy</div>`;
      const list = el(`<div></div>`);
      strategies.forEach(s => {
        const c = el(`<div class="option-card ${data.strategy===s.key?"selected":""}"><div class="icon">${s.key==='dca_trend'?"📈":"⚏"}</div><div class="body"><b>${s.label} <span class="tag">${s.tag}</span></b><span>${s.description}</span></div></div>`);
        c.onclick = () => { data.strategy = s.key; renderStep(); };
        list.appendChild(c);
      });
      body.appendChild(list);
      body.appendChild(navButtons(true, !!data.strategy));
    } else if (step === 3) {
      body.innerHTML = `<div class="section-title">Step 4 · Risk Profile</div>`;
      const list = el(`<div></div>`);
      [["conservative","Conservative","Lower risk — smaller size, tighter limits"],
       ["balanced","Balanced","Moderate size and limits"],
       ["aggressive","Aggressive","Higher risk — larger size, wider limits"]].forEach(([key,label,desc]) => {
        const c = el(`<div class="option-card ${data.risk_profile===key?"selected":""}"><div class="icon">⚑</div><div class="body"><b>${label}</b><span>${desc}</span></div></div>`);
        c.onclick = () => { data.risk_profile = key; renderStep(); };
        list.appendChild(c);
      });
      body.appendChild(list);
      body.appendChild(el(`<div class="section-title">Capital</div>`));
      const chipRow = el(`<div class="capital-chip-row"></div>`);
      [100,250,500,1000].forEach(v => {
        const chip = el(`<div class="capital-chip ${data.capital===v?"selected":""}">$${v}</div>`);
        chip.onclick = () => { data.capital = v; renderStep(); };
        chipRow.appendChild(chip);
      });
      const customChip = el(`<div class="capital-chip">Custom</div>`);
      customChip.onclick = () => {
        const v = prompt("Enter custom capital amount ($)");
        if (v && !isNaN(v)) { data.capital = parseFloat(v); renderStep(); }
      };
      chipRow.appendChild(customChip);
      body.appendChild(chipRow);
      body.appendChild(navButtons(true, data.capital > 0));
    } else if (step === 4) {
      const acct = accounts.find(a => a.id === data.account_id);
      body.innerHTML = `<div class="section-title">Step 5 · Review</div>
        <div class="review-row"><span>Broker</span><span>${acct.broker_name}</span></div>
        <div class="review-row"><span>Account</span><span>${acct.label} (${acct.account_type})</span></div>
        <div class="review-row"><span>Symbol</span><span>${data.symbol}</span></div>
        <div class="review-row"><span>Strategy</span><span>${strategies.find(s=>s.key===data.strategy).label}</span></div>
        <div class="review-row"><span>Risk</span><span>${data.risk_profile}</span></div>
        <div class="review-row"><span>Capital</span><span>${money(data.capital)}</span></div>
        <div class="review-row"><span>Leverage</span><span>1:${acct.leverage}</span></div>
        ${acct.account_type === "live" ? `<div class="checkbox-row"><input type="checkbox" id="live-ack"><label for="live-ack">I understand that live trading involves financial risk and this bot will place real orders on my connected broker account.</label></div>` : ""}
        <div id="wiz-error"></div>
        <div class="btn-row">
          <button class="btn" id="back-btn">Back</button>
          <button class="btn primary" id="start-btn" style="flex:1">Start Bot</button>
        </div>`;
      body.querySelector("#back-btn").onclick = () => { step--; renderStep(); };
      body.querySelector("#start-btn").onclick = async () => {
        if (acct.account_type === "live" && !body.querySelector("#live-ack").checked) {
          body.querySelector("#wiz-error").innerHTML = `<div class="error-box">Please confirm you understand the live-trading risk.</div>`;
          return;
        }
        try {
          const created = await api("/bots", { method: "POST", body: JSON.stringify({
            account_id: data.account_id, symbol: data.symbol, strategy: data.strategy,
            risk_profile: data.risk_profile, capital: data.capital,
          }) });
          await api(`/bots/${created.id}/start`, { method: "POST" });
          overlay.remove();
          onDone && onDone();
        } catch (e) {
          body.querySelector("#wiz-error").innerHTML = `<div class="error-box">${e.message}</div>`;
        }
      };
      return;
    }
  }

  function navButtons(showBack, canNext) {
    const row = el(`<div class="btn-row"></div>`);
    if (showBack) { const b = el(`<button class="btn">Back</button>`); b.onclick = () => { step--; renderStep(); }; row.appendChild(b); }
    const n = el(`<button class="btn primary" style="flex:1" ${canNext?"":"disabled"}>Next</button>`);
    if (canNext) n.onclick = () => { step++; renderStep(); };
    else n.style.opacity = 0.5;
    row.appendChild(n);
    return row;
  }

  renderStep();
}

// -------------------------------------------------------------- bot detail
async function renderBotDetail(main, botId) {
  main.innerHTML = `<div class="topbar"><h1>Bot Details</h1><a href="#/bots" class="btn">← Back to My Bots</a></div><div id="bot-detail">Loading…</div>`;
  const container = main.querySelector("#bot-detail");
  const [bots, perf, trades] = await Promise.all([
    api("/bots?mode=demo"), api(`/bots/${botId}/performance`), api(`/bots/${botId}/trades`)
  ]);
  const b = bots.find(x => String(x.id) === String(botId));
  if (!b) { container.innerHTML = `<div class="error-box">Bot not found</div>`; return; }

  container.innerHTML = "";
  container.appendChild(botCard(b));

  const perfCard = el(`<div class="card" style="margin-top:16px">
    <div class="section-title" style="margin-top:0">Performance</div>
    <div class="grid cols-4">
      ${statCard("Total P/L", money(perf.total_pl), perf.total_pl)}
      ${statCard("Win Rate", perf.win_rate + "%")}
      ${statCard("Profit Factor", perf.profit_factor)}
      ${statCard("Max Drawdown", perf.max_drawdown_pct + "%")}
      ${statCard("Total Trades", perf.total_trades)}
      ${statCard("Average Win", money(perf.average_win))}
      ${statCard("Average Loss", money(perf.average_loss))}
      ${statCard("Largest Win", money(perf.largest_win))}
    </div>
  </div>`);
  container.appendChild(perfCard);

  const logCard = el(`<div class="card" style="margin-top:16px">
    <div class="section-title" style="margin-top:0">Activity Log</div>
    <div class="filters">${["all","info","trade","warning","risk","error"].map(l=>`<button data-l="${l}" class="${l==='all'?'active':''}">${l}</button>`).join("")}</div>
    <div id="log-list">Loading…</div>
  </div>`);
  container.appendChild(logCard);

  async function loadLog(level) {
    const events = await api(`/bots/${botId}/events?level=${level}`);
    const list = logCard.querySelector("#log-list");
    list.innerHTML = events.length ? "" : `<div class="tiny">No log entries yet.</div>`;
    events.forEach(ev => {
      list.appendChild(el(`<div class="log-line ${ev.level}"><span class="t">${ev.created_at.slice(11,19)}</span><span>${ev.message}</span></div>`));
    });
  }
  logCard.querySelectorAll(".filters button").forEach(btn => {
    btn.onclick = () => {
      logCard.querySelectorAll(".filters button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      loadLog(btn.dataset.l);
    };
  });
  loadLog("all");

  const tradesCard = el(`<div class="card" style="margin-top:16px">
    <div class="section-title" style="margin-top:0">Trade History</div>
    <div id="trade-list">${trades.length ? "" : '<div class="tiny">No closed trades yet.</div>'}</div>
  </div>`);
  const tlist = tradesCard.querySelector("#trade-list");
  trades.forEach(t => {
    tlist.appendChild(el(`<div class="log-line"><span class="t">${t.closed_at.slice(0,16).replace("T"," ")}</span>
      <span>${t.side.toUpperCase()} ${t.symbol} ${t.volume} lots @ ${t.entry_price} → ${t.exit_price} ·
      <b class="${plClass(t.pl)}">${money(t.pl)}</b> (${t.close_reason})</span></div>`));
  });
  container.appendChild(tradesCard);
}

// ---------------------------------------------------------------- history
async function renderHistory(main) {
  main.innerHTML = `<div class="topbar"><h1>Trading History</h1></div><div id="hist-body">Loading…</div>`;
  const bots = await api("/bots?mode=demo");
  const all = [];
  for (const b of bots) {
    const trades = await api(`/bots/${b.id}/trades`);
    trades.forEach(t => all.push(Object.assign({ bot_name: b.name, symbol_display: b.symbol }, t)));
  }
  all.sort((a, b) => (a.closed_at < b.closed_at ? 1 : -1));
  const body = main.querySelector("#hist-body");
  if (!all.length) { body.innerHTML = `<div class="empty-state">No trades yet.</div>`; return; }
  body.innerHTML = "";
  all.forEach(t => {
    body.appendChild(el(`<div class="log-line"><span class="t">${t.closed_at.slice(0,16).replace("T"," ")}</span>
      <span>${t.bot_name} · ${t.side.toUpperCase()} ${t.symbol} ${t.volume} lots · <b class="${plClass(t.pl)}">${money(t.pl)}</b> (${t.close_reason})</span></div>`));
  });
}

// --------------------------------------------------------------- settings
function renderSettings(main) {
  main.innerHTML = `<div class="topbar"><h1>Settings</h1></div>
    <div class="card">
      <div class="review-row"><span>Email</span><span>${state.user.email}</span></div>
      <div class="review-row"><span>Display name</span><span>${state.user.display_name || "—"}</span></div>
      <div class="review-row"><span>Role</span><span>${state.user.role}</span></div>
    </div>`;
}

// ----------------------------------------------------------------- admin
async function renderAdmin(main) {
  main.innerHTML = `<div class="topbar"><h1>Admin</h1></div><div id="admin-body">Loading…</div>`;
  const [overview, config] = await Promise.all([api("/admin/overview"), api("/admin/config")]);
  const body = main.querySelector("#admin-body");
  body.innerHTML = `
    <div class="grid cols-4">
      ${statCard("Total Users", overview.users)}
      ${statCard("Active Bots", overview.active_bots)}
      ${statCard("Total Trades", overview.total_trades)}
      ${statCard("Broker Accounts", overview.broker_accounts)}
    </div>
    <div class="section-title">Global Configuration</div>
    <div class="card">
      <div class="field"><label>Demo starting balance ($)</label><input id="cfg-demo-balance" value="${config.demo_starting_balance}"></div>
      <div class="field"><label>Global max drawdown (%)</label><input id="cfg-max-dd" value="${config.global_max_drawdown_pct}"></div>
      <div class="field"><label>Max bots per user</label><input id="cfg-max-bots" value="${config.max_bots_per_user}"></div>
      <button class="btn primary" id="save-cfg">Save</button>
      <div id="cfg-msg"></div>
    </div>`;
  body.querySelector("#save-cfg").onclick = async () => {
    try {
      await api("/admin/config", { method: "POST", body: JSON.stringify({
        demo_starting_balance: body.querySelector("#cfg-demo-balance").value,
        global_max_drawdown_pct: body.querySelector("#cfg-max-dd").value,
        max_bots_per_user: body.querySelector("#cfg-max-bots").value,
      }) });
      body.querySelector("#cfg-msg").innerHTML = `<div class="tiny" style="color:var(--green);margin-top:8px">Saved.</div>`;
    } catch (e) {
      body.querySelector("#cfg-msg").innerHTML = `<div class="error-box">${e.message}</div>`;
    }
  };
}

// ---------------------------------------------------- slow fallback poll
// Live updates are pushed via SSE (connectStream). This is just a safety
// net in case a connection drops silently without firing onerror.
setInterval(() => {
  if (!state.token) return;
  const route = state.route.replace("#/", "");
  if (route.startsWith("dashboard") || route === "") renderDashboard(document.getElementById("main")).catch(() => {});
  else if (route.startsWith("bots") && !route.includes("/")) renderBots(document.getElementById("main")).catch(() => {});
}, 20000);

boot();
