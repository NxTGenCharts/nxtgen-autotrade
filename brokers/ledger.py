"""
Shared bookkeeping for bot_positions / trades / broker_accounts.

Every adapter (Demo, Deriv, and future ones) calls these instead of writing
SQL inline, so a position opened via any broker shows up identically in the
dashboard, bot detail page, and performance stats — the UI and the bot
engine never need to know which adapter placed a given trade.
"""
from datetime import datetime, timezone
from brokers.base import BrokerError
from models import get_db


def _now():
    return datetime.now(timezone.utc).isoformat()


def open_position(bot_id, account_id, symbol, side, volume, entry_price, margin_used,
                   take_profit=None, stop_loss=None, broker_ref=None, track_account=True):
    """track_account=False skips touching broker_accounts.used_margin — used by
    adapters (like Deriv) whose account_info() derives used_margin/balance live
    from the broker itself rather than from a locally-mirrored balance."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO bot_positions
           (bot_id, symbol, side, volume, entry_price, take_profit, stop_loss,
            margin_used, broker_ref, opened_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'open')""",
        (bot_id, symbol, side, volume, entry_price, take_profit, stop_loss,
         margin_used, broker_ref, _now()),
    )
    if track_account:
        db.execute(
            "UPDATE broker_accounts SET used_margin = used_margin + ?, last_sync_at=? WHERE id=?",
            (margin_used, _now(), account_id),
        )
    db.commit()
    return cur.lastrowid


def get_open_position(position_id):
    db = get_db()
    pos = db.execute("SELECT * FROM bot_positions WHERE id=?", (position_id,)).fetchone()
    if not pos or pos["status"] != "open":
        raise BrokerError("Position not open")
    return pos


def close_position(position_id, account_id, exit_price, commission, pl, pl_pct, reason,
                    update_balance=True, track_account=True):
    """update_balance=False lets an adapter whose broker tracks its own
    authoritative balance (e.g. Deriv, refetched live) skip double-applying
    P/L to broker_accounts.balance. track_account=False additionally skips
    releasing margin_used locally (used together with track_account=False
    on open_position, for the same reason)."""
    db = get_db()
    pos = get_open_position(position_id)

    db.execute("UPDATE bot_positions SET status='closed' WHERE id=?", (position_id,))
    db.execute(
        """INSERT INTO trades
           (bot_id, account_id, symbol, side, volume, entry_price, exit_price,
            commission, pl, pl_pct, opened_at, closed_at, close_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pos["bot_id"], account_id, pos["symbol"], pos["side"], pos["volume"],
         pos["entry_price"], exit_price, commission, pl, pl_pct,
         pos["opened_at"], _now(), reason),
    )
    if track_account:
        if update_balance:
            db.execute(
                "UPDATE broker_accounts SET balance = balance + ?, used_margin = used_margin - ?, last_sync_at=? WHERE id=?",
                (pl, pos["margin_used"], _now(), account_id),
            )
        else:
            db.execute(
                "UPDATE broker_accounts SET used_margin = used_margin - ?, last_sync_at=? WHERE id=?",
                (pos["margin_used"], _now(), account_id),
            )
    db.commit()
    return pos


def open_positions_for_account(account_id):
    db = get_db()
    return db.execute(
        """SELECT bp.* FROM bot_positions bp
           JOIN bots b ON b.id = bp.bot_id
           WHERE b.account_id=? AND bp.status='open'""",
        (account_id,),
    ).fetchall()


def sum_used_margin(account_id):
    return sum(p["margin_used"] for p in open_positions_for_account(account_id))
