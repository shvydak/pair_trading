"""
SQLite persistence for open positions and closed trade history.
"""
import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "pair_trading.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS open_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol1        TEXT    NOT NULL,
                symbol2        TEXT    NOT NULL,
                side           TEXT    NOT NULL,
                qty1           REAL    NOT NULL,
                qty2           REAL    NOT NULL,
                hedge_ratio    REAL    NOT NULL,
                entry_zscore   REAL,
                entry_price1   REAL,
                entry_price2   REAL,
                size_usd       REAL,
                sizing_method  TEXT,
                leverage       INTEGER,
                tp_zscore      REAL,
                sl_zscore      REAL,
                tp_smart       INTEGER DEFAULT 0,
                opened_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS triggers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol1      TEXT    NOT NULL,
                symbol2      TEXT    NOT NULL,
                side         TEXT    NOT NULL,
                type         TEXT    NOT NULL,
                zscore       REAL    NOT NULL,
                tp_smart     INTEGER DEFAULT 0,
                status       TEXT    DEFAULT 'active',
                created_at   TEXT    NOT NULL,
                triggered_at TEXT
            );

            CREATE TABLE IF NOT EXISTS closed_trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol1        TEXT    NOT NULL,
                symbol2        TEXT    NOT NULL,
                side           TEXT    NOT NULL,
                qty1           REAL    NOT NULL,
                qty2           REAL    NOT NULL,
                hedge_ratio    REAL    NOT NULL,
                entry_zscore   REAL,
                exit_zscore    REAL,
                entry_price1   REAL,
                entry_price2   REAL,
                exit_price1    REAL,
                exit_price2    REAL,
                pnl            REAL,
                size_usd       REAL,
                sizing_method  TEXT,
                leverage       INTEGER,
                opened_at      TEXT    NOT NULL,
                closed_at      TEXT    NOT NULL
            );
        """)
    _migrate()


def _migrate() -> None:
    """Add columns introduced after initial schema."""
    with _conn() as conn:
        for table, col, typedef in [
            ("open_positions", "tp_zscore",     "REAL"),
            ("open_positions", "sl_zscore",     "REAL"),
            ("open_positions", "tp_smart",      "INTEGER DEFAULT 0"),
            ("open_positions", "timeframe",     "TEXT DEFAULT '1h'"),
            ("open_positions", "candle_limit",  "INTEGER DEFAULT 500"),
            ("open_positions", "zscore_window", "INTEGER DEFAULT 20"),
            ("triggers",       "timeframe",     "TEXT DEFAULT '1h'"),
            ("triggers",       "zscore_window", "INTEGER DEFAULT 20"),
            ("triggers",       "alert_pct",     "REAL DEFAULT 1.0"),
            ("triggers",       "last_fired_at", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists


def save_open_position(
    symbol1: str,
    symbol2: str,
    side: str,
    qty1: float,
    qty2: float,
    hedge_ratio: float,
    entry_zscore: Optional[float] = None,
    entry_price1: Optional[float] = None,
    entry_price2: Optional[float] = None,
    size_usd: Optional[float] = None,
    sizing_method: Optional[str] = None,
    leverage: Optional[int] = None,
    timeframe: str = "1h",
    candle_limit: int = 500,
    zscore_window: int = 20,
) -> int:
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM open_positions WHERE symbol1=? AND symbol2=?",
            (symbol1, symbol2),
        ).fetchone()
        if existing:
            raise ValueError(
                f"Position already open for {symbol1}/{symbol2} (id={existing[0]}). "
                "Close or delete it first."
            )
        cur = conn.execute(
            """
            INSERT INTO open_positions
              (symbol1, symbol2, side, qty1, qty2, hedge_ratio,
               entry_zscore, entry_price1, entry_price2,
               size_usd, sizing_method, leverage,
               timeframe, candle_limit, zscore_window, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol1, symbol2, side, qty1, qty2, hedge_ratio,
                entry_zscore, entry_price1, entry_price2,
                size_usd, sizing_method, leverage,
                timeframe, candle_limit, zscore_window,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def close_position(
    position_id: int,
    exit_price1: float,
    exit_price2: float,
    pnl: float,
    exit_zscore: Optional[float] = None,
) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM open_positions WHERE id = ?", (position_id,)
        ).fetchone()
        if not row:
            return False

        conn.execute(
            """
            INSERT INTO closed_trades
              (symbol1, symbol2, side, qty1, qty2, hedge_ratio,
               entry_zscore, exit_zscore, entry_price1, entry_price2,
               exit_price1, exit_price2, pnl, size_usd, sizing_method, leverage,
               opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["symbol1"], row["symbol2"], row["side"],
                row["qty1"], row["qty2"], row["hedge_ratio"],
                row["entry_zscore"], exit_zscore,
                row["entry_price1"], row["entry_price2"],
                exit_price1, exit_price2, pnl,
                row["size_usd"], row["sizing_method"], row["leverage"],
                row["opened_at"], datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        return True


def find_open_position(symbol1: str, symbol2: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM open_positions
            WHERE symbol1 = ? AND symbol2 = ?
            ORDER BY opened_at DESC LIMIT 1
            """,
            (symbol1, symbol2),
        ).fetchone()
        return dict(row) if row else None


def get_open_positions() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM open_positions ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_open_position(position_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        return cur.rowcount > 0


def set_position_triggers(
    position_id: int,
    tp_zscore: Optional[float],
    sl_zscore: Optional[float],
    tp_smart: bool = False,
) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE open_positions SET tp_zscore = ?, sl_zscore = ?, tp_smart = ? WHERE id = ?",
            (tp_zscore, sl_zscore, int(tp_smart), position_id),
        )
        return cur.rowcount > 0


def get_closed_trades(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_trades ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Triggers (standalone TP/SL orders, independent of open_positions)
# ---------------------------------------------------------------------------

def save_trigger(
    symbol1: str,
    symbol2: str,
    side: str,
    type: str,
    zscore: float,
    tp_smart: bool = False,
    timeframe: str = "1h",
    zscore_window: int = 20,
    alert_pct: float = 1.0,
) -> int:
    """Save a new TP/SL/alert trigger. Returns the trigger id."""
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO triggers
              (symbol1, symbol2, side, type, zscore, tp_smart, status,
               timeframe, zscore_window, alert_pct, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                symbol1, symbol2, side, type, zscore, int(tp_smart),
                timeframe, zscore_window, alert_pct,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def find_active_alert(symbol1: str, symbol2: str, zscore: float) -> Optional[dict]:
    """Return existing active alert trigger for (sym1, sym2, zscore), or None."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM triggers
            WHERE symbol1 = ? AND symbol2 = ? AND type = 'alert'
              AND zscore = ? AND status = 'active'
            LIMIT 1
            """,
            (symbol1, symbol2, zscore),
        ).fetchone()
        return dict(row) if row else None


def get_active_triggers() -> list[dict]:
    """Return all triggers with status='active'."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE status = 'active' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_triggers_for_pair(symbol1: str, symbol2: str) -> list[dict]:
    """Return all triggers (any status) for a given pair."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE symbol1 = ? AND symbol2 = ? ORDER BY created_at DESC",
            (symbol1, symbol2),
        ).fetchall()
        return [dict(r) for r in rows]


def cancel_trigger(trigger_id: int) -> bool:
    """Set trigger status to 'cancelled'. Returns True if found."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE triggers SET status = 'cancelled' WHERE id = ? AND status = 'active'",
            (trigger_id,),
        )
        return cur.rowcount > 0


def trigger_fired(trigger_id: int) -> bool:
    """Set trigger status to 'triggered' with current timestamp. Returns True if found."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE triggers SET status = 'triggered', triggered_at = ? WHERE id = ? AND status = 'active'",
            (datetime.now(timezone.utc).isoformat(), trigger_id),
        )
        return cur.rowcount > 0


def alert_fired(trigger_id: int) -> bool:
    """Record that an alert notification was sent. Keeps status='active' for hysteresis."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE triggers SET last_fired_at = ? WHERE id = ? AND status = 'active'",
            (datetime.now(timezone.utc).isoformat(), trigger_id),
        )
        return cur.rowcount > 0


def get_recent_alerts(minutes: int = 60) -> list[dict]:
    """Return active alert triggers that fired within the last N minutes."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM triggers
            WHERE type = 'alert' AND status = 'active'
              AND last_fired_at >= datetime('now', ?)
            ORDER BY last_fired_at DESC
            """,
            (f"-{minutes} minutes",),
        ).fetchall()
        return [dict(r) for r in rows]
