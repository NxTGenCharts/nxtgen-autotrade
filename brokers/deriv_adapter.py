"""
DerivAdapter — real implementation against DerivSession (brokers/deriv_session.py),
which speaks Deriv's actual WebSocket protocol (see that file's docstring).

PRODUCT MAPPING NOTE: NxTGen's DCA Trend / GRID Sideways strategies assume a
continuously-held position with floating P/L that can be closed at any time
at the current market price — that's how CFD/forex-style trading (and our
DemoBrokerAdapter) works. Deriv's classic "Rise/Fall" contracts (CALL/PUT)
are NOT that — they're fixed-duration binary options that settle
automatically and can't be actively managed. So this adapter trades Deriv
**Multipliers** instead (MULTUP/MULTDOWN), which behave like a leveraged
CFD position: continuously priced, closable anytime via sell(), and support
native take_profit/stop_loss. "volume" here is the USD stake handed to
Deriv's proposal/buy calls, not a lot size — get_symbol_info() reports
contract_size=1 / tick_value=1 so the strategies' generic position-sizing
math degrades gracefully into "size the stake" rather than "size the lots".
A fixed default multiplier is used for now; production should instead fetch
each symbol's allowed multiplier range via a `contracts_for` call and let
the risk engine pick one.

Honesty note on connectivity: this sandbox's outbound network is an
explicit allowlist and ws.derivws.com is not on it, so connect_account()
here cannot actually reach production Deriv — see the "Deriv integration"
section of README.md for exactly how that fails and how it's verified
instead (11 tests against a protocol-accurate local mock server in
tests/test_deriv_session.py + tests/test_deriv_adapter.py, including a
full bot-creation-and-trading run in tests/test_deriv_live_bot.py).
"""
import os

from brokers.base import IBrokerAdapter, BrokerError, NotConnectedError
from brokers.deriv_session import DerivSession, DerivAPIError
from brokers.ws_client import WebSocketError
from brokers import ledger

DERIV_APP_ID = os.environ.get("DERIV_APP_ID")
DERIV_API_TOKEN = os.environ.get("DERIV_API_TOKEN")

DEFAULT_MULTIPLIER = 50
CONTRACT_DURATION_NONE = None  # Multipliers have no fixed expiry

# Rough decimal-places-per-category heuristic used for display/rounding only
# (Deriv reports the real pip size per symbol via active_symbols in production).
DIGITS_BY_CATEGORY = {"forex": 5, "synthetic_index": 2, "cryptocurrency": 2,
                       "commodities": 2, "indices": 2, "other": 2}


class DerivAdapter(IBrokerAdapter):
    broker_id = "deriv"
    broker_name = "Deriv"

    def __init__(self):
        self._sessions = {}  # account_id (broker_accounts.id, or a temp key pre-insert) -> DerivSession

    def _session_for(self, account_id):
        session = self._sessions.get(account_id)
        if session is None:
            raise NotConnectedError(
                "This Deriv account has no open session. Call connect_account() with a "
                "real API token first."
            )
        return session

    def rekey_session(self, old_key, new_key):
        """Used right after a broker_accounts row is inserted: the session was
        opened under a temporary key (we don't have the DB row id yet at
        connect time), so move it to the real id once we do."""
        session = self._sessions.pop(old_key, None)
        if session is not None:
            self._sessions[new_key] = session

    def _call(self, session, payload, timeout=None):
        try:
            return session.request(payload, timeout=timeout)
        except DerivAPIError as e:
            raise BrokerError(str(e))

    # --- connection -------------------------------------------------
    def connect_account(self, credentials: dict) -> dict:
        app_id = credentials.get("app_id") or DERIV_APP_ID
        token = credentials.get("api_token") or DERIV_API_TOKEN
        account_id = credentials.get("account_id", "default")

        if not app_id or not token:
            raise NotConnectedError(
                "Deriv requires app_id and api_token. Set DERIV_APP_ID / DERIV_API_TOKEN "
                "(or pass them in credentials) and retry."
            )

        session = DerivSession(app_id=app_id, api_token=token, ws_url=credentials.get("ws_url"))
        try:
            session.connect()  # this is the line that needs real network egress
        except (OSError, WebSocketError) as e:
            raise NotConnectedError(
                f"Could not reach Deriv's WebSocket server (wss://ws.derivws.com): {e}. "
                f"This environment's outbound network is restricted to an explicit allowlist "
                f"and ws.derivws.com is not on it, so the handshake is refused before any "
                f"credentials are even sent. Deploy this adapter somewhere with real internet "
                f"access and a valid token to go live — nothing else needs to change."
            )
        except DerivAPIError as e:
            session.close()
            raise BrokerError(f"Deriv rejected authorization: {e}")
        self._sessions[account_id] = session
        acc = session.authorized_account or {}
        return {
            "status": "connected", "broker_id": self.broker_id, "account": acc,
            "account_type": "demo" if acc.get("is_virtual") else "live",
            "label": acc.get("loginid", account_id),
            "balance": acc.get("balance", 0.0),
            "currency": acc.get("currency", "USD"),
        }

    def disconnect_account(self, account_id: str) -> None:
        session = self._sessions.pop(account_id, None)
        if session:
            session.close()

    # --- account state ------------------------------------------------
    def get_account_info(self, account_id: str) -> dict:
        session = self._session_for(account_id)
        bal = self._call(session, {"balance": 1})["balance"]
        acc = session.authorized_account or {}
        balance = bal["balance"]
        used_margin = ledger.sum_used_margin(account_id)  # sum of open Multiplier stakes
        equity = balance  # Deriv reports realized balance; live floating P/L would need
                           # a proposal_open_contract subscription per open position (not
                           # wired up in this build — see README).
        free_margin = equity - used_margin
        margin_level = (equity / used_margin * 100) if used_margin > 0 else 0.0
        return {
            "account_id": account_id,
            "broker_id": self.broker_id,
            "broker_name": self.broker_name,
            "account_type": "demo" if acc.get("is_virtual") else "live",
            "label": acc.get("loginid", str(account_id)),
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "unrealized_pl": 0.0,
            "used_margin": round(used_margin, 2),
            "free_margin": round(free_margin, 2),
            "margin_level_pct": round(margin_level, 2),
            "leverage": DEFAULT_MULTIPLIER,
            "currency": bal.get("currency", acc.get("currency", "USD")),
            "status": "connected",
        }

    def get_balance(self, account_id: str) -> float:
        return self.get_account_info(account_id)["balance"]

    def get_equity(self, account_id: str) -> float:
        return self.get_account_info(account_id)["equity"]

    def get_margin(self, account_id: str) -> dict:
        info = self.get_account_info(account_id)
        return {"used": info["used_margin"], "free": info["free_margin"], "level_pct": info["margin_level_pct"]}

    def get_leverage(self, account_id: str) -> int:
        return DEFAULT_MULTIPLIER

    # --- symbols / market data -----------------------------------------
    def get_symbols(self, account_id: str) -> list:
        session = self._session_for(account_id)
        symbols = self._call(session, {"active_symbols": "brief"})["active_symbols"]
        return [{"broker_symbol": s["symbol"], "display_name": s["display_name"],
                  "category": s.get("market", "other")} for s in symbols]

    def get_symbol_info(self, account_id: str, symbol: str) -> dict:
        session = self._session_for(account_id)
        symbols = self._call(session, {"active_symbols": "full"})["active_symbols"]
        match = next((s for s in symbols if s["symbol"] == symbol), None)
        if not match:
            raise BrokerError(f"Unknown Deriv symbol {symbol}")
        category = match.get("market", "other")
        return {
            "broker_symbol": symbol, "display_name": match.get("display_name", symbol),
            "category": category, "digits": DIGITS_BY_CATEGORY.get(category, 2),
            "contract_size": 1, "volume_min": 1, "volume_max": 2000, "volume_step": 1,
            "tick_value": 1,
        }

    def get_price(self, account_id: str, symbol: str) -> dict:
        session = self._session_for(account_id)
        tick = self._call(session, {"ticks": symbol})["tick"]
        return {"bid": tick["quote"], "ask": tick["quote"], "mid": tick["quote"], "time": tick.get("epoch")}

    # --- trading ------------------------------------------------------
    def get_positions(self, account_id: str) -> list:
        return [dict(r) for r in ledger.open_positions_for_account(account_id)]

    def get_orders(self, account_id: str) -> list:
        return []  # Multipliers are bought outright, not resting orders

    def place_order(self, account_id: str, symbol: str, side: str, volume: float,
                     order_type: str = "market", price: float = None,
                     take_profit: float = None, stop_loss: float = None,
                     client_order_id: str = None, bot_id: int = None) -> dict:
        if bot_id is None:
            raise BrokerError("Deriv adapter requires bot_id to attribute the position")
        session = self._session_for(account_id)
        contract_type = "MULTUP" if side == "buy" else "MULTDOWN"

        limit_order = {}
        if take_profit is not None:
            limit_order["take_profit"] = abs(take_profit)
        if stop_loss is not None:
            limit_order["stop_loss"] = abs(stop_loss)

        proposal_req = {
            "proposal": 1, "amount": volume, "basis": "stake", "contract_type": contract_type,
            "currency": "USD", "symbol": symbol, "multiplier": DEFAULT_MULTIPLIER,
        }
        if limit_order:
            proposal_req["limit_order"] = limit_order
        proposal = self._call(session, proposal_req)["proposal"]
        bought = self._call(session, {"buy": proposal["id"], "price": proposal["ask_price"]})["buy"]

        entry_price = proposal.get("spot") or self.get_price(account_id, symbol)["mid"]
        position_id = ledger.open_position(
            bot_id, account_id, symbol, side, volume, entry_price, margin_used=volume,
            take_profit=take_profit, stop_loss=stop_loss,
            broker_ref=str(bought["contract_id"]), track_account=False,
        )
        return {"position_id": position_id, "symbol": symbol, "side": side,
                "volume": volume, "fill_price": entry_price, "margin_used": volume,
                "broker_ref": bought["contract_id"]}

    def modify_order(self, account_id: str, order_id: str, **kwargs) -> dict:
        raise BrokerError("Not yet implemented: Deriv Multiplier limit_order updates need "
                           "a dedicated 'contract_update' call keyed by the live contract_id.")

    def cancel_order(self, account_id: str, order_id: str) -> None:
        pass  # no resting orders to cancel

    def close_position(self, account_id: str, position_id: str, volume: float = None,
                        reason: str = "manual") -> dict:
        pos = ledger.get_open_position(position_id)
        if not pos["broker_ref"]:
            raise BrokerError(f"Position {position_id} has no Deriv contract_id on record")
        session = self._session_for(account_id)
        result = self._call(session, {"sell": int(pos["broker_ref"]), "price": 0})["sell"]

        sold_for = result.get("sold_for", 0.0)
        stake = pos["margin_used"]
        net_pl = sold_for - stake
        pl_pct = (net_pl / stake * 100) if stake else 0
        exit_price = self.get_price(account_id, pos["symbol"])["mid"]

        # update_balance=False: Deriv's own balance() call already reflects the
        # settlement the moment sell() completes — we must not add net_pl again.
        ledger.close_position(position_id, account_id, exit_price, commission=0.0, pl=net_pl,
                               pl_pct=pl_pct, reason=reason, update_balance=False, track_account=False)
        return {"position_id": position_id, "exit_price": exit_price, "pl": net_pl, "commission": 0.0}

    def get_trade_history(self, account_id: str, limit: int = 100) -> list:
        db_rows = self._local_trade_history(account_id, limit)
        return db_rows

    def _local_trade_history(self, account_id, limit):
        from models import get_db
        db = get_db()
        rows = db.execute(
            "SELECT * FROM trades WHERE account_id=? ORDER BY closed_at DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
