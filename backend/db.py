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
                opened_at      TEXT    NOT NULL
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
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO open_positions
              (symbol1, symbol2, side, qty1, qty2, hedge_ratio,
               entry_zscore, entry_price1, entry_price2,
               size_usd, sizing_method, leverage, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol1, symbol2, side, qty1, qty2, hedge_ratio,
                entry_zscore, entry_price1, entry_price2,
                size_usd, sizing_method, leverage,
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


def get_closed_trades(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_trades ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
