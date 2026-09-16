"""Small in-memory rolling window of mid-prices per symbol, used by strategies
for SMA / volatility calculations. Not persisted — acceptable for a demo feed;
a live deployment would source this from the broker's historical candles."""
import threading
from collections import deque

_lock = threading.Lock()
_history = {}  # symbol -> deque[float]
MAX_LEN = 200


def push(symbol, mid_price):
    with _lock:
        buf = _history.setdefault(symbol, deque(maxlen=MAX_LEN))
        buf.append(mid_price)


def sma(symbol, n):
    with _lock:
        buf = _history.get(symbol)
        if not buf or len(buf) < n:
            return None
        vals = list(buf)[-n:]
        return sum(vals) / len(vals)


def stddev_pct(symbol, n):
    """Rough recent volatility as a % of price, used for grid spacing / stop distance."""
    with _lock:
        buf = _history.get(symbol)
        if not buf or len(buf) < n:
            return None
        vals = list(buf)[-n:]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        sd = var ** 0.5
        return (sd / mean * 100) if mean else 0


def latest(symbol):
    with _lock:
        buf = _history.get(symbol)
        return buf[-1] if buf else None


def ready(symbol, n):
    with _lock:
        buf = _history.get(symbol)
        return bool(buf) and len(buf) >= n
