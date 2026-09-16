"""
BotManager — the worker loop. In this single-process build it runs as a daemon
thread; the architecture doc (README) describes how this same loop becomes an
independently-scalable worker process (trading-worker) talking to the API via
the job queue in production.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

from models import get_db
from brokers.demo_broker import DemoBrokerAdapter
from brokers.deriv_adapter import DerivAdapter
from engine import price_history
from engine.risk_engine import check_drawdown, check_global_kill_switch
from engine.strategies import dca_trend, grid_sideways
from engine import events_bus

TICK_SECONDS = float(os.environ.get("NXTGEN_TICK_SECONDS", 2.0))

_broker_registry = {"demo": DemoBrokerAdapter(), "deriv": DerivAdapter()}


def get_broker(broker_id):
    b = _broker_registry.get(broker_id)
    if not b:
        raise ValueError(f"No adapter registered for broker '{broker_id}' yet")
    return b


def broker_for_account(account_id):
    """Looks up which live adapter instance owns this account — the seam that
    lets bots on a Deriv account and bots on the built-in demo account run
    side by side under the exact same engine loop."""
    db = get_db()
    row = db.execute("SELECT broker_id FROM broker_accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise ValueError(f"No broker_accounts row for account_id={account_id}")
    return get_broker(row["broker_id"])


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bot_user_id(bot_id):
    db = get_db()
    row = db.execute("SELECT user_id FROM bots WHERE id=?", (bot_id,)).fetchone()
    return row["user_id"] if row else None


def log_event(bot_id, level, message):
    db = get_db()
    db.execute(
        "INSERT INTO bot_events (bot_id, level, message, created_at) VALUES (?,?,?,?)",
        (bot_id, level, message, _now()),
    )
    db.commit()
    user_id = _bot_user_id(bot_id)
    if user_id:
        payload = {"bot_id": bot_id, "level": level, "message": message, "t": _now()}
        events_bus.publish(user_id, "bot:log", payload)
        if level == "trade":
            events_bus.publish(user_id, "bot:trade", payload)
        if level == "risk":
            events_bus.publish(user_id, "risk:event", payload)


def _publish_bot_update(bot_id, state):
    user_id = _bot_user_id(bot_id)
    if user_id:
        events_bus.publish(user_id, "bot:update", {"bot_id": bot_id, "state": state})


STRATEGIES = {
    "dca_trend": dca_trend,
    "grid_sideways": grid_sideways,
}


class BotManager:
    def __init__(self):
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            try:
                self._tick_all()
            except Exception as e:  # worker must never die silently
                print(f"[bot_manager] tick error: {e}")
            time.sleep(TICK_SECONDS)

    def _tick_all(self):
        db = get_db()
        bots = db.execute("SELECT * FROM bots WHERE state='running'").fetchall()

        by_account = {}
        for b in bots:
            by_account.setdefault(b["account_id"], []).append(b)

        for account_id, account_bots in by_account.items():
            try:
                broker = broker_for_account(account_id)
            except ValueError as e:
                for b in account_bots:
                    log_event(b["id"], "error", str(e))
                continue

            seen_symbols = {b["symbol"] for b in account_bots}
            for symbol in seen_symbols:
                try:
                    quote = broker.get_price(account_id, symbol)
                    if quote:
                        mid = quote.get("mid", (quote["bid"] + quote["ask"]) / 2)
                        price_history.push(symbol, mid)
                except Exception as e:
                    for b in account_bots:
                        if b["symbol"] == symbol:
                            log_event(b["id"], "error", f"Price feed error: {e}")

            try:
                account_info = broker.get_account_info(account_id)
            except Exception as e:
                for b in account_bots:
                    log_event(b["id"], "error", f"Account sync error: {e}")
                continue

            triggered, reason = check_global_kill_switch(account_info)
            if triggered:
                for b in account_bots:
                    self._force_stop(b, f"Global kill switch: {reason}")
                continue

            for bot in account_bots:
                self._tick_bot(bot, broker, account_info)

    def _tick_bot(self, bot, broker, account_info):
        cfg = json.loads(bot["config_json"])
        strat = STRATEGIES.get(bot["strategy"])
        if not strat:
            log_event(bot["id"], "error", f"Unknown strategy {bot['strategy']}")
            return

        equity_now = account_info["equity"]
        should_stop, dd_pct, peak = check_drawdown(bot, equity_now)
        db = get_db()
        db.execute(
            "UPDATE bots SET peak_equity=?, max_drawdown_pct=MAX(max_drawdown_pct, ?) WHERE id=?",
            (peak, dd_pct, bot["id"]),
        )
        db.commit()
        if should_stop:
            self._force_stop(bot, f"Bot drawdown limit reached ({dd_pct:.1f}%)")
            return

        try:
            strat.on_tick(bot, broker, bot["account_id"], cfg, log_event)
        except Exception as e:
            log_event(bot["id"], "error", f"Strategy error: {e}")

    def _force_stop(self, bot, reason):
        db = get_db()
        positions = db.execute(
            "SELECT * FROM bot_positions WHERE bot_id=? AND status='open'", (bot["id"],)
        ).fetchall()
        try:
            broker = broker_for_account(bot["account_id"])
        except ValueError:
            broker = None
        for p in positions:
            if broker is None:
                break
            try:
                broker.close_position(bot["account_id"], p["id"], reason="risk_stop")
            except Exception:
                pass
        db.execute(
            "UPDATE bots SET state='risk_stopped', stopped_at=? WHERE id=?",
            (_now(), bot["id"]),
        )
        db.commit()
        log_event(bot["id"], "risk", reason)
        _publish_bot_update(bot["id"], "risk_stopped")


bot_manager = BotManager()


def start_bot(bot_id):
    db = get_db()
    db.execute("UPDATE bots SET state='running', started_at=COALESCE(started_at, ?) WHERE id=?", (_now(), bot_id))
    db.commit()
    log_event(bot_id, "info", "Bot started")
    _publish_bot_update(bot_id, "running")


def pause_bot(bot_id):
    db = get_db()
    db.execute("UPDATE bots SET state='paused' WHERE id=?", (bot_id,))
    db.commit()
    log_event(bot_id, "info", "Bot paused")
    _publish_bot_update(bot_id, "paused")


def resume_bot(bot_id):
    db = get_db()
    db.execute("UPDATE bots SET state='running' WHERE id=?", (bot_id,))
    db.commit()
    log_event(bot_id, "info", "Bot resumed")
    _publish_bot_update(bot_id, "running")


def stop_bot(bot_id, close_positions=True):
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    if close_positions and bot:
        try:
            broker = broker_for_account(bot["account_id"])
            positions = db.execute(
                "SELECT * FROM bot_positions WHERE bot_id=? AND status='open'", (bot_id,)
            ).fetchall()
            for p in positions:
                broker.close_position(bot["account_id"], p["id"], reason="manual")
        except ValueError:
            pass
    db.execute("UPDATE bots SET state='stopped', stopped_at=? WHERE id=?", (_now(), bot_id))
    db.commit()
    log_event(bot_id, "info", "Bot stopped")
    _publish_bot_update(bot_id, "stopped")


def stop_all_bots_for_user(user_id):
    db = get_db()
    bots = db.execute("SELECT id FROM bots WHERE user_id=? AND state='running'", (user_id,)).fetchall()
    for b in bots:
        stop_bot(b["id"])
    return len(bots)
