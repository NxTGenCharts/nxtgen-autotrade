"""
Grid Sideways Bot.

  1. Only operate when the market is judged range-bound (SMA fast/slow close
     together, i.e. no established trend beyond `trend_escape_threshold_pct`).
  2. On (re)start, center a grid of `grid_levels` around the current price,
     spaced by `grid_spacing_pct`.
  3. Buy at levels below center, sell at levels above center. When price
     returns one level in profit, close that specific grid position
     (small, repeatable profit) — this is NOT the same as DCA averaging.
  4. TREND ESCAPE PROTECTION: if the market starts trending strongly, stop
     opening new grid entries; existing positions are still governed by the
     bot's max_drawdown_pct kill switch.
"""
from models import get_db
from engine import price_history
from engine.risk_engine import check_can_open_position, RiskViolation

FAST_N = 8
SLOW_N = 21

# bot_id -> {'center': float, 'levels': [float], 'symbol': str}
_grid_state = {}


def _trend_strength_pct(symbol):
    fast = price_history.sma(symbol, FAST_N)
    slow = price_history.sma(symbol, SLOW_N)
    if fast is None or slow is None or slow == 0:
        return 0
    return abs(fast - slow) / slow * 100


def _open_positions(bot_id):
    db = get_db()
    return db.execute(
        "SELECT * FROM bot_positions WHERE bot_id=? AND status='open' ORDER BY grid_level",
        (bot_id,),
    ).fetchall()


def _init_grid(bot_id, symbol, center, cfg):
    spacing = cfg["grid_spacing_pct"] / 100
    n = cfg["grid_levels"]
    levels = []
    for i in range(1, n // 2 + 1):
        levels.append(center * (1 - spacing * i))  # buy levels below
        levels.append(center * (1 + spacing * i))  # sell levels above
    levels.sort()
    _grid_state[bot_id] = {"center": center, "levels": levels, "symbol": symbol}


def on_tick(bot, broker, account_id, cfg, log):
    symbol = bot["symbol"]
    if not price_history.ready(symbol, SLOW_N):
        return

    quote = broker.get_price(account_id, symbol)
    if not quote:
        return
    info = broker.get_symbol_info(account_id, symbol)
    mid = (quote["bid"] + quote["ask"]) / 2

    state = _grid_state.get(bot["id"])
    if not state or state["symbol"] != symbol:
        _init_grid(bot["id"], symbol, mid, cfg)
        log(bot["id"], "info", f"Grid initialized around {mid:.4f} ({cfg['grid_levels']} levels, {cfg['grid_spacing_pct']}% spacing)")
        state = _grid_state[bot["id"]]

    positions = _open_positions(bot["id"])
    trend_strength = _trend_strength_pct(symbol)
    escaping = trend_strength >= cfg["trend_escape_threshold_pct"]

    # --- manage existing grid positions: close on their target level ---
    tp_pct = cfg["take_profit_pct_per_grid"] / 100
    for p in positions:
        sign = 1 if p["side"] == "buy" else -1
        close_price = quote["bid"] if p["side"] == "buy" else quote["ask"]
        move_pct = (close_price - p["entry_price"]) / p["entry_price"] * sign
        if move_pct >= tp_pct:
            broker.close_position(account_id, p["id"], reason="grid_exit")
            log(bot["id"], "trade", f"Grid level filled: closed {p['side'].upper()} {symbol} @ {close_price:.4f} (+{move_pct*100:.2f}%)")

    if escaping:
        log(bot["id"], "risk", f"Trend escape protection active on {symbol} (strength {trend_strength:.2f}%) — no new grid entries.")
        return

    # --- open new grid entries where price has crossed an unfilled level ---
    account_info = broker.get_account_info(account_id)
    current_exposure = sum(p["margin_used"] for p in positions)
    capital_per_level = cfg["capital"] / max(1, cfg["grid_levels"])

    for level in state["levels"]:
        already_has = any(abs(p["entry_price"] - level) / level < (cfg["grid_spacing_pct"] / 100) * 0.4 for p in positions)
        if already_has:
            continue
        side = "buy" if level < state["center"] else "sell"
        crossed = (side == "buy" and mid <= level) or (side == "sell" and mid >= level)
        if not crossed:
            continue
        volume = max(info["volume_min"], round(capital_per_level / mid / info["contract_size"], 2))
        volume = min(volume, info["volume_max"])
        try:
            check_can_open_position(bot, cfg, len(positions), current_exposure)
            order = broker.place_order(account_id, symbol, side, volume, bot_id=bot["id"])
            db = get_db()
            db.execute("UPDATE bot_positions SET grid_level=? WHERE id=?", (state["levels"].index(level), order["position_id"]))
            db.commit()
            log(bot["id"], "trade", f"Grid entry: {side.upper()} {symbol} {order['volume']} lots @ {order['fill_price']} (level {level:.4f})")
            positions = _open_positions(bot["id"])
            current_exposure = sum(p["margin_used"] for p in positions)
        except RiskViolation as e:
            log(bot["id"], "risk", f"Grid entry blocked: {e}")
