import random
import string
from datetime import datetime, timezone

from brokers.base import IBrokerAdapter, BrokerError
from brokers.price_engine import price_engine
from brokers import ledger
from models import get_db
from engine import events_bus

COMMISSION_PER_LOT = 3.0       # flat $ round-turn commission per 1.0 lot, applied on close
SLIPPAGE_MAX_PIPS = 0.6        # worst-case simulated slippage on market execution


def _now():
    return datetime.now(timezone.utc).isoformat()


class DemoBrokerAdapter(IBrokerAdapter):
    broker_id = "demo"
    broker_name = "NxTGen Demo"

    def __init__(self):
        self._loaded_symbols = False

    # --- setup ----------------------------------------------------------
    def _ensure_price_feed(self):
        if self._loaded_symbols:
            return
        db = get_db()
        rows = db.execute("SELECT * FROM symbols WHERE broker_id='demo'").fetchall()
        for r in rows:
            price_engine.register_symbol(
                r["broker_symbol"], r["base_price"], r["daily_vol_pct"], r["digits"], r["spread_pips"]
            )
        price_engine.start()
        self._loaded_symbols = True

    # --- connection -------------------------------------------------
    def connect_account(self, credentials: dict) -> dict:
        # Demo accounts are provisioned at signup; nothing to "connect".
        return {"status": "connected", "broker_id": self.broker_id}

    def disconnect_account(self, account_id: str) -> None:
        db = get_db()
        db.execute("UPDATE broker_accounts SET status='disconnected' WHERE id=?", (account_id,))
        db.commit()

    # --- account state ------------------------------------------------
    def _row(self, account_id):
        db = get_db()
        row = db.execute("SELECT * FROM broker_accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise BrokerError("Account not found")
        return row

    def _publish_account_update(self, account_id):
        try:
            row = self._row(account_id)
            events_bus.publish(row["user_id"], "account:update", self.get_account_info(account_id))
        except Exception:
            pass  # never let a pub/sub hiccup break trading

    def _unrealized_pl(self, account_id):
        self._ensure_price_feed()
        positions = ledger.open_positions_for_account(account_id)
        total = 0.0
        for p in positions:
            info = self.get_symbol_info(account_id, p["symbol"])
            price = self.get_price(account_id, p["symbol"])
            if not price:
                continue
            close_price = price["bid"] if p["side"] == "buy" else price["ask"]
            sign = 1 if p["side"] == "buy" else -1
            total += (close_price - p["entry_price"]) * sign * p["volume"] * info["contract_size"]
        return total

    def get_account_info(self, account_id: str) -> dict:
        row = self._row(account_id)
        upl = self._unrealized_pl(account_id)
        equity = row["balance"] + upl
        used_margin = row["used_margin"]
        free_margin = equity - used_margin
        margin_level = (equity / used_margin * 100) if used_margin > 0 else 0.0
        return {
            "account_id": row["id"],
            "broker_id": row["broker_id"],
            "broker_name": row["broker_name"],
            "account_type": row["account_type"],
            "label": row["label"],
            "balance": round(row["balance"], 2),
            "equity": round(equity, 2),
            "unrealized_pl": round(upl, 2),
            "used_margin": round(used_margin, 2),
            "free_margin": round(free_margin, 2),
            "margin_level_pct": round(margin_level, 2),
            "leverage": row["leverage"],
            "currency": row["currency"],
            "status": row["status"],
        }

    def get_balance(self, account_id: str) -> float:
        return self._row(account_id)["balance"]

    def get_equity(self, account_id: str) -> float:
        return self.get_account_info(account_id)["equity"]

    def get_margin(self, account_id: str) -> dict:
        info = self.get_account_info(account_id)
        return {"used": info["used_margin"], "free": info["free_margin"], "level_pct": info["margin_level_pct"]}

    def get_leverage(self, account_id: str) -> int:
        return self._row(account_id)["leverage"]

    # --- symbols / market data -----------------------------------------
    def get_symbols(self, account_id: str) -> list:
        self._ensure_price_feed()
        db = get_db()
        rows = db.execute("SELECT * FROM symbols WHERE broker_id='demo' ORDER BY category, display_name").fetchall()
        out = []
        for r in rows:
            out.append({
                "broker_symbol": r["broker_symbol"],
                "display_name": r["display_name"],
                "category": r["category"],
                "digits": r["digits"],
            })
        return out

    def get_symbol_info(self, account_id: str, symbol: str) -> dict:
        db = get_db()
        r = db.execute("SELECT * FROM symbols WHERE broker_id='demo' AND broker_symbol=?", (symbol,)).fetchone()
        if not r:
            raise BrokerError(f"Unknown symbol {symbol}")
        return {
            "broker_symbol": r["broker_symbol"],
            "display_name": r["display_name"],
            "category": r["category"],
            "digits": r["digits"],
            "contract_size": r["contract_size"],
            "volume_min": r["volume_min"],
            "volume_max": r["volume_max"],
            "volume_step": r["volume_step"],
            "tick_value": r["tick_value"],
            "spread_pips": r["spread_pips"],
        }

    def get_price(self, account_id: str, symbol: str) -> dict:
        self._ensure_price_feed()
        return price_engine.get_price(symbol)

    # --- trading ------------------------------------------------------
    def get_positions(self, account_id: str) -> list:
        return [dict(r) for r in ledger.open_positions_for_account(account_id)]

    def get_orders(self, account_id: str) -> list:
        return []  # demo executes market orders immediately; no resting order book

    def _apply_slippage(self, price, side, digits):
        slip_pips = random.uniform(0, SLIPPAGE_MAX_PIPS)
        slip = slip_pips * (10 ** -digits)
        return price + slip if side == "buy" else price - slip

    def place_order(self, account_id: str, symbol: str, side: str, volume: float,
                     order_type: str = "market", price: float = None,
                     take_profit: float = None, stop_loss: float = None,
                     client_order_id: str = None, bot_id: int = None) -> dict:
        if bot_id is None:
            raise BrokerError("Demo adapter requires bot_id to attribute the position")

        info = self.get_symbol_info(account_id, symbol)
        quote = self.get_price(account_id, symbol)
        if not quote:
            raise BrokerError(f"No price feed for {symbol}")

        volume = max(info["volume_min"], min(info["volume_max"], round(volume / info["volume_step"]) * info["volume_step"]))
        raw_price = quote["ask"] if side == "buy" else quote["bid"]
        fill_price = round(self._apply_slippage(raw_price, side, info["digits"]), info["digits"])

        notional = fill_price * volume * info["contract_size"]
        leverage = self.get_leverage(account_id)
        margin_required = notional / leverage

        free_margin = self.get_account_info(account_id)["free_margin"]
        if margin_required > free_margin:
            raise BrokerError(
                f"Insufficient free margin: need ${margin_required:,.2f}, have ${free_margin:,.2f}"
            )

        position_id = ledger.open_position(
            bot_id, account_id, symbol, side, volume, fill_price, margin_required,
            take_profit=take_profit, stop_loss=stop_loss,
        )
        self._publish_account_update(account_id)
        return {
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "fill_price": fill_price,
            "margin_used": margin_required,
        }

    def modify_order(self, account_id: str, order_id: str, **kwargs) -> dict:
        db = get_db()
        fields, values = [], []
        for k in ("take_profit", "stop_loss"):
            if k in kwargs:
                fields.append(f"{k}=?")
                values.append(kwargs[k])
        if fields:
            values.append(order_id)
            db.execute(f"UPDATE bot_positions SET {', '.join(fields)} WHERE id=?", values)
            db.commit()
        return {"position_id": order_id, **kwargs}

    def cancel_order(self, account_id: str, order_id: str) -> None:
        pass  # no resting orders in the demo adapter

    def close_position(self, account_id: str, position_id: str, volume: float = None,
                        reason: str = "manual") -> dict:
        pos = ledger.get_open_position(position_id)
        info = self.get_symbol_info(account_id, pos["symbol"])
        quote = self.get_price(account_id, pos["symbol"])
        close_side = "sell" if pos["side"] == "buy" else "buy"
        raw_price = quote["bid"] if pos["side"] == "buy" else quote["ask"]
        exit_price = round(self._apply_slippage(raw_price, close_side, info["digits"]), info["digits"])

        sign = 1 if pos["side"] == "buy" else -1
        gross_pl = (exit_price - pos["entry_price"]) * sign * pos["volume"] * info["contract_size"]
        commission = round(COMMISSION_PER_LOT * pos["volume"], 2)
        net_pl = gross_pl - commission
        pl_pct = (net_pl / pos["margin_used"] * 100) if pos["margin_used"] else 0

        ledger.close_position(position_id, account_id, exit_price, commission, net_pl, pl_pct, reason)
        self._publish_account_update(account_id)
        return {"position_id": position_id, "exit_price": exit_price, "pl": net_pl, "commission": commission}

    def get_trade_history(self, account_id: str, limit: int = 100) -> list:
        db = get_db()
        rows = db.execute(
            "SELECT * FROM trades WHERE account_id=? ORDER BY closed_at DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def new_client_order_id():
    return "cid_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
