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
`status`, `coint_pvalue`, `coint_checked_at`, `opened_at`.

**`status` values:**
- `open` — active, normal position
- `partial_close` — smart close failed on one leg; manual intervention required
- `liquidated` — LIQUIDATION event received from Binance ACCOUNT_UPDATE
- `adl_detected` — ADL event received

### `position_legs` — leg-level detail
One row per execution entry for a leg. Supports averaging/pyramiding.
Fields: `position_id`, `leg_number` (1 or 2), `symbol`, `side`, `qty`, `entry_price`,
`client_order_id`, `status` (open/closed), `opened_at`, `closed_at`.

### `funding_history` — funding fee log
Fields: `position_id`, `symbol`, `amount` (negative=paid, positive=received),
`asset`, `paid_at`.
Source: Binance `ACCOUNT_UPDATE` WebSocket events with `reason=FUNDING_FEE`.

### `closed_trades` — closed position archive
Contains all fields from `open_positions` plus exit prices, exit_zscore, pnl,
`commission`, `commission_asset`.

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
Format: `PT_{position_id}_{leg_label}_{exec_id}` (max 36 chars, e.g. `PT_5_leg1_a3f2b1c4`).
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

### `reconcile_positions` — every 5 minutes
Compares DB open positions vs exchange positions.
For each DB position: checks if both legs exist on exchange, compares qty.
**Detection only** — never auto-corrects. Sends Telegram warnings on mismatch.

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

---

## `find_open_position` Filter

`db.find_open_position(sym1, sym2)` excludes positions with `status IN ('liquidated', 'adl_detected')`.
These positions are "dead" on the exchange — attempting to close them would be rejected.
`partial_close` positions ARE returned — the user may want to manually inspect or partially re-close.

---

## UI Indicators (frontend)

Position row in the Positions tab shows:
- **Status badge** (pair cell): red `ЛИКВИДАЦИЯ`, orange `ADL`, yellow `ЧАСТИЧНОЕ ЗАКРЫТИЕ`
- **Cointegration dot** (next to symbol): green (p<0.01), yellow (p<0.05), red (p≥0.05)
- **Funding sub-line** (PnL cell): `Funding: ±$X.XX` when `funding_total ≠ 0`

All three update in-place every 5s via `GET /api/dashboard` (no full row rebuild).

---

## What NOT to Change Without Reading This First

1. **`find_open_position`** — the status filter is intentional; removing it breaks liquidated position safety
2. **`reduceOnly=True`** — must be on ALL close/rollback orders; removing it risks position explosion on overlapping symbols
3. **`_enrich_positions`** — must never use `exchange_pos["size"]` for PnL; always `db["qty"]`
4. **`_handle_funding`** proportional distribution — must divide by total notional; assigning full amount to each position is a bug
5. **`LegState.absorb_order` commission** — uses `max()` because both WS and REST now provide cumulative totals
6. **ROLLBACK on close** — must NOT place a market order to re-open a closed leg; only `partial_close` + alert
