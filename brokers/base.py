"""
IBrokerAdapter — the common contract every broker integration must implement.

This is the seam that lets NxTGen AutoTrade add new brokers (Deriv, MT5-compatible,
Binance, Bybit, ...) without ever touching the strategy engine, risk engine, or UI.
Nothing above this layer is allowed to know a broker's specific API shape.
"""
from abc import ABC, abstractmethod


class BrokerError(Exception):
    """Raised for any broker-level failure (auth, connectivity, rejected order)."""
    pass


class NotConnectedError(BrokerError):
    """Raised by real-broker adapters that have no live credentials configured yet.

    We never fake a live connection. If a live adapter (Deriv/MT5/Binance) has not
    been given real credentials, every call raises this instead of returning
    fabricated data.
    """
    pass


class IBrokerAdapter(ABC):
    """Every broker adapter (Demo, Deriv, MT5Bridge, Binance, Bybit, ...) implements this."""

    broker_id: str = "base"
    broker_name: str = "Base Broker"

    # --- connection -------------------------------------------------
    @abstractmethod
    def connect_account(self, credentials: dict) -> dict:
        """Validate credentials & establish a session. Returns account summary."""
        raise NotImplementedError

    @abstractmethod
    def disconnect_account(self, account_id: str) -> None:
        raise NotImplementedError

    # --- account state ------------------------------------------------
    @abstractmethod
    def get_account_info(self, account_id: str) -> dict:
        """balance, equity, margin, free_margin, margin_level, leverage, currency"""
        raise NotImplementedError

    @abstractmethod
    def get_balance(self, account_id: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_equity(self, account_id: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_margin(self, account_id: str) -> dict:
        """{'used': x, 'free': y, 'level_pct': z}"""
        raise NotImplementedError

    @abstractmethod
    def get_leverage(self, account_id: str) -> int:
        raise NotImplementedError

    # --- symbols / market data -----------------------------------------
    @abstractmethod
    def get_symbols(self, account_id: str) -> list:
        """Only symbols actually tradable on this account. Each item includes the
        broker-native symbol (e.g. 'XAUUSDm') plus normalized display metadata."""
        raise NotImplementedError

    @abstractmethod
    def get_symbol_info(self, account_id: str, symbol: str) -> dict:
        """contract_size, volume_min, volume_max, volume_step, tick_value, digits"""
        raise NotImplementedError

    @abstractmethod
    def get_price(self, account_id: str, symbol: str) -> dict:
        """{'bid': x, 'ask': y, 'time': iso8601}"""
        raise NotImplementedError

    # --- trading ------------------------------------------------------
    @abstractmethod
    def get_positions(self, account_id: str) -> list:
        raise NotImplementedError

    @abstractmethod
    def get_orders(self, account_id: str) -> list:
        raise NotImplementedError

    @abstractmethod
    def place_order(self, account_id: str, symbol: str, side: str, volume: float,
                     order_type: str = "market", price: float = None,
                     take_profit: float = None, stop_loss: float = None,
                     client_order_id: str = None) -> dict:
        """client_order_id enables idempotent execution across restarts."""
        raise NotImplementedError

    @abstractmethod
    def modify_order(self, account_id: str, order_id: str, **kwargs) -> dict:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, account_id: str, order_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def close_position(self, account_id: str, position_id: str, volume: float = None) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_trade_history(self, account_id: str, limit: int = 100) -> list:
        raise NotImplementedError
