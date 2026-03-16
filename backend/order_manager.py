"""
Smart limit-order execution engine for pair trades.

State machine:
  PLACING → PASSIVE (dynamic bid/ask repricing) → AGGRESSIVE (semi-aggressive repricing) → FORCING (market) → OPEN
                                                                                                          ↘ ROLLBACK → DONE
                                                                                                          ↘ CANCELLED
"""
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from logger import get_logger
import telegram_bot as tg_bot

log = get_logger("order_manager")


# ─── Enums ────────────────────────────────────────────────────────────────────

class LegStatus(str, Enum):
    WAITING    = "waiting"
    PARTIAL    = "partial"
    FILLED     = "filled"
    DUST       = "dust"
    CANCELLED  = "cancelled"
    FAILED     = "failed"


class ExecStatus(str, Enum):
    PLACING    = "placing"
    PASSIVE    = "passive"      # waiting for maker fill at bid/ask
    AGGRESSIVE = "aggressive"   # passive timeout — orders moved to ask/bid
    FORCING    = "forcing"      # aggressive timeout — sending market orders
    OPEN       = "open"         # both legs fully filled, position live
    ROLLBACK   = "rollback"     # one leg failed; closing the filled leg
    DONE       = "done"         # terminal: clean close or rollback complete
    CANCELLED  = "cancelled"    # terminal: user-requested cancel
    FAILED     = "failed"       # terminal: unrecoverable error


# ─── Config ───────────────────────────────────────────────────────────────────

@dataclass
class ExecConfig:
    passive_s:    float = 30.0   # total dynamic passive window
    aggressive_s: float = 20.0   # total semi-aggressive window
    allow_market: bool  = True   # use market order as final fallback
    poll_s:       float = 2.0    # order-status poll interval
    reprice_s:    float = 4.0    # do not cancel/re-place more often than this


# ─── Per-leg state ────────────────────────────────────────────────────────────

@dataclass
class LegState:
    symbol:    str
    side:      str      # 'buy' | 'sell'
    qty:       float

    order_id:  Optional[str]   = None
    status:    LegStatus       = LegStatus.WAITING
    filled:    float           = 0.0
    remaining: float           = 0.0
    avg_price: Optional[float] = None
    working_price: Optional[float] = None
    last_reprice_at: float         = field(default_factory=time.time)
    hold_until_stage_end: bool     = False

    def __post_init__(self):
        self.remaining = self.qty

    @property
    def is_done(self) -> bool:
        return self.status in (LegStatus.FILLED, LegStatus.DUST)

    def absorb_order(self, order: dict) -> None:
        """Sync fill state from a ccxt order dict."""
        filled    = float(order.get("filled")    or 0)
        remaining = float(order.get("remaining") or 0)
        avg       = order.get("average")
        status    = order.get("status", "open")

        self.filled    = filled
        self.remaining = remaining
        if avg:
            self.avg_price = float(avg)

        if status in ("closed", "filled") or remaining <= 1e-9:
            self.status = LegStatus.FILLED
        elif status in ("canceled", "cancelled"):
            self.status = LegStatus.PARTIAL if filled > 0 else LegStatus.CANCELLED
        elif status in ("rejected", "expired"):
            self.status = LegStatus.PARTIAL if filled > 0 else LegStatus.FAILED
        elif filled > 0:
            self.status = LegStatus.PARTIAL
        else:
            self.status = LegStatus.WAITING


# ─── Execution context ────────────────────────────────────────────────────────

@dataclass
class ExecContext:
    exec_id:     str
    leg1:        LegState
    leg2:        LegState
    config:      ExecConfig
    spread_side: str             # 'long_spread' | 'short_spread'

    hedge_ratio:   Optional[float] = None
    entry_zscore:  Optional[float] = None
    size_usd:      Optional[float] = None
    sizing_method: Optional[str]   = None
    leverage:      int             = 1
    timeframe:     str             = "1h"
    candle_limit:  int             = 500
    zscore_window: int             = 20

    # Close-mode fields
    is_close:      bool            = False
    close_db_id:   Optional[int]   = None
    entry_price1:  Optional[float] = None
    entry_price2:  Optional[float] = None
    exit_zscore:   Optional[float] = None

    status:     ExecStatus    = ExecStatus.PLACING
    started_at: float         = field(default_factory=time.time)
    events:     list[str]     = field(default_factory=list)
    db_id:      Optional[int] = None
    cancel_req: bool          = False

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def evt(self, msg: str) -> None:
        self.events.append(f"[{self.elapsed():5.1f}s] {msg}")
        log.info("[%s] %s", self.exec_id, msg)

    def to_dict(self) -> dict:
        def _leg(l: LegState) -> dict:
            return {
                "symbol":    l.symbol,
                "side":      l.side,
                "qty":       l.qty,
                "filled":    round(l.filled, 8),
                "remaining": round(l.remaining, 8),
                "avg_price": l.avg_price,
                "status":    l.status,
                "order_id":  l.order_id,
            }
        return {
            "exec_id":    self.exec_id,
            "status":     self.status,
            "elapsed_s":  round(self.elapsed(), 1),
            "spread_side": self.spread_side,
            "is_close":    self.is_close,
            "close_db_id": self.close_db_id,
            "leg1":       _leg(self.leg1),
            "leg2":       _leg(self.leg2),
            "events":     self.events[-40:],
            "db_id":      self.db_id,
        }


# ─── Main execution coroutine ─────────────────────────────────────────────────

async def run_execution(ctx: ExecContext, client, db_module) -> None:
    """
    Runs as a background asyncio.Task.
    Mutates ctx in-place — callers poll ctx.to_dict().
    """
    cfg = ctx.config
    deadline_aggressive = ctx.started_at + cfg.passive_s
    deadline_market     = ctx.started_at + cfg.passive_s + cfg.aggressive_s

    try:
        # ── 1. Fetch orderbooks ────────────────────────────────────────────────
        ob1, ob2 = await asyncio.gather(
            client.fetch_order_book(ctx.leg1.symbol),
            client.fetch_order_book(ctx.leg2.symbol),
        )
        p1 = _passive_price(ctx.leg1.side, ob1)
        p2 = _passive_price(ctx.leg2.side, ob2)

        ctx.evt(
            f"Orderbook  {ctx.leg1.symbol}: bid={ob1['bid']} ask={ob1['ask']} "
            f"spread={ob1['spread_pct']:.3f}%"
        )
        ctx.evt(
            f"Orderbook  {ctx.leg2.symbol}: bid={ob2['bid']} ask={ob2['ask']} "
            f"spread={ob2['spread_pct']:.3f}%"
        )

        # ── 2. Place both legs as passive limit orders ────────────────────────
        ctx.evt(f"Placing passive limits: {ctx.leg1.symbol}@{p1} | {ctx.leg2.symbol}@{p2}")
        ord1, ord2 = await asyncio.gather(
            client.place_limit_order(ctx.leg1.symbol, ctx.leg1.side, ctx.leg1.qty, p1),
            client.place_limit_order(ctx.leg2.symbol, ctx.leg2.side, ctx.leg2.qty, p2),
        )
        ctx.leg1.order_id = ord1["id"]
        ctx.leg2.order_id = ord2["id"]
        ctx.leg1.working_price = p1
        ctx.leg2.working_price = p2
        ctx.leg1.last_reprice_at = time.time()
        ctx.leg2.last_reprice_at = ctx.leg1.last_reprice_at
        ctx.status = ExecStatus.PASSIVE
        ctx.evt(f"Orders live: {ctx.leg1.order_id} | {ctx.leg2.order_id}")

        # ── 3. Poll loop ───────────────────────────────────────────────────────
        while True:
            await asyncio.sleep(cfg.poll_s)

            if ctx.cancel_req:
                await _cancel_open_orders(ctx, client)
                ctx.status = ExecStatus.CANCELLED
                ctx.evt("Cancelled by user")
                return

            await _refresh_fills(ctx, client)

            if ctx.leg1.is_done and ctx.leg2.is_done:
                break

            now = time.time()

            if ctx.status == ExecStatus.PASSIVE:
                if now >= deadline_aggressive:
                    ctx.status = ExecStatus.AGGRESSIVE
                    ctx.evt("Passive window ended — switching to semi-aggressive repricing")
                    await _start_stage(ctx, client, mode="semi")
                else:
                    await _reprice_live_orders(ctx, client, mode="passive")
                continue

            if ctx.status == ExecStatus.AGGRESSIVE:
                if now >= deadline_market:
                    ctx.status = ExecStatus.FORCING
                    ctx.evt("Semi-aggressive timeout — forcing remaining fills")
                    if cfg.allow_market:
                        await _force_market(ctx, client)
                        await asyncio.sleep(3)
                        await _refresh_fills(ctx, client)
                    else:
                        await _close_stage_orders(ctx, client)
                    break
                await _reprice_live_orders(ctx, client, mode="semi")

        # ── 4. Outcome ─────────────────────────────────────────────────────────
        if ctx.leg1.is_done and ctx.leg2.is_done:
            ctx.status = ExecStatus.OPEN
            ctx.evt(
                f"OPEN  {ctx.leg1.symbol}@{ctx.leg1.avg_price} | "
                f"{ctx.leg2.symbol}@{ctx.leg2.avg_price}"
            )
            if ctx.is_close and ctx.close_db_id:
                # Calculate PnL: leg1=sym1, leg2=sym2; avg_price = exit price
                pnl = None
                if (ctx.entry_price1 and ctx.entry_price2
                        and ctx.leg1.avg_price and ctx.leg2.avg_price):
                    sign = 1 if ctx.spread_side == "long_spread" else -1
                    pnl1 = ctx.leg1.qty * (ctx.leg1.avg_price - ctx.entry_price1) * sign
                    pnl2 = ctx.leg2.qty * (ctx.entry_price2 - ctx.leg2.avg_price) * sign
                    pnl = round(pnl1 + pnl2, 4)
                db_module.close_position(
                    ctx.close_db_id,
                    ctx.leg1.avg_price,
                    ctx.leg2.avg_price,
                    pnl,
                    ctx.exit_zscore,
                )
                ctx.db_id = ctx.close_db_id
                ctx.evt(f"Position closed in DB id={ctx.close_db_id} pnl={pnl}")
                asyncio.create_task(tg_bot.notify_position_closed(
                    ctx.leg1.symbol, ctx.leg2.symbol, ctx.spread_side,
                    pnl, ctx.exit_zscore, reason="smart",
                ))
            else:
                pos_id = db_module.save_open_position(
                    symbol1=ctx.leg1.symbol,
                    symbol2=ctx.leg2.symbol,
                    side=ctx.spread_side,
                    qty1=ctx.leg1.filled,
                    qty2=ctx.leg2.filled,
                    hedge_ratio=ctx.hedge_ratio or 1.0,
                    entry_zscore=ctx.entry_zscore,
                    entry_price1=ctx.leg1.avg_price,
                    entry_price2=ctx.leg2.avg_price,
                    size_usd=ctx.size_usd,
                    sizing_method=ctx.sizing_method,
                    leverage=ctx.leverage,
                    timeframe=ctx.timeframe,
                    candle_limit=ctx.candle_limit,
                    zscore_window=ctx.zscore_window,
                )
                ctx.db_id = pos_id
                ctx.evt(f"Saved to DB id={pos_id}")
                asyncio.create_task(tg_bot.notify_position_opened(
                    ctx.leg1.symbol, ctx.leg2.symbol, ctx.spread_side,
                    ctx.entry_zscore, ctx.leg1.avg_price, ctx.leg2.avg_price,
                    ctx.size_usd, ctx.leverage,
                ))
        else:
            # Partial fill → rollback
            filled_leg = None
            for leg in (ctx.leg1, ctx.leg2):
                if leg.filled > 0:
                    filled_leg = leg
                    break

            if filled_leg:
                ctx.status = ExecStatus.ROLLBACK
                ctx.evt(
                    f"Incomplete fill — leg1={ctx.leg1.status} leg2={ctx.leg2.status}. "
                    f"Rolling back {filled_leg.symbol}"
                )
                asyncio.create_task(tg_bot.notify_rollback(
                    ctx.leg1.symbol, ctx.leg2.symbol, ctx.exec_id,
                ))
                await _rollback_leg(filled_leg, client, ctx)
            else:
                ctx.evt("Execution ended — nothing filled, no rollback needed")

            ctx.status = ExecStatus.DONE

    except Exception as exc:
        ctx.status = ExecStatus.FAILED
        ctx.evt(f"FATAL: {exc}")
        log.error("Execution %s failed: %s", ctx.exec_id, exc, exc_info=True)
        asyncio.create_task(tg_bot.notify_execution_failed(
            ctx.leg1.symbol, ctx.leg2.symbol, ctx.exec_id, str(exc),
        ))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _passive_price(side: str, ob: dict) -> float:
    """Maker placement: buy at bid, sell at ask."""
    return float(ob["bid"] if side == "buy" else ob["ask"])


def _taker_price(side: str, ob: dict) -> float:
    """Crosses the spread: buy at ask, sell at bid."""
    return float(ob["ask"] if side == "buy" else ob["bid"])


async def _refresh_fills(ctx: ExecContext, client) -> None:
    legs_to_poll = [l for l in (ctx.leg1, ctx.leg2) if not l.is_done and l.order_id]
    if not legs_to_poll:
        return
    results = await asyncio.gather(
        *[client.fetch_order(l.symbol, l.order_id) for l in legs_to_poll],
        return_exceptions=True,
    )
    for leg, result in zip(legs_to_poll, results):
        if isinstance(result, Exception):
            ctx.evt(f"  Poll error {leg.symbol}: {result}")
            continue
        prev = leg.status
        leg.absorb_order(result)
        if leg.status != prev:
            ctx.evt(
                f"  {leg.symbol}: {prev} → {leg.status} "
                f"(filled={leg.filled:.6f} rem={leg.remaining:.6f})"
            )


async def _start_stage(ctx: ExecContext, client, mode: str) -> None:
    """Rebuild remaining working orders for the next stage."""
    unfilled = [l for l in (ctx.leg1, ctx.leg2) if not l.is_done]
    if not unfilled:
        return

    obs = await _fetch_orderbooks(client, unfilled)
    now = time.time()

    for leg in unfilled:
        if leg.order_id:
            try:
                await client.cancel_order(leg.symbol, leg.order_id)
            except Exception as e:
                ctx.evt(f"  Cancel {leg.symbol} warn: {e} — refreshing fill")
            await _refresh_fills(ctx, client)

        leg.order_id = None
        leg.working_price = None
        leg.hold_until_stage_end = False

        if leg.is_done:
            continue

        ob = obs.get(leg.symbol)
        if not ob:
            ctx.evt(f"  Stage switch skipped for {leg.symbol}: no orderbook")
            leg.status = LegStatus.FAILED
            continue

        new_price = _target_price(mode, leg.side, ob)
        can_place, reason = await _can_place_remaining(client, leg, new_price)
        if not can_place:
            _finalise_non_placeable_leg(ctx, leg, reason)
            continue

        await _place_remaining_limit(ctx, client, leg, new_price, mode, now=now)


async def _reprice_live_orders(ctx: ExecContext, client, mode: str) -> None:
    """Reprice live limit orders within the current stage."""
    now = time.time()
    legs = [
        l for l in (ctx.leg1, ctx.leg2)
        if not l.is_done and not l.hold_until_stage_end and (now - l.last_reprice_at) >= ctx.config.reprice_s
    ]
    if not legs:
        return

    obs = await _fetch_orderbooks(client, legs)
    for leg in legs:
        ob = obs.get(leg.symbol)
        if not ob:
            leg.last_reprice_at = now
            continue

        new_price = _target_price(mode, leg.side, ob)
        if leg.working_price is not None and _same_price(leg.working_price, new_price):
            leg.last_reprice_at = now
            continue

        can_place, reason = await _can_place_remaining(client, leg, new_price)
        if not can_place:
            leg.hold_until_stage_end = True
            leg.last_reprice_at = now
            ctx.evt(
                f"  Hold {leg.symbol}: remaining {leg.remaining:.6f} "
                f"cannot be re-placed ({reason}); keeping current order until stage end"
            )
            continue

        if leg.order_id:
            try:
                await client.cancel_order(leg.symbol, leg.order_id)
            except Exception as e:
                ctx.evt(f"  Cancel {leg.symbol} warn: {e} — refreshing fill")
            await _refresh_fills(ctx, client)

        if leg.is_done:
            continue

        can_place, reason = await _can_place_remaining(client, leg, new_price)
        if not can_place:
            _finalise_non_placeable_leg(ctx, leg, reason)
            continue

        await _place_remaining_limit(ctx, client, leg, new_price, mode, now=now)


async def _force_market(ctx: ExecContext, client) -> None:
    """Cancel remaining limit orders and fill via market."""
    for leg in (ctx.leg1, ctx.leg2):
        if leg.is_done:
            continue
        if leg.order_id:
            try:
                await client.cancel_order(leg.symbol, leg.order_id)
            except Exception:
                pass
        await _refresh_fills(ctx, client)
        if leg.remaining > 1e-9:
            ob = await client.fetch_order_book(leg.symbol)
            ref_price = _taker_price(leg.side, ob)
            can_place, reason = await _can_place_remaining(client, leg, ref_price)
            if not can_place:
                _finalise_non_placeable_leg(ctx, leg, reason)
                continue
            ctx.evt(f"  Market fill: {leg.symbol} {leg.side} {leg.remaining:.6f}")
            try:
                prev_filled = leg.filled
                prev_avg = leg.avg_price
                order = await client.place_order(leg.symbol, leg.side, leg.remaining, order_type="market")
                market_filled = float(order.get("filled") or leg.remaining)
                market_avg = order.get("average")
                leg.filled = min(leg.qty, prev_filled + market_filled)
                leg.remaining = 0.0
                leg.status    = LegStatus.FILLED
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


async def _cancel_open_orders(ctx: ExecContext, client) -> None:
    for leg in (ctx.leg1, ctx.leg2):
        if not leg.is_done and leg.order_id:
            try:
                await client.cancel_order(leg.symbol, leg.order_id)
                leg.status = LegStatus.CANCELLED
                leg.order_id = None
                leg.working_price = None
            except Exception:
                pass


async def _rollback_leg(leg: LegState, client, ctx: ExecContext) -> None:
    """Close a filled leg at market to neutralize exposure."""
    reverse = "sell" if leg.side == "buy" else "buy"
    ctx.evt(f"ROLLBACK: closing {leg.symbol} {reverse} {leg.filled:.6f} at market")
    try:
        await client.place_order(leg.symbol, reverse, leg.filled, order_type="market")
        ctx.evt(f"Rollback OK: {leg.symbol}")
    except Exception as e:
        ctx.evt(f"ROLLBACK FAILED {leg.symbol}: {e} — MANUAL ACTION REQUIRED")
        log.error("ROLLBACK FAILED %s: %s", leg.symbol, e)


async def _close_stage_orders(ctx: ExecContext, client) -> None:
    """Cancel any still-open orders at stage end."""
    for leg in (ctx.leg1, ctx.leg2):
        if leg.is_done or not leg.order_id:
            continue
        try:
            await client.cancel_order(leg.symbol, leg.order_id)
        except Exception:
            pass
    await _refresh_fills(ctx, client)
    for leg in (ctx.leg1, ctx.leg2):
        if leg.order_id and leg.status in (LegStatus.CANCELLED, LegStatus.FAILED):
            leg.order_id = None
            leg.working_price = None


async def _fetch_orderbooks(client, legs: list[LegState]) -> dict[str, dict]:
    results = await asyncio.gather(
        *[client.fetch_order_book(leg.symbol) for leg in legs],
        return_exceptions=True,
    )
    obs: dict[str, dict] = {}
    for leg, result in zip(legs, results):
        if isinstance(result, Exception):
            continue
        obs[leg.symbol] = result
    return obs


def _target_price(mode: str, side: str, ob: dict) -> float:
    if mode == "semi":
        return _semi_aggressive_price(side, ob)
    return _passive_price(side, ob)


def _semi_aggressive_price(side: str, ob: dict) -> float:
    bid = ob.get("bid")
    ask = ob.get("ask")
    if bid is None or ask is None or ask <= bid:
        return _passive_price(side, ob)
    spread = float(ask) - float(bid)
    if side == "buy":
        return float(bid) + spread * 0.25
    return float(ask) - spread * 0.25


def _same_price(a: float, b: float) -> bool:
    tol = max(1e-12, max(abs(a), abs(b)) * 1e-9)
    return abs(a - b) <= tol


async def _can_place_remaining(client, leg: LegState, price: float) -> tuple[bool, str]:
    if leg.remaining <= 1e-9:
        return False, "nothing remaining"

    rounded = await client.round_amount(leg.symbol, leg.remaining)
    if rounded <= 0:
        return False, "rounds to zero"

    ok, actual, minimum = await client.check_min_notional(leg.symbol, leg.remaining, price)
    if not ok:
        return False, f"notional ${actual:.4f} < min ${minimum:.4f}"

    return True, ""


async def _place_remaining_limit(
    ctx: ExecContext,
    client,
    leg: LegState,
    price: float,
    mode: str,
    *,
    now: float,
) -> None:
    label = "passive" if mode == "passive" else "semi-aggressive"
    ctx.evt(f"  Reprice {leg.symbol}: {leg.remaining:.6f} @ {price} ({label})")
    try:
        new_ord = await client.place_limit_order(leg.symbol, leg.side, leg.remaining, price)
        leg.order_id = new_ord["id"]
        leg.working_price = price
        leg.last_reprice_at = now
        leg.hold_until_stage_end = False
    except Exception as e:
        ctx.evt(f"  Reprice FAILED {leg.symbol}: {e}")
        leg.status = LegStatus.FAILED
        leg.order_id = None
        leg.working_price = None


def _finalise_non_placeable_leg(ctx: ExecContext, leg: LegState, reason: str) -> None:
    leg.order_id = None
    leg.working_price = None
    leg.hold_until_stage_end = False
    if leg.filled > 0:
        leg.status = LegStatus.DUST
        ctx.evt(
            f"  Accept {leg.symbol}: remaining {leg.remaining:.6f} "
            f"is below exchange minimum ({reason}); keeping filled qty={leg.filled:.6f}"
        )
    else:
        leg.status = LegStatus.CANCELLED
        ctx.evt(
            f"  Stop {leg.symbol}: no fill and remaining {leg.remaining:.6f} "
            f"cannot be placed ({reason})"
        )
