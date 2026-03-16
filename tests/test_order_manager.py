"""
Unit tests for order_manager.py — Smart v2 execution engine.

These tests use lightweight async fakes for Binance/DB so we can verify:
- dynamic passive repricing
- semi-aggressive stage pricing
- "hold until stage end" behavior for non-placeable residuals
- dust residuals being accepted and saved as the actual filled qty
"""
import asyncio

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

    async def place_limit_order(self, symbol, side, amount, price):
        order_id = f"L{self.next_order_id}"
        self.next_order_id += 1
        self.limit_orders.append({
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
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

    async def place_order(self, symbol, side, amount, order_type="market"):
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

    def save_open_position(self, **kwargs):
        self.saved = kwargs
        return 123

    def close_position(self, *args):
        self.closed = args
        return True


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
