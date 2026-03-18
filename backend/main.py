import asyncio
import functools
import json
import math
import os
import time
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from binance_client import BinanceClient
from strategy import PairTradingStrategy
import db
from logger import get_logger
from order_manager import ExecConfig, ExecContext, LegState, run_execution
import telegram_bot as tg_bot

load_dotenv()

log = get_logger("pair_trading")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v):
    """Convert a value to float, returning None for NaN/Inf."""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except Exception:
        return None


def _clean(obj):
    """Recursively sanitise NaN/Inf so FastAPI can serialise to JSON."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return _safe_float(obj)
    if isinstance(obj, np.floating):
        return _safe_float(float(obj))
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

client = BinanceClient()
strategy = PairTradingStrategy()

# active smart executions: exec_id -> ExecContext
active_executions: dict = {}
# timestamps of when each execution was created (for TTL cleanup)
_exec_created_at: dict[str, float] = {}
_EXEC_TTL = 7200  # remove terminal executions after 2 hours
# track which terminal execs have already been persisted to execution_history table
_exec_saved_to_db: set[str] = set()

SUPPORTED_MARGIN_ASSETS = {"USDT", "USDC"}


async def _run_sync(func, *args):
    """Run a CPU-bound function in a thread-pool so it doesn't block the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))


# ---------------------------------------------------------------------------
# Shared price cache — single OHLCV feed for WS + monitor + future watchlist
# ---------------------------------------------------------------------------

class PriceCache:
    """
    Centralised OHLCV cache.  Consumers call subscribe() to register a pair;
    a single background task (run()) refreshes all subscribed keys every 5 s.
    Reference-counting: entries are kept alive only while at least one
    subscriber is active.

    Key = (sym1, sym2, timeframe, limit)
    Entry = {"price1": pd.Series, "price2": pd.Series, "df1": DataFrame, "df2": DataFrame}
    """

    FEED_INTERVAL = 2  # seconds between refreshes

    def __init__(self) -> None:
        self._store: dict[tuple, dict] = {}
        self._refs:  dict[tuple, int]  = {}

    def subscribe(self, sym1: str, sym2: str, tf: str, limit: int) -> tuple:
        key = (sym1, sym2, tf, limit)
        self._refs[key] = self._refs.get(key, 0) + 1
        return key

    def unsubscribe(self, key: tuple) -> None:
        if key not in self._refs:
            return
        self._refs[key] -= 1
        if self._refs[key] <= 0:
            self._refs.pop(key, None)
            self._store.pop(key, None)

    def get(self, key: tuple) -> Optional[dict]:
        return self._store.get(key)

    def find_cached(self, sym1: str, sym2: str, tf: str, limit: int) -> Optional[dict]:
        """Find a cache entry for (sym1, sym2, tf) with at least `limit` rows."""
        # Exact key first
        exact = self._store.get((sym1, sym2, tf, limit))
        if exact is not None:
            return exact
        # Any key with same (sym1, sym2, tf) and enough data
        for (s1, s2, t, l), entry in self._store.items():
            if s1 == sym1 and s2 == sym2 and t == tf and l >= limit:
                return entry
        return None

    async def _refresh_one(self, key: tuple) -> None:
        sym1, sym2, tf, limit = key
        df1, df2 = await asyncio.gather(
            client.fetch_ohlcv(sym1, tf, limit),
            client.fetch_ohlcv(sym2, tf, limit),
        )
        p1 = df1["close"]
        p2 = df2["close"]
        p1, p2 = p1.align(p2, join="inner")
        df1_aligned = df1.loc[p1.index]
        df2_aligned = df2.loc[p2.index]
        self._store[key] = {"price1": p1, "price2": p2, "df1": df1_aligned, "df2": df2_aligned}

    async def run(self) -> None:
        """Background task: refresh all subscribed keys every FEED_INTERVAL seconds."""
        while True:
            keys = list(self._refs.keys())
            if keys:
                results = await asyncio.gather(
                    *[self._refresh_one(k) for k in keys],
                    return_exceptions=True,
                )
                for k, r in zip(keys, results):
                    if isinstance(r, Exception):
                        log.warning(f"price_cache refresh error {k}: {r}")
            await asyncio.sleep(self.FEED_INTERVAL)


price_cache = PriceCache()

# Watchlist subscriptions: tag → cache_key
# Tag format: "{sym1}|{sym2}|{tf}|{limit}"
_watchlist_keys: dict[str, tuple] = {}


def _pair_meta(meta1: dict, meta2: dict) -> dict:
    asset1 = meta1.get("margin_asset")
    asset2 = meta2.get("margin_asset")
    shared_asset = asset1 if asset1 == asset2 else None
    return {
        "symbol1": meta1.get("symbol"),
        "symbol2": meta2.get("symbol"),
        "id1": meta1.get("id"),
        "id2": meta2.get("id"),
        "margin_asset1": asset1,
        "margin_asset2": asset2,
        "shared_margin_asset": shared_asset,
        "tradeable": bool(shared_asset and shared_asset in SUPPORTED_MARGIN_ASSETS),
    }


async def _resolve_pair(symbol1: str, symbol2: str) -> tuple[dict, dict]:
    raw1 = _normalise_symbol(symbol1)
    raw2 = _normalise_symbol(symbol2)
    meta1, meta2 = await asyncio.gather(
        client.get_market_info(raw1),
        client.get_market_info(raw2),
    )
    return meta1, meta2


def _shared_margin_asset(meta1: dict, meta2: dict) -> Optional[str]:
    asset1 = meta1.get("margin_asset")
    asset2 = meta2.get("margin_asset")
    if asset1 == asset2 and asset1 in SUPPORTED_MARGIN_ASSETS:
        return asset1
    return None


def _require_tradeable_pair(meta1: dict, meta2: dict) -> str:
    margin_asset = _shared_margin_asset(meta1, meta2)
    if margin_asset:
        return margin_asset
    raise HTTPException(
        status_code=400,
        detail=(
            "Trading requires both legs to use the same supported margin asset. "
            f"{meta1.get('id')} settles in {meta1.get('margin_asset')}, "
            f"{meta2.get('id')} settles in {meta2.get('margin_asset')}."
        ),
    )


async def _do_market_close(
    pos: dict, exit_zscore: Optional[float] = None, reason: str = "auto"
) -> dict:
    """Close both legs of a DB position at market. Returns result dict."""
    sym1 = pos["symbol1"]
    sym2 = pos["symbol2"]
    ticker1, ticker2 = await asyncio.gather(
        client.fetch_ticker(sym1),
        client.fetch_ticker(sym2),
    )
    price1 = ticker1["last"]
    price2 = ticker2["last"]

    positions = await client.get_positions()
    pos_map = {p["symbol"]: p for p in positions}
    p1 = pos_map.get(sym1)
    p2 = pos_map.get(sym2)

    qty1 = abs(p1["size"]) if p1 else pos["qty1"]
    qty2 = abs(p2["size"]) if p2 else pos["qty2"]
    side1 = "sell" if (p1 and p1["side"] == "long") else "buy"
    side2 = "buy" if (p2 and p2["side"] == "short") else "sell"

    await asyncio.gather(
        client.place_order(sym1, side1, qty1),
        client.place_order(sym2, side2, qty2),
    )

    pnl = None
    if pos.get("entry_price1") and pos.get("entry_price2"):
        sign = 1 if pos["side"] == "long_spread" else -1
        pnl = round(
            pos["qty1"] * (price1 - pos["entry_price1"]) * sign
            + pos["qty2"] * (pos["entry_price2"] - price2) * sign,
            4,
        )
    db.close_position(pos["id"], price1, price2, pnl, exit_zscore)
    asyncio.create_task(tg_bot.notify_position_closed(
        pos["symbol1"], pos["symbol2"], pos["side"], pnl, exit_zscore, reason=reason,
    ))
    return {"pnl": pnl, "price1": price1, "price2": price2}


async def _do_smart_close_trigger(pos: dict, exit_zscore: Optional[float] = None) -> str:
    """Start a smart limit-order close for a triggered position. Returns exec_id."""
    import uuid

    sym1, sym2 = pos["symbol1"], pos["symbol2"]
    spread_side = pos["side"]  # "long_spread" | "short_spread"
    # Closing reverses the spread direction
    side1 = "sell" if spread_side == "long_spread" else "buy"
    side2 = "buy"  if spread_side == "long_spread" else "sell"

    positions = await client.get_positions()
    pos_map = {p["symbol"]: p for p in positions}
    p1 = pos_map.get(sym1)
    p2 = pos_map.get(sym2)
    qty1 = abs(p1["size"]) if p1 else pos["qty1"]
    qty2 = abs(p2["size"]) if p2 else pos["qty2"]

    exec_id = uuid.uuid4().hex[:8]
    cfg = ExecConfig(passive_s=30.0, aggressive_s=20.0, allow_market=True)
    ctx = ExecContext(
        exec_id=exec_id,
        leg1=LegState(symbol=sym1, side=side1, qty=qty1),
        leg2=LegState(symbol=sym2, side=side2, qty=qty2),
        config=cfg,
        spread_side=spread_side,
        is_close=True,
        close_db_id=pos["id"],
        entry_price1=pos.get("entry_price1"),
        entry_price2=pos.get("entry_price2"),
        exit_zscore=exit_zscore,
    )
    active_executions[exec_id] = ctx
    _exec_created_at[exec_id] = time.monotonic()
    asyncio.create_task(run_execution(ctx, client, db))
    return exec_id


_MONITOR_ZSCORE_WINDOW = 20
_MONITOR_TIMEFRAME = "1h"


async def monitor_position_triggers() -> None:
    """
    Background task: check TP/SL/alert triggers every 2 s.

    Two sources of triggers:
    1. Legacy: tp_zscore/sl_zscore columns on open_positions
    2. New: rows in the `triggers` table with status='active'

    Fetches OHLCV directly (not via price_cache) with a small limit so it
    can run every 2 s without hitting Binance rate limits.

    Optimisations:
    - All OHLCV fetches for the cycle are gathered in parallel (one batch).
    - Hedge ratio for standalone triggers is cached with 60 s TTL.
    """
    await asyncio.sleep(15)  # wait for startup
    closing_tags: set[str] = set()     # tags currently being closed (smart)
    closing_pairs: set[tuple] = set()  # (sym1,sym2) pairs being closed — prevents double close across position TP + standalone trigger
    alert_states: dict[str, str] = {}  # tag → "idle" | "alerted" (hysteresis)
    # Hedge ratio cache for standalone triggers: (sym1, sym2, tf, limit) → (hedge, timestamp)
    _hedge_cache: dict[tuple, tuple[float, float]] = {}
    _HEDGE_TTL = 60.0  # recompute hedge ratio once per minute

    while True:
        try:
            current_tags: set[str] = set()

            # ── Collect all items that need OHLCV data ──────────────────────
            positions = db.get_open_positions()
            active_triggers = db.get_active_triggers()

            # Build a list of fetch tasks: (key, sym1, sym2, tf, limit)
            fetch_specs = []  # list of (fetch_key, sym1, sym2, tf, limit)
            # Map from fetch_key to items that need this data
            pos_items = []   # (pos, tag, tp, sl, fetch_key)
            trig_items = []  # (trig, tag, fetch_key)

            for pos in positions:
                tp = pos.get("tp_zscore")
                sl = pos.get("sl_zscore")
                if tp is None and sl is None:
                    continue
                pos_id = pos["id"]
                tag = f"pos_{pos_id}"
                current_tags.add(tag)
                if tag in closing_tags:
                    continue
                pos_tf = pos.get("timeframe") or _MONITOR_TIMEFRAME
                pos_zw = pos.get("zscore_window") or _MONITOR_ZSCORE_WINDOW
                limit = min(pos.get("candle_limit") or (pos_zw * 5), 500)
                fk = (pos["symbol1"], pos["symbol2"], pos_tf, limit)
                fetch_specs.append(fk)
                pos_items.append((pos, tag, tp, sl, fk))

            for trig in active_triggers:
                trig_id = trig["id"]
                tag = f"trig_{trig_id}"
                current_tags.add(tag)
                if tag in closing_tags:
                    continue
                sym1, sym2 = trig["symbol1"], trig["symbol2"]
                trig_tf = trig.get("timeframe") or _MONITOR_TIMEFRAME
                trig_zw = trig.get("zscore_window") or _MONITOR_ZSCORE_WINDOW
                limit = max(trig_zw * 3, 60)
                fk = (sym1, sym2, trig_tf, limit)
                fetch_specs.append(fk)
                trig_items.append((trig, tag, fk))

            # ── Deduplicate and fetch all OHLCV in parallel ─────────────────
            unique_specs = list(set(fetch_specs))
            ohlcv_data = {}  # fk → (p1, p2) aligned Series
            if unique_specs:
                async def _fetch_pair(fk):
                    s1, s2, tf, lim = fk
                    df1, df2 = await asyncio.gather(
                        client.fetch_ohlcv(s1, tf, lim),
                        client.fetch_ohlcv(s2, tf, lim),
                    )
                    p1, p2 = df1["close"], df2["close"]
                    p1, p2 = p1.align(p2, join="inner")
                    return fk, p1, p2

                results = await asyncio.gather(
                    *[_fetch_pair(fk) for fk in unique_specs],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, Exception):
                        log.warning(f"monitor: batch fetch error: {r}")
                        continue
                    fk, p1, p2 = r
                    ohlcv_data[fk] = (p1, p2)

            # ── 1. Legacy: open_positions with tp_zscore/sl_zscore ──────────
            for pos, tag, tp, sl, fk in pos_items:
                pos_id = pos["id"]
                pos_zw = pos.get("zscore_window") or _MONITOR_ZSCORE_WINDOW
                if fk not in ohlcv_data:
                    continue
                try:
                    p1, p2 = ohlcv_data[fk]
                    hedge = pos["hedge_ratio"]
                    spread = strategy.calculate_spread(p1, p2, hedge)
                    zscore_series = strategy.calculate_zscore(spread, window=pos_zw)
                    current_z = float(zscore_series.dropna().iloc[-1])

                    # Direction-agnostic: TP = mean reversion (|z| shrinks),
                    # SL = divergence (|z| grows). Works for any side/sign.
                    abs_z = abs(current_z)
                    trigger = None
                    if tp is not None and abs_z <= tp:
                        trigger = "tp"
                    elif sl is not None and abs_z >= sl:
                        trigger = "sl"

                    if trigger:
                        sym1, sym2 = pos["symbol1"], pos["symbol2"]
                        # Guard: if another source already started a close for this pair, skip
                        if (sym1, sym2) in closing_pairs:
                            continue
                        threshold = tp if trigger == "tp" else sl
                        log.info(
                            f"TRIGGER {trigger.upper()} | pos={pos_id} "
                            f"{pos['symbol1']}/{pos['symbol2']} | z={current_z:.3f} | "
                            f"tp={tp} sl={sl}"
                        )
                        await tg_bot.notify_trigger_fired(
                            pos["symbol1"], pos["symbol2"], pos["side"],
                            trigger, current_z, threshold,
                        )
                        # Safety: check that the position still exists on the exchange.
                        live_positions = await client.get_positions()
                        live_syms = {p["symbol"] for p in live_positions if p.get("size")}
                        if sym1 not in live_syms and sym2 not in live_syms:
                            log.warning(
                                f"TRIGGER {trigger.upper()} | pos={pos_id} | "
                                f"no live positions found — position was closed manually. "
                                f"Removing stale DB record."
                            )
                            db.delete_open_position(pos_id)
                            current_tags.discard(tag)
                            continue

                        # Clear TP/SL in DB immediately — prevents re-firing on the
                        # next monitor cycle regardless of closing_tags state.
                        db.set_position_triggers(pos_id, None, None, False)

                        closing_pairs.add((sym1, sym2))  # block standalone triggers for same pair

                        smart_key = "tp_smart" if trigger == "tp" else "sl_smart"
                        use_smart = bool(pos.get(smart_key, True))
                        if use_smart:
                            exec_id = await _do_smart_close_trigger(pos, exit_zscore=current_z)
                            closing_tags.add(tag)
                            log.info(
                                f"AUTO-SMART-CLOSE {trigger.upper()} | pos={pos_id} | "
                                f"exec_id={exec_id}"
                            )
                        else:
                            result = await _do_market_close(pos, exit_zscore=current_z, reason=trigger)
                            log.info(
                                f"AUTO-CLOSE {trigger.upper()} | pos={pos_id} | "
                                f"pnl={result['pnl']}"
                            )
                            current_tags.discard(tag)
                except Exception as e:
                    log.warning(f"monitor: error checking pos {pos_id}: {e}")

            # ── 2. Standalone triggers table ────────────────────────────────
            now_mono = time.monotonic()
            for trig, tag, fk in trig_items:
                trig_id = trig["id"]
                trig_zw = trig.get("zscore_window") or _MONITOR_ZSCORE_WINDOW
                if fk not in ohlcv_data:
                    continue

                sym1, sym2 = trig["symbol1"], trig["symbol2"]

                # Guard: if position TP/SL already started a close for this pair, skip
                if (sym1, sym2) in closing_pairs:
                    continue

                try:
                    p1, p2 = ohlcv_data[fk]

                    # Hedge ratio cache: recompute only if expired (60s TTL)
                    cached_hr = _hedge_cache.get(fk)
                    if cached_hr and (now_mono - cached_hr[1]) < _HEDGE_TTL:
                        hedge = cached_hr[0]
                    else:
                        hedge = strategy.calculate_hedge_ratio(p1, p2)
                        _hedge_cache[fk] = (hedge, now_mono)

                    spread = strategy.calculate_spread(p1, p2, hedge)
                    zscore_series = strategy.calculate_zscore(spread, window=trig_zw)
                    current_z = float(zscore_series.dropna().iloc[-1])

                    side = trig["side"]
                    trig_type = trig["type"]  # "tp" | "sl" | "alert"
                    trig_z = trig["zscore"]

                    # ── Alert trigger: notify-only, no position close ────────
                    if trig_type == "alert":
                        alert_pct = trig.get("alert_pct") or 1.0
                        tag_state = alert_states.get(tag, "idle")
                        if tag_state == "idle" and abs(current_z) >= alert_pct * abs(trig_z):
                            log.info(
                                f"ALERT | trig_id={trig_id} | {sym1}/{sym2} | "
                                f"z={current_z:.3f} threshold={trig_z}"
                            )
                            await tg_bot.notify_alert(sym1, sym2, current_z, trig_z)
                            db.alert_fired(trig_id)
                            alert_states[tag] = "alerted"
                        elif tag_state == "alerted" and abs(current_z) <= tg_bot.ALERT_RESET_Z:
                            alert_states[tag] = "idle"
                        continue  # never close position for alert type

                    # Direction-agnostic: same as position triggers
                    abs_z = abs(current_z)
                    fired = False
                    if trig_type == "tp" and abs_z <= trig_z:
                        fired = True
                    elif trig_type == "sl" and abs_z >= trig_z:
                        fired = True

                    if fired:
                        # Guard: if position TP/SL already started a close for this pair, skip
                        if (sym1, sym2) in closing_pairs:
                            continue
                        closing_pairs.add((sym1, sym2))
                        log.info(
                            f"STANDALONE TRIGGER FIRED | trig_id={trig_id} | "
                            f"{sym1}/{sym2} | {side} | {trig_type} z={trig_z} | "
                            f"current_z={current_z:.3f}"
                        )
                        await tg_bot.notify_trigger_fired(
                            sym1, sym2, side, trig_type, current_z, trig_z,
                        )
                        live_positions = await client.get_positions()
                        live_syms = {p["symbol"] for p in live_positions if p.get("size")}
                        if sym1 not in live_syms and sym2 not in live_syms:
                            log.warning(
                                f"STANDALONE TRIGGER | trig_id={trig_id} | "
                                f"no live positions found — cancelling trigger."
                            )
                            db.trigger_fired(trig_id)
                            current_tags.discard(tag)
                            continue

                        db_pos = db.find_open_position(sym1, sym2)
                        if db_pos:
                            smart_key = "tp_smart" if trig_type == "tp" else "sl_smart"
                            use_smart = bool(trig.get(smart_key, True))
                            if use_smart:
                                exec_id = await _do_smart_close_trigger(db_pos, exit_zscore=current_z)
                                closing_tags.add(tag)
                                log.info(
                                    f"AUTO-SMART-CLOSE via trigger | trig_id={trig_id} | "
                                    f"exec_id={exec_id}"
                                )
                            else:
                                result = await _do_market_close(db_pos, exit_zscore=current_z, reason=trig_type)
                                log.info(
                                    f"AUTO-CLOSE via trigger | trig_id={trig_id} | "
                                    f"pnl={result['pnl']}"
                                )
                        else:
                            log.warning(
                                f"STANDALONE TRIGGER | trig_id={trig_id} | "
                                f"no DB position found for {sym1}/{sym2} — marking as fired."
                            )

                        db.trigger_fired(trig_id)
                        smart_key2 = "tp_smart" if trig_type == "tp" else "sl_smart"
                        if not (db_pos and bool(trig.get(smart_key2, True))):
                            current_tags.discard(tag)
                except Exception as e:
                    log.warning(f"monitor: error checking trigger {trig_id}: {e}")

            # Clean up stale hedge cache entries
            _hedge_cache_keys = list(_hedge_cache.keys())
            active_fks = set(fk for _, _, fk in trig_items)
            for hk in _hedge_cache_keys:
                if hk not in active_fks:
                    _hedge_cache.pop(hk, None)

            # ── Cleanup ─────────────────────────────────────────────────────
            closing_tags &= current_tags
            # closing_pairs is rebuilt each cycle (local to the loop body),
            # so it auto-resets. But we also keep pairs with active smart closes:
            active_syms = set()
            for t in closing_tags:
                # Extract sym pair from pos_items or trig_items that are still closing
                for pos, ptag, *_ in pos_items:
                    if ptag == t:
                        active_syms.add((pos["symbol1"], pos["symbol2"]))
                for trig, ttag, *_ in trig_items:
                    if ttag == t:
                        active_syms.add((trig["symbol1"], trig["symbol2"]))
            closing_pairs = active_syms
            alert_states = {k: v for k, v in alert_states.items() if k in current_tags}

        except Exception as e:
            log.warning(f"monitor: outer error: {e}")

        # Persist terminal executions to execution_history (idempotent, runs once per exec)
        for eid in list(active_executions.keys()):
            ctx = active_executions[eid]
            if ctx.status.name in ("DONE", "CANCELLED", "FAILED", "OPEN") and eid not in _exec_saved_to_db:
                try:
                    d = ctx.to_dict()
                    db.save_execution_history(
                        exec_id=eid,
                        db_id=d.get("db_id"),
                        close_db_id=d.get("close_db_id"),
                        is_close=bool(d.get("is_close", False)),
                        status=str(d["status"]),
                        symbol1=ctx.leg1.symbol,
                        symbol2=ctx.leg2.symbol,
                        data_json=json.dumps(d),
                    )
                    _exec_saved_to_db.add(eid)
                except Exception as _e:
                    log.warning(f"exec_history save failed {eid}: {_e}")

        # Clean up terminal executions older than TTL
        now = time.monotonic()
        for eid in list(active_executions.keys()):
            ctx = active_executions[eid]
            created = _exec_created_at.get(eid, now)
            if ctx.status.name in ("DONE", "CANCELLED", "FAILED", "OPEN") and (now - created) > _EXEC_TTL:
                active_executions.pop(eid, None)
                _exec_created_at.pop(eid, None)
                _exec_saved_to_db.discard(eid)

        await asyncio.sleep(2)


async def lifespan(app: FastAPI):
    db.init_db()
    await tg_bot.setup()
    _bg_tasks = [
        asyncio.create_task(price_cache.run()),
        asyncio.create_task(monitor_position_triggers()),
        asyncio.create_task(tg_bot.start_polling()),
    ]
    log.info("Pair Trading backend started")
    yield
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    await client.close()
    await tg_bot.stop()
    log.info("Pair Trading backend stopped")


app = FastAPI(title="Pair Trading API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://localhost:3000",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:3000",
        "null",        # file:// origin
        "*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/symbols")
async def get_symbols():
    """Return list of available USDT-M and USDC-M perpetual futures symbols."""
    try:
        markets = await client.get_available_futures_meta()
        return {
            "symbols": [item["id"] for item in markets],
            "markets": markets,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history")
async def get_history(
    symbol1: str = Query(...),
    symbol2: str = Query(...),
    timeframe: str = Query("1h"),
    limit: int = Query(500),
    zscore_window: int = Query(20),
):
    """Return OHLCV history, spread, z-score and pair statistics."""
    try:
        meta1, meta2 = await _resolve_pair(symbol1, symbol2)
        sym1 = meta1["symbol"]
        sym2 = meta2["symbol"]

        # Try PriceCache first — instant if pair is already tracked (watchlist/WS)
        cached = price_cache.find_cached(sym1, sym2, timeframe, limit)
        if cached and "df1" in cached and "df2" in cached:
            price1 = cached["price1"].iloc[-limit:]
            price2 = cached["price2"].iloc[-limit:]
            df1 = cached["df1"].iloc[-limit:]
            df2 = cached["df2"].iloc[-limit:]
        else:
            df1, df2 = await asyncio.gather(
                client.fetch_ohlcv(sym1, timeframe, limit),
                client.fetch_ohlcv(sym2, timeframe, limit),
            )

            price1 = df1["close"]
            price2 = df2["close"]

            # Align on common timestamps
            price1, price2 = price1.align(price2, join="inner")
            df1 = df1.loc[price1.index]
            df2 = df2.loc[price2.index]

        # CPU-bound stats — run in thread pool so the event loop stays responsive
        def _compute_stats():
            hr = strategy.calculate_hedge_ratio(price1, price2)
            sp = strategy.calculate_spread(price1, price2, hr)
            zs = strategy.calculate_zscore(sp, window=zscore_window)
            coint = strategy.cointegration_test(price1, price2)
            hl = strategy.calculate_half_life(sp)
            hu = strategy.calculate_hurst_exponent(sp)
            corr = strategy.calculate_correlation(price1, price2)
            a1 = strategy.calculate_atr(df1)
            a2 = strategy.calculate_atr(df2)
            return hr, sp, zs, coint, hl, hu, corr, a1, a2

        hedge_ratio, spread, zscore, coint_result, half_life, hurst, correlation, atr1, atr2 = \
            await _run_sync(_compute_stats)

        timestamps = [str(ts) for ts in price1.index]

        return _clean({
            "pair": _pair_meta(meta1, meta2),
            "timestamps": timestamps,
            "price1": price1.tolist(),
            "price2": price2.tolist(),
            "spread": spread.tolist(),
            "zscore": zscore.tolist(),
            "hedge_ratio": hedge_ratio,
            "stats": {
                "cointegration": coint_result,
                "half_life": half_life,
                "hurst_exponent": hurst,
                "correlation": correlation,
                "spread_mean": float(spread.mean()),
                "spread_std": float(spread.std()),
                "current_zscore": float(zscore.dropna().iloc[-1]) if not zscore.dropna().empty else None,
                "atr1": atr1,
                "atr2": atr2,
                "atr_ratio": atr1 / atr2 if atr2 else None,
            },
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class WatchlistItem(BaseModel):
    sym1: str
    sym2: str
    timeframe: str = "1h"
    limit: int = 100
    zscore_window: int = 20


@app.post("/api/watchlist/data")
async def get_watchlist_data(items: list[WatchlistItem]):
    """
    Subscribe watchlist pairs to PriceCache and return current z-score + spread.
    Reconciles subscriptions: removes pairs that are no longer in the watchlist.
    """
    results = []
    new_tags: set[str] = set()

    # Phase 1: subscribe and collect cached data (or seed cache)
    cached_items = []  # list of (item, cached_data) or None for failed items
    for item in items:
        sym1 = _normalise_symbol(item.sym1)
        sym2 = _normalise_symbol(item.sym2)
        tag = f"{sym1}|{sym2}|{item.timeframe}|{item.limit}"
        new_tags.add(tag)

        # Subscribe to PriceCache if not already tracked
        if tag not in _watchlist_keys:
            key = price_cache.subscribe(sym1, sym2, item.timeframe, item.limit)
            _watchlist_keys[tag] = key
        else:
            key = _watchlist_keys[tag]

        cached = price_cache.get(key)
        if cached is None:
            # Cache not populated yet — fetch directly and seed the cache
            try:
                df1, df2 = await asyncio.gather(
                    client.fetch_ohlcv(sym1, item.timeframe, item.limit),
                    client.fetch_ohlcv(sym2, item.timeframe, item.limit),
                )
                p1 = df1["close"]
                p2 = df2["close"]
                p1, p2 = p1.align(p2, join="inner")
                df1_a = df1.loc[p1.index]
                df2_a = df2.loc[p2.index]
                price_cache._store[key] = {"price1": p1, "price2": p2, "df1": df1_a, "df2": df2_a}
                cached = price_cache.get(key)
            except Exception as e:
                log.warning(f"watchlist fetch error {tag}: {e}")
                results.append({"sym1": item.sym1, "sym2": item.sym2,
                                 "timeframe": item.timeframe,
                                 "current_zscore": None, "spread": None})
                cached_items.append(None)
                continue

        cached_items.append((item, cached))

    # Phase 2: compute all z-scores in a single thread-pool call
    compute_indices = []  # indices into cached_items that need computation
    compute_args = []     # (p1, p2, zw) tuples
    for ci_idx, entry in enumerate(cached_items):
        if entry is None:
            continue
        item, cached = entry
        compute_indices.append(ci_idx)
        compute_args.append((cached["price1"], cached["price2"], item.zscore_window))

    def _batch_calc():
        batch_results = []
        for p1, p2, zw in compute_args:
            h = strategy.calculate_hedge_ratio(p1, p2)
            sp = strategy.calculate_spread(p1, p2, h)
            zs = strategy.calculate_zscore(sp, window=zw)
            zd = zs.dropna()
            cz = float(zd.iloc[-1]) if not zd.empty else None
            ls = float(sp.iloc[-1]) if not sp.empty else None
            batch_results.append((cz, ls))
        return batch_results

    if compute_args:
        batch = await _run_sync(_batch_calc)
    else:
        batch = []

    # Phase 3: assemble results
    batch_idx = 0
    for ci_idx, entry in enumerate(cached_items):
        if entry is None:
            continue
        item, _ = entry
        cz, ls = batch[batch_idx]
        batch_idx += 1
        results.append({
            "sym1": item.sym1,
            "sym2": item.sym2,
            "timeframe": item.timeframe,
            "current_zscore": _safe_float(cz),
            "spread": _safe_float(ls),
        })

    # Unsubscribe pairs removed from watchlist
    for tag in set(_watchlist_keys) - new_tags:
        price_cache.unsubscribe(_watchlist_keys.pop(tag))

    return results


class SparklineRequest(BaseModel):
    sym1: str
    sym2: str
    timeframe: str = "1h"
    limit: int = 100
    zscore_window: int = 20


@app.post("/api/batch/sparklines")
async def batch_sparklines(items: list[SparklineRequest]):
    """
    Batch sparkline data for multiple positions in a single request.
    Uses PriceCache when available; fetches from Binance otherwise.
    Returns z-score array, spread array, hedge_ratio, and timestamps per item.
    """
    results = []

    # Phase 1: resolve symbols and check cache
    resolved = []   # (item, sym1, sym2)
    to_fetch = []   # indices into resolved that need Binance fetch
    for item in items:
        sym1 = _normalise_symbol(item.sym1)
        sym2 = _normalise_symbol(item.sym2)
        resolved.append((item, sym1, sym2))

    # Phase 2: get data — from cache or Binance
    data_entries = []  # (price1, price2, df1, df2) per item
    fetch_tasks = {}   # idx → asyncio task
    for idx, (item, sym1, sym2) in enumerate(resolved):
        cached = price_cache.find_cached(sym1, sym2, item.timeframe, item.limit)
        if cached and "df1" in cached and "df2" in cached:
            p1 = cached["price1"].iloc[-item.limit:]
            p2 = cached["price2"].iloc[-item.limit:]
            d1 = cached["df1"].iloc[-item.limit:]
            d2 = cached["df2"].iloc[-item.limit:]
            data_entries.append((p1, p2, d1, d2))
        else:
            data_entries.append(None)
            to_fetch.append(idx)

    if to_fetch:
        async def _fetch_one(idx):
            item, sym1, sym2 = resolved[idx]
            df1, df2 = await asyncio.gather(
                client.fetch_ohlcv(sym1, item.timeframe, item.limit),
                client.fetch_ohlcv(sym2, item.timeframe, item.limit),
            )
            p1, p2 = df1["close"], df2["close"]
            p1, p2 = p1.align(p2, join="inner")
            return idx, p1, p2, df1.loc[p1.index], df2.loc[p2.index]

        fetch_results = await asyncio.gather(
            *[_fetch_one(i) for i in to_fetch],
            return_exceptions=True,
        )
        for r in fetch_results:
            if isinstance(r, Exception):
                log.warning(f"batch sparkline fetch error: {r}")
                continue
            idx, p1, p2, d1, d2 = r
            data_entries[idx] = (p1, p2, d1, d2)

    # Phase 3: compute stats in single thread-pool call
    compute_args = []  # (idx, p1, p2, zw)
    for idx, entry in enumerate(data_entries):
        if entry is None:
            continue
        p1, p2, _, _ = entry
        compute_args.append((idx, p1, p2, resolved[idx][0].zscore_window))

    def _batch_compute():
        out = {}
        for idx, p1, p2, zw in compute_args:
            hr = strategy.calculate_hedge_ratio(p1, p2)
            sp = strategy.calculate_spread(p1, p2, hr)
            zs = strategy.calculate_zscore(sp, window=zw)
            out[idx] = (hr, sp.tolist(), zs.tolist(), [str(t) for t in p1.index])
        return out

    computed = await _run_sync(_batch_compute) if compute_args else {}

    # Phase 4: assemble results
    for idx, (item, sym1, sym2) in enumerate(resolved):
        if idx in computed:
            hr, sp_list, zs_list, ts_list = computed[idx]
            results.append(_clean({
                "sym1": item.sym1, "sym2": item.sym2,
                "timeframe": item.timeframe,
                "zscore": zs_list, "spread": sp_list,
                "hedge_ratio": hr, "timestamps": ts_list,
            }))
        else:
            results.append({
                "sym1": item.sym1, "sym2": item.sym2,
                "timeframe": item.timeframe,
                "zscore": [], "spread": [],
                "hedge_ratio": None, "timestamps": [],
            })

    return results


@app.get("/api/backtest")
async def run_backtest(
    symbol1: str = Query(...),
    symbol2: str = Query(...),
    timeframe: str = Query("1h"),
    limit: int = Query(500),
    entry_threshold: float = Query(2.0),
    exit_threshold: float = Query(0.5),
    position_size_usd: float = Query(1000.0),
    zscore_window: int = Query(20),
):
    """Run a backtest and return results."""
    try:
        meta1, meta2 = await _resolve_pair(symbol1, symbol2)
        sym1 = meta1["symbol"]
        sym2 = meta2["symbol"]

        df1, df2 = await asyncio.gather(
            client.fetch_ohlcv(sym1, timeframe, limit),
            client.fetch_ohlcv(sym2, timeframe, limit),
        )

        price1 = df1["close"]
        price2 = df2["close"]
        price1, price2 = price1.align(price2, join="inner")

        hedge_ratio = strategy.calculate_hedge_ratio(price1, price2)

        result = strategy.calculate_backtest(
            price1, price2,
            hedge_ratio=hedge_ratio,
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold,
            position_size_usd=position_size_usd,
            zscore_window=zscore_window,
        )
        return _clean(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/status")
async def get_status():
    """Check Binance API connection status."""
    if not client.has_creds:
        return {"connected": False, "reason": "no_keys"}
    try:
        balances = await client.get_all_balances()
        return {"connected": True, "balances": _clean(balances)}
    except Exception as e:
        return {"connected": False, "reason": "auth_error", "message": str(e)}


@app.get("/api/positions")
async def get_positions():
    """Return open Binance futures positions."""
    try:
        positions = await client.get_positions()
        return {"positions": _clean(positions)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/all_positions")
async def get_all_positions():
    """
    Single endpoint: fetch exchange positions once, return both
    strategy DB positions (enriched with live data) and raw exchange positions.
    Avoids double get_positions() calls from the frontend.
    """
    db_positions = db.get_open_positions()

    try:
        live_positions = await client.get_positions()
        live_map = {p["symbol"]: p for p in live_positions}
    except Exception:
        live_positions = []
        live_map = {}

    enriched = []
    for pos in db_positions:
        sym1, sym2 = pos["symbol1"], pos["symbol2"]
        live1 = live_map.get(sym1, {})
        live2 = live_map.get(sym2, {})
        mark_price1 = live1.get("mark_price") or pos.get("entry_price1")
        mark_price2 = live2.get("mark_price") or pos.get("entry_price2")
        pnl = None
        if (mark_price1 and pos.get("entry_price1")
                and mark_price2 and pos.get("entry_price2")):
            sign = 1 if pos["side"] == "long_spread" else -1
            p1 = pos["qty1"] * (mark_price1 - pos["entry_price1"]) * sign
            p2 = pos["qty2"] * (pos["entry_price2"] - mark_price2) * sign
            pnl = round(p1 + p2, 4)
        enriched.append({
            **pos,
            "mark_price1": mark_price1,
            "mark_price2": mark_price2,
            "unrealized_pnl": pnl,
            "liq_price1": live1.get("liquidation_price"),
            "liq_price2": live2.get("liquidation_price"),
        })

    return _clean({
        "strategy_positions": enriched,
        "exchange_positions": live_positions,
    })


@app.get("/api/balance")
async def get_balance(asset: Optional[str] = Query(None)):
    """Return futures balance for a specific asset or all supported assets."""
    try:
        if asset:
            balance = await client.get_balance(asset)
            return _clean(balance)
        balances = await client.get_all_balances()
        return _clean({"assets": balances})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dashboard")
async def get_dashboard(alert_minutes: int = Query(60)):
    """
    Combined endpoint: positions (enriched) + balances + recent alerts.
    Replaces three separate polling calls with one round-trip.
    """
    db_positions = db.get_open_positions()

    # Fetch live data + balances + alerts in parallel
    try:
        live_positions, balances = await asyncio.gather(
            client.get_positions(),
            client.get_all_balances(),
        )
        live_map = {p["symbol"]: p for p in live_positions}
    except Exception:
        live_positions = []
        live_map = {}
        balances = []

    # Enrich strategy positions with live data
    enriched = []
    for pos in db_positions:
        sym1, sym2 = pos["symbol1"], pos["symbol2"]
        live1 = live_map.get(sym1, {})
        live2 = live_map.get(sym2, {})
        mark_price1 = live1.get("mark_price") or pos.get("entry_price1")
        mark_price2 = live2.get("mark_price") or pos.get("entry_price2")
        pnl = None
        if (mark_price1 and pos.get("entry_price1")
                and mark_price2 and pos.get("entry_price2")):
            sign = 1 if pos["side"] == "long_spread" else -1
            p1 = pos["qty1"] * (mark_price1 - pos["entry_price1"]) * sign
            p2 = pos["qty2"] * (pos["entry_price2"] - mark_price2) * sign
            pnl = round(p1 + p2, 4)
        enriched.append({
            **pos,
            "mark_price1": mark_price1,
            "mark_price2": mark_price2,
            "unrealized_pnl": pnl,
            "liq_price1": live1.get("liquidation_price"),
            "liq_price2": live2.get("liquidation_price"),
        })

    # Recent alerts — pure SQLite, no network call
    recent_alerts = db.get_recent_alerts(alert_minutes)

    return _clean({
        "strategy_positions": enriched,
        "exchange_positions": live_positions,
        "balances": balances,
        "recent_alerts": recent_alerts,
    })


@app.get("/api/db/positions")
async def get_db_positions():
    """Return open positions saved by the strategy (with entry context)."""
    return {"positions": _clean(db.get_open_positions())}


@app.get("/api/db/positions/enriched")
async def get_db_positions_enriched():
    """
    Return open DB positions enriched with live Binance mark prices and
    unrealised PnL calculated from entry prices.
    """
    db_positions = db.get_open_positions()
    if not db_positions:
        return {"positions": []}

    try:
        live_positions = await client.get_positions()
        live_map = {p["symbol"]: p for p in live_positions}
    except Exception:
        live_map = {}

    enriched = []
    for pos in db_positions:
        sym1, sym2 = pos["symbol1"], pos["symbol2"]
        live1 = live_map.get(sym1, {})
        live2 = live_map.get(sym2, {})

        mark_price1 = live1.get("mark_price") or pos.get("entry_price1")
        mark_price2 = live2.get("mark_price") or pos.get("entry_price2")

        pnl = None
        if (mark_price1 and pos.get("entry_price1")
                and mark_price2 and pos.get("entry_price2")):
            sign = 1 if pos["side"] == "long_spread" else -1
            p1 = pos["qty1"] * (mark_price1 - pos["entry_price1"]) * sign
            p2 = pos["qty2"] * (pos["entry_price2"] - mark_price2) * sign
            pnl = round(p1 + p2, 4)

        enriched.append({
            **pos,
            "mark_price1": mark_price1,
            "mark_price2": mark_price2,
            "unrealized_pnl": pnl,
            "liq_price1": live1.get("liquidation_price"),
            "liq_price2": live2.get("liquidation_price"),
        })

    return {"positions": _clean(enriched)}


@app.delete("/api/db/positions/{position_id}")
async def delete_db_position(position_id: int):
    """Delete an open position DB record (does NOT close exchange positions)."""
    deleted = db.delete_open_position(position_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
    log.warning(f"DB position {position_id} deleted manually (no exchange action)")
    return {"deleted": True, "id": position_id}


class TriggerRequest(BaseModel):
    tp_zscore: Optional[float] = None
    sl_zscore: Optional[float] = None
    tp_smart: bool = True
    sl_smart: bool = True


@app.post("/api/db/positions/{position_id}/triggers")
async def set_triggers(position_id: int, req: TriggerRequest):
    """Set TP/SL z-score triggers for an open position."""
    ok = db.set_position_triggers(
        position_id, req.tp_zscore, req.sl_zscore, req.tp_smart, req.sl_smart
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
    log.info(
        f"Triggers set for position {position_id}: "
        f"TP={req.tp_zscore} (smart={req.tp_smart}) SL={req.sl_zscore} (smart={req.sl_smart})"
    )
    return {
        "ok": True, "id": position_id,
        "tp_zscore": req.tp_zscore, "sl_zscore": req.sl_zscore,
        "tp_smart": req.tp_smart, "sl_smart": req.sl_smart,
    }


# ---------------------------------------------------------------------------
# Standalone trigger endpoints (new triggers table)
# ---------------------------------------------------------------------------

class TriggerCreateRequest(BaseModel):
    symbol1: str
    symbol2: str
    side: str           # long_spread | short_spread
    type: str           # tp | sl | alert
    zscore: float
    tp_smart: bool = True
    sl_smart: bool = True
    timeframe: str = "1h"
    zscore_window: int = 20
    alert_pct: float = 1.0   # fraction of zscore at which alert fires (1.0 = 100%)


@app.get("/api/triggers")
async def get_triggers():
    """Return all active triggers."""
    return {"triggers": _clean(db.get_active_triggers())}


@app.get("/api/alerts/recent")
async def get_recent_alerts(minutes: int = Query(60)):
    """Return active alert triggers that fired within the last N minutes."""
    return {"alerts": _clean(db.get_recent_alerts(minutes))}


@app.post("/api/triggers")
async def create_trigger(req: TriggerCreateRequest):
    """Create a new TP/SL/alert trigger. For alerts: replace duplicate (same sym+zscore)."""
    sym1 = _normalise_symbol(req.symbol1)
    sym2 = _normalise_symbol(req.symbol2)

    # For alert type: cancel existing alert with same (sym1, sym2, zscore) before creating
    if req.type == "alert":
        existing = db.find_active_alert(sym1, sym2, req.zscore)
        if existing:
            db.cancel_trigger(existing["id"])
            log.info(f"Alert replaced: cancelled old id={existing['id']} for {sym1}/{sym2} z={req.zscore}")

    trigger_id = db.save_trigger(
        symbol1=sym1,
        symbol2=sym2,
        side=req.side,
        type=req.type,
        zscore=req.zscore,
        tp_smart=req.tp_smart,
        sl_smart=req.sl_smart,
        timeframe=req.timeframe,
        zscore_window=req.zscore_window,
        alert_pct=req.alert_pct,
    )
    log.info(
        f"Trigger created: id={trigger_id} | {sym1}/{sym2} | "
        f"{req.side} | {req.type} z={req.zscore} pct={req.alert_pct} "
        f"tf={req.timeframe} w={req.zscore_window}"
    )
    return {"id": trigger_id, "ok": True}


@app.delete("/api/triggers/{trigger_id}")
async def delete_trigger(trigger_id: int):
    """Cancel an active trigger."""
    ok = db.cancel_trigger(trigger_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Active trigger {trigger_id} not found")
    log.info(f"Trigger {trigger_id} cancelled")
    return {"ok": True}


@app.get("/api/db/history")
async def get_db_history(limit: int = Query(100)):
    """Return closed trade history."""
    return {"trades": _clean(db.get_closed_trades(limit))}


class TradeRequest(BaseModel):
    symbol1: str
    symbol2: str
    action: str                       # "open" | "close"
    side: str                         # "long_spread" | "short_spread"
    size_usd: float
    hedge_ratio: float
    sizing_method: str = "ols"        # "ols" | "atr" | "equal"
    atr1: Optional[float] = None
    atr2: Optional[float] = None
    leverage: int = 1                 # futures leverage to set before trade
    entry_zscore: Optional[float] = None
    exit_zscore: Optional[float] = None
    timeframe: str = "1h"
    candle_limit: int = 500
    zscore_window: int = 20


class SmartTradeRequest(BaseModel):
    symbol1: str
    symbol2: str
    action: str = "open"              # "open" | "close"
    side: str                         # "long_spread" | "short_spread"
    size_usd: float
    hedge_ratio: float
    sizing_method: str = "ols"
    atr1: Optional[float] = None
    atr2: Optional[float] = None
    leverage: int = 1
    entry_zscore: Optional[float] = None
    exit_zscore: Optional[float] = None
    timeframe: str = "1h"
    candle_limit: int = 500
    zscore_window: int = 20
    # Execution parameters
    passive_s: float = 30.0
    aggressive_s: float = 20.0
    allow_market: bool = True


@app.get("/api/pre_trade_check")
async def pre_trade_check(
    symbol1: str = Query(...),
    symbol2: str = Query(...),
    size_usd: float = Query(...),
    hedge_ratio: float = Query(...),
    sizing_method: str = Query("ols"),
    atr1: Optional[float] = Query(None),
    atr2: Optional[float] = Query(None),
    leverage: int = Query(1),
):
    """
    Validate trade requirements before execution.
    Returns checks: balance, min_notional, lot_size, leverage.
    """
    try:
        meta1, meta2 = await _resolve_pair(symbol1, symbol2)
        sym1 = meta1["symbol"]
        sym2 = meta2["symbol"]
        pair = _pair_meta(meta1, meta2)
        margin_asset = _shared_margin_asset(meta1, meta2)

        ticker1, ticker2 = await asyncio.gather(
            client.fetch_ticker(sym1),
            client.fetch_ticker(sym2),
        )
        price1 = ticker1["last"]
        price2 = ticker2["last"]

        sizes = strategy.calculate_position_sizes(
            price1=price1,
            price2=price2,
            size_usd=size_usd,
            hedge_ratio=hedge_ratio,
            atr1=atr1,
            atr2=atr2,
            method=sizing_method,
        )
        qty1 = sizes["qty1"]
        qty2 = sizes["qty2"]

        checks = []

        checks.append({
            "name": "margin_asset",
            "ok": bool(margin_asset),
            "detail": (
                f"Shared margin asset: {margin_asset}"
                if margin_asset
                else (
                    "Trading requires both legs to use the same margin asset. "
                    f"{meta1['id']} uses {meta1.get('margin_asset')}, "
                    f"{meta2['id']} uses {meta2.get('margin_asset')}."
                )
            ),
        })

        # Balance check — required margin ≈ size_usd / leverage (+10% buffer)
        if client.has_creds and margin_asset:
            try:
                balance = await client.get_balance(margin_asset)
                free = balance.get("free", 0)
                required_margin = round(size_usd / leverage * 1.1, 2)
                checks.append({
                    "name": "balance",
                    "ok": free >= required_margin,
                    "detail": (
                        f"Free {margin_asset}: {free:.2f}, "
                        f"required ~{required_margin:.2f} ({margin_asset} at {leverage}x leverage)"
                    ),
                })
            except Exception as e:
                checks.append({"name": "balance", "ok": False, "detail": str(e)})
        elif margin_asset:
            checks.append({
                "name": "balance",
                "ok": None,
                "detail": f"No API keys — cannot check {margin_asset} balance",
            })
        else:
            checks.append({
                "name": "balance",
                "ok": False,
                "detail": "Balance check skipped because the pair mixes different margin assets",
            })

        # Min notional checks
        ok1, notional1, min1 = await client.check_min_notional(sym1, qty1, price1)
        ok2, notional2, min2 = await client.check_min_notional(sym2, qty2, price2)
        checks.append({
            "name": f"min_notional_{sym1}",
            "ok": ok1,
            "detail": f"Notional: ${notional1:.2f}, min required: ${min1:.2f}",
        })
        checks.append({
            "name": f"min_notional_{sym2}",
            "ok": ok2,
            "detail": f"Notional: ${notional2:.2f}, min required: ${min2:.2f}",
        })

        # Lot size info
        rounded_qty1 = await client.round_amount(sym1, qty1)
        rounded_qty2 = await client.round_amount(sym2, qty2)
        checks.append({
            "name": "lot_size",
            "ok": True,
            "detail": (
                f"{sym1}: {qty1:.8f} → {rounded_qty1}  |  "
                f"{sym2}: {qty2:.8f} → {rounded_qty2}"
            ),
        })

        # Leverage
        checks.append({
            "name": "leverage",
            "ok": 1 <= leverage <= 20,
            "detail": f"{leverage}x (max safe: 20x)",
        })

        all_ok = all(c["ok"] for c in checks if c["ok"] is not None)

        return _clean({
            "ok": all_ok,
            "pair": pair,
            "margin_asset": margin_asset,
            "checks": checks,
            "sizes": {
                "qty1": qty1,
                "qty2": qty2,
                "rounded_qty1": rounded_qty1,
                "rounded_qty2": rounded_qty2,
                "notional1": notional1,
                "notional2": notional2,
            },
            "prices": {"price1": price1, "price2": price2},
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trade/smart")
async def start_smart_trade(req: SmartTradeRequest):
    """
    Start a smart limit-order execution in the background.
    Returns exec_id for polling via GET /api/execution/{exec_id}.
    """
    try:
        meta1, meta2 = await _resolve_pair(req.symbol1, req.symbol2)
        _require_tradeable_pair(meta1, meta2)
        sym1 = meta1["symbol"]
        sym2 = meta2["symbol"]

        import uuid
        exec_id = str(uuid.uuid4())[:8]

        cfg = ExecConfig(
            passive_s=req.passive_s,
            aggressive_s=req.aggressive_s,
            allow_market=req.allow_market,
        )

        if req.action == "close":
            # ── Close existing position ────────────────────────────────────
            db_pos = db.find_open_position(sym1, sym2)
            if not db_pos:
                raise HTTPException(404, f"No open DB position found for {sym1}/{sym2}")

            # Use actual exchange qty; fall back to DB qty
            try:
                live_positions = await client.get_positions()
                pos_map = {p["symbol"]: p for p in live_positions}
            except Exception:
                pos_map = {}
            p1 = pos_map.get(sym1)
            p2 = pos_map.get(sym2)
            close_qty1 = abs(p1["size"]) if p1 else db_pos["qty1"]
            close_qty2 = abs(p2["size"]) if p2 else db_pos["qty2"]

            # Reverse the spread direction
            if db_pos["side"] == "long_spread":
                side1, side2 = "sell", "buy"
            else:
                side1, side2 = "buy", "sell"

            ctx = ExecContext(
                exec_id=exec_id,
                leg1=LegState(symbol=sym1, side=side1, qty=close_qty1),
                leg2=LegState(symbol=sym2, side=side2, qty=close_qty2),
                config=cfg,
                spread_side=db_pos["side"],
                is_close=True,
                close_db_id=db_pos["id"],
                entry_price1=db_pos.get("entry_price1"),
                entry_price2=db_pos.get("entry_price2"),
                exit_zscore=req.exit_zscore,
                size_usd=db_pos.get("size_usd"),
                sizing_method=db_pos.get("sizing_method"),
                leverage=db_pos.get("leverage") or 1,
            )
            log.info(f"Smart close started: {exec_id} | {sym1}/{sym2} | {db_pos['side']}")

        else:
            # ── Open new position ──────────────────────────────────────────
            ticker1, ticker2 = await asyncio.gather(
                client.fetch_ticker(sym1),
                client.fetch_ticker(sym2),
            )
            price1 = ticker1["last"]
            price2 = ticker2["last"]

            sizes = strategy.calculate_position_sizes(
                price1=price1,
                price2=price2,
                size_usd=req.size_usd,
                hedge_ratio=req.hedge_ratio,
                atr1=req.atr1,
                atr2=req.atr2,
                method=req.sizing_method,
            )
            qty1 = sizes["qty1"]
            qty2 = sizes["qty2"]

            # Validate min notional (parallel)
            (ok1, notional1, min1), (ok2, notional2, min2) = await asyncio.gather(
                client.check_min_notional(sym1, qty1, price1),
                client.check_min_notional(sym2, qty2, price2),
            )
            if not ok1:
                raise HTTPException(400, f"{sym1}: notional ${notional1:.2f} < min ${min1:.2f}")
            if not ok2:
                raise HTTPException(400, f"{sym2}: notional ${notional2:.2f} < min ${min2:.2f}")

            # Set leverage (parallel, best-effort)
            async def _set_lev(sym):
                try:
                    await client.set_leverage(sym, req.leverage)
                except Exception as lev_err:
                    log.warning(f"Could not set leverage for {sym}: {lev_err}")
            await asyncio.gather(_set_lev(sym1), _set_lev(sym2))

            side1, side2 = ("buy", "sell") if req.side == "long_spread" else ("sell", "buy")

            ctx = ExecContext(
                exec_id=exec_id,
                leg1=LegState(symbol=sym1, side=side1, qty=qty1),
                leg2=LegState(symbol=sym2, side=side2, qty=qty2),
                config=cfg,
                spread_side=req.side,
                hedge_ratio=req.hedge_ratio,
                entry_zscore=req.entry_zscore,
                size_usd=req.size_usd,
                sizing_method=req.sizing_method,
                leverage=req.leverage,
                timeframe=req.timeframe,
                candle_limit=req.candle_limit,
                zscore_window=req.zscore_window,
            )
            log.info(f"Smart execution started: {exec_id} | {sym1}/{sym2} | {req.side}")

        active_executions[exec_id] = ctx
        _exec_created_at[exec_id] = time.monotonic()
        asyncio.create_task(run_execution(ctx, client, db))

        return {"exec_id": exec_id, "status": "started", "action": req.action}

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Smart trade error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/executions")
async def list_executions():
    """Return all active execution contexts (for progress monitoring)."""
    return {"executions": [_clean(ctx.to_dict()) for ctx in active_executions.values()]}


@app.get("/api/executions/history")
async def get_executions_history(limit: int = Query(50, le=200)):
    """Return persisted terminal executions with full event log, newest first."""
    rows = db.get_execution_history(limit=limit)
    result = []
    for row in rows:
        try:
            data = json.loads(row["data_json"])
        except Exception:
            data = {}
        result.append({
            "exec_id":      row["exec_id"],
            "db_id":        row["db_id"],
            "close_db_id":  row["close_db_id"],
            "is_close":     bool(row["is_close"]),
            "status":       row["status"],
            "symbol1":      row["symbol1"],
            "symbol2":      row["symbol2"],
            "completed_at": row["completed_at"],
            **_clean(data),
        })
    return {"history": result}


@app.get("/api/execution/{exec_id}")
async def get_execution(exec_id: str):
    """Poll execution status."""
    ctx = active_executions.get(exec_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Execution not found")
    return _clean(ctx.to_dict())


@app.delete("/api/execution/{exec_id}")
async def cancel_execution(exec_id: str):
    """Request cancellation of a running execution."""
    ctx = active_executions.get(exec_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Execution not found")
    ctx.cancel_req = True
    return {"exec_id": exec_id, "cancel_requested": True}


@app.post("/api/trade")
async def execute_trade(req: TradeRequest):
    """
    Execute a pair trade.
    long_spread  -> buy symbol1, sell symbol2
    short_spread -> sell symbol1, buy symbol2
    action=close -> flatten both legs
    """
    try:
        meta1, meta2 = await _resolve_pair(req.symbol1, req.symbol2)
        _require_tradeable_pair(meta1, meta2)
        sym1 = meta1["symbol"]
        sym2 = meta2["symbol"]

        ticker1, ticker2 = await asyncio.gather(
            client.fetch_ticker(sym1),
            client.fetch_ticker(sym2),
        )
        price1 = ticker1["last"]
        price2 = ticker2["last"]

        sizes = strategy.calculate_position_sizes(
            price1=price1,
            price2=price2,
            size_usd=req.size_usd,
            hedge_ratio=req.hedge_ratio,
            atr1=req.atr1,
            atr2=req.atr2,
            method=req.sizing_method,
        )
        qty1 = sizes["qty1"]
        qty2 = sizes["qty2"]

        if req.action == "open":
            # --- 1. Validate minimum notional FIRST (before touching exchange state) ---
            ok1, notional1, min1 = await client.check_min_notional(sym1, qty1, price1)
            ok2, notional2, min2 = await client.check_min_notional(sym2, qty2, price2)
            if not ok1:
                raise HTTPException(
                    status_code=400,
                    detail=f"{sym1}: notional ${notional1:.2f} is below exchange minimum ${min1:.2f}",
                )
            if not ok2:
                raise HTTPException(
                    status_code=400,
                    detail=f"{sym2}: notional ${notional2:.2f} is below exchange minimum ${min2:.2f}",
                )

            # --- 2. Set leverage (best-effort; may fail if position already exists) ---
            for sym in (sym1, sym2):
                try:
                    await client.set_leverage(sym, req.leverage)
                    log.info(f"Leverage set to {req.leverage}x for {sym}")
                except Exception as lev_err:
                    log.warning(f"Could not set leverage for {sym}: {lev_err}")

            side1, side2 = ("buy", "sell") if req.side == "long_spread" else ("sell", "buy")

            # --- 3. Place orders (qty rounded inside place_order) ---
            order1, order2 = await asyncio.gather(
                client.place_order(sym1, side1, qty1),
                client.place_order(sym2, side2, qty2),
            )

            # --- 4. Persist to DB — use actual rounded qty from order response ---
            actual_qty1 = float(order1.get("amount") or qty1)
            actual_qty2 = float(order2.get("amount") or qty2)
            pos_id = db.save_open_position(
                symbol1=sym1,
                symbol2=sym2,
                side=req.side,
                qty1=actual_qty1,
                qty2=actual_qty2,
                hedge_ratio=req.hedge_ratio,
                entry_zscore=req.entry_zscore,
                entry_price1=price1,
                entry_price2=price2,
                size_usd=req.size_usd,
                sizing_method=req.sizing_method,
                leverage=req.leverage,
                timeframe=req.timeframe,
                candle_limit=req.candle_limit,
                zscore_window=req.zscore_window,
            )
            log.info(
                f"OPEN {req.side} | {sym1}/{sym2} | "
                f"qty1={qty1:.6f} qty2={qty2:.6f} | "
                f"price1={price1} price2={price2} | "
                f"z={req.entry_zscore} | lev={req.leverage}x | "
                f"sizing={req.sizing_method} | db_id={pos_id}"
            )
            asyncio.create_task(tg_bot.notify_position_opened(
                sym1, sym2, req.side,
                req.entry_zscore, price1, price2,
                req.size_usd, req.leverage,
            ))

            return _clean({"status": "ok", "db_id": pos_id, "order1": order1, "order2": order2})

        else:  # close
            # --- Find DB position for PnL tracking ---
            db_pos = db.find_open_position(sym1, sym2)

            # --- Determine close direction from Binance positions ---
            positions = await client.get_positions()
            pos_map = {p["symbol"]: p for p in positions}
            p1 = pos_map.get(sym1)
            p2 = pos_map.get(sym2)

            if p1 is None and p2 is None and db_pos is None:
                return {"status": "no open positions to close"}

            side1 = "sell" if (p1 and p1["side"] == "long") else "buy"
            side2 = "buy" if (p2 and p2["side"] == "short") else "sell"
            close_qty1 = abs(p1["size"]) if p1 else qty1
            close_qty2 = abs(p2["size"]) if p2 else qty2

            order1, order2 = await asyncio.gather(
                client.place_order(sym1, side1, close_qty1),
                client.place_order(sym2, side2, close_qty2),
            )

            # --- Calculate PnL and close DB record ---
            pnl = None
            if db_pos and db_pos.get("entry_price1") and db_pos.get("entry_price2"):
                entry_p1 = db_pos["entry_price1"]
                entry_p2 = db_pos["entry_price2"]
                sign = 1 if db_pos["side"] == "long_spread" else -1
                pnl1 = db_pos["qty1"] * (price1 - entry_p1) * sign
                pnl2 = db_pos["qty2"] * (entry_p2 - price2) * sign
                pnl = round(pnl1 + pnl2, 4)
                db.close_position(db_pos["id"], price1, price2, pnl, req.exit_zscore)

            log.info(
                f"CLOSE | {sym1}/{sym2} | "
                f"pnl={pnl} | z_exit={req.exit_zscore} | "
                f"db_id={db_pos['id'] if db_pos else 'n/a'}"
            )
            if db_pos:
                asyncio.create_task(tg_bot.notify_position_closed(
                    sym1, sym2, db_pos["side"], pnl, req.exit_zscore, reason="manual",
                ))

            return _clean({"status": "ok", "pnl": pnl, "order1": order1, "order2": order2})

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Trade error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------------

@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    """
    Accept {symbol1, symbol2, timeframe, zscore_window, limit, hedge_ratio}
    and broadcast live updates every 5 seconds.
    Reads from price_cache — no direct Binance calls while the feed is running.
    """
    await websocket.accept()
    cache_key = None
    try:
        data = await websocket.receive_text()
        params = json.loads(data)
        meta1, meta2 = await _resolve_pair(
            params.get("symbol1", "BTC/USDT:USDT"),
            params.get("symbol2", "ETH/USDT:USDT"),
        )
        symbol1 = meta1["symbol"]
        symbol2 = meta2["symbol"]
        timeframe = params.get("timeframe", "1h")
        zscore_window = int(params.get("zscore_window", 20))
        history_limit = max(int(params.get("limit", zscore_window * 3)), zscore_window * 3)
        fixed_hedge_ratio = params.get("hedge_ratio")
        if fixed_hedge_ratio is not None:
            fixed_hedge_ratio = float(fixed_hedge_ratio)

        cache_key = price_cache.subscribe(symbol1, symbol2, timeframe, history_limit)

        while True:
            entry = price_cache.get(cache_key)
            if entry is not None:
                try:
                    price1 = entry["price1"]
                    price2 = entry["price2"]
                    hedge_ratio = fixed_hedge_ratio if fixed_hedge_ratio is not None else strategy.calculate_hedge_ratio(price1, price2)
                    spread = strategy.calculate_spread(price1, price2, hedge_ratio)
                    zscore = strategy.calculate_zscore(spread, window=zscore_window)
                    payload = _clean({
                        "timestamp": str(price1.index[-1]),
                        "price1": float(price1.iloc[-1]),
                        "price2": float(price2.iloc[-1]),
                        "spread": float(spread.iloc[-1]),
                        "zscore": float(zscore.dropna().iloc[-1]) if not zscore.dropna().empty else 0.0,
                        "hedge_ratio": hedge_ratio,
                    })
                    await websocket.send_text(json.dumps(payload))
                except Exception as inner_e:
                    await websocket.send_text(json.dumps({"error": str(inner_e)}))
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if cache_key is not None:
            price_cache.unsubscribe(cache_key)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _normalise_symbol(symbol: str) -> str:
    """Convert BTCUSDT/BTCUSDC -> BTC/USDT or BTC/USDC for ccxt."""
    symbol = symbol.upper().strip()
    if ":" in symbol:
        return symbol
    if "/" not in symbol:
        for quote in ("USDT", "USDC", "BUSD"):
            if symbol.endswith(quote):
                return symbol[:-len(quote)] + "/" + quote
    return symbol
