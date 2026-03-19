# Fix clientOrderId Reuse — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix a production bug where reusing the same `clientOrderId` for all orders in one execution causes Binance to return stale cached fill data, resulting in incorrect `leg.filled`, spurious DUST flush, and a residual open position on the exchange.

**Architecture:** Replace `_make_client_order_id(ctx, leg_label)` (returns a per-execution constant) with `_make_placement_id(ctx, leg_label)` (generates a fresh UUID suffix each call). Additionally, register the market order with UserDataFeed in `_force_market` to ensure WS fill events are tracked.

**Tech Stack:** Python, asyncio, ccxt/Binance Futures API, pytest

---

## Root Cause Summary

`_make_client_order_id` returns `PT_{pos_id}_{leg}_{exec_id}` — the same string for all limit orders, all reprice orders, and the forcing market order for a given leg in one execution.

When a limit order with `clientOrderId=X` is cancelled and a market order is placed reusing `clientOrderId=X`, Binance may return cached data from the cancelled order (stale `avgPrice`/`executedQty`). This corrupts `leg.filled`, triggering the DUST flush for a non-existent remainder.

---

## Files

- **Modify:** `backend/order_manager.py` — rename function, update 4 call sites, fix `_force_market`
- **Modify:** `tests/test_order_manager.py` — update 3 existing tests, add 2 new tests

---

## Task 1: Update existing clientOrderId tests

**Files:**
- Modify: `tests/test_order_manager.py:355-375`

- [ ] **Step 1: Update the three existing tests to use `_make_placement_id`**

Replace the three `test_make_client_order_id_*` tests:

```python
# clientOrderId — unique per placement
# ---------------------------------------------------------------------------

def test_make_placement_id_format():
    ctx = _ctx()
    ctx.db_id = 7
    pid = om._make_placement_id(ctx, "leg1")
    assert pid.startswith("PT_7_leg1_")
    assert len(pid) <= 36


def test_make_placement_id_uses_close_db_id_when_no_db_id():
    ctx = _ctx()
    ctx.db_id = None
    ctx.close_db_id = 99
    pid = om._make_placement_id(ctx, "leg2")
    assert pid.startswith("PT_99_leg2_")


def test_make_placement_id_max_36_chars():
    ctx = _ctx()
    ctx.db_id = 123456
    pid = om._make_placement_id(ctx, "leg1")
    assert len(pid) <= 36


def test_make_placement_id_unique_per_call():
    """Each call must return a different ID — no reuse across placements."""
    ctx = _ctx()
    ctx.db_id = 5
    ids = {om._make_placement_id(ctx, "leg1") for _ in range(10)}
    assert len(ids) == 10
```

- [ ] **Step 2: Run updated tests to confirm they FAIL (function not yet renamed)**

```bash
cd /Users/y.shvydak/Projects/pair_trading && .venv/bin/pytest tests/test_order_manager.py -k "placement_id" -v
```
Expected: `AttributeError: module 'order_manager' has no attribute '_make_placement_id'`

- [ ] **Step 3: Commit test update**

```bash
git add tests/test_order_manager.py
git commit -m "test(order_manager): update clientOrderId tests for _make_placement_id"
```

---

## Task 2: Implement `_make_placement_id` in order_manager.py

**Files:**
- Modify: `backend/order_manager.py:191-194` (rename function + logic)
- Modify: `backend/order_manager.py:235-236` (initial placement call sites)
- Modify: `backend/order_manager.py:782` (`_place_remaining_limit`)
- Modify: `backend/order_manager.py:627` (`_force_market`)

- [ ] **Step 1: Rename function and change suffix to UUID**

Replace:
```python
def _make_client_order_id(ctx: "ExecContext", leg_label: str) -> str:
    """Build a traceable clientOrderId: PT_{pos_id}_{leg}_{exec_id} (max 36 chars)."""
    pos_id = ctx.db_id or ctx.close_db_id or 0
    return f"PT_{pos_id}_{leg_label}_{ctx.exec_id}"[:36]
```

With:
```python
def _make_placement_id(ctx: "ExecContext", leg_label: str) -> str:
    """Build a unique clientOrderId per order placement: PT_{pos_id}_{leg}_{uuid8}.
    Using a fresh UUID suffix prevents Binance from returning stale cached fill
    data when a clientOrderId is reused after cancellation.
    """
    pos_id = ctx.db_id or ctx.close_db_id or 0
    suffix = uuid.uuid4().hex[:8]
    return f"PT_{pos_id}_{leg_label}_{suffix}"[:36]
```

Make sure `import uuid` is present at the top of the file.

- [ ] **Step 2: Update call sites — initial placement (lines 235-236)**

Replace:
```python
params1 = {"clientOrderId": _make_client_order_id(ctx, "leg1")}
params2 = {"clientOrderId": _make_client_order_id(ctx, "leg2")}
```
With:
```python
params1 = {"clientOrderId": _make_placement_id(ctx, "leg1")}
params2 = {"clientOrderId": _make_placement_id(ctx, "leg2")}
```

- [ ] **Step 3: Update call site in `_place_remaining_limit` (line 782)**

Replace:
```python
params = {"clientOrderId": _make_client_order_id(ctx, leg_label)}
```
With:
```python
params = {"clientOrderId": _make_placement_id(ctx, leg_label)}
```

- [ ] **Step 4: Update call site in `_force_market` (line 627)**

Replace:
```python
market_params = {"clientOrderId": _make_client_order_id(ctx, leg_label)}
```
With:
```python
market_params = {"clientOrderId": _make_placement_id(ctx, leg_label)}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/y.shvydak/Projects/pair_trading && .venv/bin/pytest tests/test_order_manager.py -k "placement_id" -v
```
Expected: 4 tests PASS

- [ ] **Step 6: Run full test suite to check no regressions**

```bash
cd /Users/y.shvydak/Projects/pair_trading && .venv/bin/pytest tests/ -v
```
Expected: all tests PASS

- [ ] **Step 7: Commit implementation**

```bash
git add backend/order_manager.py
git commit -m "fix(order_manager): unique clientOrderId per placement to prevent Binance cache hit"
```

---

## Task 3: Add test for market order ID tracking in `_force_market`

**Files:**
- Modify: `tests/test_order_manager.py` — add new test after DUST flush section

- [ ] **Step 1: Write the failing test**

Add this test in the `_force_market` section (after the DUST flush tests):

```python
# ---------------------------------------------------------------------------
# _force_market — market order registered with UserDataFeed
# ---------------------------------------------------------------------------

def test_force_market_updates_leg_order_id(monkeypatch):
    """After _force_market places a market order, leg.order_id must be updated
    to the new market order's ID so UserDataFeed tracks fill events correctly."""
    _patch_runtime(monkeypatch)

    registered = []

    class FakeUDF:
        def register_order(self, order_id):
            registered.append(order_id)
        def get_generation(self):
            return 0
        async def wait_for_order_update(self, gen, timeout):
            return gen + 1

    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [{"bid": 100.0, "ask": 101.0, "spread_pct": 1.0}],
            "BBB/USDC:USDC": [{"bid": 200.0, "ask": 201.0, "spread_pct": 0.5}],
        },
        limit_status_scripts=[
            # leg1: fill immediately
            [{"id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 100.0}],
            # leg2: never fills → goes to _force_market
            [{"id": "L2", "status": "open", "filled": 0.0, "remaining": 1.0, "average": None}],
        ],
        market_prices={"BBB/USDC:USDC": 201.0},
    )

    ctx = _ctx(passive_s=0.0, aggressive_s=0.0, poll_s=2.0, reprice_s=4.0)
    ctx.is_close = True
    ctx.close_db_id = 10
    ctx.user_data_feed = FakeUDF()
    ctx.leg1 = om.LegState(symbol="AAA/USDC:USDC", side="sell", qty=1.0)
    ctx.leg2 = om.LegState(symbol="BBB/USDC:USDC", side="buy", qty=1.0)
    ctx.leg1.last_reprice_at = 0.0
    ctx.leg2.last_reprice_at = 0.0
    ctx.entry_price1 = 95.0
    ctx.entry_price2 = 195.0

    db = FakeDb()
    asyncio.run(om.run_execution(ctx, client, db))

    # The market order for leg2 must have been placed
    market_order_ids = [o["id"] for o in client.market_orders if o.get("symbol") == "BBB/USDC:USDC"]
    assert len(market_order_ids) >= 1
    market_id = market_order_ids[-1]

    # leg.order_id must be updated to the market order's ID
    assert ctx.leg2.order_id == market_id

    # UDF must have registered the market order
    assert market_id in registered
```

- [ ] **Step 2: Run to confirm it FAILS**

```bash
cd /Users/y.shvydak/Projects/pair_trading && .venv/bin/pytest tests/test_order_manager.py -k "test_force_market_updates_leg_order_id" -v
```
Expected: FAIL — `AssertionError: assert ctx.leg2.order_id == market_id`

- [ ] **Step 3: Commit failing test**

```bash
git add tests/test_order_manager.py
git commit -m "test(order_manager): add failing test for market order ID tracking in _force_market"
```

---

## Task 4: Implement market order tracking in `_force_market`

**Files:**
- Modify: `backend/order_manager.py:603-646` (`_force_market`)
- Modify: `backend/order_manager.py` — update `_force_market` signature + call site

- [ ] **Step 1: Update `_force_market` signature and body**

Change signature from:
```python
async def _force_market(ctx: ExecContext, client) -> None:
```
To:
```python
async def _force_market(ctx: ExecContext, client, udf=None, registered_orders: set | None = None) -> None:
```

After the successful market order placement (after `leg.avg_price` is set), add:
```python
# Update leg.order_id to market order ID and register with UserDataFeed
leg.order_id = str(order["id"])
if udf:
    udf.register_order(leg.order_id)
    if registered_orders is not None:
        registered_orders.add(leg.order_id)
```

The full updated success block in `_force_market`:
```python
try:
    prev_filled = leg.filled
    prev_avg = leg.avg_price
    leg_label = "leg1" if leg is ctx.leg1 else "leg2"
    market_params = {"clientOrderId": _make_placement_id(ctx, leg_label)}
    if ctx.is_close:
        market_params["reduceOnly"] = True
    order = await client.place_order(leg.symbol, leg.side, leg.remaining, order_type="market", params=market_params)
    market_filled = float(order.get("filled") or leg.remaining)
    market_avg = order.get("average")
    leg.filled = min(leg.qty, prev_filled + market_filled)
    leg.remaining = 0.0
    leg.status    = LegStatus.FILLED
    # Track market order in UserDataFeed
    leg.order_id = str(order["id"])
    if udf:
        udf.register_order(leg.order_id)
        if registered_orders is not None:
            registered_orders.add(leg.order_id)
    if market_avg:
        market_avg = float(market_avg)
        if prev_avg is not None and prev_filled > 0:
            total_cost = prev_avg * prev_filled + market_avg * market_filled
            leg.avg_price = total_cost / max(leg.filled, 1e-9)
        else:
            leg.avg_price = market_avg
except Exception as e:
    ctx.evt(f"  Market order FAILED {leg.symbol}: {e}")
    leg.status = LegStatus.FAILED
```

- [ ] **Step 2: Update the call site in `run_execution`**

Find where `_force_market` is called (around line 300) and update to pass `udf` and `_registered_orders`:

Replace:
```python
await _force_market(ctx, client)
```
With:
```python
await _force_market(ctx, client, udf=udf, registered_orders=_registered_orders)
```

- [ ] **Step 3: Run the new test**

```bash
cd /Users/y.shvydak/Projects/pair_trading && .venv/bin/pytest tests/test_order_manager.py -k "test_force_market_updates_leg_order_id" -v
```
Expected: PASS

- [ ] **Step 4: Run full test suite**

```bash
cd /Users/y.shvydak/Projects/pair_trading && .venv/bin/pytest tests/ -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/order_manager.py tests/test_order_manager.py
git commit -m "fix(order_manager): track market order ID in _force_market and register with UserDataFeed"
```

---

## Task 5: Update docs

- [ ] **Step 1: Update `docs/POSITION_TRACKING.md` — clientOrderId section**

Find the `clientOrderId` section and update the description:

Old text:
> Format: `PT_{position_id}_{leg_label}_{exec_id}` (max 36 chars, e.g. `PT_5_leg1_a3f2b1c4`).

New text:
> Format: `PT_{position_id}_{leg_label}_{uuid8}` (max 36 chars, e.g. `PT_5_leg1_a3f2b1c4`).
> A fresh UUID suffix is generated per placement — not per execution — to prevent Binance from
> returning stale cached fill data when the same clientOrderId is reused after cancellation.

- [ ] **Step 2: Commit docs update**

```bash
git add docs/POSITION_TRACKING.md
git commit -m "docs: update clientOrderId format description — UUID per placement"
```

---

## Verification

After all tasks:
```bash
cd /Users/y.shvydak/Projects/pair_trading && .venv/bin/pytest tests/ -v --tb=short
```
All tests must pass.
