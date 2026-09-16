"""
DCA Trend Bot.

Rules-based, not indicator soup:
  1. Identify trend via SMA(fast) vs SMA(slow) crossover with a minimum separation
     threshold (avoids trading noise around a flat cross).
  2. Enter only in the direction of the identified trend.
  3. Add further DCA entries only if price pulls back against the position by
     `dca_step_pct` AND the trend has not invalidated AND hard caps are not hit.
  4. Every add is capped by max_dca_orders and max_exposure_usd — this is the
     hard ceiling that prevents an unbounded martingale.
  5. Manage a combined take-profit off the average entry price, and a hard
     stop/invalidation distance from the average entry.
"""
from models import get_db
from engine import price_history
from engine.risk_engine import check_can_open_position, RiskViolation

FAST_N = 8
SLOW_N = 21
TREND_MIN_SEPARATION_PCT = 0.02  # SMA fast must lead slow by at least this % to count as a trend


def _trend(symbol):
    fast = price_history.sma(symbol, FAST_N)
    slow = price_history.sma(symbol, SLOW_N)
    if fast is None or slow is None or slow == 0:
        return "unknown"
    sep_pct = (fast - slow) / slow * 100
    if sep_pct >= TREND_MIN_SEPARATION_PCT:
        return "bullish"
    if sep_pct <= -TREND_MIN_SEPARATION_PCT:
        return "bearish"
    return "neutral"


def _open_positions(bot_id, symbol):
    db = get_db()
    return db.execute(
        "SELECT * FROM bot_positions WHERE bot_id=? AND symbol=? AND status='open' ORDER BY opened_at",
        (bot_id, symbol),
    ).fetchall()


def _avg_entry(positions):
    total_vol = sum(p["volume"] for p in positions)
    if total_vol == 0:
        return None, 0
    weighted = sum(p["entry_price"] * p["volume"] for p in positions)
    return weighted / total_vol, total_vol


def on_tick(bot, broker, account_id, cfg, log):
    symbol = bot["symbol"]

    if not price_history.ready(symbol, SLOW_N):
        return  # still warming up trend history

    trend = _trend(symbol)
    positions = _open_positions(bot["id"], symbol)
    quote = broker.get_price(account_id, symbol)
    if not quote:
        return
    info = broker.get_symbol_info(account_id, symbol)

    account_info = broker.get_account_info(account_id)
    current_exposure = sum(p["margin_used"] for p in positions)

    # --- no position yet: look for a fresh entry in the trend direction ---
    if not positions:
        if trend in ("bullish", "bearish"):
            side = "buy" if trend == "bullish" else "sell"
            volume = _size_for_risk(cfg, account_info, quote, info)
            try:
                check_can_open_position(bot, cfg, len(positions), current_exposure)
                order = broker.place_order(account_id, symbol, side, volume, bot_id=bot["id"])
                log(bot["id"], "trade", f"Trend detected: {trend.capitalize()}. Opened {side.upper()} {symbol} {order['volume']} lots @ {order['fill_price']}")
            except RiskViolation as e:
                log(bot["id"], "risk", f"Entry blocked: {e}")
        return

    # --- position(s) open: manage DCA adds, take-profit, stop-loss ---
    avg_entry, total_vol = _avg_entry(positions)
    side = positions[0]["side"]
    sign = 1 if side == "buy" else -1
    current_price = quote["bid"] if side == "buy" else quote["ask"]
    move_pct = (current_price - avg_entry) / avg_entry * 100 * sign

    tp_pct = cfg["take_profit_pct"]
    sl_pct = cfg["stop_loss_pct"]

    if move_pct >= tp_pct:
        for p in positions:
            broker.close_position(account_id, p["id"], reason="take_profit")
        log(bot["id"], "trade", f"Take profit triggered on {symbol} (+{move_pct:.2f}%). All positions closed.")
        return

    if move_pct <= -sl_pct:
        for p in positions:
            broker.close_position(account_id, p["id"], reason="stop_loss")
        log(bot["id"], "risk", f"Stop loss / invalidation hit on {symbol} ({move_pct:.2f}%). All positions closed.")
        return

    # Trend reversal invalidates further DCA even if price hasn't hit SL yet
    reversed_trend = (side == "buy" and trend == "bearish") or (side == "sell" and trend == "bullish")
    if reversed_trend:
        log(bot["id"], "info", f"Trend invalidated for open {side.upper()} on {symbol}; no further DCA adds.")
        return

    # Consider adding a DCA order
    adverse_move_pct = -move_pct  # positive when price has moved against us
    dca_step = cfg["dca_step_pct"]
    already = len(positions)
    if adverse_move_pct >= dca_step * already and already < cfg["max_dca_orders"]:
        volume = _size_for_risk(cfg, account_info, quote, info)
        try:
            check_can_open_position(bot, cfg, already, current_exposure)
            order = broker.place_order(account_id, symbol, side, volume, bot_id=bot["id"])
            log(bot["id"], "trade", f"DCA add #{already+1} on {symbol}: {side.upper()} {order['volume']} lots @ {order['fill_price']}")
        except RiskViolation as e:
            log(bot["id"], "risk", f"DCA add blocked: {e}")
    else:
        log(bot["id"], "info", f"{symbol} DCA condition not satisfied (adverse {adverse_move_pct:.2f}%)")


def _size_for_risk(cfg, account_info, quote, info):
    risk_usd = cfg["capital"] * cfg["risk_per_trade_pct"] / 100
    price = quote["ask"]
    stop_distance_pct = cfg["stop_loss_pct"] / 100
    stop_value_per_lot = price * stop_distance_pct * info["contract_size"]
    if stop_value_per_lot <= 0:
        volume = info["volume_min"]
    else:
        volume = risk_usd / stop_value_per_lot
    volume = max(info["volume_min"], min(info["volume_max"], volume))
    step = info["volume_step"]
    volume = round(round(volume / step) * step, 2)
    return max(volume, info["volume_min"])
