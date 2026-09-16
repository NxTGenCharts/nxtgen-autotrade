"""
PriceEngine — drives the demo broker's market data.

IMPORTANT / HONESTY NOTE:
This sandbox has no outbound network access, so this build cannot stream real
broker/exchange feeds. Rather than pretend to be "live data" (which would violate
the no-fake-performance requirement), prices here are generated with a calibrated
random walk (per-symbol volatility, realistic tick size) starting from real-world
reference prices. It is clearly a simulation engine, not a live feed. In production,
this class is replaced 1:1 by a feed that subscribes to each connected broker's
real quotes via its adapter's subscribe_market_data() — nothing else in the app
needs to change, because everything downstream only ever talks to IBrokerAdapter.
"""
import math
import random
import threading
import time
from datetime import datetime, timezone


class PriceEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._prices = {}   # symbol -> {'mid': float, 'time': iso}
        self._meta = {}     # symbol -> {'daily_vol_pct': float, 'digits': int, 'spread_pips': float}
        self._thread = None
        self._running = False

    def register_symbol(self, symbol, base_price, daily_vol_pct, digits, spread_pips):
        with self._lock:
            if symbol not in self._prices:
                self._prices[symbol] = {
                    "mid": base_price,
                    "time": datetime.now(timezone.utc).isoformat(),
                }
            self._meta[symbol] = {
                "daily_vol_pct": daily_vol_pct,
                "digits": digits,
                "spread_pips": spread_pips,
            }

    def start(self, tick_seconds=2.0):
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                self._tick()
                time.sleep(tick_seconds)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _tick(self):
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            for symbol, state in self._prices.items():
                meta = self._meta.get(symbol, {"daily_vol_pct": 1.0})
                # Convert a rough "daily" vol % into a per-tick stddev assuming
                # ~43200 ticks/day at tick_seconds=2. Small, deliberately gentle.
                daily_vol = meta["daily_vol_pct"] / 100.0
                per_tick_sigma = daily_vol / math.sqrt(43200) * state["mid"]
                drift = random.gauss(0, per_tick_sigma)
                # gentle mean reversion toward a slow-moving trailing anchor (not the
                # original start price) so genuine multi-minute trends can still form
                anchor = state.get("anchor", state["mid"])
                reversion = (anchor - state["mid"]) * 0.0003
                new_mid = max(0.0001, state["mid"] + drift + reversion)
                state["mid"] = new_mid
                state["time"] = now
                state["anchor"] = anchor * 0.998 + new_mid * 0.002

    def get_price(self, symbol):
        with self._lock:
            state = self._prices.get(symbol)
            meta = self._meta.get(symbol)
            if not state or not meta:
                return None
            digits = meta["digits"]
            pip = 10 ** (-digits + 1) if digits > 1 else 0.1 ** digits
            half_spread = (meta["spread_pips"] * (10 ** -digits)) / 2
            mid = state["mid"]
            bid = round(mid - half_spread, digits)
            ask = round(mid + half_spread, digits)
            return {"bid": bid, "ask": ask, "mid": round(mid, digits), "time": state["time"]}


# module-level singleton shared by the whole process
price_engine = PriceEngine()
