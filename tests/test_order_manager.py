"""
Unit tests for order_manager.py — Smart v2 execution engine.

These tests use lightweight async fakes for Binance/DB so we can verify:
- dynamic passive repricing
- semi-aggressive stage pricing
- "hold until stage end" behavior for non-placeable residuals
- dust residuals being accepted and saved as the actual filled qty
"""
import asyncio

import pytest

import order_manager as om


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class FakeClient:
    def __init__(self, *, orderbooks, limit_status_scripts, min_notional=None, market_prices=None):
        self.orderbooks = {
            symbol: list(values)
            for symbol, values in orderbooks.items()
        }
        self.limit_status_scripts = list(limit_status_scripts)
        self.order_statuses = {}
        self.next_order_id = 1
        self.limit_orders = []
        self.cancelled = []
        self.market_orders = []
        self.min_notional = min_notional or {}
        self.market_prices = market_prices or {}

    async def fetch_order_book(self, symbol, limit=5):
        values = self.orderbooks[symbol]
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    async def place_limit_order(self, symbol, side, amount, price, params=None):
        order_id = f"L{self.next_order_id}"
        self.next_order_id += 1
        self.limit_orders.append({
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
            "params": params or {},
        })
        script = self.limit_status_scripts.pop(0) if self.limit_status_scripts else []
        self.order_statuses[order_id] = list(script)
        return {"id": order_id, "amount": amount, "price": price}

    async def fetch_order(self, symbol, order_id):
        script = self.order_statuses.get(order_id, [])
        if not script:
            return {"id": order_id, "status": "open", "filled": 0.0, "remaining": 0.0}
        if len(script) > 1:
            return script.pop(0)
        return script[0]

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return {"id": order_id, "status": "canceled"}

    async def round_amount(self, symbol, amount):
        return float(amount)

    async def check_min_notional(self, symbol, amount, price):
        minimum = float(self.min_notional.get(symbol, 0.0))
        actual = float(amount) * float(price)
        return actual >= minimum, actual, minimum

    async def place_order(self, symbol, side, amount, order_type="market", params=None):
        self.market_orders.append({
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "order_type": order_type,
        })
        return {
            "id": f"M{len(self.market_orders)}",
            "filled": amount,
            "average": self.market_prices.get(symbol, 0.0),
            "amount": amount,
        }


class FakeDb:
    def __init__(self):
        self.saved = None
        self.closed = None
        self.added_entries = []

    def save_open_position(self, **kwargs):
        self.saved = kwargs
        return 123

    def close_position(self, *args, **kwargs):
        self.closed = args
        return True

    def save_position_leg(self, *args, **kwargs):
        pass

    def close_position_legs(self, *args, **kwargs):
        pass

    def add_position_entry(self, *args, **kwargs):
        self.added_entries.append((args, kwargs))
        return True

    def set_position_status(self, *args, **kwargs):
        pass


async def _noop(*args, **kwargs):
    return None


def _patch_runtime(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(om.time, "time", clock.time)
    monkeypatch.setattr(om.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(om.tg_bot, "notify_position_opened", _noop)
    monkeypatch.setattr(om.tg_bot, "notify_position_closed", _noop)
    monkeypatch.setattr(om.tg_bot, "notify_rollback", _noop)
    monkeypatch.setattr(om.tg_bot, "notify_execution_failed", _noop)
    return clock


def _ctx(**cfg_overrides):
    cfg = om.ExecConfig(**cfg_overrides)
    ctx = om.ExecContext(
        exec_id="exec1234",
        leg1=om.LegState(symbol="AAA/USDC:USDC", side="buy", qty=1.0),
        leg2=om.LegState(symbol="BBB/USDC:USDC", side="sell", qty=2.0),
        config=cfg,
        spread_side="long_spread",
        hedge_ratio=1.2,
        entry_zscore=-2.0,
        size_usd=1000.0,
        sizing_method="ols",
        leverage=1,
    )
    ctx.started_at = 0.0
    ctx.leg1.last_reprice_at = 0.0
    ctx.leg2.last_reprice_at = 0.0
    return ctx


def test_run_execution_reprices_with_dynamic_passive(monkeypatch):
    _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [
                {"bid": 100.0, "ask": 101.0, "spread_pct": 1.0},
                {"bid": 103.0, "ask": 104.0, "spread_pct": 0.9},
            ],
            "BBB/USDC:USDC": [
                {"bid": 200.0, "ask": 201.0, "spread_pct": 0.5},
                {"bid": 198.0, "ask": 199.0, "spread_pct": 0.5},
            ],
        },
        limit_status_scripts=[
            [
                {"id": "L1", "status": "open", "filled": 0.0, "remaining": 1.0},
                {"id": "L1", "status": "open", "filled": 0.0, "remaining": 1.0},
                {"id": "L1", "status": "open", "filled": 0.0, "remaining": 1.0},
            ],
            [
                {"id": "L2", "status": "open", "filled": 0.0, "remaining": 2.0},
                {"id": "L2", "status": "open", "filled": 0.0, "remaining": 2.0},
                {"id": "L2", "status": "open", "filled": 0.0, "remaining": 2.0},
            ],
            [
                {"id": "L3", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 103.0},
            ],
            [
                {"id": "L4", "status": "closed", "filled": 2.0, "remaining": 0.0, "average": 199.0},
            ],
        ],
    )
    db = FakeDb()
    ctx = _ctx(passive_s=10.0, aggressive_s=10.0, poll_s=2.0, reprice_s=4.0)

    asyncio.run(om.run_execution(ctx, client, db))

    assert ctx.status == om.ExecStatus.OPEN
    assert len(client.limit_orders) == 4
    assert client.limit_orders[0]["price"] == 100.0
    assert client.limit_orders[1]["price"] == 201.0
    assert client.limit_orders[2]["price"] == 103.0
    assert client.limit_orders[3]["price"] == 199.0
    assert client.limit_orders[2]["amount"] == 1.0
    assert client.limit_orders[3]["amount"] == 2.0
    assert db.saved["qty1"] == 1.0
    assert db.saved["qty2"] == 2.0


def test_run_execution_switches_to_semi_aggressive_prices(monkeypatch):
    _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [
                {"bid": 100.0, "ask": 101.0, "spread_pct": 1.0},
                {"bid": 100.0, "ask": 104.0, "spread_pct": 4.0},
            ],
            "BBB/USDC:USDC": [
                {"bid": 200.0, "ask": 201.0, "spread_pct": 0.5},
                {"bid": 200.0, "ask": 204.0, "spread_pct": 2.0},
            ],
        },
        limit_status_scripts=[
            [
                {"id": "L1", "status": "open", "filled": 0.0, "remaining": 1.0},
                {"id": "L1", "status": "open", "filled": 0.0, "remaining": 1.0},
            ],
            [
                {"id": "L2", "status": "open", "filled": 0.0, "remaining": 2.0},
                {"id": "L2", "status": "open", "filled": 0.0, "remaining": 2.0},
            ],
            [
                {"id": "L3", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 101.0},
            ],
            [
                {"id": "L4", "status": "closed", "filled": 2.0, "remaining": 0.0, "average": 203.0},
            ],
        ],
    )
    db = FakeDb()
    ctx = _ctx(passive_s=2.0, aggressive_s=10.0, poll_s=2.0, reprice_s=4.0)

    asyncio.run(om.run_execution(ctx, client, db))

    assert ctx.status == om.ExecStatus.OPEN
    assert len(client.limit_orders) == 4
    assert client.limit_orders[2]["price"] == 101.0
    assert client.limit_orders[3]["price"] == 203.0


def test_reprice_live_orders_holds_non_placeable_residual_until_stage_end(monkeypatch):
    clock = _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [{"bid": 100.0, "ask": 101.0, "spread_pct": 1.0}],
        },
        limit_status_scripts=[],
        min_notional={"AAA/USDC:USDC": 50.0},
    )
    ctx = _ctx()
    leg = ctx.leg1
    leg.order_id = "LIVE1"
    leg.remaining = 0.1
    leg.filled = 0.9
    leg.working_price = 99.0
    leg.last_reprice_at = 0.0
    clock.now = 4.0

    asyncio.run(om._reprice_live_orders(ctx, client, mode="passive"))

    assert leg.hold_until_stage_end is True
    assert client.cancelled == []
    assert leg.order_id == "LIVE1"


def test_run_execution_accepts_dust_and_saves_actual_filled_qty(monkeypatch):
    _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [
                {"bid": 100.0, "ask": 101.0, "spread_pct": 1.0},
            ],
            "BBB/USDC:USDC": [
                {"bid": 200.0, "ask": 201.0, "spread_pct": 0.5},
                {"bid": 200.0, "ask": 201.0, "spread_pct": 0.5},
            ],
        },
        limit_status_scripts=[
            [
                {"id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 100.0},
            ],
            [
                {"id": "L2", "status": "open", "filled": 1.8, "remaining": 0.2, "average": 200.0},
                {"id": "L2", "status": "open", "filled": 1.8, "remaining": 0.2, "average": 200.0},
            ],
        ],
        min_notional={"BBB/USDC:USDC": 1000.0},
    )
    db = FakeDb()
    ctx = _ctx(passive_s=2.0, aggressive_s=10.0, poll_s=2.0, reprice_s=4.0)

    asyncio.run(om.run_execution(ctx, client, db))

    assert ctx.status == om.ExecStatus.OPEN
    assert ctx.leg2.status == om.LegStatus.DUST
    assert db.saved["qty1"] == 1.0
    assert db.saved["qty2"] == 1.8
    assert len(client.limit_orders) == 2


# ---------------------------------------------------------------------------
# reduceOnly on close orders
# ---------------------------------------------------------------------------

def test_close_orders_have_reduce_only(monkeypatch):
    """All limit orders placed in is_close mode must carry reduceOnly=True."""
    _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [{"bid": 100.0, "ask": 101.0, "spread_pct": 1.0}],
            "BBB/USDC:USDC": [{"bid": 200.0, "ask": 201.0, "spread_pct": 0.5}],
        },
        limit_status_scripts=[
            [{"id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 100.0}],
            [{"id": "L2", "status": "closed", "filled": 2.0, "remaining": 0.0, "average": 200.0}],
        ],
    )
    db = FakeDb()
    db.closed_with_kwargs = {}

    original_close = db.close_position
    def _tracking_close(*a, **kw):
        db.closed_with_kwargs = kw
        return original_close(*a, **kw)
    db.close_position = _tracking_close

    ctx = _ctx(passive_s=10.0, aggressive_s=10.0, poll_s=2.0, reprice_s=4.0)
    ctx.is_close = True
    ctx.close_db_id = 42
    ctx.entry_price1 = 95.0
    ctx.entry_price2 = 195.0
    # Flip sides for closing: leg1 sell, leg2 buy
    ctx.leg1 = om.LegState(symbol="AAA/USDC:USDC", side="sell", qty=1.0)
    ctx.leg2 = om.LegState(symbol="BBB/USDC:USDC", side="buy", qty=2.0)
    ctx.leg1.last_reprice_at = 0.0
    ctx.leg2.last_reprice_at = 0.0

    asyncio.run(om.run_execution(ctx, client, db))

    for order in client.limit_orders:
        assert order.get("params", {}).get("reduceOnly") is True, \
            f"Order {order['id']} is missing reduceOnly=True"


# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Commission tracking — max() prevents double-count on REST re-polls
# ---------------------------------------------------------------------------

def test_commission_max_prevents_double_count_on_repeated_absorb():
    """REST fetch_order returns cumulative commission — absorb_order must not add it twice."""
    leg = om.LegState(symbol="AAA/USDC:USDC", side="buy", qty=1.0)
    # First REST poll: partial fill, cumulative commission = 0.01
    leg.absorb_order({
        "id": "L1", "status": "open", "filled": 0.5, "remaining": 0.5,
        "average": 100.0, "commission": 0.01, "commission_asset": "USDC",
    })
    assert leg.commission == pytest.approx(0.01)
    # Second REST poll: full fill, cumulative commission = 0.03
    leg.absorb_order({
        "id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0,
        "average": 100.0, "commission": 0.03, "commission_asset": "USDC",
    })
    # Must be 0.03 (final cumulative), not 0.04 (0.01 + 0.03)
    assert leg.commission == pytest.approx(0.03)


def test_commission_accumulates_ws_per_fill_events():
    """UserDataFeed accumulates per-fill deltas into cumulative commission.
    absorb_order receives cumulative values from UserDataFeed, so max() is correct."""
    leg = om.LegState(symbol="AAA/USDC:USDC", side="buy", qty=1.0)
    # WS event 1: UserDataFeed stores cumulative=0.01 (0 + 0.01)
    leg.absorb_order({
        "id": "L1", "status": "open", "filled": 0.3, "remaining": 0.7,
        "average": 100.0, "commission": 0.01, "commission_asset": "USDC",
    })
    # WS event 2: UserDataFeed stores cumulative=0.03 (0.01 + 0.02)
    leg.absorb_order({
        "id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0,
        "average": 100.0, "commission": 0.03, "commission_asset": "USDC",
    })
    # max(0.01, 0.03) = 0.03
    assert leg.commission == pytest.approx(0.03)


def test_absorb_order_preserves_partial_fill_across_repriced_orders():
    """A new residual order must not erase fills from the previous placement."""
    leg = om.LegState(symbol="AAA/USDC:USDC", side="buy", qty=0.421)

    # First placement partially fills 0.007, leaving 0.414 for the next order.
    leg.absorb_order({
        "id": "L1",
        "status": "canceled",
        "filled": 0.007,
        "remaining": 0.414,
        "average": 53.57,
    })
    assert leg.status == om.LegStatus.PARTIAL
    assert leg.filled == pytest.approx(0.007)
    assert leg.remaining == pytest.approx(0.414)

    # The repriced order is a fresh order for only the residual 0.414.
    # Its per-order filled resets to 0, but total leg progress must stay 0.007.
    leg.absorb_order({
        "id": "L2",
        "status": "open",
        "filled": 0.0,
        "remaining": 0.414,
        "average": 53.58,
    })
    assert leg.status == om.LegStatus.PARTIAL
    assert leg.filled == pytest.approx(0.007)
    assert leg.remaining == pytest.approx(0.414)

    # Once the residual order fully fills, the total must become 0.421.
    leg.absorb_order({
        "id": "L2",
        "status": "closed",
        "filled": 0.414,
        "remaining": 0.0,
        "average": 53.59,
    })
    assert leg.status == om.LegStatus.FILLED
    assert leg.filled == pytest.approx(0.421)
    assert leg.remaining == pytest.approx(0.0)


def test_average_execution_preserves_partial_fill_and_adds_full_qty(monkeypatch):
    """Averaging must add the total filled qty, not just the repriced residual."""
    _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [{"bid": 100.0, "ask": 101.0, "spread_pct": 1.0}],
            "BBB/USDC:USDC": [{"bid": 200.0, "ask": 201.0, "spread_pct": 0.5}],
        },
        limit_status_scripts=[
            [
                {"id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 100.0},
            ],
            [
                {"id": "L2", "status": "open", "filled": 0.2, "remaining": 0.8, "average": 200.0},
                {"id": "L2", "status": "canceled", "filled": 0.2, "remaining": 0.8, "average": 200.0},
            ],
            [
                {"id": "L3", "status": "closed", "filled": 0.8, "remaining": 0.0, "average": 201.0},
            ],
        ],
    )
    db = FakeDb()
    ctx = _ctx(passive_s=2.0, aggressive_s=10.0, poll_s=2.0, reprice_s=4.0)
    ctx.is_average = True
    ctx.average_position_id = 77
    ctx.leg2 = om.LegState(symbol="BBB/USDC:USDC", side="sell", qty=1.0)
    ctx.leg2.last_reprice_at = 0.0

    asyncio.run(om.run_execution(ctx, client, db))

    assert ctx.status == om.ExecStatus.OPEN
    assert ctx.db_id == 77
    assert len(db.added_entries) == 2
    leg1_args, _ = db.added_entries[0]
    leg2_args, _ = db.added_entries[1]
    assert leg1_args[0] == 77
    assert leg1_args[1] == 1
    assert leg1_args[2] == pytest.approx(1.0)
    assert leg2_args[0] == 77
    assert leg2_args[1] == 2
    assert leg2_args[2] == pytest.approx(1.0)
    assert ctx.leg2.filled == pytest.approx(1.0)
    assert ctx.leg2.remaining == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ROLLBACK for close — sets partial_close status, does not re-open
# ---------------------------------------------------------------------------

def test_close_partial_fill_sets_partial_close_status(monkeypatch):
    """When close fails on one leg, status becomes partial_close in DB — no rollback market order."""
    _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [{"bid": 100.0, "ask": 101.0, "spread_pct": 1.0}],
            "BBB/USDC:USDC": [{"bid": 200.0, "ask": 201.0, "spread_pct": 0.5}],
        },
        limit_status_scripts=[
            # leg1 closes OK
            [{"id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 100.0}],
            # leg2 initial order (L2) — stays open through passive stage
            [{"id": "L2", "status": "open", "filled": 0.0, "remaining": 2.0}],
            # leg2 repriced order (L3) in aggressive stage — also stays open
            [{"id": "L3", "status": "open", "filled": 0.0, "remaining": 2.0}],
        ],
    )
    db = FakeDb()
    db.statuses_set = []
    original_set = db.set_position_status
    def _track_status(*a, **kw):
        db.statuses_set.append(a)
        return original_set(*a, **kw)
    db.set_position_status = _track_status

    # allow_market=False so leg2 cannot be force-filled — triggers partial_close path
    ctx = _ctx(passive_s=2.0, aggressive_s=2.0, poll_s=2.0, reprice_s=10.0, allow_market=False)
    ctx.is_close = True
    ctx.close_db_id = 42
    ctx.entry_price1 = 95.0
    ctx.entry_price2 = 195.0
    ctx.leg1 = om.LegState(symbol="AAA/USDC:USDC", side="sell", qty=1.0)
    ctx.leg2 = om.LegState(symbol="BBB/USDC:USDC", side="buy", qty=2.0)
    ctx.leg1.last_reprice_at = 0.0
    ctx.leg2.last_reprice_at = 0.0

    asyncio.run(om.run_execution(ctx, client, db))

    assert ctx.status == om.ExecStatus.DONE
    # partial_close must be set in DB
    assert any(a[1] == "partial_close" for a in db.statuses_set), \
        "Expected set_position_status(..., 'partial_close') to be called"
    # No market orders placed (no re-open/rollback)
    assert client.market_orders == []


# ---------------------------------------------------------------------------
# DUST flush — avg_price updated after flush
# ---------------------------------------------------------------------------

def test_dust_flush_updates_avg_price_and_saves_correct_pnl(monkeypatch):
    """After dust flush, avg_price must be recalculated; DB gets the updated exit price."""
    _patch_runtime(monkeypatch)

    flush_price = 105.0

    class FakeClientWithDustFlush(FakeClient):
        async def place_order(self, symbol, side, amount, order_type="market", params=None):
            result = await super().place_order(symbol, side, amount, order_type, params)
            # Simulate market order returning a fill price
            result["average"] = flush_price
            return result

    client = FakeClientWithDustFlush(
        orderbooks={
            "AAA/USDC:USDC": [{"bid": 100.0, "ask": 101.0, "spread_pct": 1.0}],
            "BBB/USDC:USDC": [
                {"bid": 200.0, "ask": 201.0, "spread_pct": 0.5},
                {"bid": 200.0, "ask": 201.0, "spread_pct": 0.5},
            ],
        },
        limit_status_scripts=[
            [{"id": "L1", "status": "closed", "filled": 1.0, "remaining": 0.0, "average": 100.0}],
            # leg2: partial fill 1.8, then becomes DUST (min notional exceeded for remainder)
            [
                {"id": "L2", "status": "open", "filled": 1.8, "remaining": 0.2, "average": 200.0},
                {"id": "L2", "status": "open", "filled": 1.8, "remaining": 0.2, "average": 200.0},
            ],
        ],
        min_notional={"BBB/USDC:USDC": 1000.0},
        market_prices={"BBB/USDC:USDC": flush_price},
    )
    db = FakeDb()
    db.closed_kwargs = {}
    original_close = db.close_position
    def _track_close(*a, **kw):
        db.closed_kwargs = kw
        return original_close(*a, **kw)
    db.close_position = _track_close

    ctx = _ctx(passive_s=2.0, aggressive_s=10.0, poll_s=2.0, reprice_s=4.0)
    ctx.is_close = True
    ctx.close_db_id = 42
    ctx.entry_price1 = 95.0
    ctx.entry_price2 = 195.0
    ctx.leg1 = om.LegState(symbol="AAA/USDC:USDC", side="sell", qty=1.0)
    ctx.leg2 = om.LegState(symbol="BBB/USDC:USDC", side="buy", qty=2.0)
    ctx.leg1.last_reprice_at = 0.0
    ctx.leg2.last_reprice_at = 0.0

    asyncio.run(om.run_execution(ctx, client, db))

    # leg2 was partially filled (1.8) then dust-flushed (0.2 at flush_price)
    # Expected weighted avg: (1.8*200 + 0.2*105) / 2.0 = (360 + 21) / 2 = 190.5
    assert ctx.leg2.avg_price == pytest.approx(190.5)
    # Market flush order was placed
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["symbol"] == "BBB/USDC:USDC"


# ---------------------------------------------------------------------------
# _force_market — market order ID tracked in leg and UserDataFeed
# ---------------------------------------------------------------------------

def test_force_market_updates_leg_order_id(monkeypatch):
    """After _force_market places a market order, leg.order_id is updated to the
    new market order's ID and UserDataFeed is notified to track fill events."""
    _patch_runtime(monkeypatch)

    registered = []

    class FakeUDF:
        def register_order(self, order_id):
            registered.append(order_id)

    client = FakeClient(
        orderbooks={
            "BBB/USDC:USDC": [{"bid": 200.0, "ask": 201.0, "spread_pct": 0.5}],
        },
        limit_status_scripts=[
            # L1 — the active limit order on the exchange (will be cancelled in _force_market)
            [{"id": "L1", "status": "open", "filled": 0.0, "remaining": 1.0, "average": None}],
        ],
        market_prices={"BBB/USDC:USDC": 201.0},
    )

    # Set up a context where leg2 has an active limit order and leg1 is already done
    ctx = _ctx(passive_s=30.0, aggressive_s=20.0, poll_s=2.0, reprice_s=4.0)
    ctx.is_close = True
    ctx.close_db_id = 10
    ctx.leg1 = om.LegState(symbol="AAA/USDC:USDC", side="sell", qty=1.0)
    ctx.leg2 = om.LegState(symbol="BBB/USDC:USDC", side="buy", qty=1.0)
    ctx.leg1.status = om.LegStatus.FILLED
    ctx.leg2.order_id = "L1"
    ctx.leg2.last_reprice_at = 0.0
    # Pre-populate order status so _refresh_fills sees the limit order as still open/unfilled
    client.order_statuses["L1"] = [{"id": "L1", "status": "open", "filled": 0.0, "remaining": 1.0, "average": None}]

    udf = FakeUDF()
    registered_orders: set = set()

    asyncio.run(om._force_market(ctx, client, udf=udf, registered_orders=registered_orders))

    # Market order for leg2 was placed
    market_orders_leg2 = [o for o in client.market_orders if o.get("symbol") == "BBB/USDC:USDC"]
    assert len(market_orders_leg2) >= 1

    # leg.order_id must be updated to the new market order ID
    assert ctx.leg2.order_id is not None
    assert ctx.leg2.order_id.startswith("M")

    # UDF must have been notified with the market order ID
    assert ctx.leg2.order_id in registered
    assert ctx.leg2.order_id in registered_orders


def test_close_execution_uses_accumulated_qty_after_averaging(monkeypatch):
    """Smart close of an averaged position must use the full accumulated leg qty."""
    _patch_runtime(monkeypatch)
    client = FakeClient(
        orderbooks={
            "AAA/USDC:USDC": [{"bid": 100.0, "ask": 101.0, "spread_pct": 1.0}],
            "BBB/USDC:USDC": [{"bid": 200.0, "ask": 201.0, "spread_pct": 0.5}],
        },
        limit_status_scripts=[
            [
                {"id": "L1", "status": "closed", "filled": 1.5, "remaining": 0.0, "average": 100.0},
            ],
            [
                {"id": "L2", "status": "open", "filled": 0.3, "remaining": 2.7, "average": 200.0},
                {"id": "L2", "status": "canceled", "filled": 0.3, "remaining": 2.7, "average": 200.0},
            ],
            [
                {"id": "L3", "status": "closed", "filled": 2.7, "remaining": 0.0, "average": 201.0},
            ],
        ],
    )
    db = FakeDb()
    ctx = _ctx(passive_s=2.0, aggressive_s=10.0, poll_s=2.0, reprice_s=4.0)
    ctx.is_close = True
    ctx.close_db_id = 42
    ctx.entry_price1 = 95.0
    ctx.entry_price2 = 195.0
    ctx.spread_side = "long_spread"
    ctx.leg1 = om.LegState(symbol="AAA/USDC:USDC", side="sell", qty=1.5)
    ctx.leg2 = om.LegState(symbol="BBB/USDC:USDC", side="buy", qty=3.0)
    ctx.leg1.last_reprice_at = 0.0
    ctx.leg2.last_reprice_at = 0.0

    asyncio.run(om.run_execution(ctx, client, db))

    assert ctx.status == om.ExecStatus.OPEN
    assert ctx.leg1.filled == pytest.approx(1.5)
    assert ctx.leg2.filled == pytest.approx(3.0)
    assert db.closed is not None
    assert db.closed[0] == 42
    assert len(client.market_orders) == 0
