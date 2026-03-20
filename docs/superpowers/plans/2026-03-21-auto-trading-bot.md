# Auto-Trading Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-pair automated trading bot that opens/closes positions on z-score signals using existing execution infrastructure.

**Architecture:** New `bot_configs` SQLite table stores per-pair bot configuration. New `monitor_auto_trading` background task handles entries and averaging. Existing `monitor_position_triggers` handles exits (TP/SL) — extended only to write `last_close_reason` before closing and to respect `avg_in_progress` flag.

**Tech Stack:** Python/FastAPI, SQLite (db.py patterns), asyncio background tasks, existing `order_manager.run_execution`, PriceCache, aiogram v3 (Telegram)

**Spec:** `docs/superpowers/specs/2026-03-21-auto-trading-bot-design.md`

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `backend/db.py` | Add `bot_configs` table + 8 CRUD functions |
| Modify | `backend/telegram_bot.py` | Add `notify_bot_paused()` |
| Modify | `backend/main.py` | Extend `monitor_position_triggers`; add `monitor_auto_trading`; add 5 API endpoints; register task in lifespan |
| Modify | `frontend/index.html` | BOT badge in watchlist; Bot Config Modal; chart overlay; Positions tab BOT button |
| Modify | `tests/test_db.py` | Tests for all new bot_configs DB functions |
| Create | `tests/test_bot_monitor.py` | Tests for monitor_auto_trading state machine logic |

---

## Task 1: DB Layer — bot_configs table and CRUD

**Files:**
- Modify: `backend/db.py`
- Modify: `tests/test_db.py`

Read these before starting:
- `backend/db.py` lines 18–163 (init_db, _migrate, existing patterns)
- `tests/test_db.py` lines 1–60 (test pattern: `tmp_db` fixture, `_save()` helper)

### Step 1.1 — Write failing tests for `save_bot_config` and `get_bot_configs`

- [ ] Add to `tests/test_db.py`:

```python
# ---------------------------------------------------------------------------
# bot_configs
# ---------------------------------------------------------------------------

def _save_wl(db, sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT"):
    """Save a minimal watchlist item and return its id."""
    return db.save_watchlist_item(
        symbol1=sym1, symbol2=sym2, timeframe="1h", zwindow=20,
        candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )


def test_save_bot_config_returns_id(tmp_db):
    wl_id = _save_wl(tmp_db)
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id,
        symbol1="BTC/USDT:USDT",
        symbol2="ETH/USDT:USDT",
        tp_zscore=0.5,
        sl_zscore=4.0,
    )
    assert isinstance(cfg_id, int) and cfg_id >= 1


def test_get_bot_configs_returns_list(tmp_db):
    wl_id = _save_wl(tmp_db)
    tmp_db.save_bot_config(
        watchlist_id=wl_id,
        symbol1="BTC/USDT:USDT",
        symbol2="ETH/USDT:USDT",
        tp_zscore=0.5,
        sl_zscore=4.0,
    )
    configs = tmp_db.get_bot_configs()
    assert len(configs) == 1
    assert configs[0]["symbol1"] == "BTC/USDT:USDT"
    assert configs[0]["status"] == "disabled"


def test_save_bot_config_upsert(tmp_db):
    """Second save for same watchlist_id updates the row."""
    wl_id = _save_wl(tmp_db)
    tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=1.0, sl_zscore=3.0,
    )
    configs = tmp_db.get_bot_configs()
    assert len(configs) == 1
    assert configs[0]["tp_zscore"] == 1.0


def test_set_bot_status(tmp_db):
    wl_id = _save_wl(tmp_db)
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "waiting")
    configs = tmp_db.get_bot_configs()
    assert configs[0]["status"] == "waiting"


def test_set_bot_close_reason(tmp_db):
    wl_id = _save_wl(tmp_db)
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_close_reason(cfg_id, "sl")
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["last_close_reason"] == "sl"


def test_set_bot_avg_in_progress(tmp_db):
    wl_id = _save_wl(tmp_db)
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_avg_in_progress(cfg_id, True)
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["avg_in_progress"] == 1


def test_delete_bot_config(tmp_db):
    wl_id = _save_wl(tmp_db)
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    assert tmp_db.delete_bot_config(cfg_id) is True
    assert tmp_db.get_bot_configs() == []


def test_bot_config_cascade_delete(tmp_db):
    """Deleting watchlist item also removes bot_config (ON DELETE CASCADE)."""
    wl_id = _save_wl(tmp_db)
    tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.delete_watchlist_item(wl_id)
    assert tmp_db.get_bot_configs() == []


def test_get_active_bot_configs(tmp_db):
    """get_active_bot_configs returns only waiting/in_position rows."""
    wl1 = _save_wl(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT")
    wl2 = _save_wl(tmp_db, "BTC/USDT:USDT", "SOL/USDT:USDT")
    cfg1 = tmp_db.save_bot_config(
        watchlist_id=wl1, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    cfg2 = tmp_db.save_bot_config(
        watchlist_id=wl2, symbol1="BTC/USDT:USDT", symbol2="SOL/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg1, "waiting")
    tmp_db.set_bot_status(cfg2, "disabled")
    active = tmp_db.get_active_bot_configs()
    assert len(active) == 1
    assert active[0]["symbol2"] == "ETH/USDT:USDT"
```

- [ ] Run tests to confirm they fail:

```bash
cd /Users/y.shvydak/Projects/pair_trading
.venv/bin/pytest tests/test_db.py -k "bot_config" -v
```

Expected: `AttributeError` or `FAILED` — functions don't exist yet.

### Step 1.2 — Add bot_configs table to `init_db` in `backend/db.py`

**Sub-step A — Enable FK enforcement in `_conn()`** (required for cascade delete to work):

- [ ] In `backend/db.py`, modify `_conn()`:

```python
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
```

SQLite disables FK enforcement by default. Without this, `ON DELETE CASCADE` is silently ignored and the cascade delete test will fail.

**Sub-step B — Add the table** to `init_db()`, after the `watchlist` CREATE statement:

```python
            CREATE TABLE IF NOT EXISTS bot_configs (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id         INTEGER REFERENCES watchlist(id) ON DELETE CASCADE,
                symbol1              TEXT NOT NULL,
                symbol2              TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'disabled',
                last_close_reason    TEXT,
                tp_zscore            REAL NOT NULL,
                sl_zscore            REAL NOT NULL,
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
```

The `UNIQUE(watchlist_id)` constraint is required for `INSERT ... ON CONFLICT(watchlist_id) DO UPDATE` (upsert) to work correctly. Without it, SQLite raises `OperationalError: no such constraint`.

### Step 1.3 — Add CRUD functions at the bottom of `backend/db.py`

- [ ] Add after the last existing function:

```python
# ---------------------------------------------------------------------------
# bot_configs
# ---------------------------------------------------------------------------

def save_bot_config(
    watchlist_id: int,
    symbol1: str,
    symbol2: str,
    tp_zscore: float,
    sl_zscore: float,
    tp_smart: bool = True,
    sl_smart: bool = True,
    confirmation_minutes: int = 0,
    avg_levels_json: Optional[str] = None,
) -> int:
    """Create or update bot config for a watchlist item. Returns id."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
    """Update bot status. Also resets current_avg_level when transitioning out of in_position."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        reset_avg = status in ("waiting", "paused_after_sl", "disabled")
        if reset_avg:
            conn.execute(
                "UPDATE bot_configs SET status=?, current_avg_level=0, updated_at=? WHERE id=?",
                (status, now, config_id),
            )
        else:
            conn.execute(
                "UPDATE bot_configs SET status=?, updated_at=? WHERE id=?",
                (status, now, config_id),
            )
        return conn.execute(
            "SELECT changes()"
        ).fetchone()[0] > 0


def set_bot_close_reason(config_id: int, reason: str) -> None:
    """Write last_close_reason before a position is closed by monitor_position_triggers."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        conn.execute(
            "UPDATE bot_configs SET last_close_reason=?, updated_at=? WHERE id=?",
            (reason, now, config_id),
        )


def set_bot_avg_in_progress(config_id: int, in_progress: bool) -> None:
    """Set/clear the averaging-in-progress flag."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        conn.execute(
            "UPDATE bot_configs SET avg_in_progress=?, updated_at=? WHERE id=?",
            (int(in_progress), now, config_id),
        )


def increment_bot_avg_level(config_id: int) -> None:
    """Increment current_avg_level by 1 after a successful averaging fill."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        conn.execute(
            "UPDATE bot_configs SET current_avg_level=current_avg_level+1, updated_at=? WHERE id=?",
            (now, config_id),
        )


def delete_bot_config(config_id: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM bot_configs WHERE id = ?", (config_id,))
        return conn.execute("SELECT changes()").fetchone()[0] > 0
```

### Step 1.4 — Run tests and verify they pass

- [ ] Run:

```bash
cd /Users/y.shvydak/Projects/pair_trading
.venv/bin/pytest tests/test_db.py -k "bot_config" -v
```

Expected: all 9 bot_config tests PASS.

- [ ] Run full suite to check no regressions:

```bash
.venv/bin/pytest tests/ -v --tb=short
```

Expected: all 311 existing tests pass + 9 new = 320 total.

### Step 1.5 — Commit

```bash
git add backend/db.py tests/test_db.py
git commit -m "feat(db): add bot_configs table and CRUD functions"
```

---

## Task 2: Telegram — `notify_bot_paused`

**Files:**
- Modify: `backend/telegram_bot.py`
- Modify: `tests/test_telegram_bot.py`

Read: `backend/telegram_bot.py` lines 120–290 (notification function patterns).

### Step 2.1 — Write failing test

- [ ] Add to `tests/test_telegram_bot.py`:

```python
def test_notify_bot_paused_calls_fire(monkeypatch):
    fired = []
    monkeypatch.setattr(telegram_bot, "_fire", lambda msg: fired.append(msg))
    asyncio.run(telegram_bot.notify_bot_paused("BTC/USDT:USDT", "ETH/USDT:USDT", "sl"))
    assert len(fired) == 1
    assert "Бот остановлен" in fired[0] or "паузе" in fired[0].lower()
```

- [ ] Run to confirm FAIL:

```bash
.venv/bin/pytest tests/test_telegram_bot.py -k "notify_bot_paused" -v
```

### Step 2.2 — Implement in `backend/telegram_bot.py`

- [ ] Add after `notify_execution_failed`:

```python
async def notify_bot_paused(sym1: str, sym2: str, reason: str) -> None:
    """Notify when the bot pauses after SL/liquidation."""
    pair = _fmt_pair(sym1, sym2)
    reason_map = {"sl": "Stop Loss", "liquidation": "Ликвидация", "manual": "Ручное закрытие"}
    reason_label = reason_map.get(reason, reason.upper())
    _fire(
        f"⏸ <b>Бот на паузе</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Причина: {reason_label}\n"
        f"Включите автоторговлю вручную для возобновления"
    )
```

### Step 2.3 — Run test and full suite

```bash
.venv/bin/pytest tests/test_telegram_bot.py -k "notify_bot_paused" -v
.venv/bin/pytest tests/ -v --tb=short
```

### Step 2.4 — Commit

```bash
git add backend/telegram_bot.py tests/test_telegram_bot.py
git commit -m "feat(telegram): add notify_bot_paused notification"
```

---

## Task 3: Extend `monitor_position_triggers` — last_close_reason + avg_in_progress

**Files:**
- Modify: `backend/main.py`

Read: `backend/main.py` lines 438–780 (full `monitor_position_triggers` function).

Two small changes to the existing monitor:

**Change A:** Before firing TP or SL on a position, check if a bot_config exists for that pair and `avg_in_progress = 1`. If so, delay (skip this cycle, up to 70s max before forcing close anyway).

**Change B:** Before calling the close function, write `last_close_reason` to the matching bot_config row.

### Step 3.1 — Write failing test

- [ ] Create `tests/test_bot_monitor.py`:

```python
"""
Tests for bot monitor helpers — last_close_reason and avg_in_progress guard.
These test the logic in isolation using mocks, not the full async monitor loop.
"""
import pytest
import asyncio


def test_set_bot_close_reason_on_tp(tmp_db):
    """After TP fires, last_close_reason should be 'tp'."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")

    # Simulate what monitor_position_triggers does before closing
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg is not None
    tmp_db.set_bot_close_reason(cfg["id"], "tp")

    updated = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert updated["last_close_reason"] == "tp"


def test_avg_in_progress_blocks_close(tmp_db):
    """When avg_in_progress=1, the bot status should block TP/SL in monitor."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")
    tmp_db.set_bot_avg_in_progress(cfg_id, True)

    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["avg_in_progress"] == 1
```

- [ ] Run to confirm they pass (these test the DB layer, not the monitor itself):

```bash
.venv/bin/pytest tests/test_bot_monitor.py -v
```

### Step 3.2 — Extend `monitor_position_triggers` in `backend/main.py`

Find the TP/SL block inside `monitor_position_triggers` around line 536–592. Add the two extensions:

**Extension A — avg_in_progress guard** (add inside the `if trigger:` block, BEFORE the actual close):

- [ ] Add `_avg_wait_start: dict[str, float] = {}` at the top of `monitor_position_triggers` alongside the other in-memory state variables (`closing_tags`, `alert_states`, etc.).

- [ ] After `if trigger:` and before `if (sym1, sym2) in closing_pairs:`, add:

```python
                        # Guard: if bot averaging is in progress for this pair,
                        # delay TP/SL up to 70s to avoid race condition.
                        # IMPORTANT: do NOT use closing_tags for this — it is
                        # pruned every cycle. Use _avg_wait_start exclusively.
                        bot_cfg = db.get_bot_config_by_pair(sym1, sym2)
                        if bot_cfg and bot_cfg.get("avg_in_progress"):
                            avg_wait_key = f"avg_wait_{pos_id}"
                            if avg_wait_key not in _avg_wait_start:
                                _avg_wait_start[avg_wait_key] = time.monotonic()
                                log.info(
                                    f"monitor: avg_in_progress for pos {pos_id}, "
                                    f"delaying {trigger.upper()} close (0s / 70s)"
                                )
                            elapsed = time.monotonic() - _avg_wait_start[avg_wait_key]
                            if elapsed < 70:
                                log.info(
                                    f"monitor: avg_in_progress for pos {pos_id}, "
                                    f"delaying {trigger.upper()} close ({elapsed:.0f}s / 70s)"
                                )
                                continue
                            else:
                                log.warning(
                                    f"monitor: avg_in_progress timeout (70s) for pos {pos_id}, "
                                    f"forcing {trigger.upper()} close"
                                )
                                db.set_bot_avg_in_progress(bot_cfg["id"], False)
                                del _avg_wait_start[avg_wait_key]
```

- [ ] Inside the `try:` block (not after `except`), immediately after `closing_tags &= current_tags` and before `alert_states = ...`, add stale key cleanup:

```python
            # Clean stale avg_wait keys for positions no longer tracked
            # Must be inside try: block — current_tags is defined there
            for k in list(_avg_wait_start):
                pos_id_str = k.replace("avg_wait_", "")
                if f"pos_{pos_id_str}" not in current_tags:
                    del _avg_wait_start[k]
```

**Extension B — write last_close_reason** (add after the `trigger` is determined, before calling `_do_smart_close_trigger` or `_do_market_close`):

- [ ] After `db.set_position_triggers(pos_id, None, None, False)`, add:

```python
                        # Inform bot monitor of the close reason before we close
                        if bot_cfg := db.get_bot_config_by_pair(sym1, sym2):
                            db.set_bot_close_reason(bot_cfg["id"], trigger)
```

Apply the same pattern for the standalone triggers section (around line 694–710): add the same `last_close_reason` write before the close calls for standalone `tp`/`sl` triggers that have a matching DB position.

### Step 3.3 — Run full test suite (no monitor async tests — we test via integration)

```bash
.venv/bin/pytest tests/ -v --tb=short
```

Expected: all tests pass.

### Step 3.4 — Commit

```bash
git add backend/main.py tests/test_bot_monitor.py
git commit -m "feat(monitor): write last_close_reason and respect avg_in_progress for bot positions"
```

---

## Task 4: New background task — `monitor_auto_trading` (entry + adoption)

**Files:**
- Modify: `backend/main.py`

This task adds the full bot entry loop. Read before starting:
- `backend/main.py` lines 438–460 (monitor_position_triggers header — copy the pattern)
- `backend/main.py` lines 1905–2050 (smart trade open pattern — we replicate it for auto-open)
- `backend/main.py` lines 944–971 (lifespan — we add the new task here)
- `docs/superpowers/specs/2026-03-21-auto-trading-bot-design.md` (full spec)

### Step 4.1 — Write tests for entry detection logic

- [ ] Add to `tests/test_bot_monitor.py`:

```python
def test_bot_status_transitions_waiting_to_in_position(tmp_db):
    """set_bot_status('in_position') moves status correctly."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "waiting")
    tmp_db.set_bot_status(cfg_id, "in_position")
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["status"] == "in_position"
    assert cfg["current_avg_level"] == 0  # not reset on in_position transition


def test_bot_status_reset_avg_level_on_waiting(tmp_db):
    """Transitioning to 'waiting' resets current_avg_level."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")
    tmp_db.increment_bot_avg_level(cfg_id)
    tmp_db.increment_bot_avg_level(cfg_id)
    # simulate TP: transition back to waiting
    tmp_db.set_bot_status(cfg_id, "waiting")
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["current_avg_level"] == 0


def test_bot_status_reset_avg_level_on_sl(tmp_db):
    """Transitioning to 'paused_after_sl' also resets current_avg_level."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")
    tmp_db.increment_bot_avg_level(cfg_id)
    tmp_db.set_bot_status(cfg_id, "paused_after_sl")
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["current_avg_level"] == 0
```

- [ ] Run to confirm they pass:

```bash
.venv/bin/pytest tests/test_bot_monitor.py -v
```

### Step 4.2 — Add `monitor_auto_trading` to `backend/main.py`

- [ ] Add this function immediately after `monitor_position_triggers` (before `reconcile_positions`):

```python
async def monitor_auto_trading() -> None:
    """
    Background task: auto-open positions and manage averaging for bot_configs.

    Runs every 2s. For each active bot_config (status='waiting'/'in_position'):
    - 'waiting': adopt existing position OR open new one when z-score hits entry_z
    - 'in_position': detect position close (update bot status) + fire averaging levels

    Exit logic (TP/SL) is handled by monitor_position_triggers — not here.
    """
    await asyncio.sleep(20)  # wait for PriceCache to populate
    _signal_first_seen: dict[int, float] = {}   # cfg_id → monotonic when signal first seen
    _bot_keys: dict[int, tuple] = {}            # cfg_id → PriceCache cache_key

    while True:
        try:
            active_cfgs = db.get_active_bot_configs()
            current_ids = {cfg["id"] for cfg in active_cfgs}

            # Unsubscribe PriceCache for bots no longer active
            for cid in list(_bot_keys):
                if cid not in current_ids:
                    price_cache.unsubscribe(_bot_keys.pop(cid))
                    _signal_first_seen.pop(cid, None)

            # Load watchlist once per cycle — not once per bot config
            wl_map = {w["id"]: w for w in db.get_watchlist()}

            for cfg in active_cfgs:
                cid = cfg["id"]
                sym1, sym2 = cfg["symbol1"], cfg["symbol2"]

                # Load watchlist params (timeframe, entry_z, sizing, leverage, etc.)
                wl = wl_map.get(cfg["watchlist_id"])
                if wl is None:
                    log.warning(
                        "BOT: watchlist item %s missing for bot cfg %s — disabling",
                        cfg["watchlist_id"], cid,
                    )
                    db.set_bot_status(cid, "disabled")
                    continue

                tf = wl.get("timeframe") or "1h"
                zw = int(wl.get("zwindow") or 20)
                limit = int(wl.get("candle_limit") or 500)
                entry_z = float(wl.get("entry_z") or 2.0)
                size_usd = float(wl.get("pos_size") or 1000)
                sizing = wl.get("sizing") or "ols"
                leverage = int(wl.get("leverage") or 1)

                # Subscribe to PriceCache if needed
                if cid not in _bot_keys:
                    _bot_keys[cid] = price_cache.subscribe(sym1, sym2, tf, limit)
                cache_entry = price_cache.get(_bot_keys[cid])
                if cache_entry is None:
                    continue  # data not ready yet

                p1 = cache_entry["price1"]
                p2 = cache_entry["price2"]
                if len(p1) < zw:
                    continue

                hedge = strategy.calculate_hedge_ratio(p1, p2)
                spread = strategy.calculate_spread(p1, p2, hedge)
                zscore_series = strategy.calculate_zscore(spread, window=zw)
                if zscore_series.dropna().empty:
                    continue
                current_z = float(zscore_series.dropna().iloc[-1])
                abs_z = abs(current_z)

                # ── WAITING ──────────────────────────────────────────────────
                if cfg["status"] == "waiting":
                    # 1. Adopt existing position (server restart / manual trade)
                    existing_pos = db.find_open_position(sym1, sym2)
                    if existing_pos:
                        log.info(
                            "BOT ADOPT | cfg=%s | %s/%s | existing pos %s",
                            cid, sym1, sym2, existing_pos["id"],
                        )
                        # Set TP/SL on position if not already set
                        if existing_pos.get("tp_zscore") is None:
                            db.set_position_triggers(
                                existing_pos["id"],
                                cfg["tp_zscore"],
                                cfg["sl_zscore"],
                                bool(cfg["tp_smart"]),
                                bool(cfg["sl_smart"]),
                            )
                        db.set_bot_status(cid, "in_position")
                        _signal_first_seen.pop(cid, None)
                        continue

                    # 2. Check entry signal
                    if abs_z >= entry_z:
                        now_mono = time.monotonic()
                        conf_min = cfg.get("confirmation_minutes") or 0
                        if conf_min > 0:
                            if cid not in _signal_first_seen:
                                _signal_first_seen[cid] = now_mono
                                log.info(
                                    "BOT SIGNAL | cfg=%s | %s/%s | z=%.3f >= %.2f "
                                    "| confirmation timer started (%dm)",
                                    cid, sym1, sym2, current_z, entry_z, conf_min,
                                )
                                continue
                            elapsed_min = (now_mono - _signal_first_seen[cid]) / 60
                            if elapsed_min < conf_min:
                                continue  # waiting for confirmation
                        # Confirmation passed (or not required) — check balance
                        try:
                            meta1, meta2 = await asyncio.gather(
                                client.get_market_info(_normalise_symbol(sym1)),
                                client.get_market_info(_normalise_symbol(sym2)),
                            )
                            margin_asset = _shared_margin_asset(meta1, meta2)
                            if margin_asset and client.has_creds:
                                balance = await client.get_balance(margin_asset)
                                free = balance.get("free", 0)
                                required = size_usd / leverage * 1.1
                                if free < required:
                                    log.warning(
                                        "BOT SKIP | cfg=%s | %s/%s | "
                                        "insufficient balance %.2f < %.2f %s",
                                        cid, sym1, sym2, free, required, margin_asset,
                                    )
                                    _signal_first_seen.pop(cid, None)
                                    continue
                        except Exception as e:
                            log.warning("BOT balance check error cfg=%s: %s", cid, e)
                            continue

                        # Determine entry side
                        side = "long_spread" if current_z < 0 else "short_spread"
                        log.info(
                            "BOT OPEN | cfg=%s | %s/%s | z=%.3f | side=%s | size=%.0f",
                            cid, sym1, sym2, current_z, side, size_usd,
                        )
                        _signal_first_seen.pop(cid, None)
                        try:
                            await _bot_open_position(
                                cfg=cfg, wl=wl, sym1=sym1, sym2=sym2,
                                side=side, size_usd=size_usd,
                                sizing=sizing, leverage=leverage,
                                current_z=current_z, hedge=hedge,
                            )
                            db.set_bot_status(cid, "in_position")
                        except Exception as e:
                            log.warning("BOT OPEN failed cfg=%s: %s", cid, e)
                    else:
                        _signal_first_seen.pop(cid, None)

                # ── IN_POSITION ───────────────────────────────────────────────
                elif cfg["status"] == "in_position":
                    pos = db.find_open_position(sym1, sym2)

                    if pos is None:
                        # Position is gone — read close reason set by monitor_position_triggers
                        reason = cfg.get("last_close_reason") or "sl"
                        if reason == "tp":
                            log.info(
                                "BOT TP | cfg=%s | %s/%s → waiting", cid, sym1, sym2
                            )
                            db.set_bot_status(cid, "waiting")
                        else:
                            log.info(
                                "BOT SL/LIQ | cfg=%s | %s/%s reason=%s → paused_after_sl",
                                cid, sym1, sym2, reason,
                            )
                            db.set_bot_status(cid, "paused_after_sl")
                            asyncio.create_task(
                                tg_bot.notify_bot_paused(sym1, sym2, reason)
                            )
                        _signal_first_seen.pop(cid, None)
                        continue

                    # Check averaging
                    avg_levels_raw = cfg.get("avg_levels_json")
                    if avg_levels_raw and not cfg.get("avg_in_progress"):
                        import json as _json
                        try:
                            avg_levels = _json.loads(avg_levels_raw)
                        except Exception:
                            avg_levels = []
                        current_level = cfg.get("current_avg_level") or 0
                        if current_level < len(avg_levels):
                            next_level = avg_levels[current_level]
                            next_z = float(next_level["z"])
                            next_size = float(next_level["size_usd"])
                            if abs_z >= next_z:
                                log.info(
                                    "BOT AVG | cfg=%s | %s/%s | level=%d | z=%.3f >= %.2f",
                                    cid, sym1, sym2, current_level, abs_z, next_z,
                                )
                                db.set_bot_avg_in_progress(cid, True)
                                asyncio.create_task(
                                    _bot_averaging_task(
                                        cfg_id=cid,
                                        pos=pos,
                                        sym1=sym1, sym2=sym2,
                                        size_usd=next_size,
                                        sizing=sizing,
                                        leverage=leverage,
                                        current_z=current_z,
                                        hedge=hedge,
                                        p1=p1, p2=p2,
                                    )
                                )

        except Exception as e:
            log.warning("monitor_auto_trading outer error: %s", e)

        await asyncio.sleep(2)
```

### Step 4.3 — Add helper `_bot_open_position` to `backend/main.py`

**Important:** `ExecContext` (in `order_manager.py`) does NOT have `tp_zscore`/`sl_zscore`/`tp_smart`/`sl_smart` fields. After `run_execution` completes, set TP/SL on the new DB position using `db.set_position_triggers()`.

- [ ] Add before `monitor_auto_trading`:

```python
async def _bot_open_position(
    cfg: dict, wl: dict, sym1: str, sym2: str,
    side: str, size_usd: float, sizing: str, leverage: int,
    current_z: float, hedge: float,
) -> None:
    """Open a new position for the bot via smart execution, then set TP/SL."""
    import uuid

    ticker1, ticker2 = await asyncio.gather(
        client.fetch_ticker(sym1),
        client.fetch_ticker(sym2),
    )
    price1 = ticker1["last"]
    price2 = ticker2["last"]

    # Min-notional check before placing orders
    sizes = strategy.calculate_position_sizes(
        price1=price1, price2=price2,
        size_usd=size_usd, hedge_ratio=hedge,
        atr1=None, atr2=None, method=sizing,
    )
    qty1 = sizes["qty1"]
    qty2 = sizes["qty2"]

    (ok1, notional1, min1), (ok2, notional2, min2) = await asyncio.gather(
        client.check_min_notional(sym1, qty1, price1),
        client.check_min_notional(sym2, qty2, price2),
    )
    if not ok1:
        raise ValueError(f"{sym1}: notional ${notional1:.2f} < min ${min1:.2f}")
    if not ok2:
        raise ValueError(f"{sym2}: notional ${notional2:.2f} < min ${min2:.2f}")

    side1, side2 = ("buy", "sell") if side == "long_spread" else ("sell", "buy")

    exec_id = uuid.uuid4().hex[:8]
    cfg_exec = ExecConfig(passive_s=30.0, aggressive_s=20.0, allow_market=True)
    ctx = ExecContext(
        exec_id=exec_id,
        leg1=LegState(symbol=sym1, side=side1, qty=qty1),
        leg2=LegState(symbol=sym2, side=side2, qty=qty2),
        config=cfg_exec,
        spread_side=side,
        is_close=False,
        hedge_ratio=hedge,
        entry_zscore=current_z,
        size_usd=size_usd,
        sizing_method=sizing,
        leverage=leverage,
        timeframe=wl.get("timeframe") or "1h",
        candle_limit=int(wl.get("candle_limit") or 500),
        zscore_window=int(wl.get("zwindow") or 20),
    )
    for sym in (sym1, sym2):
        if sym not in _book_feeds:
            feed = BookTickerFeed(sym)
            feed.start()
            _book_feeds[sym] = feed
    ctx.book_feeds = _book_feeds
    ctx.user_data_feed = _user_data_feed
    active_executions[exec_id] = ctx
    _exec_created_at[exec_id] = time.monotonic()

    # Awaited so we know the position exists before setting in_position status.
    # NOTE: run_execution can take up to 50s (passive_s=30 + aggressive_s=20).
    # This blocks monitor_auto_trading for that duration — if 2+ bots open
    # simultaneously, they queue sequentially. Acceptable for the current usage
    # scale; revisit if >5 concurrent bots are needed.
    await run_execution(ctx, client, db)

    # Set TP/SL on the new DB position (ExecContext does not carry these fields)
    # Also validates that the position was actually created (ROLLBACK → no position → raise)
    new_pos = db.find_open_position(sym1, sym2)
    if new_pos:
        db.set_position_triggers(
            new_pos["id"],
            cfg["tp_zscore"],
            cfg["sl_zscore"],
            bool(cfg["tp_smart"]),
            bool(cfg["sl_smart"]),
        )
    else:
        # run_execution ended in ROLLBACK or FAILED — position was never opened
        raise RuntimeError(
            f"run_execution completed but no open position for {sym1}/{sym2} "
            f"(exec={exec_id}, status={ctx.status})"
        )
        # Caller (monitor_auto_trading) catches this, logs warning, leaves bot in 'waiting'
```

### Step 4.4 — Register task in lifespan

- [ ] In `backend/main.py` lifespan, add `monitor_auto_trading` to `_bg_tasks`:

```python
    _bg_tasks = [
        asyncio.create_task(price_cache.run()),
        asyncio.create_task(monitor_position_triggers()),
        asyncio.create_task(monitor_auto_trading()),   # ← add this
        asyncio.create_task(tg_bot.start_polling()),
        asyncio.create_task(reconcile_positions()),
        asyncio.create_task(health_check_coint()),
    ]
```

### Step 4.5 — Run tests

```bash
.venv/bin/pytest tests/ -v --tb=short
```

Expected: all tests pass (monitor_auto_trading is not unit-tested as an async loop — integration tested manually).

### Step 4.6 — Commit

```bash
git add backend/main.py
git commit -m "feat(bot): add monitor_auto_trading background task with entry detection and position adoption"
```

---

## Task 5: Averaging task helper

**Files:**
- Modify: `backend/main.py`

### Step 5.1 — Add `_bot_averaging_task` to `backend/main.py`

- [ ] Add after `_bot_open_position`:

```python
async def _bot_averaging_task(
    cfg_id: int,
    pos: dict,
    sym1: str, sym2: str,
    size_usd: float, sizing: str, leverage: int,
    current_z: float, hedge: float, p1, p2,
) -> None:
    """
    Coroutine launched as asyncio.Task to add an averaging entry.
    Sets avg_in_progress=0 when done regardless of outcome.
    """
    import uuid
    try:
        ticker1, ticker2 = await asyncio.gather(
            client.fetch_ticker(sym1),
            client.fetch_ticker(sym2),
        )
        price1 = ticker1["last"]
        price2 = ticker2["last"]

        sizes = strategy.calculate_position_sizes(
            price1=price1, price2=price2,
            size_usd=size_usd, hedge_ratio=hedge,
            atr1=None, atr2=None, method=sizing,
        )
        qty1 = sizes["qty1"]
        qty2 = sizes["qty2"]

        # Same direction as existing position
        side1, side2 = ("buy", "sell") if pos["side"] == "long_spread" else ("sell", "buy")

        exec_id = uuid.uuid4().hex[:8]
        cfg_exec = ExecConfig(passive_s=30.0, aggressive_s=20.0, allow_market=True)
        ctx = ExecContext(
            exec_id=exec_id,
            leg1=LegState(symbol=sym1, side=side1, qty=qty1),
            leg2=LegState(symbol=sym2, side=side2, qty=qty2),
            config=cfg_exec,
            spread_side=pos["side"],
            is_close=False,
            is_average=True,
            average_position_id=pos["id"],
            hedge_ratio=hedge,
            entry_zscore=current_z,
            size_usd=size_usd,
            sizing_method=sizing,
            leverage=pos.get("leverage") or 1,
        )
        for sym in (sym1, sym2):
            if sym not in _book_feeds:
                feed = BookTickerFeed(sym)
                feed.start()
                _book_feeds[sym] = feed
        ctx.book_feeds = _book_feeds
        ctx.user_data_feed = _user_data_feed
        active_executions[exec_id] = ctx
        _exec_created_at[exec_id] = time.monotonic()

        await run_execution(ctx, client, db)

        # Check if position still exists after fill
        if db.find_open_position(sym1, sym2):
            db.increment_bot_avg_level(cfg_id)
            log.info("BOT AVG DONE | cfg=%s | %s/%s | exec=%s", cfg_id, sym1, sym2, exec_id)
        else:
            log.warning(
                "BOT AVG ORPHAN | cfg=%s | %s/%s | position gone at fill time",
                cfg_id, sym1, sym2,
            )
    except Exception as e:
        log.warning("BOT AVG FAILED | cfg=%s | %s/%s: %s", cfg_id, sym1, sym2, e)
    finally:
        db.set_bot_avg_in_progress(cfg_id, False)
```

### Step 5.2 — Run tests

```bash
.venv/bin/pytest tests/ -v --tb=short
```

### Step 5.3 — Commit

```bash
git add backend/main.py
git commit -m "feat(bot): add averaging task helper with orphan detection"
```

---

## Task 6: API Endpoints

**Files:**
- Modify: `backend/main.py`

### Step 6.1 — Add Pydantic models and 5 endpoints

- [ ] Find the Pydantic models block (around `WatchlistItemDB`, `WatchlistStats` etc.) and add:

```python
class BotConfigRequest(BaseModel):
    watchlist_id: int
    symbol1: str
    symbol2: str
    tp_zscore: float
    sl_zscore: float
    tp_smart: bool = True
    sl_smart: bool = True
    confirmation_minutes: int = 0
    avg_levels_json: Optional[str] = None  # JSON string


@app.get("/api/bot/configs")
async def list_bot_configs():
    return {"configs": db.get_bot_configs()}


@app.post("/api/bot/configs")
async def upsert_bot_config(req: BotConfigRequest):
    cfg_id = db.save_bot_config(
        watchlist_id=req.watchlist_id,
        symbol1=_normalise_symbol(req.symbol1),
        symbol2=_normalise_symbol(req.symbol2),
        tp_zscore=req.tp_zscore,
        sl_zscore=req.sl_zscore,
        tp_smart=req.tp_smart,
        sl_smart=req.sl_smart,
        confirmation_minutes=req.confirmation_minutes,
        avg_levels_json=req.avg_levels_json,
    )
    return {"id": cfg_id}


@app.delete("/api/bot/configs/{config_id}")
async def delete_bot_config(config_id: int):
    ok = db.delete_bot_config(config_id)
    if not ok:
        raise HTTPException(404, f"Bot config {config_id} not found")
    return {"ok": True}


@app.patch("/api/bot/configs/{config_id}/enable")
async def enable_bot_config(config_id: int):
    ok = db.set_bot_status(config_id, "waiting")
    if not ok:
        raise HTTPException(404, f"Bot config {config_id} not found")
    return {"status": "waiting"}


@app.patch("/api/bot/configs/{config_id}/disable")
async def disable_bot_config(config_id: int):
    ok = db.set_bot_status(config_id, "disabled")
    if not ok:
        raise HTTPException(404, f"Bot config {config_id} not found")
    return {"status": "disabled"}
```

### Step 6.2 — Run tests

```bash
.venv/bin/pytest tests/ -v --tb=short
```

### Step 6.3 — Manual smoke test (server must be running)

```bash
# Start server
cd /Users/y.shvydak/Projects/pair_trading/backend
../.venv/bin/uvicorn main:app --reload --port 8080

# In another terminal:
curl -s http://localhost:8080/api/bot/configs | python3 -m json.tool
# Expected: {"configs": []}
```

### Step 6.4 — Commit

```bash
git add backend/main.py
git commit -m "feat(api): add /api/bot/configs endpoints (list, upsert, delete, enable, disable)"
```

---

## Task 7: Frontend — BOT badge in watchlist + Bot Config Modal

**Files:**
- Modify: `frontend/index.html`

This is the largest frontend task. Read first:
- `frontend/index.html` — search for `wl_alert_btn`, `addAlertTrigger`, `renderWatchlist` to understand the 🔔 bell pattern. The BOT badge follows the exact same pattern.

### Step 7.1 — Add i18n keys

- [ ] In `frontend/index.html`, find the `I18N` object (search for `"wl_alert_btn"`) and add:

```javascript
// in I18N.ru:
wl_bot_btn: "BOT",
wl_bot_waiting: "Ждёт",
wl_bot_in_position: "В позиции",
wl_bot_paused: "Пауза",
bot_modal_title: "Авто-торговля",
bot_tp: "Take Profit (z-score)",
bot_sl: "Stop Loss (z-score)",
bot_tp_smart: "Смарт-исполнение TP",
bot_sl_smart: "Смарт-исполнение SL",
bot_confirmation: "Подтверждение сигнала",
bot_confirmation_unit: "мин",
bot_avg_levels: "Усреднение",
bot_avg_add: "+ Уровень",
bot_save: "Сохранить",
bot_enable: "Включить",
bot_disable: "Выключить",

// in I18N.en: (same keys, English values)
wl_bot_btn: "BOT",
wl_bot_waiting: "Waiting",
wl_bot_in_position: "In Position",
wl_bot_paused: "Paused",
bot_modal_title: "Auto-Trading",
bot_tp: "Take Profit (z-score)",
bot_sl: "Stop Loss (z-score)",
bot_tp_smart: "Smart execution TP",
bot_sl_smart: "Smart execution SL",
bot_confirmation: "Signal confirmation",
bot_confirmation_unit: "min",
bot_avg_levels: "Averaging",
bot_avg_add: "+ Level",
bot_save: "Save",
bot_enable: "Enable",
bot_disable: "Disable",
```

### Step 7.2 — Add `_cachedBotConfigs` state and `refreshBotConfigs()`

- [ ] Find where `_cachedAlerts` is defined (module-level) and add nearby:

```javascript
let _cachedBotConfigs = []; // fetched from GET /api/bot/configs

async function refreshBotConfigs() {
    try {
        const r = await fetch('/api/bot/configs');
        const data = await r.json();
        _cachedBotConfigs = data.configs || [];
    } catch(e) {
        console.warn('refreshBotConfigs error', e);
    }
}

function _getBotConfigForWatchlistItem(w) {
    // Match on watchlist_id — the same symbol pair can appear in multiple
    // watchlist rows (different timeframes), each with its own bot config.
    return _cachedBotConfigs.find(c => c.watchlist_id === w.id) || null;
}
```

### Step 7.3 — Add BOT badge to `renderWatchlist()`

- [ ] In `renderWatchlist()`, find where the 🔔 bell badge is rendered (search for `wl_alert_btn` or `addAlertFromRow`). Add the BOT badge immediately after it using the same `classList`/`style.color` pattern (remember: dynamic Tailwind classes don't work — use inline styles):

```javascript
// BOT badge — same pattern as bell badge
const botCfg = _getBotConfigForWatchlistItem(w);
const botBadge = document.createElement('span');
botBadge.textContent = t('wl_bot_btn');
botBadge.className = 'text-xs font-bold px-1 py-0.5 rounded cursor-pointer select-none ml-1';
botBadge.style.border = '1px solid currentColor';

const BOT_COLORS = {
    disabled: '#6b7280',     // gray
    waiting: '#22c55e',      // green
    in_position: '#eab308',  // yellow
    paused_after_sl: '#ef4444', // red
};
const botStatus = botCfg ? botCfg.status : 'disabled';
botBadge.style.color = BOT_COLORS[botStatus] || BOT_COLORS.disabled;
botBadge.style.opacity = botCfg ? '1' : '0.4';

botBadge.title = botCfg ? (t('wl_bot_' + botStatus.replace('paused_after_sl','paused')) || botStatus) : t('wl_bot_btn');

botBadge.onclick = (e) => {
    e.stopPropagation();
    openBotConfigModal(w, botCfg);
};

// append after bell badge
```

### Step 7.4 — Add `openBotConfigModal()` function

- [ ] Add this function (before `renderWatchlist`):

```javascript
function openBotConfigModal(wlItem, existingCfg) {
    // Pre-fill from existingCfg or from watchlist defaults
    const tp = existingCfg ? existingCfg.tp_zscore : (parseFloat(wlItem.exit_z) || 0.5);
    const sl = existingCfg ? existingCfg.sl_zscore : ((parseFloat(wlItem.entry_z) || 2.0) * 2);
    const tpSmart = existingCfg ? !!existingCfg.tp_smart : true;
    const slSmart = existingCfg ? !!existingCfg.sl_smart : true;
    const confMin = existingCfg ? (existingCfg.confirmation_minutes || 0) : 0;
    const avgLevels = existingCfg && existingCfg.avg_levels_json
        ? JSON.parse(existingCfg.avg_levels_json)
        : [];
    const isEnabled = existingCfg && existingCfg.status !== 'disabled';
    const statusLabel = existingCfg
        ? (t('wl_bot_' + existingCfg.status.replace('paused_after_sl','paused')) || existingCfg.status)
        : t('wl_bot_btn');

    // Build modal HTML
    const pair = `${wlItem.symbol1.split(':')[0]} / ${wlItem.symbol2.split(':')[0]}`;

    let avgRowsHtml = avgLevels.map((lvl, i) => `
        <div class="flex gap-2 items-center" id="avg-row-${i}">
            <input type="number" step="0.1" value="${lvl.z}" class="w-20 bg-gray-700 text-white rounded px-2 py-1 text-sm avg-z" placeholder="z">
            <input type="number" step="1" value="${lvl.size_usd}" class="w-24 bg-gray-700 text-white rounded px-2 py-1 text-sm avg-size" placeholder="USD">
            <button onclick="this.closest('[id^=avg-row]').remove()" class="text-red-400 text-sm">✕</button>
        </div>
    `).join('');

    const html = `
    <div id="bot-modal-overlay" class="fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-50" onclick="if(event.target.id==='bot-modal-overlay')closeBotModal()">
      <div class="bg-gray-800 rounded-lg p-5 w-80 text-white text-sm" onclick="event.stopPropagation()">
        <div class="flex justify-between items-center mb-3">
          <div>
            <div class="font-bold text-base">${t('bot_modal_title')}</div>
            <div class="text-gray-400 text-xs">${pair}</div>
          </div>
          <div class="text-xs text-gray-400">${statusLabel}</div>
        </div>
        <div class="space-y-2">
          <div class="flex justify-between items-center">
            <label class="text-gray-300">${t('bot_tp')}</label>
            <input id="bot-tp" type="number" step="0.1" value="${tp}" class="w-20 bg-gray-700 text-white rounded px-2 py-1 text-sm">
          </div>
          <div class="flex justify-between items-center">
            <label class="text-gray-300">${t('bot_sl')}</label>
            <input id="bot-sl" type="number" step="0.1" value="${sl}" class="w-20 bg-gray-700 text-white rounded px-2 py-1 text-sm">
          </div>
          <div class="flex justify-between items-center">
            <label class="text-gray-300">${t('bot_tp_smart')}</label>
            <input id="bot-tp-smart" type="checkbox" ${tpSmart ? 'checked' : ''}>
          </div>
          <div class="flex justify-between items-center">
            <label class="text-gray-300">${t('bot_sl_smart')}</label>
            <input id="bot-sl-smart" type="checkbox" ${slSmart ? 'checked' : ''}>
          </div>
          <div class="flex justify-between items-center">
            <label class="text-gray-300">${t('bot_confirmation')}</label>
            <div class="flex items-center gap-1">
              <input id="bot-conf-min" type="number" min="0" step="1" value="${confMin}" class="w-16 bg-gray-700 text-white rounded px-2 py-1 text-sm">
              <span class="text-gray-400">${t('bot_confirmation_unit')}</span>
            </div>
          </div>
        </div>
        <div class="mt-3 border-t border-gray-700 pt-3">
          <div class="text-gray-400 mb-2">${t('bot_avg_levels')}</div>
          <div id="avg-levels-container" class="space-y-1">${avgRowsHtml}</div>
          <button onclick="addAvgLevelRow()" class="mt-1 text-xs text-blue-400 hover:text-blue-300">${t('bot_avg_add')}</button>
        </div>
        <div class="flex gap-2 mt-4">
          <button onclick="closeBotModal()" class="flex-1 bg-gray-700 hover:bg-gray-600 rounded py-1">${t('cancel') || 'Отмена'}</button>
          <button onclick="saveBotConfig(${wlItem.id}, ${existingCfg ? existingCfg.id : 'null'})"
            class="flex-1 bg-blue-600 hover:bg-blue-500 rounded py-1">${t('bot_save')}</button>
        </div>
        ${existingCfg ? `
        <div class="flex gap-2 mt-2">
          ${isEnabled
            ? `<button onclick="toggleBotEnabled(${existingCfg.id}, false)" class="flex-1 text-xs bg-gray-700 hover:bg-gray-600 rounded py-1">${t('bot_disable')}</button>`
            : `<button onclick="toggleBotEnabled(${existingCfg.id}, true)" class="flex-1 text-xs bg-green-700 hover:bg-green-600 rounded py-1">${t('bot_enable')}</button>`
          }
        </div>` : ''}
      </div>
    </div>`;

    document.body.insertAdjacentHTML('beforeend', html);
    // Store wlItem ref for save
    window._botModalWlItem = wlItem;
}

function closeBotModal() {
    document.getElementById('bot-modal-overlay')?.remove();
}

function addAvgLevelRow() {
    const container = document.getElementById('avg-levels-container');
    const i = container.children.length;
    const row = document.createElement('div');
    row.className = 'flex gap-2 items-center';
    row.id = `avg-row-${i}`;
    row.innerHTML = `
        <input type="number" step="0.1" class="w-20 bg-gray-700 text-white rounded px-2 py-1 text-sm avg-z" placeholder="z">
        <input type="number" step="1" class="w-24 bg-gray-700 text-white rounded px-2 py-1 text-sm avg-size" placeholder="USD">
        <button onclick="this.closest('div').remove()" class="text-red-400 text-sm">✕</button>`;
    container.appendChild(row);
}

async function saveBotConfig(wlId, existingCfgId) {
    const wlItem = window._botModalWlItem;
    const tp = parseFloat(document.getElementById('bot-tp').value);
    const sl = parseFloat(document.getElementById('bot-sl').value);
    const tpSmart = document.getElementById('bot-tp-smart').checked;
    const slSmart = document.getElementById('bot-sl-smart').checked;
    const confMin = parseInt(document.getElementById('bot-conf-min').value) || 0;

    // Collect averaging levels
    const avgLevels = [];
    document.querySelectorAll('#avg-levels-container .avg-z').forEach((zInput, i) => {
        const sizeInput = document.querySelectorAll('#avg-levels-container .avg-size')[i];
        const z = parseFloat(zInput.value);
        const size = parseFloat(sizeInput.value);
        if (!isNaN(z) && !isNaN(size) && z > 0 && size > 0) {
            avgLevels.push({z, size_usd: size});
        }
    });

    const body = {
        watchlist_id: wlId,
        symbol1: wlItem.symbol1,
        symbol2: wlItem.symbol2,
        tp_zscore: tp,
        sl_zscore: sl,
        tp_smart: tpSmart,
        sl_smart: slSmart,
        confirmation_minutes: confMin,
        avg_levels_json: avgLevels.length ? JSON.stringify(avgLevels) : null,
    };

    try {
        const r = await fetch('/api/bot/configs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(await r.text());
        const data = await r.json();

        // Re-enable only if was in 'waiting' state.
        // If was 'in_position': upsert preserves status, no enable call needed.
        // Calling /enable when in_position would reset current_avg_level and
        // transition bot to 'waiting', breaking an active position.
        // Look up prevStatus from cache using existingCfgId (existingCfg is
        // out of scope here — it belongs to openBotConfigModal's closure).
        const prevStatus = existingCfgId
            ? (_cachedBotConfigs.find(c => c.id === existingCfgId)?.status ?? 'disabled')
            : 'disabled';
        if (prevStatus === 'waiting') {
            await fetch(`/api/bot/configs/${data.id}/enable`, {method: 'PATCH'});
        }

        closeBotModal();
        await refreshBotConfigs();
        renderWatchlist();
    } catch(e) {
        alert('Error saving bot config: ' + e.message);
    }
}

async function toggleBotEnabled(cfgId, enable) {
    const url = enable ? `/api/bot/configs/${cfgId}/enable` : `/api/bot/configs/${cfgId}/disable`;
    await fetch(url, {method: 'PATCH'});
    closeBotModal();
    await refreshBotConfigs();
    renderWatchlist();
}
```

### Step 7.5 — Call `refreshBotConfigs()` at startup

- [ ] Find the startup `Promise.all([initWatchlist(), refreshTriggersCache()])` call and add `refreshBotConfigs()`:

```javascript
Promise.all([initWatchlist(), refreshTriggersCache(), refreshBotConfigs()])
    .then(() => { renderWatchlist(); connectWatchlistWS(); });
```

### Step 7.6 — Refresh bot configs in the 5s positions poller

- [ ] In `loadAllPositions()` (called every 5s), add `refreshBotConfigs()` call (non-blocking) to keep badge colors up to date:

```javascript
// at the top of loadAllPositions, alongside existing refreshes:
refreshBotConfigs().then(() => renderWatchlist());
```

### Step 7.7 — Test in browser

Start server, open `http://localhost:8080`, verify:
- [ ] BOT badge appears on each watchlist row (gray when no config)
- [ ] Click opens modal with correct pre-filled values
- [ ] Save creates config, badge turns green
- [ ] Enable/Disable toggles work, badge color updates
- [ ] Page reload preserves state

### Step 7.8 — Commit

```bash
git add frontend/index.html
git commit -m "feat(frontend): add BOT badge to watchlist and Bot Config Modal"
```

---

## Task 8: Frontend — Z-score chart overlay (bot lines)

**Files:**
- Modify: `frontend/index.html`

Read: search for `renderSpreadChart` and `chartjs-plugin-annotation` usage in `index.html` to understand how existing horizontal lines (entry_z, exit_z) are drawn.

### Step 8.1 — Add `_getBotLinesForCurrentPair()` helper

- [ ] Add near the chart rendering functions:

```javascript
function _getBotLinesForCurrentPair() {
    // Returns bot TP/SL/avg line values if bot is active for current pair
    if (!state.sym1 || !state.sym2) return null;
    const cfg = _cachedBotConfigs.find(c =>
        c.symbol1 === state.sym1 && c.symbol2 === state.sym2 &&
        c.status !== 'disabled'
    );
    if (!cfg) return null;
    const avgLevels = cfg.avg_levels_json ? JSON.parse(cfg.avg_levels_json) : [];
    return {
        tp: cfg.tp_zscore,
        sl: cfg.sl_zscore,
        avgZscores: avgLevels.map(l => l.z),
    };
}
```

### Step 8.2 — Extend `renderSpreadChart()` to draw bot lines when active

- [ ] In `renderSpreadChart()`, find where `entry_z` / `exit_z` annotation lines are built (search for `annotation` or `entryZ`). Add a conditional block after the existing lines:

```javascript
// Bot overlay lines — only when bot is active for this pair
const botLines = _getBotLinesForCurrentPair();
if (botLines) {
    // TP lines (±tp_zscore, green dashed)
    [botLines.tp, -botLines.tp].forEach(val => {
        annotations[`bot_tp_${val}`] = {
            type: 'line', yMin: val, yMax: val,
            borderColor: '#22c55e', borderWidth: 1,
            borderDash: [4, 4],
            label: { display: false },
        };
    });
    // SL lines (±sl_zscore, red dashed)
    [botLines.sl, -botLines.sl].forEach(val => {
        annotations[`bot_sl_${val}`] = {
            type: 'line', yMin: val, yMax: val,
            borderColor: '#ef4444', borderWidth: 1,
            borderDash: [4, 4],
            label: { display: false },
        };
    });
    // Averaging lines (yellow dashed)
    botLines.avgZscores.forEach((z, i) => {
        [z, -z].forEach(val => {
            annotations[`bot_avg_${i}_${val}`] = {
                type: 'line', yMin: val, yMax: val,
                borderColor: '#eab308', borderWidth: 1,
                borderDash: [2, 4],
                label: { display: false },
            };
        });
    });
} else {
    // Bot inactive — show watchlist lines (entry_z, exit_z) as currently
    // (existing code — no change needed here if it already runs by default)
}
```

**Note:** If the existing code always shows entry_z/exit_z lines, wrap them in an `else` so they're hidden when bot is active (per spec: bot lines replace watchlist lines).

### Step 8.3 — Test in browser

- [ ] Enable bot on a pair, run Analyze — bot TP/SL lines appear on z-score chart
- [ ] Disable bot, run Analyze — watchlist entry_z/exit_z lines reappear
- [ ] With averaging levels set, yellow dashed lines appear

### Step 8.4 — Commit

```bash
git add frontend/index.html
git commit -m "feat(frontend): show bot TP/SL/avg lines on z-score chart when bot is active"
```

---

## Task 9: Frontend — Positions tab BOT button

**Files:**
- Modify: `frontend/index.html`

Read: search for `loadAllPositions` and the positions row rendering (search for `pos.tp_smart`, `AUTO` or how the `↗`, `✕ M`, `◎ S`, `🗑` buttons are built in the positions table).

### Step 9.1 — Add `AUTO` badge to bot-opened positions

- [ ] In the positions row rendering, identify bot-opened positions by checking `_cachedBotConfigs`. A position is bot-opened if its `symbol1`/`symbol2` matches an `in_position` bot config. Add:

```javascript
const isBotPos = _cachedBotConfigs.some(c =>
    c.symbol1 === pos.symbol1 && c.symbol2 === pos.symbol2 &&
    c.status === 'in_position'
);
// In the row HTML, add badge:
// <span class="text-xs font-bold px-1 rounded" style="color:#eab308;border:1px solid #eab308">AUTO</span>
```

### Step 9.2 — Add BOT button to each position row

- [ ] After the existing action buttons (↗, ✕ M, ◎ S, 🗑), add:

```javascript
<button onclick="openBotConfigFromPosition(${JSON.stringify(pos).replace(/"/g,'&quot;')})"
    class="text-xs px-1 py-0.5 rounded"
    style="color:#6b7280;border:1px solid #6b7280">BOT</button>
```

Add the handler:

```javascript
async function openBotConfigFromPosition(pos) {
    // Find watchlist item for this pair
    const wlItem = _watchlistItems.find(w =>
        w.symbol1 === pos.symbol1 && w.symbol2 === pos.symbol2
    );
    if (!wlItem) {
        alert(t('bot_no_watchlist') || 'Добавьте пару в Watchlist для настройки бота');
        return;
    }
    const existingCfg = _getBotConfigForWatchlistItem(wlItem);
    openBotConfigModal(wlItem, existingCfg);
}
```

Add i18n key: `bot_no_watchlist: "Добавьте пару в Watch List для настройки бота"` (ru) / `"Add pair to Watch List to configure the bot"` (en).

### Step 9.3 — Test in browser

- [ ] Open a position manually, go to Positions tab, see BOT button
- [ ] Click BOT → modal opens pre-filled from watchlist (or prompts to add to watchlist)
- [ ] Save config → bot badge turns green on watchlist row
- [ ] Position opened by bot shows AUTO badge

### Step 9.4 — Commit

```bash
git add frontend/index.html
git commit -m "feat(frontend): add BOT button and AUTO badge to positions tab"
```

---

## Final: Integration Test

- [ ] Start server: `cd backend && ../.venv/bin/uvicorn main:app --reload --port 8080`
- [ ] Open `http://localhost:8080`
- [ ] Add a pair to watchlist (e.g. BTC/ETH)
- [ ] Click BOT badge → configure TP=0.5, SL=4.0, enable
- [ ] Watch server logs — after z-score crosses entry_z, bot should open position (`BOT OPEN | cfg=...`)
- [ ] Position appears in Positions tab with `AUTO` badge
- [ ] When z returns to TP level, `monitor_position_triggers` closes it, bot logs `BOT TP | cfg=...`
- [ ] Bot returns to `waiting` (green badge)

- [ ] Run full test suite one last time:

```bash
cd /Users/y.shvydak/Projects/pair_trading
.venv/bin/pytest tests/ -v --tb=short
```

Expected: all tests pass.
