"""
SQLite persistence for open positions and closed trade history.
"""
import json
import sqlite3
import os
import time
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "pair_trading.db")
_DEBUG_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", ".cursor", "debug-342352.log")
_DEBUG_SESSION_ID = "342352"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": _DEBUG_SESSION_ID,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
    except Exception:
        pass


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
                tp_smart       INTEGER DEFAULT 1,
                sl_smart       INTEGER DEFAULT 1,
                opened_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS triggers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol1      TEXT    NOT NULL,
                symbol2      TEXT    NOT NULL,
                side         TEXT    NOT NULL,
                type         TEXT    NOT NULL,
                zscore       REAL    NOT NULL,
                tp_smart     INTEGER DEFAULT 1,
                sl_smart     INTEGER DEFAULT 1,
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

            CREATE TABLE IF NOT EXISTS execution_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                exec_id      TEXT    UNIQUE NOT NULL,
                db_id        INTEGER,
                close_db_id  INTEGER,
                is_close     INTEGER NOT NULL DEFAULT 0,
                status       TEXT    NOT NULL,
                symbol1      TEXT    NOT NULL,
                symbol2      TEXT    NOT NULL,
                data_json    TEXT    NOT NULL,
                completed_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS position_legs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id      INTEGER REFERENCES open_positions(id),
                leg_number       INTEGER NOT NULL,
                symbol           TEXT    NOT NULL,
                side             TEXT    NOT NULL,
                qty              REAL    NOT NULL,
                entry_price      REAL,
                client_order_id  TEXT,
                status           TEXT    DEFAULT 'open',
                opened_at        TEXT    NOT NULL,
                closed_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS funding_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                symbol      TEXT    NOT NULL,
                amount      REAL    NOT NULL,
                asset       TEXT    NOT NULL,
                paid_at     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol1      TEXT    NOT NULL,
                symbol2      TEXT    NOT NULL,
                timeframe    TEXT    NOT NULL DEFAULT '1h',
                zwindow      INTEGER NOT NULL DEFAULT 20,
                candle_limit INTEGER NOT NULL DEFAULT 500,
                entry_z      REAL    NOT NULL DEFAULT 2.0,
                exit_z       REAL    NOT NULL DEFAULT 1.0,
                pos_size     TEXT    NOT NULL DEFAULT '1000',
                sizing       TEXT    NOT NULL DEFAULT 'ols',
                leverage     TEXT    NOT NULL DEFAULT '1',
                created_at   TEXT    NOT NULL,
                UNIQUE(symbol1, symbol2, timeframe)
            );

            CREATE TABLE IF NOT EXISTS bot_configs (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id         INTEGER REFERENCES watchlist(id) ON DELETE CASCADE,
                symbol1              TEXT NOT NULL,
                symbol2              TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'disabled',
                last_close_reason    TEXT,
                tp_zscore            REAL,
                sl_zscore            REAL,
                tp_smart             INTEGER NOT NULL DEFAULT 1,
                sl_smart             INTEGER NOT NULL DEFAULT 1,
                confirmation_minutes INTEGER NOT NULL DEFAULT 0,
                avg_levels_json      TEXT,
                current_avg_level    INTEGER NOT NULL DEFAULT 0,
                avg_in_progress      INTEGER NOT NULL DEFAULT 0,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL,
                UNIQUE(watchlist_id)
            );
        """)
    _migrate()


def _migrate() -> None:
    """Add columns introduced after initial schema."""
    with _conn() as conn:
        for table, col, typedef in [
            ("open_positions", "tp_zscore",     "REAL"),
            ("open_positions", "sl_zscore",     "REAL"),
            ("open_positions", "tp_smart",      "INTEGER DEFAULT 1"),
            ("open_positions", "sl_smart",      "INTEGER DEFAULT 1"),
            ("open_positions", "timeframe",     "TEXT DEFAULT '1h'"),
            ("open_positions", "candle_limit",  "INTEGER DEFAULT 500"),
            ("open_positions", "zscore_window", "INTEGER DEFAULT 20"),
            ("triggers",       "sl_smart",      "INTEGER DEFAULT 1"),
            ("triggers",       "timeframe",     "TEXT DEFAULT '1h'"),
            ("triggers",       "zscore_window", "INTEGER DEFAULT 20"),
            ("triggers",       "alert_pct",     "REAL DEFAULT 1.0"),
            ("triggers",       "last_fired_at", "TEXT"),
            ("open_positions", "status",           "TEXT DEFAULT 'open'"),
            ("open_positions", "coint_pvalue",     "REAL"),
            ("open_positions", "coint_checked_at", "TEXT"),
            ("closed_trades",  "commission",       "REAL DEFAULT 0"),
            ("closed_trades",  "commission_asset", "TEXT"),
            ("triggers",       "candle_limit",     "INTEGER"),
            ("watchlist",      "half_life",        "REAL"),
            ("watchlist",      "hurst",            "REAL"),
            ("watchlist",      "corr",             "REAL"),
            ("watchlist",      "pval",             "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists
        _migrate_bot_configs_nullable_thresholds(conn)


def _migrate_bot_configs_nullable_thresholds(conn: sqlite3.Connection) -> None:
    """Rebuild legacy bot_configs table so TP/SL can be nullable."""
    try:
        cols = conn.execute("PRAGMA table_info(bot_configs)").fetchall()
    except Exception:
        return
    if not cols:
        return

    notnull_map = {row["name"]: int(row["notnull"]) for row in cols}
    if not (notnull_map.get("tp_zscore") or notnull_map.get("sl_zscore")):
        return

    conn.executescript("""
        ALTER TABLE bot_configs RENAME TO bot_configs_old;

        CREATE TABLE bot_configs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            watchlist_id         INTEGER REFERENCES watchlist(id) ON DELETE CASCADE,
            symbol1              TEXT NOT NULL,
            symbol2              TEXT NOT NULL,
            status               TEXT NOT NULL DEFAULT 'disabled',
            last_close_reason    TEXT,
            tp_zscore            REAL,
            sl_zscore            REAL,
            tp_smart             INTEGER NOT NULL DEFAULT 1,
            sl_smart             INTEGER NOT NULL DEFAULT 1,
            confirmation_minutes INTEGER NOT NULL DEFAULT 0,
            avg_levels_json      TEXT,
            current_avg_level    INTEGER NOT NULL DEFAULT 0,
            avg_in_progress      INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL,
            UNIQUE(watchlist_id)
        );

        INSERT INTO bot_configs (
            id, watchlist_id, symbol1, symbol2, status, last_close_reason,
            tp_zscore, sl_zscore, tp_smart, sl_smart, confirmation_minutes,
            avg_levels_json, current_avg_level, avg_in_progress, created_at, updated_at
        )
        SELECT
            id, watchlist_id, symbol1, symbol2, status, last_close_reason,
            tp_zscore, sl_zscore, tp_smart, sl_smart, confirmation_minutes,
            avg_levels_json, current_avg_level, avg_in_progress, created_at, updated_at
        FROM bot_configs_old;

        DROP TABLE bot_configs_old;
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
        # region agent log
        _debug_log(
            run_id="initial-debug",
            hypothesis_id="H5",
            location="backend/db.py:save-open-position",
            message="Saved open position row",
            data={
                "position_id": cur.lastrowid,
                "symbol1": symbol1,
                "symbol2": symbol2,
                "side": side,
                "qty1": qty1,
                "qty2": qty2,
                "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
            },
        )
        # endregion
        return cur.lastrowid


def close_position(
    position_id: int,
    exit_price1: float,
    exit_price2: float,
    pnl: float,
    exit_zscore: Optional[float] = None,
    commission: float = 0.0,
    commission_asset: str = "",
) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM open_positions WHERE id = ?", (position_id,)
        ).fetchone()
        # region agent log
        _debug_log(
            run_id="initial-debug",
            hypothesis_id="H6",
            location="backend/db.py:close-position",
            message="close_position called",
            data={
                "position_id": position_id,
                "row_found": bool(row),
                "exit_zscore": exit_zscore,
                "pnl": pnl,
                "commission": commission,
            },
        )
        # endregion
        if not row:
            return False

        conn.execute(
            """
            INSERT INTO closed_trades
              (symbol1, symbol2, side, qty1, qty2, hedge_ratio,
               entry_zscore, exit_zscore, entry_price1, entry_price2,
               exit_price1, exit_price2, pnl, size_usd, sizing_method, leverage,
               opened_at, closed_at, commission, commission_asset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["symbol1"], row["symbol2"], row["side"],
                row["qty1"], row["qty2"], row["hedge_ratio"],
                row["entry_zscore"], exit_zscore,
                row["entry_price1"], row["entry_price2"],
                exit_price1, exit_price2, pnl,
                row["size_usd"], row["sizing_method"], row["leverage"],
                row["opened_at"], datetime.now(timezone.utc).isoformat(),
                commission, commission_asset or "",
            ),
        )
        conn.execute("DELETE FROM position_legs WHERE position_id = ?", (position_id,))
        conn.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        return True


def find_open_position(symbol1: str, symbol2: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM open_positions
            WHERE symbol1 = ? AND symbol2 = ?
              AND status NOT IN ('liquidated', 'adl_detected')
            ORDER BY opened_at DESC LIMIT 1
            """,
            (symbol1, symbol2),
        ).fetchone()
        if row is None and symbol1 == "SOL/USDC" and symbol2 == "LTC/USDC":
            all_rows = conn.execute(
                "SELECT id, symbol1, symbol2, status, opened_at FROM open_positions ORDER BY id DESC LIMIT 5"
            ).fetchall()
            # region agent log
            _debug_log(
                run_id="initial-debug",
                hypothesis_id="H5,H6",
                location="backend/db.py:find-open-position-miss",
                message="find_open_position miss for monitored pair",
                data={
                    "lookup_symbol1": symbol1,
                    "lookup_symbol2": symbol2,
                    "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
                    "recent_open_positions": [dict(r) for r in all_rows],
                },
            )
            # endregion
        return dict(row) if row else None


def get_open_positions() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM open_positions ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_open_position(position_id: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM position_legs WHERE position_id = ?", (position_id,))
        cur = conn.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        return cur.rowcount > 0


def set_position_triggers(
    position_id: int,
    tp_zscore: Optional[float],
    sl_zscore: Optional[float],
    tp_smart: bool = True,
    sl_smart: bool = True,
) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE open_positions SET tp_zscore = ?, sl_zscore = ?, tp_smart = ?, sl_smart = ? WHERE id = ?",
            (tp_zscore, sl_zscore, int(tp_smart), int(sl_smart), position_id),
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
    tp_smart: bool = True,
    sl_smart: bool = True,
    timeframe: str = "1h",
    zscore_window: int = 20,
    alert_pct: float = 1.0,
    candle_limit: Optional[int] = None,
) -> int:
    """Save a new TP/SL/alert trigger. Returns the trigger id."""
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO triggers
              (symbol1, symbol2, side, type, zscore, tp_smart, sl_smart, status,
               timeframe, zscore_window, alert_pct, candle_limit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (
                symbol1, symbol2, side, type, zscore, int(tp_smart), int(sl_smart),
                timeframe, zscore_window, alert_pct, candle_limit,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def find_active_alert(
    symbol1: str,
    symbol2: str,
    zscore: float,
    timeframe: str = "1h",
    zscore_window: int = 20,
    candle_limit: Optional[int] = None,
) -> Optional[dict]:
    """Return active alert matching pair, z threshold, TF, z-window, and lookback (candle_limit), or None."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM triggers
            WHERE symbol1 = ? AND symbol2 = ? AND type = 'alert'
              AND zscore = ? AND status = 'active'
              AND timeframe = ? AND zscore_window = ?
              AND ((? IS NULL AND candle_limit IS NULL)
                   OR (? IS NOT NULL AND candle_limit = ?))
            LIMIT 1
            """,
            (
                symbol1,
                symbol2,
                zscore,
                timeframe,
                zscore_window,
                candle_limit,
                candle_limit,
                candle_limit,
            ),
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
    """Hard-delete a trigger. Returns True if a row was deleted."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
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
    """Record that an alert notification was sent. Keeps row active for hysteresis."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE triggers SET last_fired_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), trigger_id),
        )
        return cur.rowcount > 0


def get_recent_alerts(minutes: int = 60) -> list[dict]:
    """Return alert triggers that fired within the last N minutes (any status)."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM triggers
            WHERE type = 'alert'
              AND last_fired_at >= datetime('now', ?)
            ORDER BY last_fired_at DESC
            """,
            (f"-{minutes} minutes",),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Execution history (persisted smart execution logs)
# ---------------------------------------------------------------------------

def save_execution_history(
    exec_id: str,
    db_id: Optional[int],
    close_db_id: Optional[int],
    is_close: bool,
    status: str,
    symbol1: str,
    symbol2: str,
    data_json: str,
) -> None:
    """Persist a terminal smart execution snapshot. Idempotent via INSERT OR IGNORE."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO execution_history
              (exec_id, db_id, close_db_id, is_close, status, symbol1, symbol2,
               data_json, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exec_id, db_id, close_db_id, int(is_close), status,
                symbol1, symbol2, data_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_execution_history(limit: int = 100) -> list[dict]:
    """Return most recent completed execution snapshots, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM execution_history ORDER BY completed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Position status management
# ---------------------------------------------------------------------------

def set_position_status(position_id: int, status: str) -> bool:
    """Update status of an open position (e.g. 'open', 'partial_close', 'liquidated')."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE open_positions SET status = ? WHERE id = ?",
            (status, position_id),
        )
        return cur.rowcount > 0


def update_position_coint_health(position_id: int, pvalue: float) -> bool:
    """Store latest cointegration p-value for an open position."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE open_positions SET coint_pvalue = ?, coint_checked_at = ? WHERE id = ?",
            (pvalue, datetime.now(timezone.utc).isoformat(), position_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Position legs (leg-level tracking)
# ---------------------------------------------------------------------------

def save_position_leg(
    position_id: int,
    leg_number: int,
    symbol: str,
    side: str,
    qty: float,
    entry_price: Optional[float] = None,
    client_order_id: Optional[str] = None,
) -> int:
    """Create a position leg record. Returns leg id."""
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO position_legs
              (position_id, leg_number, symbol, side, qty, entry_price,
               client_order_id, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                position_id, leg_number, symbol, side, qty, entry_price,
                client_order_id, datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def get_position_legs(position_id: int) -> list[dict]:
    """Return all legs for a position, ordered by leg_number."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM position_legs WHERE position_id = ? ORDER BY leg_number, opened_at",
            (position_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def close_position_legs(position_id: int) -> bool:
    """Mark all open legs of a position as closed."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE position_legs SET status = 'closed', closed_at = ? WHERE position_id = ? AND status = 'open'",
            (datetime.now(timezone.utc).isoformat(), position_id),
        )
        return cur.rowcount > 0


def add_position_entry(
    position_id: int,
    leg_number: int,
    new_qty: float,
    new_entry_price: float,
    client_order_id: Optional[str] = None,
) -> bool:
    """
    Add a new entry (averaging) to an existing position.
    Updates weighted average price and cumulative qty in open_positions.
    Returns True if position was found and updated.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT qty1, qty2, entry_price1, entry_price2 FROM open_positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            return False

        if leg_number == 1:
            old_qty = row["qty1"]
            old_price = row["entry_price1"] or new_entry_price
            new_total = old_qty + new_qty
            new_avg = (old_qty * old_price + new_qty * new_entry_price) / new_total
            conn.execute(
                "UPDATE open_positions SET qty1 = ?, entry_price1 = ? WHERE id = ?",
                (new_total, new_avg, position_id),
            )
        else:
            old_qty = row["qty2"]
            old_price = row["entry_price2"] or new_entry_price
            new_total = old_qty + new_qty
            new_avg = (old_qty * old_price + new_qty * new_entry_price) / new_total
            conn.execute(
                "UPDATE open_positions SET qty2 = ?, entry_price2 = ? WHERE id = ?",
                (new_total, new_avg, position_id),
            )

        # Record the new entry as a separate leg row
        symbol_row = conn.execute(
            "SELECT symbol1, symbol2, side FROM open_positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        if symbol_row:
            symbol = symbol_row["symbol1"] if leg_number == 1 else symbol_row["symbol2"]
            spread_side = symbol_row["side"]
            leg_side = ("buy" if spread_side == "long_spread" else "sell") if leg_number == 1 else \
                       ("sell" if spread_side == "long_spread" else "buy")
            conn.execute(
                """
                INSERT INTO position_legs
                  (position_id, leg_number, symbol, side, qty, entry_price,
                   client_order_id, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    position_id, leg_number, symbol, leg_side, new_qty, new_entry_price,
                    client_order_id, datetime.now(timezone.utc).isoformat(),
                ),
            )
        return True


# ---------------------------------------------------------------------------
# Funding history
# ---------------------------------------------------------------------------

def save_funding_history(
    position_id: Optional[int],
    symbol: str,
    amount: float,
    asset: str,
) -> int:
    """Record a funding fee payment/receipt for a position leg."""
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO funding_history (position_id, symbol, amount, asset, paid_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (position_id, symbol, amount, asset, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def get_funding_total(position_id: int) -> float:
    """Return total funding paid/received for a position (negative = paid, positive = received)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM funding_history WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        return float(row["total"]) if row else 0.0


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def get_watchlist() -> list[dict]:
    """Return all watchlist items ordered by creation time."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_watchlist_item(item_id: int) -> Optional[dict]:
    """Return one watchlist item by id, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM watchlist WHERE id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None


def save_watchlist_item(
    symbol1: str,
    symbol2: str,
    timeframe: str = "1h",
    zwindow: int = 20,
    candle_limit: int = 500,
    entry_z: float = 2.0,
    exit_z: float = 1.0,
    pos_size: str = "1000",
    sizing: str = "ols",
    leverage: str = "1",
) -> int:
    """Insert or update a watchlist item. Returns the row id."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO watchlist
              (symbol1, symbol2, timeframe, zwindow, candle_limit,
               entry_z, exit_z, pos_size, sizing, leverage, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol1, symbol2, timeframe) DO UPDATE SET
              zwindow      = excluded.zwindow,
              candle_limit = excluded.candle_limit,
              entry_z      = excluded.entry_z,
              exit_z       = excluded.exit_z,
              pos_size     = excluded.pos_size,
              sizing       = excluded.sizing,
              leverage     = excluded.leverage
            """,
            (
                symbol1, symbol2, timeframe, zwindow, candle_limit,
                entry_z, exit_z, pos_size, sizing, leverage,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        row = conn.execute(
            "SELECT id FROM watchlist WHERE symbol1=? AND symbol2=? AND timeframe=?",
            (symbol1, symbol2, timeframe),
        ).fetchone()
        return row["id"]


def delete_watchlist_item(item_id: int) -> bool:
    """Delete a watchlist item by id. Returns True if found."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE id = ?", (item_id,))
        return cur.rowcount > 0


def update_watchlist_stats(item_id: int, half_life: Optional[float], hurst: Optional[float], corr: Optional[float], pval: Optional[float]) -> bool:
    """Update computed stats for a watchlist item. Returns True if found."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE watchlist SET half_life=?, hurst=?, corr=?, pval=? WHERE id=?",
            (half_life, hurst, corr, pval, item_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# bot_configs
# ---------------------------------------------------------------------------

def parse_avg_levels_json(
    avg_levels_json: Optional[str],
    entry_z: float,
    *,
    strict: bool = False,
) -> list[dict]:
    """Parse/sort averaging levels and optionally reject unsafe configs."""
    if not avg_levels_json:
        return []
    try:
        raw_levels = json.loads(avg_levels_json)
    except Exception as e:
        if strict:
            raise ValueError("Averaging levels JSON is invalid.") from e
        return []

    if not isinstance(raw_levels, list):
        if strict:
            raise ValueError("Averaging levels must be a list.")
        return []

    cleaned: list[dict] = []
    for item in raw_levels:
        try:
            z = float(item["z"])
            size_usd = float(item["size_usd"])
        except Exception as e:
            if strict:
                raise ValueError("Each averaging level must contain numeric z and size_usd.") from e
            continue

        if z <= 0 or size_usd <= 0:
            if strict:
                raise ValueError("Averaging levels must use positive z and size values.")
            continue

        cleaned.append({"z": z, "size_usd": size_usd})

    cleaned.sort(key=lambda lvl: lvl["z"])

    normalized: list[dict] = []
    last_z = float(entry_z)
    for lvl in cleaned:
        if lvl["z"] <= float(entry_z):
            if strict:
                raise ValueError("Each averaging level must be strictly greater than Entry Z.")
            continue
        if lvl["z"] <= last_z:
            if strict:
                raise ValueError("Averaging levels must be strictly increasing.")
            continue
        normalized.append(lvl)
        last_z = lvl["z"]

    return normalized


def normalize_avg_levels_json(avg_levels_json: Optional[str], entry_z: float, *, strict: bool = False) -> Optional[str]:
    """Return normalized averaging JSON or None when no usable levels remain."""
    levels = parse_avg_levels_json(avg_levels_json, entry_z, strict=strict)
    return json.dumps(levels) if levels else None


def save_bot_config(
    watchlist_id: int,
    symbol1: str,
    symbol2: str,
    tp_zscore: Optional[float],
    sl_zscore: Optional[float],
    tp_smart: int = 1,
    sl_smart: int = 1,
    confirmation_minutes: int = 0,
    avg_levels_json: Optional[str] = None,
) -> int:
    """Create or update bot config for a watchlist item. Returns id."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    wl = get_watchlist_item(watchlist_id)
    entry_z = float((wl or {}).get("entry_z") or 2.0)
    avg_levels_json = normalize_avg_levels_json(avg_levels_json, entry_z, strict=True)
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO bot_configs
              (watchlist_id, symbol1, symbol2, tp_zscore, sl_zscore,
               tp_smart, sl_smart, confirmation_minutes, avg_levels_json,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(watchlist_id) DO UPDATE SET
              tp_zscore            = excluded.tp_zscore,
              sl_zscore            = excluded.sl_zscore,
              tp_smart             = excluded.tp_smart,
              sl_smart             = excluded.sl_smart,
              confirmation_minutes = excluded.confirmation_minutes,
              avg_levels_json      = excluded.avg_levels_json,
              updated_at           = excluded.updated_at
            """,
            (
                watchlist_id, symbol1, symbol2, tp_zscore, sl_zscore,
                int(tp_smart), int(sl_smart), confirmation_minutes, avg_levels_json,
                now, now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM bot_configs WHERE watchlist_id = ?", (watchlist_id,)
        ).fetchone()
        return row["id"]


def get_bot_configs() -> list[dict]:
    """Return all bot_configs rows."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM bot_configs ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def get_active_bot_configs() -> list[dict]:
    """Return bot_configs with status IN ('waiting', 'in_position')."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_configs WHERE status IN ('waiting', 'in_position') ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_bot_config_by_pair(symbol1: str, symbol2: str) -> Optional[dict]:
    """Return bot_config for a given pair, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM bot_configs WHERE symbol1 = ? AND symbol2 = ?",
            (symbol1, symbol2),
        ).fetchone()
        return dict(row) if row else None


def set_bot_status(config_id: int, status: str) -> bool:
    """Update bot status. Resets current_avg_level when transitioning out of in_position."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        reset_avg = status in ("waiting", "paused_after_sl", "disabled")
        if reset_avg:
            cur = conn.execute(
                "UPDATE bot_configs SET status=?, current_avg_level=0, updated_at=? WHERE id=?",
                (status, now, config_id),
            )
        else:
            cur = conn.execute(
                "UPDATE bot_configs SET status=?, updated_at=? WHERE id=?",
                (status, now, config_id),
            )
        return cur.rowcount > 0


def set_bot_close_reason(config_id: int, reason: Optional[str]) -> bool:
    """Write last_close_reason before a position is closed by monitor_position_triggers."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bot_configs SET last_close_reason=?, updated_at=? WHERE id=?",
            (reason, now, config_id),
        )
        return cur.rowcount > 0


def set_bot_avg_in_progress(config_id: int, in_progress: bool) -> bool:
    """Set/clear the averaging-in-progress flag."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bot_configs SET avg_in_progress=?, updated_at=? WHERE id=?",
            (int(in_progress), now, config_id),
        )
        return cur.rowcount > 0


def increment_bot_avg_level(config_id: int) -> bool:
    """Increment current_avg_level by 1 after a successful averaging fill."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bot_configs SET current_avg_level=current_avg_level+1, updated_at=? WHERE id=?",
            (now, config_id),
        )
        return cur.rowcount > 0


def delete_bot_config(config_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM bot_configs WHERE id = ?", (config_id,))
        return cur.rowcount > 0
