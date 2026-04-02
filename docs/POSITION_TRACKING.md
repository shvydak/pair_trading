# Position Tracking — Architecture Guide

> **For Claude Code:** This document describes how position tracking works in this pair trading platform.
> Read it before modifying `order_manager.py`, `db.py`, `main.py`, or `user_data_feed.py`.

---

## Core Principle: DB is the Single Source of Truth

**The exchange sees symbols. The platform sees pairs.**

When BTC/ETH and BTC/LTC are open simultaneously, Binance shows one net BTC position.
The platform cannot use the exchange to determine qty or direction for a specific pair.

**Rule:** All close quantities and directions come from DB. The exchange is only used for:
- Mark prices (for PnL display)
- Commission amounts (from WebSocket events)
- Balance checks

---

## DB Schema

### `open_positions` — pair-level "header"
One row per open pair trade. Contains: `symbol1`, `symbol2`, `side`, `qty1`, `qty2`,
`entry_price1`, `entry_price2`, `entry_zscore`, `hedge_ratio`, `size_usd`, `leverage`,
`timeframe`, `candle_limit`, `zscore_window`, `tp_zscore`, `sl_zscore`,
`tp_smart`, `sl_smart`, `status`, `coint_pvalue`, `coint_checked_at`,
`sync_state`, `sync_note`, `synced_at`, `opened_at`.

**`status` values:**
- `open` — active, normal position
- `partial_close` — smart close failed on one leg; manual intervention required
- `liquidated` — LIQUIDATION event received from Binance ACCOUNT_UPDATE
- `adl_detected` — ADL event received

### `position_legs` — leg-level detail
One row per execution entry for a leg. Supports averaging/pyramiding.
Fields: `position_id`, `leg_number` (1 or 2), `symbol`, `side`, `qty`, `entry_price`,
`client_order_id`, `source` (`app` / `manual_sync`), `note`,
`status` (open/closed), `opened_at`, `closed_at`.

### `funding_history` — funding fee log
Fields: `position_id`, `symbol`, `amount` (negative=paid, positive=received),
`asset`, `paid_at`.
Source: Binance `ACCOUNT_UPDATE` WebSocket events with `reason=FUNDING_FEE`.

### `closed_trades` — closed position archive
Contains all fields from `open_positions` plus exit prices, exit_zscore, pnl,
`commission`, `commission_asset`.

### `bot_configs` — bot template/state, not the live position
One row per watchlist item used by auto-trading. Stores:
- Future bot settings for the pair: `tp_zscore`, `sl_zscore`, `tp_smart`, `sl_smart`
- Bot lifecycle state: `status`, `last_close_reason`
- Averaging plan/state: `avg_levels_json`, `current_avg_level`, `avg_in_progress`

Important distinction:
- `bot_configs` affects what the bot will do on the next open/adopt cycle
- `open_positions` is what `monitor_position_triggers` reads for the current live position
- Editing one does **not** automatically rewrite the other

---

## Order Execution Flow (`order_manager.py`)

### State machine
```
PLACING → PASSIVE → AGGRESSIVE → FORCING → OPEN
                                          ↓
                                     (if is_close) → close DB record → DONE
                                     (if !is_close) → save DB record → OPEN
                              ↓
                         ROLLBACK (open only) or partial_close (close only)
```

### Execution modes (`ExecContext` flags)
- `is_close=False` — opening a new position
- `is_close=True, close_db_id=N` — closing existing position (DB id=N)
- `is_average=True, average_position_id=N` — adding to existing position (averaging)

### `clientOrderId` on every order
Format: `PT_{position_id}_{leg_label}_{uuid8}` (max 36 chars, e.g. `PT_5_leg1_a3f2b1c4`).
A **fresh UUID suffix is generated per placement** — not per execution — so each limit order,
reprice, and market fallback gets a unique ID. This prevents Binance from returning stale
cached fill data when a clientOrderId is reused after cancellation (which caused incorrect
`executedQty`/`avgPrice` responses and spurious DUST flushes).
Purpose: crash recovery — on server restart, `_reconcile_on_startup` queries Binance for
open orders with `PT_` prefix to detect orphaned orders.

### `reduceOnly=True` on ALL close orders
Applied to: initial limit orders, repriced limit orders, market fallback, DUST flush.
Safety net: if DB qty > actual exchange qty (manual intervention happened),
Binance will automatically cap the order to the real position size.

### DUST flush (after smart close)
When a leg reaches `LegStatus.DUST` (remaining qty below exchange minimum notional),
the smart execution marks it done and accepts the partial fill.
After BOTH legs are done, for each leg where `leg.qty - leg.filled > 1e-9`:
1. Place `reduceOnly` market order for the exact remainder
2. Recalculate weighted average exit price: `(prev_avg * filled + flush_price * dust) / total_qty`
3. Update `leg.filled = leg.qty`
This ensures the DB gets the correct exit price for PnL calculation.

### ROLLBACK semantics — different for open vs close
- **Open failed** (one leg filled, other did not): rollback the filled leg at market
  (`reduceOnly=True`), set status `ROLLBACK`, notify Telegram.
- **Close failed** (one leg closed, other did not): do NOT re-open the closed leg.
  Set `partial_close` status in DB, alert Telegram "manual intervention required", status=DONE.

### Commission tracking
Source: Binance WebSocket `ORDER_TRADE_UPDATE`, field `"n"` (per-fill delta).
`UserDataFeed` accumulates deltas into a cumulative total per order in `_fill_data`.
`LegState.absorb_order()` uses `max(self.commission, incoming)` — works correctly
whether the source is WS (cumulative from UserDataFeed) or REST fallback (ccxt cumulative).
Final commission saved to `closed_trades` on position close.

---

## Close Operations (`main.py`)

All close paths follow the same rule:

```python
# ALWAYS use DB for direction and qty:
side = pos["side"]          # "long_spread" or "short_spread"
qty1 = pos["qty1"]          # from DB, not from exchange
qty2 = pos["qty2"]

# Direction for closing (reverse of opening):
# long_spread opened:  buy leg1, sell leg2
# long_spread closes:  sell leg1, buy leg2
leg1_close_side = "sell" if side == "long_spread" else "buy"
leg2_close_side = "buy"  if side == "long_spread" else "sell"
```

**Three close paths:**
1. `_do_market_close()` — instant market orders, direct DB close
2. `_do_smart_close_trigger()` — called by TP/SL monitor, starts smart execution
3. `POST /api/trade/smart` with `action="close"` — user-initiated smart close

All three use DB qty + `reduceOnly=True`. None query the exchange for qty or direction.

---

## PnL Calculation (`_enrich_positions`)

PnL is calculated from DB qty × mark price, **never** from exchange position size.
This prevents double-counting when the same symbol appears in multiple pairs.

```python
sign = 1 if pos["side"] == "long_spread" else -1
leg1_pnl = pos["qty1"] * (mark_price1 - pos["entry_price1"]) * sign
leg2_pnl = pos["qty2"] * (pos["entry_price2"] - mark_price2) * sign
pnl = leg1_pnl + leg2_pnl
```

Mark prices come from `client.get_positions()` responses keyed by symbol —
each position's price is fetched independently, not matched to a DB record by symbol.
`funding_total` is added from `db.get_funding_total(pos["id"])`.

---

## Real-time Events (`user_data_feed.py`)

`UserDataFeed` listens to Binance Futures User Data Stream WebSocket.

### `ORDER_TRADE_UPDATE`
- Updates `_fill_data[order_id]` with fill status, qty, avg price, cumulative commission
- Notifies registered waiters (`wait_for_order_update`) — wakes `order_manager` immediately
- `order_manager` calls `register_order(id)` before placing, `unregister_order(id)` after

### `ACCOUNT_UPDATE`
Handled by `_handle_account_update()`:
- `reason=LIQUIDATION` → fires `_liquidation_callbacks` → `_handle_liquidation` in `main.py`
  → sets position `status=liquidated` in DB, sends Telegram alert
- `reason=ADL` → fires `_adl_callbacks` → `_handle_adl` → sets `status=adl_detected`
- `reason=FUNDING_FEE` → fires `_funding_callbacks` → `_handle_funding` in `main.py`
  → distributes funding proportionally by notional, saves to `funding_history`

**Funding fee distribution:** Binance sends total balance change for an asset, not per-position.
Platform distributes proportionally: `share = amount * (notional_i / total_notional)`.

---

## Background Tasks (`main.py`)

### `monitor_position_triggers` — every 2 seconds
Reads TP/SL from `open_positions` and standalone alert/trigger rows from `triggers`.

Important current behavior:
- Uses **fresh OLS hedge ratio** (`strategy.calculate_hedge_ratio`) every cycle
  instead of the stored entry-time hedge ratio
- TP/SL is direction-agnostic: TP when `abs(z)` shrinks to/below TP, SL when
  `abs(z)` grows to/above SL
- If bot averaging is in progress for the pair, TP/SL close is delayed for up to
  70 seconds via `avg_in_progress`

This is the monitor that governs the **current open position**.

### `monitor_auto_trading` — every 2 seconds
Manages bot entry detection, position adoption, and averaging for `bot_configs`.

Important current behavior:
- Handles entry and averaging only; exit logic is **not here**
- When the bot opens a new position, it copies TP/SL from `bot_configs` into
  `open_positions`
- When the bot adopts an already-open DB position while status=`waiting`, it only
  copies TP/SL from `bot_configs` if that position currently has no TP/SL set
- `last_close_reason` is written before close and later used to decide whether the
  bot returns to `waiting` (TP) or goes to `paused_after_sl` (SL/liquidation)

### `reconcile_positions` — every 5 minutes
Compares DB open positions vs exchange positions.
For each DB position: checks if both legs exist on exchange, compares qty,
and classifies the mismatch.

Current behavior:
- If both symbols are unique across active strategy positions and both legs changed
  proportionally in the same direction, reconcile performs a guarded DB sync:
  - `manual_average` → grows `qty1/qty2`, updates weighted entry, writes `manual_sync`
    rows into `position_legs`
  - `manual_partial_close` → reduces `qty1/qty2`, keeps the remaining entry basis in sync,
    writes `manual_sync` reduction rows into `position_legs`
- If the case is ambiguous (shared symbols across pairs, only one leg changed,
  one leg missing, direction mismatch), reconcile does **not** mutate DB.
  Instead it stores `sync_state` + `sync_note` on `open_positions` and logs warnings.
- If both legs disappeared from exchange, reconcile marks the position as
  `sync_state=external_closed` for operator review instead of silently rewriting history.

### `health_check_coint` — every 4 hours
Re-runs cointegration test for each open position.
Data source: PriceCache if available (fast), REST fallback if not.
If `p-value > 0.05`: logs WARNING + Telegram alert "pair may have lost cointegration".
Updates `coint_pvalue` and `coint_checked_at` in DB.

### `_reconcile_on_startup` — once on server start
Fetches open orders from Binance with our `PT_` clientOrderId prefix.
Logs any orphaned orders (from a crashed session) for manual review.
**Does not auto-cancel** — operator decides.

---

## Averaging / Pyramiding

Adding to an existing position uses `action="average"` in `POST /api/trade/smart`:
- `ExecContext.is_average=True`, `average_position_id=<existing DB id>`
- On success: calls `db.add_position_entry()` for each leg
- `add_position_entry` computes new weighted average: `avg = (old_qty*old_price + new_qty*new_price) / (old_qty + new_qty)`
- Adds a new row to `position_legs` tracking this entry separately
- `qty1`/`qty2` in `open_positions` grow cumulatively

Important consequence:
- A later TP/SL close uses the **full accumulated** `qty1`/`qty2` from `open_positions`
- So if a position was opened and then averaged once or multiple times, TP/SL is
  intended to close the whole combined position, not just the latest add

### Manual exchange adjustments

If you manually average or manually reduce a pair on Binance, the platform still keeps
`open_positions` as the source of truth for strategy PnL and close qty. To avoid stale
Pnl/qty after those manual actions, reconcile can sync DB to exchange, but only when it
is safe:

- Safe auto-sync requires:
  - no overlapping strategy position sharing either symbol
  - both legs still present on exchange
  - both legs changed proportionally in the same direction
- When safe, the platform writes system `position_legs.source='manual_sync'` rows so the
  audit trail still shows what was changed outside the app.
- When not safe, the platform does **not** guess. It leaves the DB untouched and marks
  the row with a sync warning instead.

This preserves the strategy journal while keeping `Strategy Positions` honest after
manual exchange intervention.

---

## `find_open_position` Filter

`db.find_open_position(sym1, sym2)` excludes positions with `status IN ('liquidated', 'adl_detected')`.
These positions are "dead" on the exchange — attempting to close them would be rejected.
`partial_close` positions ARE returned — the user may want to manually inspect or partially re-close.

---

## UI Indicators (frontend)

Position row in the Positions tab shows:
- **Status badge** (pair cell): red `ЛИКВИДАЦИЯ`, orange `ADL`, yellow `ЧАСТИЧНОЕ ЗАКРЫТИЕ`
- **Sync badge** (pair cell): blue `Синхр.` after a guarded manual sync, amber
  `Рассинхрон` when DB/exchange mismatch is ambiguous, orange `Закрыта на бирже`
  when exchange no longer has either leg
- **Cointegration dot** (next to symbol): green (p<0.01), yellow (p<0.05), red (p≥0.05)
- **Funding sub-line** (PnL cell): `Funding: ±$X.XX` when `funding_total ≠ 0`
- **Desync hint** (PnL cell): shown when a row is in ambiguous sync state, to make it clear
  that the displayed strategy PnL may be stale until the mismatch is resolved

All three update in-place every 5s via `GET /api/dashboard` (no full row rebuild).

---

## BOT Popup vs TP/SL in Position Row

These are related, but they are **not the same control**.

### BOT popup (`BOT` button)
Writes to `bot_configs`.

Use it to change:
- Bot template TP/SL for future entries
- Smart-vs-market close preference for bot-managed TP/SL
- Confirmation timer
- Averaging levels
- Enable/disable bot state

Important current behavior:
- `tp_zscore` and `sl_zscore` in `bot_configs` are nullable
- Leaving both empty means **entry-only bot**: the bot may open positions, but it
  will not arm TP/SL for future positions unless values are set later
- Saving BOT popup settings does **not** rewrite TP/SL of an already-open position

### TP/SL inputs in the Positions row (`TP`, `SL`, `Set`, cancel badges)
Writes directly to `open_positions` via `POST /api/db/positions/{id}/triggers`.

Use it to change:
- TP/SL of the **current live position**
- Remove only TP, only SL, or both
- Toggle smart close preference for the current position

Important current behavior:
- `monitor_position_triggers` reads these fields from `open_positions` every 2 seconds
- So changing TP/SL here takes effect for the current position without waiting for
  the next bot trade
- These manual changes do **not** update the BOT template in `bot_configs`

### Practical rule of thumb
- Want to change the **current trade**? Edit TP/SL in the Positions row.
- Want to change **future bot trades**? Edit the BOT popup.
- After the current position fully closes, the next bot-opened trade will again get
  TP/SL from `bot_configs`, not from whatever manual values were used on the old position.

---

## What NOT to Change Without Reading This First

1. **`find_open_position`** — the status filter is intentional; removing it breaks liquidated position safety
2. **`reduceOnly=True`** — must be on ALL close/rollback orders; removing it risks position explosion on overlapping symbols
3. **`_enrich_positions`** — must never use `exchange_pos["size"]` for PnL; always `db["qty"]`
4. **`_handle_funding`** proportional distribution — must divide by total notional; assigning full amount to each position is a bug
5. **`LegState.absorb_order` commission** — uses `max()` because both WS and REST now provide cumulative totals
6. **ROLLBACK on close** — must NOT place a market order to re-open a closed leg; only `partial_close` + alert
