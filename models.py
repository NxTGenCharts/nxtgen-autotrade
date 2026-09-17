"""
Database layer. SQLite via Python's stdlib sqlite3 — zero external dependencies,
trivially swappable for PostgreSQL later (see README) by replacing this module's
connection function and adjusting the few dialect-specific bits (AUTOINCREMENT).
"""
import sqlite3
import os
import threading

DB_PATH = os.environ.get("NXTGEN_DB_PATH") or os.path.join(os.path.dirname(__file__), "nxtgen.db")
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

_local = threading.local()


def get_db():
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_suspended INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    broker_id TEXT NOT NULL,             -- 'demo', 'deriv', 'mt5_bridge', 'binance', ...
    broker_name TEXT NOT NULL,
    account_type TEXT NOT NULL,          -- 'demo' | 'live'
    label TEXT NOT NULL,
    external_account_id TEXT,            -- masked / broker-side id
    balance REAL NOT NULL DEFAULT 0,
    equity REAL NOT NULL DEFAULT 0,
    used_margin REAL NOT NULL DEFAULT 0,
    leverage INTEGER NOT NULL DEFAULT 100,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'connected',  -- connected | disconnected | error
    credentials_encrypted TEXT,          -- placeholder for encrypted live creds
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id TEXT NOT NULL,
    broker_symbol TEXT NOT NULL,         -- e.g. XAUUSDm
    display_name TEXT NOT NULL,          -- e.g. Gold
    category TEXT NOT NULL,              -- forex | crypto | commodities | indices | synthetic
    base_price REAL NOT NULL,
    digits INTEGER NOT NULL DEFAULT 2,
    contract_size REAL NOT NULL DEFAULT 100,
    volume_min REAL NOT NULL DEFAULT 0.01,
    volume_max REAL NOT NULL DEFAULT 50,
    volume_step REAL NOT NULL DEFAULT 0.01,
    tick_value REAL NOT NULL DEFAULT 1.0,
    spread_pips REAL NOT NULL DEFAULT 2.0,
    daily_vol_pct REAL NOT NULL DEFAULT 1.0,   -- simulated daily volatility %
    UNIQUE(broker_id, broker_symbol)
);

CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL,              -- dca_trend | grid_sideways
    symbol TEXT NOT NULL,
    risk_profile TEXT NOT NULL,          -- conservative | balanced | aggressive
    capital REAL NOT NULL,
    config_json TEXT NOT NULL,           -- resolved strategy params (json)
    state TEXT NOT NULL DEFAULT 'created',
    -- created|starting|running|paused|stopping|stopped|error|risk_stopped
    realized_pl REAL NOT NULL DEFAULT 0,
    peak_equity REAL NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    stopped_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                  -- buy | sell
    volume REAL NOT NULL,
    entry_price REAL NOT NULL,
    take_profit REAL,
    stop_loss REAL,
    grid_level INTEGER,                  -- null for DCA
    dca_step INTEGER,                    -- null for grid
    margin_used REAL NOT NULL DEFAULT 0,
    broker_ref TEXT,                     -- external broker's own id for this position (e.g. Deriv contract_id)
    opened_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'open'  -- open | closed
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    pl REAL NOT NULL,
    pl_pct REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT (datetime('now')),
    close_reason TEXT NOT NULL           -- take_profit | stop_loss | grid_exit | risk_stop | manual
);

CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    level TEXT NOT NULL,                 -- info | trade | warning | risk | error
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_SYMBOLS = [
    # broker_id, broker_symbol, display_name, category, base_price, digits, contract_size,
    # vol_min, vol_max, vol_step, tick_value, spread_pips, daily_vol_pct
    ("demo", "XAUUSDm", "Gold", "commodities", 4287.80, 2, 100, 0.01, 50, 0.01, 1.0, 25, 1.1),
    ("demo", "EURUSDm", "EUR/USD", "forex", 1.0842, 5, 100000, 0.01, 50, 0.01, 1.0, 1.2, 0.5),
    ("demo", "GBPUSDm", "GBP/USD", "forex", 1.2715, 5, 100000, 0.01, 50, 0.01, 1.0, 1.5, 0.6),
    ("demo", "USDJPYm", "USD/JPY", "forex", 148.20, 3, 100000, 0.01, 50, 0.01, 1.0, 1.4, 0.5),
    ("demo", "BTCUSDm", "Bitcoin", "crypto", 62150.0, 1, 1, 0.01, 5, 0.01, 1.0, 15, 2.8),
    ("demo", "ETHUSDm", "Ethereum", "crypto", 2650.0, 2, 1, 0.01, 10, 0.01, 1.0, 8, 3.2),
    ("demo", "USOILm", "US Oil", "commodities", 68.90, 2, 1000, 0.01, 50, 0.01, 1.0, 4, 1.8),
    ("demo", "US500m", "US 500", "indices", 5715.0, 1, 1, 0.01, 20, 0.01, 1.0, 40, 0.9),
    ("demo", "V75", "Volatility 75 Index", "synthetic", 285340.0, 2, 1, 0.01, 20, 0.01, 1.0, 30, 3.5),
]


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    # lightweight migration for dbs created before broker_ref existed
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(bot_positions)").fetchall()]
    if "broker_ref" not in cols:
        conn.execute("ALTER TABLE bot_positions ADD COLUMN broker_ref TEXT")
        conn.commit()
    cur = conn.execute("SELECT COUNT(*) c FROM symbols")
    if cur.fetchone()["c"] == 0:
        conn.executemany(
            """INSERT INTO symbols
               (broker_id, broker_symbol, display_name, category, base_price, digits,
                contract_size, volume_min, volume_max, volume_step, tick_value,
                spread_pips, daily_vol_pct)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            DEFAULT_SYMBOLS,
        )
    cur = conn.execute("SELECT COUNT(*) c FROM admin_config")
    if cur.fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO admin_config (key, value) VALUES (?, ?)",
            [
                ("demo_starting_balance", "10000"),
                ("global_max_drawdown_pct", "30"),
                ("max_bots_per_user", "10"),
            ],
        )
    conn.commit()
