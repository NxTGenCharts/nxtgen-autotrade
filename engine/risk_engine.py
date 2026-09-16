"""
RiskEngine — the layer strategies are NOT allowed to bypass.

Every signal from a strategy passes through here before it reaches the broker
adapter. This is also what enforces the "never unlimited martingale" and
"stop all bots on excessive drawdown" requirements.
"""
from models import get_db


class RiskViolation(Exception):
    pass


def get_admin_config(key, default=None, cast=float):
    db = get_db()
    row = db.execute("SELECT value FROM admin_config WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return cast(row["value"])
    except (TypeError, ValueError):
        return default


def check_can_open_position(bot_row, cfg, open_positions_count, current_exposure_usd):
    """Raises RiskViolation if opening another position would break a hard limit."""
    if open_positions_count >= cfg["max_open_positions"]:
        raise RiskViolation(f"Max open positions reached ({cfg['max_open_positions']})")
    if current_exposure_usd >= cfg["max_exposure_usd"]:
        raise RiskViolation(f"Max exposure reached (${cfg['max_exposure_usd']:,.2f})")


def check_drawdown(bot_row, equity_now):
    """Returns True (and the reason) if the bot must be force-stopped."""
    peak = max(bot_row["peak_equity"] or 0, equity_now)
    dd_pct = ((peak - equity_now) / peak * 100) if peak > 0 else 0
    cfg = __import__("json").loads(bot_row["config_json"])
    max_dd = cfg.get("max_drawdown_pct", 20)
    if dd_pct >= max_dd:
        return True, dd_pct, peak
    return False, dd_pct, peak


def check_global_kill_switch(account_info):
    """Global account-level protection: if margin level collapses, or account
    drawdown exceeds the admin-configured ceiling, every bot on the account stops."""
    global_max_dd = get_admin_config("global_max_drawdown_pct", 30)
    if account_info["margin_level_pct"] and 0 < account_info["margin_level_pct"] < 50:
        return True, "Margin level critical (<50%)"
    return False, None
