import json
import os
import re
import time

from flask import Flask, request, jsonify, send_from_directory, g, Response, stream_with_context

from models import init_db, get_db
from auth import hash_password, verify_password, create_session, login_required, admin_required, current_token, get_user_from_token
from engine.bot_manager import (
    bot_manager, get_broker, start_bot, pause_bot, resume_bot, stop_bot, stop_all_bots_for_user, log_event
)
from engine.presets import resolve_config, DCA_TREND_PRESETS, GRID_SIDEWAYS_PRESETS
from engine.risk_engine import get_admin_config
from engine import events_bus

app = Flask(__name__, static_folder="static", static_url_path="/static")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STRATEGY_META = {
    "dca_trend": {"label": "DCA Trend", "tag": "Trend", "description": "Designed for directional markets. Trend trading with capped dollar-cost-averaging."},
    "grid_sideways": {"label": "GRID Sideways", "tag": "Sideways", "description": "Designed for ranging markets. Profits from small price movements with trend-escape protection."},
}


def audit(user_id, action, detail=""):
    db = get_db()
    db.execute("INSERT INTO audit_logs (user_id, action, detail) VALUES (?,?,?)", (user_id, action, detail))
    db.commit()


# ---------------------------------------------------------------- auth ----
@app.post("/api/auth/register")
def register():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    name = (data.get("display_name") or email.split("@")[0]).strip()

    if not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return jsonify({"error": "An account with this email already exists"}), 409

    pw_hash, salt = hash_password(password)
    cur = db.execute(
        "INSERT INTO users (email, password_hash, salt, display_name) VALUES (?,?,?,?)",
        (email, pw_hash, salt, name),
    )
    user_id = cur.lastrowid

    demo_balance = get_admin_config("demo_starting_balance", 10000)
    db.execute(
        """INSERT INTO broker_accounts
           (user_id, broker_id, broker_name, account_type, label, balance, equity, leverage, currency, status)
           VALUES (?, 'demo', 'NxTGen Demo', 'demo', 'Demo Account', ?, ?, 500, 'USD', 'connected')""",
        (user_id, demo_balance, demo_balance),
    )
    db.commit()
    audit(user_id, "register", email)

    token = create_session(user_id)
    return jsonify({"token": token, "user": {"id": user_id, "email": email, "display_name": name}})


@app.post("/api/auth/login")
def login():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not verify_password(password, user["salt"], user["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401
    if user["is_suspended"]:
        return jsonify({"error": "This account has been suspended"}), 403
    token = create_session(user["id"])
    return jsonify({"token": token, "user": {"id": user["id"], "email": user["email"], "display_name": user["display_name"], "role": user["role"]}})


@app.post("/api/auth/logout")
@login_required
def logout():
    token = current_token()
    db = get_db()
    db.execute("DELETE FROM sessions WHERE token=?", (token,))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
@login_required
def me():
    u = request.user
    return jsonify({"id": u["id"], "email": u["email"], "display_name": u["display_name"], "role": u["role"]})


# ------------------------------------------------------------ accounts ----
@app.get("/api/accounts")
@login_required
def list_accounts():
    db = get_db()
    rows = db.execute("SELECT * FROM broker_accounts WHERE user_id=? ORDER BY created_at", (request.user["id"],)).fetchall()
    out = []
    for r in rows:
        try:
            broker = get_broker(r["broker_id"])
            info = broker.get_account_info(r["id"])
        except Exception:
            info = {"balance": r["balance"], "equity": r["equity"], "used_margin": r["used_margin"],
                    "free_margin": r["balance"] - r["used_margin"], "margin_level_pct": 0, "unrealized_pl": 0}
        out.append({
            "id": r["id"], "broker_id": r["broker_id"], "broker_name": r["broker_name"],
            "account_type": r["account_type"], "label": r["label"], "leverage": r["leverage"],
            "currency": r["currency"], "status": r["status"], "last_sync_at": r["last_sync_at"],
            **{k: v for k, v in info.items() if k not in ("account_id",)},
        })
    return jsonify(out)


@app.get("/api/accounts/<int:account_id>")
@login_required
def get_account(account_id):
    db = get_db()
    r = db.execute("SELECT * FROM broker_accounts WHERE id=? AND user_id=?", (account_id, request.user["id"])).fetchone()
    if not r:
        return jsonify({"error": "Account not found"}), 404
    broker = get_broker(r["broker_id"])
    return jsonify(broker.get_account_info(account_id))


@app.get("/api/accounts/<int:account_id>/symbols")
@login_required
def account_symbols(account_id):
    db = get_db()
    r = db.execute("SELECT * FROM broker_accounts WHERE id=? AND user_id=?", (account_id, request.user["id"])).fetchone()
    if not r:
        return jsonify({"error": "Account not found"}), 404
    broker = get_broker(r["broker_id"])
    return jsonify(broker.get_symbols(account_id))


@app.post("/api/accounts/connect")
@login_required
def connect_account():
    """Live broker connection. Deriv is the only external broker this build
    supports (by design — see README): it's wired to a real WebSocket
    adapter (brokers/deriv_adapter.py) and actually attempts a connection,
    returning whatever real error comes back (e.g. this environment's
    network allowlist rejecting the handshake, or Deriv rejecting a bad
    token)."""
    data = request.get_json(force=True) or {}
    broker_id = data.get("broker_id")
    if broker_id != "deriv":
        return jsonify({"error": "Only Deriv is supported in this build."}), 400

    from brokers.base import NotConnectedError, BrokerError
    from engine.bot_manager import get_broker
    adapter = get_broker("deriv")  # the persistent singleton — NOT a throwaway instance,
                                    # or the WebSocket session would die when this request ends
    temp_key = f"deriv_pending_{request.user['id']}_{int(time.time()*1000)}"
    try:
        result = adapter.connect_account({
            "app_id": data.get("app_id"),
            "api_token": data.get("api_token"),
            "account_id": temp_key,
            "ws_url": data.get("ws_url"),  # only ever set by internal tests; None in production
        })
    except NotConnectedError as e:
        return jsonify({"error": str(e)}), 502
    except BrokerError as e:
        return jsonify({"error": str(e)}), 400

    acc_info = adapter.get_account_info(temp_key)
    db = get_db()
    cur = db.execute(
        """INSERT INTO broker_accounts
           (user_id, broker_id, broker_name, account_type, label, balance, equity, leverage, currency, status)
           VALUES (?, 'deriv', 'Deriv', ?, ?, ?, ?, ?, ?, 'connected')""",
        (request.user["id"], acc_info["account_type"], acc_info["label"], acc_info["balance"],
         acc_info["equity"], acc_info["leverage"], acc_info["currency"]),
    )
    db.commit()
    new_account_id = cur.lastrowid
    adapter.rekey_session(temp_key, new_account_id)  # from temp key to the real broker_accounts.id
    audit(request.user["id"], "broker_connected", "deriv")
    return jsonify(adapter.get_account_info(new_account_id))


# ----------------------------------------------------------- strategies ---
@app.get("/api/strategies")
@login_required
def strategies():
    out = []
    for key, meta in STRATEGY_META.items():
        presets = DCA_TREND_PRESETS if key == "dca_trend" else GRID_SIDEWAYS_PRESETS
        out.append({"key": key, **meta, "risk_profiles": list(presets.keys())})
    return jsonify(out)


# ----------------------------------------------------------------- bots ---
@app.get("/api/bots")
@login_required
def list_bots():
    db = get_db()
    mode = request.args.get("mode", "demo")
    rows = db.execute(
        """SELECT b.*, a.label as account_label, a.account_type FROM bots b
           JOIN broker_accounts a ON a.id = b.account_id
           WHERE b.user_id=? AND a.account_type=? ORDER BY b.created_at DESC""",
        (request.user["id"], mode),
    ).fetchall()
    out = []
    for b in rows:
        positions = db.execute("SELECT * FROM bot_positions WHERE bot_id=? AND status='open'", (b["id"],)).fetchall()
        trades = db.execute("SELECT * FROM trades WHERE bot_id=?", (b["id"],)).fetchall()
        wins = sum(1 for t in trades if t["pl"] > 0)
        win_rate = round(wins / len(trades) * 100, 1) if trades else 0.0
        unrealized = 0.0
        broker = get_broker("demo")
        for p in positions:
            info = broker.get_symbol_info(b["account_id"], p["symbol"])
            quote = broker.get_price(b["account_id"], p["symbol"])
            if quote:
                close_price = quote["bid"] if p["side"] == "buy" else quote["ask"]
                sign = 1 if p["side"] == "buy" else -1
                unrealized += (close_price - p["entry_price"]) * sign * p["volume"] * info["contract_size"]
        total_pl = b["realized_pl"] + unrealized
        out.append({
            "id": b["id"], "name": b["name"], "strategy": b["strategy"],
            "strategy_label": STRATEGY_META.get(b["strategy"], {}).get("label", b["strategy"]),
            "symbol": b["symbol"], "risk_profile": b["risk_profile"], "capital": b["capital"],
            "state": b["state"], "account_label": b["account_label"], "account_type": b["account_type"],
            "total_pl": round(total_pl, 2),
            "total_pl_pct": round(total_pl / b["capital"] * 100, 2) if b["capital"] else 0,
            "trades": len(trades), "win_rate": win_rate, "open_positions": len(positions),
            "max_drawdown_pct": round(b["max_drawdown_pct"], 2),
            "created_at": b["created_at"], "started_at": b["started_at"],
        })
    return jsonify(out)


@app.post("/api/bots")
@login_required
def create_bot():
    data = request.get_json(force=True) or {}
    account_id = data.get("account_id")
    symbol = data.get("symbol")
    strategy = data.get("strategy")
    risk_profile = data.get("risk_profile", "balanced")
    capital = data.get("capital")
    custom_name = data.get("name")

    if strategy not in STRATEGY_META:
        return jsonify({"error": "Unknown strategy"}), 400
    if risk_profile not in ("conservative", "balanced", "aggressive"):
        return jsonify({"error": "Unknown risk profile"}), 400
    try:
        capital = float(capital)
        assert capital > 0
    except (TypeError, ValueError, AssertionError):
        return jsonify({"error": "Capital must be a positive number"}), 400

    db = get_db()
    account = db.execute("SELECT * FROM broker_accounts WHERE id=? AND user_id=?", (account_id, request.user["id"])).fetchone()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    broker = get_broker(account["broker_id"])
    info = broker.get_account_info(account_id)
    name = custom_name or f"My {account['account_type'].capitalize()} bot"
    if capital > info["free_margin"] + info["used_margin"]:  # rough available-balance check
        return jsonify({"error": f"Capital (${capital:,.2f}) exceeds available balance (${info['balance']:,.2f})"}), 400

    max_bots = get_admin_config("max_bots_per_user", 10, cast=int)
    existing = db.execute("SELECT COUNT(*) c FROM bots WHERE user_id=? AND state IN ('running','paused')", (request.user["id"],)).fetchone()["c"]
    if existing >= max_bots:
        return jsonify({"error": f"Maximum of {max_bots} active bots reached for your plan"}), 400

    cfg = resolve_config(strategy, risk_profile, capital)
    cur = db.execute(
        """INSERT INTO bots (user_id, account_id, name, strategy, symbol, risk_profile, capital, config_json, state, peak_equity)
           VALUES (?,?,?,?,?,?,?,?, 'created', ?)""",
        (request.user["id"], account_id, name, strategy, symbol, risk_profile, capital, json.dumps(cfg), info["equity"]),
    )
    db.commit()
    bot_id = cur.lastrowid
    log_event(bot_id, "info", f"Bot created: {STRATEGY_META[strategy]['label']} on {symbol}, {risk_profile} risk, ${capital:,.2f} capital")
    return jsonify({"id": bot_id, "config": cfg})


@app.get("/api/bots/<int:bot_id>")
@login_required
def get_bot(bot_id):
    db = get_db()
    b = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone()
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    return jsonify(dict(b))


@app.post("/api/bots/<int:bot_id>/start")
@login_required
def api_start_bot(bot_id):
    db = get_db()
    b = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone()
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    account = db.execute("SELECT * FROM broker_accounts WHERE id=?", (b["account_id"],)).fetchone()
    if account["broker_id"] not in ("demo", "deriv"):
        return jsonify({"error": f"{account['broker_id']} trading is not available in this build."}), 501
    start_bot(bot_id)
    bot_manager.start()
    return jsonify({"ok": True})


@app.post("/api/bots/<int:bot_id>/pause")
@login_required
def api_pause_bot(bot_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone():
        return jsonify({"error": "Bot not found"}), 404
    pause_bot(bot_id)
    return jsonify({"ok": True})


@app.post("/api/bots/<int:bot_id>/resume")
@login_required
def api_resume_bot(bot_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone():
        return jsonify({"error": "Bot not found"}), 404
    resume_bot(bot_id)
    return jsonify({"ok": True})


@app.post("/api/bots/<int:bot_id>/stop")
@login_required
def api_stop_bot(bot_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone():
        return jsonify({"error": "Bot not found"}), 404
    stop_bot(bot_id)
    return jsonify({"ok": True})


@app.post("/api/bots/stop-all")
@login_required
def api_stop_all():
    n = stop_all_bots_for_user(request.user["id"])
    audit(request.user["id"], "global_stop", f"{n} bots stopped")
    return jsonify({"stopped": n})


@app.get("/api/bots/<int:bot_id>/performance")
@login_required
def bot_performance(bot_id):
    db = get_db()
    b = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone()
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    trades = db.execute("SELECT * FROM trades WHERE bot_id=? ORDER BY closed_at", (bot_id,)).fetchall()
    wins = [t for t in trades if t["pl"] > 0]
    losses = [t for t in trades if t["pl"] <= 0]
    gross_profit = sum(t["pl"] for t in wins)
    gross_loss = abs(sum(t["pl"] for t in losses))
    equity_curve = []
    running = 0.0
    for t in trades:
        running += t["pl"]
        equity_curve.append({"t": t["closed_at"], "pl": round(running, 2)})
    return jsonify({
        "total_pl": round(b["realized_pl"] + sum(t["pl"] for t in trades) - b["realized_pl"], 2) if False else round(sum(t["pl"] for t in trades), 2),
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (round(gross_profit, 2) if gross_profit else 0),
        "average_win": round(gross_profit / len(wins), 2) if wins else 0,
        "average_loss": round(-gross_loss / len(losses), 2) if losses else 0,
        "largest_win": round(max((t["pl"] for t in wins), default=0), 2),
        "largest_loss": round(min((t["pl"] for t in losses), default=0), 2),
        "max_drawdown_pct": round(b["max_drawdown_pct"], 2),
        "equity_curve": equity_curve,
    })


@app.get("/api/bots/<int:bot_id>/trades")
@login_required
def bot_trades(bot_id):
    db = get_db()
    b = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone()
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    rows = db.execute("SELECT * FROM trades WHERE bot_id=? ORDER BY closed_at DESC LIMIT 200", (bot_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/bots/<int:bot_id>/positions")
@login_required
def bot_positions(bot_id):
    db = get_db()
    b = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone()
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    rows = db.execute("SELECT * FROM bot_positions WHERE bot_id=? AND status='open'", (bot_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/bots/<int:bot_id>/events")
@login_required
def bot_events(bot_id):
    db = get_db()
    b = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, request.user["id"])).fetchone()
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    level = request.args.get("level")
    if level and level != "all":
        rows = db.execute("SELECT * FROM bot_events WHERE bot_id=? AND level=? ORDER BY created_at DESC LIMIT 200", (bot_id, level)).fetchall()
    else:
        rows = db.execute("SELECT * FROM bot_events WHERE bot_id=? ORDER BY created_at DESC LIMIT 200", (bot_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


# ------------------------------------------------------------- dashboard --
@app.get("/api/dashboard")
@login_required
def dashboard():
    db = get_db()
    accounts = db.execute("SELECT * FROM broker_accounts WHERE user_id=?", (request.user["id"],)).fetchall()
    broker = get_broker("demo")
    total_balance = total_equity = total_free_margin = total_used_margin = 0.0
    for a in accounts:
        info = broker.get_account_info(a["id"])
        total_balance += info["balance"]
        total_equity += info["equity"]
        total_free_margin += info["free_margin"]
        total_used_margin += info["used_margin"]

    bots = db.execute("SELECT * FROM bots WHERE user_id=?", (request.user["id"],)).fetchall()
    active_bots = [b for b in bots if b["state"] == "running"]
    all_trades = db.execute(
        """SELECT t.* FROM trades t JOIN bots b ON b.id=t.bot_id WHERE b.user_id=?""",
        (request.user["id"],),
    ).fetchall()
    wins = sum(1 for t in all_trades if t["pl"] > 0)
    today = db.execute(
        """SELECT COALESCE(SUM(t.pl),0) p FROM trades t JOIN bots b ON b.id=t.bot_id
           WHERE b.user_id=? AND date(t.closed_at)=date('now')""",
        (request.user["id"],),
    ).fetchone()["p"]

    return jsonify({
        "total_balance": round(total_balance, 2),
        "equity": round(total_equity, 2),
        "free_margin": round(total_free_margin, 2),
        "used_margin": round(total_used_margin, 2),
        "unrealized_pl": round(total_equity - total_balance, 2),
        "today_pl": round(today, 2),
        "total_pl": round(sum(t["pl"] for t in all_trades), 2),
        "active_bots": len(active_bots),
        "connected_accounts": len(accounts),
        "win_rate": round(wins / len(all_trades) * 100, 1) if all_trades else 0.0,
        "total_trades": len(all_trades),
    })


# ----------------------------------------------------------------- admin --
@app.get("/api/admin/overview")
@admin_required
def admin_overview():
    db = get_db()
    users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    bots = db.execute("SELECT COUNT(*) c FROM bots WHERE state='running'").fetchone()["c"]
    trades = db.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
    accounts = db.execute("SELECT COUNT(*) c FROM broker_accounts").fetchone()["c"]
    return jsonify({"users": users, "active_bots": bots, "total_trades": trades, "broker_accounts": accounts})


@app.get("/api/admin/config")
@admin_required
def admin_get_config():
    db = get_db()
    rows = db.execute("SELECT * FROM admin_config").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.post("/api/admin/config")
@admin_required
def admin_set_config():
    data = request.get_json(force=True) or {}
    db = get_db()
    for k, v in data.items():
        db.execute("INSERT INTO admin_config (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    db.commit()
    audit(request.user["id"], "admin_config_update", json.dumps(data))
    return jsonify({"ok": True})


# --------------------------------------------------------- real-time SSE -
@app.get("/api/stream")
def stream():
    """Server-Sent Events channel: account:update, bot:update, bot:trade,
    bot:log, risk:event. EventSource can't send custom headers, so the
    session token is accepted as a query param here (same session table,
    same expiry as the Bearer-token routes)."""
    user = get_user_from_token(request.args.get("token"))
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    user_id = user["id"]

    def gen():
        q = events_bus.subscribe(user_id)
        try:
            yield "event: ready\ndata: {}\n\n"
            last_ping = time.time()
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield f"event: {ev['channel']}\ndata: {json.dumps(ev['payload'])}\n\n"
                except Exception:
                    pass
                if time.time() - last_ping > 15:
                    yield ": ping\n\n"  # SSE comment keeps proxies/browsers from closing the connection
                    last_ping = time.time()
        finally:
            events_bus.unsubscribe(user_id, q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


# ------------------------------------------------------------- health ----
@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/ready")
def ready():
    try:
        get_db().execute("SELECT 1")
        return jsonify({"status": "ready"})
    except Exception as e:
        return jsonify({"status": "not_ready", "error": str(e)}), 503


# ------------------------------------------------------------ frontend ---
@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory("static", "index.html")


with app.app_context():
    init_db()
    bot_manager.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
