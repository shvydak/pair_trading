# Auto-Trading Bot — Design Spec

**Date:** 2026-03-21
**Status:** Approved
**Feature:** Per-pair automated trading bot with averaging, TP/SL, and signal confirmation

---

## Overview

Automated pair trading bot that opens and closes positions based on z-score signals, reusing the existing execution infrastructure (order_manager, monitor_position_triggers, PriceCache). Each watchlist pair can have its own bot configuration, independently enabled or disabled.

---

## Decisions Made

| Question | Decision |
|---|---|
| After TP: re-enter? | Yes, immediately — bot returns to `waiting` |
| After SL: re-enter? | No — bot transitions to `paused_after_sl`, user re-enables manually |
| Averaging | Optional. Manual levels: each level has its own z-threshold and size_usd |
| Averaging amounts | User-specified per level (typically decreasing) |
| TP execution | One smart order (both legs simultaneously) |
| SL behavior | Disables auto-trading for the pair |
| Confirmation filter | Optional time-based (`confirmation_minutes`). 0 = disabled |
| Post-SL re-enable | Manual only (toggle in watchlist or positions tab) |
| Overlapping symbols | Allowed — independent trades, user manages portfolio risk |

---

## Data Model

### New table: `bot_configs`

```sql
CREATE TABLE IF NOT EXISTS bot_configs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id         INTEGER REFERENCES watchlist(id) ON DELETE CASCADE,
    symbol1              TEXT NOT NULL,
    symbol2              TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'disabled',
    -- status values: 'disabled' | 'waiting' | 'in_position' | 'paused_after_sl'
    last_close_reason    TEXT,
    -- 'tp' | 'sl' | 'liquidation' | 'manual' — written by monitor_position_triggers before closing
    tp_zscore            REAL NOT NULL,
    sl_zscore            REAL NOT NULL,
    tp_smart             INTEGER NOT NULL DEFAULT 1,
    sl_smart             INTEGER NOT NULL DEFAULT 1,
    confirmation_minutes INTEGER NOT NULL DEFAULT 0,
    avg_levels_json      TEXT,
    -- JSON: [{"z": 2.5, "size_usd": 300}, {"z": 3.0, "size_usd": 200}]
    current_avg_level    INTEGER NOT NULL DEFAULT 0,
    avg_in_progress      INTEGER NOT NULL DEFAULT 0,
    -- 1 while an averaging smart execution is running; blocks TP/SL from firing
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
```

**Design rationale:**
- Separate from `watchlist` table — bot execution config is distinct from analysis config
- Denormalized `symbol1`/`symbol2` — avoids JOIN in the hot monitor loop
- `watchlist_id` FK with `ON DELETE CASCADE` — if watchlist item is removed, bot config is removed too
- `last_close_reason` — written by `monitor_position_triggers` *before* closing; bot reads it to decide next state (avoids unreliable post-hoc exit_zscore comparison)
- `avg_in_progress` — prevents TP/SL close while an averaging execution is running
- `current_avg_level` — tracks how many averaging entries have been placed for the current position
- No changes to existing tables (`watchlist`, `open_positions`, `triggers`)
- **`disabled` is the explicit off state** — distinct from `paused_after_sl` which means "stopped due to SL hit"

### Status values

| Status | Meaning |
|---|---|
| `disabled` | Bot is off. User disabled it manually |
| `waiting` | Bot is on, no position open, watching for entry signal |
| `in_position` | Position is open, bot monitors averaging and waits for TP/SL |
| `paused_after_sl` | SL hit — bot automatically disabled itself. Requires manual re-enable |

### Existing infrastructure reused as-is

- `open_positions` — bot-opened positions stored here with `tp_zscore`/`sl_zscore` set
- `monitor_position_triggers` — handles TP/SL close of all positions (including bot-opened ones); extended to write `last_close_reason` to `bot_configs` and respect `avg_in_progress` flag
- `order_manager` — used for both entry and averaging orders
- `PriceCache` — all z-score data comes from here, zero additional REST calls

---

## Bot State Machine

```
[disabled]
    │  user enables (via UI)
    ▼
[waiting]
    │  |z| >= entry_z, confirmation passed, balance ok
    │  (first check each cycle: adopt existing position if present)
    ▼
[in_position]
    │  averaging: if |z| >= next avg level → add to position
    │
    │  TP fires → monitor writes last_close_reason='tp' → closes position
    │      bot detects position gone → status = 'waiting'
    │
    │  SL fires → monitor writes last_close_reason='sl' → closes position
    │      bot detects position gone → status = 'paused_after_sl'
    │
    │  Liquidation/ADL → monitor writes last_close_reason='liquidation'
    │      bot detects position gone → status = 'paused_after_sl'
    ▼
[paused_after_sl]
    │  user re-enables manually
    ▼
[waiting]
```

**State transitions:**

| From | Event | To |
|---|---|---|
| `disabled` | User enables | `waiting` |
| `waiting` | Existing position found (adoption / restart recovery) | `in_position` |
| `waiting` | `\|z\| >= entry_z` held for `confirmation_minutes`, balance ok | `in_position` |
| `waiting` | Signal lost before confirmation elapsed | remain `waiting` (timer reset) |
| `waiting` | Balance insufficient | remain `waiting` (skip cycle, log warning) |
| `in_position` | `last_close_reason = 'tp'`, position gone | `waiting` |
| `in_position` | `last_close_reason = 'sl'` or `'liquidation'`, position gone | `paused_after_sl` (reset `current_avg_level = 0`) |
| `in_position` | `\|z\|` reaches next averaging level, `avg_in_progress = 0` | remain `in_position` (launch averaging) |
| `paused_after_sl` | User re-enables manually | `waiting` |
| any | User disables | `disabled` |

**How bot detects TP vs SL close:**

`monitor_position_triggers` already knows whether it is firing TP or SL (it has the `trigger` variable). Before calling the close function, it writes `last_close_reason = 'tp'` or `'sl'` (or `'liquidation'`) to the `bot_configs` row for that pair. The bot monitor reads this column when it detects the position is gone — no ambiguous post-hoc z-score comparison needed.

---

## New Background Task: `monitor_auto_trading`

Runs every 2 seconds, started in lifespan alongside existing background tasks.

**Per-cycle logic for each active bot_config:**

```
1. Load bot_config rows with status IN ('waiting', 'in_position')
2. For each bot_config:
   a. Get watchlist item → retrieve timeframe, z_window, candle_limit, entry_z, sizing, leverage
      - If watchlist item missing (deleted while bot active): log warning, set status='disabled', skip
   b. Get z-score from PriceCache (subscribe if not already subscribed)

   c. If status = 'waiting':
      - FIRST: check if open position already exists via find_open_position(sym1, sym2)
          - If yes: adopt it (set tp_zscore/sl_zscore if missing, set status='in_position')
          - This handles: server restart mid-open, manual position then bot enabled
      - If no existing position and |z| >= entry_z:
          - If confirmation_minutes > 0: check _signal_first_seen[bot_id]
            - Not set yet: record signal_first_seen = now; wait
            - Set but not elapsed: wait
            - Elapsed: proceed to open
          - Run pre-trade balance check
          - If ok:
            - call order_manager to open position (smart execution)
            - side = 'long' if z < 0, 'short' if z > 0
            - On fill: set tp_zscore, sl_zscore on the position; set status='in_position'
      - If |z| < entry_z: reset _signal_first_seen for this bot_config_id

   d. If status = 'in_position':
      - Check if position still exists via find_open_position(sym1, sym2)
        - If not: read last_close_reason from bot_configs
          - 'tp' → status = 'waiting', reset current_avg_level = 0
          - 'sl' / 'liquidation' / NULL → status = 'paused_after_sl', reset current_avg_level = 0, Telegram notify
      - If position exists and avg_in_progress = 0:
          - Check averaging levels: if current_avg_level < len(avg_levels)
              and |z| >= avg_levels[current_avg_level].z:
            - Set avg_in_progress = 1
            - Launch averaging order (order_manager, is_average=True) as asyncio.Task
            - On task completion:
                - If fill success: increment current_avg_level
                - If position no longer exists at fill time: log warning (orphan handled by exchange)
                - Always: set avg_in_progress = 0

3. Unsubscribe PriceCache keys for pairs no longer monitored
```

**In-memory state (not persisted, resets on server restart):**
- `_signal_first_seen: dict[int, float]` — bot_config_id → monotonic timestamp when signal first detected
- `_bot_keys: dict[int, tuple]` — bot_config_id → PriceCache cache_key

**Known limitation:** Confirmation timer resets on server restart. If `confirmation_minutes = 15` and server restarts after 14 minutes of a confirmed signal, the bot waits another 15 minutes from scratch. The UI shows "confirmation in progress since [timestamp]" so the user is aware and can override manually if needed.

---

## Averaging — Race Condition Handling

An averaging smart execution takes 30–70 seconds (passive + aggressive windows). During that time, `monitor_position_triggers` may fire TP/SL and close the position.

**Prevention:**
- `avg_in_progress = 1` is set in DB *before* launching the averaging task
- `monitor_position_triggers` checks `avg_in_progress` before firing TP/SL for a bot-managed position
- If `avg_in_progress = 1`, the monitor delays the close by up to one averaging window (max ~70s); after that it closes regardless

**Recovery if averaging fill completes after position close:**
- The averaging task checks `find_open_position(sym1, sym2)` after fill
- If position is gone: log warning "orphaned averaging fill — position closed mid-execution"; no DB update
- The exchange will have a slightly different size than DB; this is resolved by the existing `reconcile_positions` background task (runs every 5 min) which alerts via Telegram on mismatch

---

## Entry Direction

Determined automatically from z-score sign:

| z-score | Spread interpretation | Action |
|---|---|---|
| z > +entry_z | Spread above mean, expect reversion down | Short spread: sell sym1, buy sym2 |
| z < -entry_z | Spread below mean, expect reversion up | Long spread: buy sym1, sell sym2 |

Matches backtesting logic exactly.

---

## Pre-Trade Check Before Auto-Open

Before placing an entry order, the bot runs:
1. Balance check: `available_margin >= size_usd / leverage * 1.1`
2. Min notional check per symbol
3. If any check fails: log warning, skip this cycle, remain in `waiting`

No Telegram notification on skipped entry (to avoid spam). Logged to file only.

---

## Averaging

- Levels defined in `avg_levels_json`: `[{"z": 2.5, "size_usd": 300}, ...]`
- Applied in order: level 0 first, then level 1, etc.
- Each level fires once per open position (`current_avg_level` tracks progress)
- Uses `order_manager` with `is_average=True, average_position_id=pos_id`
- `current_avg_level` resets to 0 when position is closed and bot returns to `waiting`
- Averaging z-thresholds must be > `entry_z` (validated on save)

---

## Position Adoption (Manual → Bot)

When bot is enabled for a pair that already has an open position, OR on server restart with a position open:

1. Bot monitor sees `status = 'waiting'` and `find_open_position(sym1, sym2)` returns a position
2. Sets `tp_zscore` / `sl_zscore` on the position (if not already set)
3. Sets `bot_config.status = 'in_position'` immediately — skips entry logic
4. Existing monitor takes over TP/SL management

This handles: user opens trade manually then activates bot, and server restart while a bot-opened position is in flight.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/bot/configs` | All bot configs |
| POST | `/api/bot/configs` | Create or update bot config for a pair |
| DELETE | `/api/bot/configs/{id}` | Delete bot config (stops bot for this pair) |
| PATCH | `/api/bot/configs/{id}/enable` | Set status = `waiting` (enable) |
| PATCH | `/api/bot/configs/{id}/disable` | Set status = `disabled` (manual off) |

---

## Frontend

### Watchlist row

- Add `BOT` badge next to the 🔔 bell icon
- Color encodes status: gray=disabled, green=waiting, yellow=in_position, red=paused_after_sl
- Click → opens Bot Config Modal

### Bot Config Modal

```
┌─────────────────────────────────────────┐
│  Авто-торговля  BTC / ETH               │
│  [● Включено]           Статус: ждёт    │
│                   (⏱ сигнал с 14:32)    │  ← shown if confirmation in progress
├─────────────────────────────────────────┤
│  Take Profit (z-score)     [ 0.5 ]      │
│  Stop Loss (z-score)       [ 4.0 ]      │
│  Смарт-исполнение TP       [✓]          │
│  Смарт-исполнение SL       [✓]          │
│  Подтверждение сигнала     [ 0 ] мин    │
├─────────────────────────────────────────┤
│  Усреднение                             │
│  z-score    Сумма USD      [+ Уровень]  │
│  [ 2.5 ]    [ 300 ]        [✕]          │
│  [ 3.0 ]    [ 200 ]        [✕]          │
├─────────────────────────────────────────┤
│           [Отмена]  [Сохранить]         │
└─────────────────────────────────────────┘
```

- Fields pre-filled from watchlist on first open (`exit_z` → TP; default SL = `entry_z * 2`)
- Changes in modal do NOT affect watchlist parameters — independent config
- Validation: avg level z-thresholds must be > entry_z; SL > entry_z; TP < entry_z
- Confirmation in progress: show "⏱ сигнал с HH:MM" sub-label in modal so user can see countdown

### Z-score chart overlay

- **Bot active (`waiting` / `in_position`) for current pair:** show bot lines (TP, SL, averaging levels as dashed horizontal lines)
- **Bot disabled:** show watchlist lines (entry_z, exit_z) as currently

Line colors:
- TP: green dashed
- SL: red dashed
- Averaging levels: yellow dashed

### Positions tab

- Bot-opened positions: `AUTO` badge on the row
- `BOT` button on each position row:
  - If bot config exists for this pair → opens Bot Config Modal (status shows `in_position`)
  - If no bot config → opens modal pre-filled from position data (adoption flow)
  - If pair not in watchlist → prompt to add to watchlist first

---

## What Is NOT In This Spec

- Volatility filter (mentioned in TODO but deferred)
- Cointegration breakdown auto-exit (existing `health_check_coint` + Telegram handles awareness; auto-action deferred)
- Strategy backtesting of bot parameters before enabling (deferred)
- Multi-level partial TP (exit in parts) — decided against; one smart order for full close

---

## Implementation Notes

- `monitor_auto_trading` runs as a separate background task from `monitor_position_triggers` — clean separation of entry logic (bot) vs exit logic (existing monitor)
- Both monitors share PriceCache subscriptions — subscribe/unsubscribe independently, PriceCache ref-counts correctly
- `monitor_position_triggers` requires a small extension: before closing a position, write `last_close_reason` to the matching `bot_configs` row (if one exists for that sym1/sym2 pair)
- `monitor_position_triggers` also checks `avg_in_progress` before firing TP/SL — delays close if averaging is in flight
- Telegram notification on auto-open: reuse `notify_position_opened`
- Telegram notification when bot pauses after SL: new `notify_bot_paused(sym1, sym2, reason)` function
- Sequential thinking MCP required before implementation (changes to monitor, db.py, order_manager integration)
