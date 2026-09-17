# NxTGen AutoTrade

A no-code automated trading platform: **Connect → Choose → Allocate → Start.**
Original product, inspired only in general UX category (not code, design, or
branding) by platforms like Elirox; built standalone so it can later plug into
NxTGen DeCrypt via its API.

**Broker scope (current):** Deriv only, by design. Earlier drafts of this
build sketched MT5-bridge and crypto-exchange (Binance/Bybit) adapters as
future architecture; those have been removed from the shipped product per
the decision to focus on Deriv exclusively for now. The `IBrokerAdapter`
interface (`brokers/base.py`) is still broker-agnostic, so adding a broker
back later is additive, not a rewrite.

This is a **Phase 1** build: it is a real, working application — not a mockup.
Every button, form, and database write is functional, including a fully
real Deriv WebSocket integration (protocol, auth, order placement, closing,
account sync) — see "Deriv integration" below for exactly what's proven and
what this sandbox's network restrictions prevent from being proven further.

---

## 1. Architecture

```
Browser (SPA, vanilla JS)
        │  fetch() JSON over HTTPS
        ▼
Flask API (app.py)  ──────────────┐
        │                          │
        ▼                          ▼
   models.py (SQLite)      auth.py (sessions, PBKDF2 password hashing)
        │
        ▼
engine/bot_manager.py  (background thread, ticks every NXTGEN_TICK_SECONDS)
        │
        ├── engine/risk_engine.py     (hard limits — the ceiling nothing can bypass)
        ├── engine/strategies/*.py    (DCA Trend, GRID Sideways — pluggable)
        └── brokers/*.py              (IBrokerAdapter + Demo/Deriv)
                                              │
                                              ▼
                                     brokers/price_engine.py
                                     (simulated feed for the demo broker)
```

**Everything above the broker adapters is broker-agnostic.** Strategies call
`broker.place_order(...)` / `broker.close_position(...)` and nothing else —
they never touch Deriv's WebSocket protocol directly. That's
the seam that lets you add a new broker without rewriting the engine.

### Why this stack (and where it diverges from the "preferred" one)

The spec's preferred stack (Next.js/NestJS/Postgres/Redis/BullMQ/Docker) is
the right *production* target and is exactly what Phase 2+ should become.
For this first working build, I used **Flask + vanilla JS + SQLite** instead,
for one concrete reason: this build environment has **no outbound network
access**, so `npm install` / `pip install` beyond what's already present
can't run. Flask was already available; a Node/Next.js toolchain and Prisma
were not, and there was no way to fetch them. Swapping the persistence layer
to Postgres later is a small, contained change (see "Scaling beyond Phase 1")
because `models.py` is the only file that touches SQL syntax directly.

### Broker adapter architecture

`brokers/base.py` defines `IBrokerAdapter` — the contract every broker
implements: `connect_account`, `get_account_info/balance/equity/margin/
leverage`, `get_symbols/symbol_info/price`, `get_positions/orders`,
`place_order/modify_order/cancel_order/close_position`, `get_trade_history`.

- **`DemoBrokerAdapter`** (`brokers/demo_broker.py`) — fully implemented.
  Simulates spread, slippage, commission, and margin against a live-feeling
  (but honestly simulated) price feed. This is what makes the platform
  usable end-to-end today.
- **`DerivAdapter`** — the only external broker this build supports (by
  design). Fully implemented against Deriv's real WebSocket protocol, not
  just stubbed — see "Deriv integration" below. With no credentials
  configured, `connect_account()` raises a clear `NotConnectedError` rather
  than faking a connection.

### Demo broker / simulated price engine

`brokers/price_engine.py` runs a calibrated random walk per symbol (own
thread, ticks every `NXTGEN_TICK_SECONDS`), seeded from realistic reference
prices with per-symbol daily-volatility settings. `DemoBrokerAdapter` prices
every fill off this feed with:
- **spread** (bid/ask from each symbol's configured spread),
- **slippage** (small random adverse fill vs. the quote),
- **commission** ($/lot, charged on close),
- **margin** (`notional / leverage`, checked against free margin before
  every fill; orders are rejected, never silently allowed, if margin is
  short).

This can't stream real broker quotes in this sandbox (no network egress).
In production, `PriceEngine` is replaced by a subscription to each
*connected* broker's real quotes via `subscribe_market_data()` on its
adapter — nothing downstream changes, because strategies and the risk
engine only ever call `broker.get_price()`.

### Deriv integration — now really wired (not just a stub)

`brokers/ws_client.py` is a dependency-free RFC 6455 WebSocket client
(handshake, framed send/recv, ping/pong, TLS) written against the stdlib
only — no `websocket-client`/`websockets` package is installed here and
there's no network to `pip install` one. `brokers/deriv_session.py` builds
Deriv's actual protocol on top of it: `authorize`, `balance`,
`active_symbols`, `ticks`, `proposal`/`buy`, `portfolio`, `sell`,
`profit_table`, with `req_id`-based request/response correlation (so
concurrent calls on one connection don't cross streams) and a subscription
registry for streaming pushes. `brokers/deriv_adapter.py` maps all of that
onto `IBrokerAdapter`.

**Product mapping:** DCA Trend / GRID Sideways assume a continuously-held
position with floating P/L that can be closed anytime at market — that's
how CFD-style trading (and `DemoBrokerAdapter`) works. Deriv's classic
Rise/Fall contracts (CALL/PUT) are fixed-duration binary options and don't
fit that model, so `DerivAdapter` trades Deriv **Multipliers**
(`MULTUP`/`MULTDOWN`) instead — a leveraged, continuously-priced position
that supports native `take_profit`/`stop_loss` and can be sold anytime.
"volume" is the USD stake passed to Deriv's `proposal`/`buy` calls; the
same shared bookkeeping module (`brokers/ledger.py`) records every open/close
for both the demo broker and Deriv identically, so the dashboard, bot
detail page, and performance stats don't need to know which adapter placed
a given trade. A fixed default multiplier (50x) is used for now — the real
per-symbol allowed range should be fetched via a `contracts_for` call before
this goes further than Phase 1.

**What's proven end-to-end vs. what isn't, precisely:** this sandbox's
outbound network is allowlisted and `ws.derivws.com` isn't on it — a live
attempt gets `HTTP 403, x-deny-reason: host_not_allowed` at the TCP/TLS
handshake, before any credentials are even sent. So instead of stopping
there, `tests/mock_deriv_server.py` is a protocol-accurate local mock of
Deriv's WebSocket API (also hand-rolled RFC 6455, server side), and the
full product flow is driven against it in
`tests/test_deriv_live_bot.py::test_connect_deriv_create_bot_and_trade_through_the_real_api`:
register → `POST /api/accounts/connect` with `broker_id: "deriv"` (the
exact route the UI calls) → `POST /api/bots` on that Deriv account →
`POST /api/bots/:id/start` → the real `BotManager`/`DcaTrendStrategy` tick
loop places a real order through `DerivAdapter` → the position lands in
`bot_positions` with Deriv's actual `contract_id` attached → closing it
records a trade with the P/L Deriv itself reported (`sold_for - stake`,
not a formula we invented) → the dashboard aggregates it alongside the
built-in demo account with no special-casing → `stop-all` reaches it too.
27 tests pass across the whole app, 13 of them specific to the Deriv path.

The only thing separating this from a live account is network egress and a
real `DERIV_APP_ID`/`DERIV_API_TOKEN` — nothing in the adapter, engine, or
UI needs to change. Try it yourself via the "Connect Deriv" button on the
Accounts page, or `POST /api/accounts/connect
{"broker_id":"deriv","app_id":"...","api_token":"..."}` — in this
environment it fails with that exact `host_not_allowed` reason surfaced
all the way to the UI, never a fake success.

### Real-time updates

`engine/events_bus.py` is an in-process pub/sub (one queue per connected
browser tab, keyed by user id), pushed to over **Server-Sent Events** at
`GET /api/stream?token=...` — a real push channel, not polling. The spec's
target is Socket.IO/WebSockets backed by Redis so multiple API processes can
fan out events; this environment can't install either, so SSE is the
drop-in equivalent for a single process. Every call site (`events_bus.publish
(user_id, channel, payload)`) stays identical if you swap the transport
later. Channels: `account:update`, `bot:update`, `bot:trade`, `bot:log`,
`risk:event` (matching the spec's WebSocket channel list; `bot:position` and
`broker:status`/`notification:new` are reserved but not yet wired). The
frontend keeps a 20s poll purely as a dead-connection safety net — the
dashboard/bot list/bot detail views actually refresh the instant a push
event arrives.

### Trading engine / bot lifecycle

States: `created → starting → running ⇄ paused → stopping → stopped`, plus
`error`, `broker_disconnected`, `risk_stopped`. `engine/bot_manager.py`
runs one loop (a daemon thread in this single-process build) that, every
tick: refreshes price history for every symbol in play → for each
account, checks the **global kill switch** (margin level, account
drawdown) → for each bot, checks its own drawdown ceiling → only then
calls the strategy's `on_tick()`. Strategies **cannot** bypass this order;
they only ever see a broker adapter, never raw account mutation. Because
starting a bot is a DB state transition read fresh on every tick, restarting
the process safely resumes bots without duplicating orders — there is no
"replay a queued order" step to duplicate.

### Risk engine

`engine/risk_engine.py` is the layer every order passes through: max open
positions, max exposure (in $), max drawdown → force-stops the bot and
flattens its positions, and an account-level kill switch (margin level
below 50%, or admin-configured max account drawdown) that stops **every**
bot on that account. DCA's `max_dca_orders` and GRID's `max_open_positions`
are hard integers from the preset table (`engine/presets.py`) — there is no
path in the code that adds a position without first checking `open_count <
max_open_positions`, which is what prevents an unbounded martingale.

### Database schema (ERD)

```
users ──< broker_accounts ──< bots ──< bot_positions
  │                             │
  │                             ├──< trades
  │                             └──< bot_events
  └──< sessions
       audit_logs
       admin_config
       symbols (broker-scoped instrument catalog)
```

Full column definitions are in `models.py::SCHEMA`. Every trade row records
entry/exit price, commission, realized P/L, and close reason — fully
traceable, per your requirement.

### Security

Passwords: PBKDF2-HMAC-SHA256, 200k iterations, random salt (`auth.py`).
Sessions: random 32-byte tokens, server-side expiry, `Bearer` header or
cookie. Role-based access (`user`/`admin`) on every admin route. Live
broker credentials are never accepted by the frontend directly and the
schema has a dedicated `credentials_encrypted` column for them —
encryption-at-rest wiring (Fernet/AES-GCM keyed by `CREDENTIAL_ENCRYPTION_KEY`)
is the next concrete step once a live adapter is actually connected, so
there's no real secret sitting unencrypted anywhere yet. `audit_logs`
records registration, admin config changes, and global stop actions.

### Integration with NxTGen DeCrypt

Nothing in this build modifies `nxtgendecrypt.site`. It's kept integratable
by design: auth is a self-contained module (`auth.py`) that could be swapped
for a shared-SSO call, the DB models are namespaced and don't assume they're
the only schema in the database, the trading engine only depends on
`IBrokerAdapter` + SQLite rows (no hidden global state), and
`NXTGEN_DECRYPT_BASE_URL` / `NXTGEN_DECRYPT_SHARED_AUTH_SECRET` are already
reserved in `.env.example` for the day you want DeCrypt to call into
AutoTrade's API (or vice versa) as a sibling product under one account.

---

## 2. What's real vs. stubbed (honesty section)

| Component | Status |
|---|---|
| Auth, sessions, demo account auto-provisioning | **Real** |
| Demo broker (spread/slippage/commission/margin) | **Real**, simulated feed (disclosed as such) |
| DCA Trend & GRID Sideways strategies | **Real** rules-based logic, capped risk |
| Risk engine (exposure/drawdown/kill switch) | **Real** |
| Bot lifecycle, activity log, trade history, performance stats | **Real** |
| Admin config (demo balance, max drawdown, max bots) | **Real** |
| Deriv adapter (Multipliers) | **Fully wired and proven end-to-end**, including real bot creation + trading, against a protocol-accurate local mock server (27 tests, 13 Deriv-specific). Blocked only by this sandbox's network allowlist — see "Deriv integration" above for the exact failure mode. |
| Backtesting, subscriptions/billing, push notifications, 2FA | **Not built yet** — Phase 5 per your phased plan |

---

## 3. Running it

### Locally
```bash
cd nxtgen-autotrade
pip install -r requirements.txt
cp .env.example .env
python3 app.py            # http://localhost:8090
```

### Tests
```bash
python3 -m unittest discover -s . -p "test_*.py" -v
```
27 tests: 14 app/API/risk tests + 13 Deriv-specific tests (WebSocket
transport/protocol, adapter surface, and a full connect → create bot →
trade integration run against the mock server).

### Docker
```bash
docker compose up --build
```
Note: the image runs with **1** gunicorn worker on purpose — the bot engine
is an in-process thread in Phase 1, and a second worker would tick every bot
twice. See "Scaling beyond Phase 1". `docker-compose.yml` mounts a named
volume at `/app/data` and sets `NXTGEN_DB_PATH` to write the SQLite file
there, so `docker compose down && docker compose up` keeps your data.

### Deploying it at a real URL (no command line)
Push the project to a GitHub repo (drag-and-drop upload works fine, no
`git` needed), then use a dashboard-only host like Render or Railway:
connect the repo, it auto-detects the `Dockerfile`, and you get a live
HTTPS URL in a couple minutes. Point a subdomain's CNAME at it from your
registrar's DNS dashboard to serve it from your own domain
(e.g. `autotrade.yourdomain.com`).

**Persistence matters here:** on a free/ephemeral instance the container's
disk is wiped on every restart or redeploy, which means every registered
user and every bot resets. Set `NXTGEN_DB_PATH` to a path on a **persistent
disk** the host gives you (Render's paid tiers offer one under
Settings → Disks, mounted at e.g. `/data`) — set
`NXTGEN_DB_PATH=/data/nxtgen.db` in that service's environment variables and
the app will create the file there automatically on first boot; nothing
else to configure.

### First admin user
No user is admin by default. Promote one manually once you have a user id:
```bash
python3 -c "from models import get_db, init_db; init_db(); db=get_db(); db.execute(\"UPDATE users SET role='admin' WHERE email=?\", ('you@example.com',)); db.commit()"
```
(On a hosted platform with no shell access, run this once via the host's
one-off "Shell"/"Console" dashboard button if it has one — e.g. Render's
service page has a **Shell** tab — rather than needing your own terminal.)

---

## 4. API summary

`POST /api/auth/register|login|logout`, `GET /api/auth/me` ·
`GET /api/accounts`, `GET /api/accounts/:id`, `GET /api/accounts/:id/symbols`,
`POST /api/accounts/connect` · `GET /api/strategies` ·
`GET|POST /api/bots`, `GET /api/bots/:id`, `POST /api/bots/:id/start|pause|resume|stop`,
`POST /api/bots/stop-all`, `GET /api/bots/:id/performance|trades|positions|events` ·
`GET /api/dashboard` · `GET/POST /api/admin/config`, `GET /api/admin/overview` ·
`GET /health`, `GET /ready`.

All routes except `/api/auth/register|login` and `/health`/`/ready` require
`Authorization: Bearer <token>`.

---

## 5. Scaling beyond Phase 1

1. **Split the worker out.** Move `engine/bot_manager.py`'s loop into its
   own process (`python -m engine.worker_entrypoint`), talking to the API
   tier only through the shared database (or a queue, once one is
   introduced). Then the API tier can run multiple gunicorn workers.
2. **Swap SQLite → PostgreSQL.** `models.py` is the only file with SQL DDL;
   the app code uses `sqlite3.Row` (dict-like) results, which a
   `psycopg2`/`RealDictCursor` swap mirrors closely. `AUTOINCREMENT` →
   `SERIAL`/`GENERATED ALWAYS AS IDENTITY` is the main dialect change.
3. **Add Redis + a queue** once there's a second worker type (e.g. a
   notification worker) that needs to react to events rather than poll.
4. **Deploy somewhere with real network egress** and set
   `DERIV_APP_ID`/`DERIV_API_TOKEN` — the Deriv adapter itself needs no
   further code changes (see "Deriv integration" above).
5. **Encrypt `credentials_encrypted`** with a real KMS-backed key before
   storing a live token anywhere persistent (currently the Deriv session
   only lives in-process memory, never written to disk).

---

## 6. Strategy & risk-management notes

- **DCA Trend**: SMA(8) vs SMA(21) crossover with a minimum-separation
  threshold defines trend direction; entries and DCA adds only fire in that
  direction; every add is capped by `max_dca_orders` and `max_exposure_usd`
  from the risk preset — this is the hard ceiling that rules out an
  unbounded martingale. Combined take-profit/stop-loss is measured off the
  volume-weighted average entry.
- **GRID Sideways**: grid levels are set around price at bot start, spaced
  by `grid_spacing_pct`; each filled level closes independently at its own
  small target (not an averaged exit); **trend escape protection** halts new
  grid entries once SMA separation exceeds `trend_escape_threshold_pct`.
- **Presets** (`conservative`/`balanced`/`aggressive`, `engine/presets.py`)
  are labeled by risk level only — nothing in the UI or API claims a
  specific win rate or guaranteed return, per your no-fake-performance
  requirement.
- Demo trading uses real simulated spread/slippage/commission/margin math;
  win rates and P/L you see are whatever the strategy actually produced
  against the (disclosed-as-simulated) feed — nothing is pre-scripted.
