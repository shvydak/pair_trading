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
from fastapi.responses import FileResponse
from pydantic import BaseModel

from binance_client import BinanceClient
from strategy import PairTradingStrategy
import db
from logger import get_logger
from order_manager import ExecConfig, ExecContext, LegState, run_execution
from symbol_feed import SymbolFeed, BookTickerFeed
from user_data_feed import UserDataFeed
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

# WebSocket feeds for smart order execution
_book_feeds: dict[str, BookTickerFeed] = {}  # ccxt symbol → BookTickerFeed
_user_data_feed = UserDataFeed(client)        # single shared User Data Stream

# active smart executions: exec_id -> ExecContext
active_executions: dict = {}
# timestamps of when each execution was created (for TTL cleanup)
_exec_created_at: dict[str, float] = {}
_EXEC_TTL = 7200  # remove terminal executions after 2 hours
# track which terminal execs have already been persisted to execution_history table
_exec_saved_to_db: set[str] = set()

SUPPORTED_MARGIN_ASSETS = {"USDT", "USDC"}

# Cointegration result cache: (sym1, sym2, tf, limit) → (result_dict, timestamp)
_coint_cache: dict[tuple, tuple[dict, float]] = {}
_COINT_TTL = 600.0  # recompute cointegration at most once per 10 minutes per pair/limit
_coint_computing: set[tuple] = set()  # keys with a background precompute already running


async def _precompute_coint(key: tuple, p1, p2) -> None:
    """Background task: compute cointegration and store in cache."""
    try:
        result = await _run_sync(strategy.cointegration_test, p1, p2)
        _coint_cache[key] = (result, time.monotonic())
    except Exception as e:
        log.debug(f"coint precompute failed for {key}: {e}")
    finally:
        _coint_computing.discard(key)


async def _run_sync(func, *args):
    """Run a CPU-bound function in a thread-pool so it doesn't block the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))


# ---------------------------------------------------------------------------
# Shared price cache — single OHLCV feed for WS + monitor + future watchlist
# ---------------------------------------------------------------------------

class PriceCache:
    """
    Centralised OHLCV cache backed by Binance WebSocket kline streams.

    Each (symbol, timeframe) is managed by one SymbolFeed instance — symbols
    are deduplicated across all subscribed pairs (e.g. BTC used in 10 pairs
    = 1 WS connection).  Consumers call subscribe()/unsubscribe() for
    pair-level ref-counting.  The run() background task starts all feeds and
    assembles pair data every ASSEMBLE_INTERVAL seconds.

    Public API (unchanged from the previous REST-polling version):
        subscribe(sym1, sym2, tf, limit) → key
        unsubscribe(key)
        get(key) → dict | None
        find_cached(sym1, sym2, tf, limit) → dict | None

    New:
        wait_update(key, timeout) — event-driven push for /ws/stream
        stop_all()               — graceful shutdown of all WS feeds
    """

    ASSEMBLE_INTERVAL = 1.0  # seconds between store refresh cycles

    def __init__(self, client=None) -> None:
        self._client = client
        self._store: dict[tuple, dict] = {}
        self._refs:  dict[tuple, int]  = {}
        # Symbol-level feed management
        self._feeds:     dict[tuple, SymbolFeed] = {}  # (sym, tf) → SymbolFeed
        self._feed_refs: dict[tuple, int]         = {}  # (sym, tf) → ref count

    def subscribe(self, sym1: str, sym2: str, tf: str, limit: int) -> tuple:
        key = (sym1, sym2, tf, limit)
        self._refs[key] = self._refs.get(key, 0) + 1
        # Ensure a SymbolFeed exists for each symbol (not started yet — run() does that)
        for sym in (sym1, sym2):
            fk = (sym, tf)
            if fk not in self._feeds:
                self._feeds[fk] = SymbolFeed(sym, tf, self._client)
                self._feed_refs[fk] = 0
            self._feed_refs[fk] += 1
        return key

    def unsubscribe(self, key: tuple) -> None:
        if key not in self._refs:
            return
        self._refs[key] -= 1
        sym1, sym2, tf, _ = key
        for sym in (sym1, sym2):
            fk = (sym, tf)
            if fk in self._feed_refs:
                self._feed_refs[fk] -= 1
                if self._feed_refs[fk] <= 0:
                    self._feed_refs.pop(fk, None)
                    feed = self._feeds.pop(fk, None)
                    if feed:
                        feed.stop()
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

    def _assemble_from_feeds(self, key: tuple) -> None:
        """Assemble a pair entry from SymbolFeed buffers and write to _store."""
        sym1, sym2, tf, _ = key
        feed1 = self._feeds.get((sym1, tf))
        feed2 = self._feeds.get((sym2, tf))
        if not feed1 or not feed2:
            return
        df1 = feed1.get_dataframe()
        df2 = feed2.get_dataframe()
        if df1 is None or df2 is None:
            return
        p1, p2 = df1["close"], df2["close"]
        p1, p2 = p1.align(p2, join="inner")
        if p1.empty:
            return
        self._store[key] = {
            "price1": p1,
            "price2": p2,
            "df1": df1.loc[p1.index],
            "df2": df2.loc[p2.index],
        }

    async def wait_update(self, key: tuple, timeout: float = 5.0) -> None:
        """
        Wait for a kline update on either symbol of this pair (up to timeout).
        After returning, the store entry is refreshed with the latest data.
        Used by /ws/stream for event-driven push instead of a fixed sleep.
        """
        sym1, sym2, tf, _ = key
        feed1 = self._feeds.get((sym1, tf))
        feed2 = self._feeds.get((sym2, tf))

        tasks = []
        if feed1:
            gen1 = feed1._generation
            tasks.append(asyncio.create_task(feed1.wait_for_update(gen1)))
        if feed2:
            gen2 = feed2._generation
            tasks.append(asyncio.create_task(feed2.wait_for_update(gen2)))

        if tasks:
            try:
                await asyncio.wait(
                    tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
        else:
            await asyncio.sleep(timeout)

        self._assemble_from_feeds(key)

    async def wait_any_update(self, keys: list[tuple], timeout: float = 5.0) -> None:
        """
        Wait for a kline update on ANY feed across all given pair keys (up to timeout).
        Deduplicates feeds so BTC subscribed by 10 pairs waits on a single task.
        After returning, all pair stores are refreshed.
        Used by /ws/watchlist to push one batch update for all pairs.
        """
        tasks = []
        seen_feeds: set[tuple] = set()
        for key in keys:
            sym1, sym2, tf, _ = key
            for sym in (sym1, sym2):
                fk = (sym, tf)
                if fk in seen_feeds:
                    continue
                seen_feeds.add(fk)
                feed = self._feeds.get(fk)
                if feed:
                    gen = feed._generation
                    tasks.append(asyncio.create_task(feed.wait_for_update(gen)))

        if tasks:
            try:
                await asyncio.wait(
                    tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
        else:
            await asyncio.sleep(timeout)

        for key in keys:
            self._assemble_from_feeds(key)

    async def run(self) -> None:
        """Background task: start all feeds and refresh pair store every ASSEMBLE_INTERVAL."""
        while True:
            # Start any feeds that are registered but not yet running
            for fk, feed in list(self._feeds.items()):
                if self._feed_refs.get(fk, 0) > 0:
                    feed.start()  # idempotent
            # Assemble all subscribed pairs from their SymbolFeed buffers
            for key in list(self._refs.keys()):
                self._assemble_from_feeds(key)
            await asyncio.sleep(self.ASSEMBLE_INTERVAL)

    async def stop_all(self) -> None:
        """Stop all SymbolFeed tasks — called on server shutdown."""
        for feed in list(self._feeds.values()):
            feed.stop()
        tasks = [
            f._task for f in self._feeds.values()
            if f._task and not f._task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


price_cache = PriceCache(client)

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

    # Direction and qty always from DB — never from exchange
    side1 = "sell" if pos["side"] == "long_spread" else "buy"
    side2 = "buy"  if pos["side"] == "long_spread" else "sell"
    qty1 = pos["qty1"]
    qty2 = pos["qty2"]

    await asyncio.gather(
        client.place_order(sym1, side1, qty1, params={"reduceOnly": True}),
        client.place_order(sym2, side2, qty2, params={"reduceOnly": True}),
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
    db.close_position_legs(pos["id"])
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

    # Qty always from DB — never from exchange
    qty1 = pos["qty1"]
    qty2 = pos["qty2"]

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
    for sym in (sym1, sym2):
        if sym not in _book_feeds:
            feed = BookTickerFeed(sym)
            feed.start()
            _book_feeds[sym] = feed
    ctx.book_feeds = _book_feeds
    ctx.user_data_feed = _user_data_feed
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

    Reads OHLCV from PriceCache (fed by WebSocket kline streams) instead of
    fetching Binance REST directly.  Manages its own PriceCache subscriptions
    so data is always live and ready.

    Optimisations:
    - Zero REST calls per cycle (data from WS feeds in PriceCache).
    - Hedge ratio for standalone triggers is cached with 60 s TTL.
    """
    await asyncio.sleep(15)  # wait for startup
    closing_tags: set[str] = set()     # tags currently being closed (smart)
    closing_pairs: set[tuple] = set()  # (sym1,sym2) pairs being closed — prevents double close across position TP + standalone trigger
    alert_states: dict[str, str] = {}  # tag → "idle" | "alerted" (hysteresis)
    # Hedge ratio cache for standalone triggers: (sym1, sym2, tf, limit) → (hedge, timestamp)
    _hedge_cache: dict[tuple, tuple[float, float]] = {}
    _HEDGE_TTL = 60.0  # recompute hedge ratio once per minute
    # PriceCache subscriptions managed by the monitor
    _monitor_keys: dict[str, tuple] = {}  # tag → cache_key

    while True:
        try:
            current_tags: set[str] = set()

            # ── Collect all items that need OHLCV data ──────────────────────
            positions = db.get_open_positions()
            active_triggers = db.get_active_triggers()

            pos_items = []   # (pos, tag, tp, sl, cache_key)
            trig_items = []  # (trig, tag, cache_key)

            for pos in positions:
                tp = pos.get("tp_zscore")
                sl = pos.get("sl_zscore")
                if tp is None and sl is None:
                    continue
                pos_id = pos["id"]
                tag = f"pos_{pos_id}"
                current_tags.add(tag)
                if tag not in _monitor_keys:
                    pos_tf = pos.get("timeframe") or _MONITOR_TIMEFRAME
                    pos_zw = pos.get("zscore_window") or _MONITOR_ZSCORE_WINDOW
                    limit = pos.get("candle_limit") or (pos_zw * 5)
                    _monitor_keys[tag] = price_cache.subscribe(
                        pos["symbol1"], pos["symbol2"], pos_tf, limit
                    )
                if tag in closing_tags:
                    continue
                pos_items.append((pos, tag, tp, sl, _monitor_keys[tag]))

            for trig in active_triggers:
                trig_id = trig["id"]
                tag = f"trig_{trig_id}"
                current_tags.add(tag)
                if tag not in _monitor_keys:
                    sym1, sym2 = trig["symbol1"], trig["symbol2"]
                    trig_tf = trig.get("timeframe") or _MONITOR_TIMEFRAME
                    trig_zw = trig.get("zscore_window") or _MONITOR_ZSCORE_WINDOW
                    limit = trig.get("candle_limit") or max(trig_zw * 3, 60)
                    _monitor_keys[tag] = price_cache.subscribe(sym1, sym2, trig_tf, limit)
                if tag in closing_tags:
                    continue
                trig_items.append((trig, tag, _monitor_keys[tag]))

            # Unsubscribe stale tags (closed positions / cancelled triggers)
            for tag in set(_monitor_keys) - current_tags:
                price_cache.unsubscribe(_monitor_keys.pop(tag))

            # ── Build ohlcv_data from PriceCache (no REST calls) ────────────
            ohlcv_data = {}  # cache_key → (p1, p2) aligned Series
            for tag, cache_key in _monitor_keys.items():
                entry = price_cache.get(cache_key)
                if entry is not None:
                    ohlcv_data[cache_key] = (entry["price1"], entry["price2"])

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
                        thresh = alert_pct * abs(trig_z)
                        abs_z = abs(current_z)
                        # First monitor cycle for this trigger in this process: align FSM with
                        # the market. If |z| is already past the gate, start in "alerted"
                        # without Telegram — avoids instant ping on create while z is extreme;
                        # user gets a notify only after z drops below ALERT_RESET_Z and breaches again.
                        if tag not in alert_states:
                            alert_states[tag] = "alerted" if abs_z >= thresh else "idle"
                            if abs_z >= thresh:
                                lim = trig.get("candle_limit")
                                log.info(
                                    f"ALERT sync (no Telegram): |z|={abs_z:.3f} >= {thresh:.3f} "
                                    f"| trig_id={trig_id} {sym1}/{sym2} "
                                    f"tf={trig.get('timeframe')} zw={trig_zw} limit={lim}"
                                )
                        tag_state = alert_states[tag]
                        if tag_state == "idle" and abs_z >= thresh:
                            log.info(
                                f"ALERT | trig_id={trig_id} | {sym1}/{sym2} | "
                                f"z={current_z:.3f} | |z|>={thresh:.3f} "
                                f"(entry_z={trig_z} alert_pct={alert_pct}) "
                                f"tf={trig.get('timeframe')} zw={trig_zw} limit={trig.get('candle_limit')}"
                            )
                            await tg_bot.notify_alert(
                                sym1, sym2, current_z, trig_z, fire_at=thresh
                            )
                            db.alert_fired(trig_id)
                            alert_states[tag] = "alerted"
                        elif tag_state == "alerted" and abs_z <= tg_bot.ALERT_RESET_Z:
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


async def _handle_liquidation(symbol: str, position_amount: float) -> None:
    """Called when Binance liquidates a position."""
    log.error("LIQUIDATION: %s remaining=%.6f", symbol, position_amount)
    # Find affected DB positions and mark as liquidated
    for pos in db.get_open_positions():
        if pos["symbol1"] == symbol or pos["symbol2"] == symbol:
            db.set_position_status(pos["id"], "liquidated")
            log.error("LIQUIDATION: marked DB position %s as liquidated", pos["id"])
    asyncio.create_task(tg_bot.notify_liquidation(symbol))


async def _handle_adl(symbol: str, position_amount: float) -> None:
    """Called when Binance performs ADL on a position."""
    log.warning("ADL: %s new_amount=%.6f", symbol, position_amount)
    for pos in db.get_open_positions():
        if pos["symbol1"] == symbol:
            db.set_position_status(pos["id"], "adl_detected")
        elif pos["symbol2"] == symbol:
            db.set_position_status(pos["id"], "adl_detected")
    asyncio.create_task(tg_bot.notify_adl(symbol))


async def _handle_funding(asset: str, amount: float) -> None:
    """Called on each funding fee payment/receipt.

    Binance ACCOUNT_UPDATE/FUNDING_FEE sends the *total* balance change for the
    asset across all open positions.  We distribute it proportionally by notional
    so each position gets its fair share, not the full amount.
    """
    open_positions = db.get_open_positions()
    # Collect positions that have a leg denominated in this asset
    matching: list[tuple[dict, str]] = []  # (pos, symbol_for_leg)
    for pos in open_positions:
        for sym in (pos["symbol1"], pos["symbol2"]):
            if asset in sym.upper():
                matching.append((pos, sym))
                break

    if not matching:
        return

    # Distribute proportionally by notional (qty × entry_price) of the relevant leg
    notionals = []
    for pos, sym in matching:
        if sym == pos["symbol1"]:
            notionals.append(pos["qty1"] * (pos["entry_price1"] or 1.0))
        else:
            notionals.append(pos["qty2"] * (pos["entry_price2"] or 1.0))

    total_notional = sum(notionals) or 1.0
    for (pos, sym), notional in zip(matching, notionals):
        share = amount * (notional / total_notional)
        db.save_funding_history(pos["id"], sym, share, asset)


async def reconcile_positions() -> None:
    """
    Background task: every 5 minutes compare DB open positions vs exchange.
    Detects missing legs or qty mismatches and alerts via Telegram.
    Does NOT auto-correct — detection only.
    """
    await asyncio.sleep(60)  # initial delay
    while True:
        try:
            open_positions = db.get_open_positions()
            if open_positions and client.has_creds:
                exchange_positions = await client.get_positions()
                exch_map = {p["symbol"]: p for p in exchange_positions}
                await client._ensure_markets()
                for pos in open_positions:
                    if pos.get("status") in ("partial_close", "liquidated", "adl_detected"):
                        continue
                    for sym, db_qty in [(pos["symbol1"], pos["qty1"]), (pos["symbol2"], pos["qty2"])]:
                        exch_pos = exch_map.get(sym)
                        if exch_pos is None:
                            log.warning(
                                "RECONCILE: %s not found on exchange (pos_id=%s)",
                                sym, pos["id"],
                            )
                            asyncio.create_task(tg_bot.notify_reconcile_mismatch(sym))
                        else:
                            exch_qty = abs(exch_pos["size"])
                            try:
                                market = client.exchange.market(sym)
                                step = float((market.get("precision") or {}).get("amount") or 0)
                            except Exception:
                                step = 0.0
                            if step > 0 and abs(exch_qty - db_qty) > step * 2:
                                log.warning(
                                    "RECONCILE: %s qty mismatch DB=%.6f exchange=%.6f (pos_id=%s)",
                                    sym, db_qty, exch_qty, pos["id"],
                                )
        except Exception as e:
            log.warning("reconcile_positions error: %s", e)
        await asyncio.sleep(300)


async def health_check_coint() -> None:
    """
    Background task: every 4 hours re-run cointegration test for open positions.
    Alerts if p-value > 0.05 (pair may no longer be cointegrated).
    """
    await asyncio.sleep(120)  # initial delay
    while True:
        try:
            open_positions = db.get_open_positions()
            for pos in open_positions:
                sym1, sym2 = pos["symbol1"], pos["symbol2"]
                tf = pos.get("timeframe") or "1h"
                limit = pos.get("candle_limit") or 500
                try:
                    entry = price_cache.find_cached(sym1, sym2, tf, limit)
                    if entry and len(entry.get("price1", [])) >= 50:
                        p1 = entry["price1"]
                        p2 = entry["price2"]
                    else:
                        df1, df2 = await asyncio.gather(
                            client.fetch_ohlcv(sym1, tf, limit),
                            client.fetch_ohlcv(sym2, tf, limit),
                        )
                        p1 = df1["close"]
                        p2 = df2["close"]
                    result = await _run_sync(strategy.cointegration_test, p1, p2)
                    pvalue = result.get("pvalue")
                    if pvalue is not None:
                        db.update_position_coint_health(pos["id"], pvalue)
                        if pvalue > 0.05:
                            log.warning(
                                "COINT HEALTH: %s/%s p=%.4f > 0.05 — cointegration may have broken",
                                sym1, sym2, pvalue,
                            )
                            asyncio.create_task(tg_bot.notify_coint_breakdown(sym1, sym2, pvalue))
                except Exception as e:
                    log.debug("health_check_coint error for %s/%s: %s", sym1, sym2, e)
        except Exception as e:
            log.warning("health_check_coint outer error: %s", e)
        await asyncio.sleep(4 * 3600)


async def _reconcile_on_startup() -> None:
    """
    On startup: query Binance for any open orders with our PT_ clientOrderId prefix.
    Logs them so the operator can manually recover if the server crashed mid-execution.
    """
    if not client.has_creds:
        return
    try:
        open_orders = await client.exchange.fetch_open_orders()
        pt_orders = [o for o in open_orders if (o.get("clientOrderId") or "").startswith("PT_")]
        if pt_orders:
            log.warning(
                "STARTUP RECONCILE: found %d orphaned PT_ orders from previous session: %s",
                len(pt_orders),
                [(o["symbol"], o["clientOrderId"], o["side"], o.get("amount")) for o in pt_orders],
            )
        else:
            log.info("STARTUP RECONCILE: no orphaned PT_ orders found")
    except Exception as e:
        log.warning("STARTUP RECONCILE error: %s", e)


async def lifespan(app: FastAPI):
    db.init_db()
    await tg_bot.setup()
    await _user_data_feed.start()  # no-op if no API credentials
    await _reconcile_on_startup()
    # Register ACCOUNT_UPDATE callbacks
    _user_data_feed.on_liquidation(_handle_liquidation)
    _user_data_feed.on_adl(_handle_adl)
    _user_data_feed.on_funding(_handle_funding)
    _bg_tasks = [
        asyncio.create_task(price_cache.run()),
        asyncio.create_task(monitor_position_triggers()),
        asyncio.create_task(tg_bot.start_polling()),
        asyncio.create_task(reconcile_positions()),
        asyncio.create_task(health_check_coint()),
    ]
    log.info("Pair Trading backend started")
    yield
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    _user_data_feed.stop()
    for feed in list(_book_feeds.values()):
        feed.stop()
    await price_cache.stop_all()
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
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(os.path.dirname(__file__), "../frontend/index.html"))


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
        if cached and "df1" in cached and "df2" in cached and len(cached["price1"]) >= limit:
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
        coint_key = (sym1, sym2, timeframe, limit)
        cached_coint = _coint_cache.get(coint_key)
        now_mono = time.monotonic()

        def _compute_stats():
            hr = strategy.calculate_hedge_ratio(price1, price2)
            sp = strategy.calculate_spread(price1, price2, hr)
            zs = strategy.calculate_zscore(sp, window=zscore_window)
            # Use cached cointegration if still fresh (expensive test, changes slowly)
            if cached_coint and (now_mono - cached_coint[1]) < _COINT_TTL:
                ct = cached_coint[0]
            else:
                ct = strategy.cointegration_test(price1, price2)
            hl = strategy.calculate_half_life(sp)
            hu = strategy.calculate_hurst_exponent(sp)
            corr = strategy.calculate_correlation(price1, price2)
            a1 = strategy.calculate_atr(df1)
            a2 = strategy.calculate_atr(df2)
            return hr, sp, zs, ct, hl, hu, corr, a1, a2

        hedge_ratio, spread, zscore, coint_result, half_life, hurst, correlation, atr1, atr2 = \
            await _run_sync(_compute_stats)

        # Store fresh cointegration result in cache
        if not cached_coint or (now_mono - cached_coint[1]) >= _COINT_TTL:
            _coint_cache[coint_key] = (coint_result, now_mono)

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
    limit: int = 500
    zscore_window: int = 20


class WatchlistItemDB(BaseModel):
    symbol1: str
    symbol2: str
    timeframe: str = "1h"
    zwindow: int = 20
    candle_limit: int = 500
    entry_z: float = 2.0
    exit_z: float = 1.0
    pos_size: str = "1000"
    sizing: str = "ols"
    leverage: str = "1"


@app.get("/api/watchlist")
async def list_watchlist():
    return {"items": db.get_watchlist()}


@app.post("/api/watchlist")
async def add_watchlist_item(item: WatchlistItemDB):
    item_id = db.save_watchlist_item(
        symbol1=item.symbol1,
        symbol2=item.symbol2,
        timeframe=item.timeframe,
        zwindow=item.zwindow,
        candle_limit=item.candle_limit,
        entry_z=item.entry_z,
        exit_z=item.exit_z,
        pos_size=item.pos_size,
        sizing=item.sizing,
        leverage=item.leverage,
    )
    return {"id": item_id}


class WatchlistStats(BaseModel):
    half_life: Optional[float] = None
    hurst: Optional[float] = None
    corr: Optional[float] = None
    pval: Optional[float] = None


@app.patch("/api/watchlist/{item_id}/stats")
async def update_watchlist_stats(item_id: int, stats: WatchlistStats):
    db.update_watchlist_stats(item_id, stats.half_life, stats.hurst, stats.corr, stats.pval)
    return {"ok": True}


@app.delete("/api/watchlist/{item_id}")
async def remove_watchlist_item(item_id: int):
    deleted = db.delete_watchlist_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    return {"ok": True}


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

    # Precompute cointegration in background for pairs where cache is missing or stale
    now_mono = time.monotonic()
    for entry in cached_items:
        if entry is None:
            continue
        item, cached = entry
        sym1 = _normalise_symbol(item.sym1)
        sym2 = _normalise_symbol(item.sym2)
        coint_key = (sym1, sym2, item.timeframe, item.limit)
        cached_coint = _coint_cache.get(coint_key)
        already_fresh = cached_coint and (now_mono - cached_coint[1]) < _COINT_TTL
        if not already_fresh and coint_key not in _coint_computing:
            _coint_computing.add(coint_key)
            asyncio.create_task(
                _precompute_coint(coint_key, cached["price1"].copy(), cached["price2"].copy())
            )

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
        if cached and "df1" in cached and "df2" in cached and len(cached["price1"]) >= item.limit:
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
    sizing_method: str = Query("ols"),
    atr1: Optional[float] = Query(None),
    atr2: Optional[float] = Query(None),
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

        # For ATR sizing: compute ATR from fetched data if not provided by client
        if sizing_method == "atr":
            if atr1 is None:
                atr1 = strategy.calculate_atr(df1)
            if atr2 is None:
                atr2 = strategy.calculate_atr(df2)

        result = strategy.calculate_backtest(
            price1, price2,
            hedge_ratio=hedge_ratio,
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold,
            position_size_usd=position_size_usd,
            zscore_window=zscore_window,
            sizing_method=sizing_method,
            atr1=atr1,
            atr2=atr2,
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


def _enrich_positions(db_positions: list[dict], live_map: dict) -> list[dict]:
    """
    Enrich DB positions with live mark prices and PnL.
    Mark prices fetched per-symbol from live_map (by symbol key).
    PnL always calculated from DB qty — never from exchange position size.
    liq_price is informational only (belongs to the symbol-level position).
    """
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
            leg1_pnl = pos["qty1"] * (mark_price1 - pos["entry_price1"]) * sign
            leg2_pnl = pos["qty2"] * (pos["entry_price2"] - mark_price2) * sign
            pnl = round(leg1_pnl + leg2_pnl, 4)
        funding_total = db.get_funding_total(pos["id"])
        enriched.append({
            **pos,
            "mark_price1": mark_price1,
            "mark_price2": mark_price2,
            "unrealized_pnl": pnl,
            "funding_total": funding_total,
            "liq_price1": live1.get("liquidation_price"),
            "liq_price2": live2.get("liquidation_price"),
        })
    return enriched


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

    return _clean({
        "strategy_positions": _enrich_positions(db_positions, live_map),
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

    # Recent alerts — pure SQLite, no network call
    recent_alerts = db.get_recent_alerts(alert_minutes)

    return _clean({
        "strategy_positions": _enrich_positions(db_positions, live_map),
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

    return {"positions": _clean(_enrich_positions(db_positions, live_map))}


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
    candle_limit: Optional[int] = None


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
    """Create a new TP/SL/alert trigger. For alerts: replace duplicate (same sym, z, TF, z-window, candle_limit)."""
    sym1 = _normalise_symbol(req.symbol1)
    sym2 = _normalise_symbol(req.symbol2)

    # For alert type: cancel existing alert with same full analysis key (incl. lookback candles)
    if req.type == "alert":
        if req.candle_limit is None or req.candle_limit <= 0:
            raise HTTPException(status_code=400, detail="Укажите количество свечей (candle_limit)")
        existing = db.find_active_alert(
            sym1,
            sym2,
            req.zscore,
            req.timeframe,
            req.zscore_window,
            req.candle_limit,
        )
        if existing:
            db.cancel_trigger(existing["id"])
            log.info(
                f"Alert replaced: cancelled old id={existing['id']} for {sym1}/{sym2} "
                f"z={req.zscore} tf={req.timeframe} w={req.zscore_window} limit={req.candle_limit}"
            )

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
        candle_limit=req.candle_limit,
    )
    log.info(
        f"Trigger created: id={trigger_id} | {sym1}/{sym2} | "
        f"{req.side} | {req.type} z={req.zscore} pct={req.alert_pct} "
        f"tf={req.timeframe} w={req.zscore_window} limit={req.candle_limit}"
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

            # Qty and direction always from DB — never from exchange
            close_qty1 = db_pos["qty1"]
            close_qty2 = db_pos["qty2"]
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

        elif req.action == "average":
            # ── Add to existing position (averaging/pyramiding) ────────────
            db_pos = db.find_open_position(sym1, sym2)
            if not db_pos:
                raise HTTPException(404, f"No open DB position found for {sym1}/{sym2}")

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

            (ok1, notional1, min1), (ok2, notional2, min2) = await asyncio.gather(
                client.check_min_notional(sym1, qty1, price1),
                client.check_min_notional(sym2, qty2, price2),
            )
            if not ok1:
                raise HTTPException(400, f"{sym1}: notional ${notional1:.2f} < min ${min1:.2f}")
            if not ok2:
                raise HTTPException(400, f"{sym2}: notional ${notional2:.2f} < min ${min2:.2f}")

            # Same direction as existing position
            side1, side2 = ("buy", "sell") if db_pos["side"] == "long_spread" else ("sell", "buy")

            ctx = ExecContext(
                exec_id=exec_id,
                leg1=LegState(symbol=sym1, side=side1, qty=qty1),
                leg2=LegState(symbol=sym2, side=side2, qty=qty2),
                config=cfg,
                spread_side=db_pos["side"],
                is_close=False,
                is_average=True,
                average_position_id=db_pos["id"],
                hedge_ratio=req.hedge_ratio,
                entry_zscore=req.entry_zscore,
                size_usd=req.size_usd,
                sizing_method=req.sizing_method,
                leverage=db_pos.get("leverage") or 1,
                timeframe=db_pos.get("timeframe") or "1h",
                candle_limit=db_pos.get("candle_limit") or 500,
                zscore_window=db_pos.get("zscore_window") or 20,
            )
            log.info(f"Smart average started: {exec_id} | {sym1}/{sym2} | size_usd={req.size_usd}")

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

        # Attach WebSocket feeds for real-time bid/ask and fill notifications
        for sym in (sym1, sym2):
            if sym not in _book_feeds:
                feed = BookTickerFeed(sym)
                feed.start()
                _book_feeds[sym] = feed
        ctx.book_feeds = _book_feeds
        ctx.user_data_feed = _user_data_feed

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
            # --- Find DB position for direction/qty/PnL tracking ---
            db_pos = db.find_open_position(sym1, sym2)

            if db_pos is None:
                return {"status": "no open positions to close"}

            # Direction and qty always from DB — never from exchange
            side1 = "sell" if db_pos["side"] == "long_spread" else "buy"
            side2 = "buy"  if db_pos["side"] == "long_spread" else "sell"
            close_qty1 = db_pos["qty1"]
            close_qty2 = db_pos["qty2"]

            order1, order2 = await asyncio.gather(
                client.place_order(sym1, side1, close_qty1, params={"reduceOnly": True}),
                client.place_order(sym2, side2, close_qty2, params={"reduceOnly": True}),
            )

            # --- Calculate PnL and close DB record ---
            pnl = None
            if db_pos.get("entry_price1") and db_pos.get("entry_price2"):
                entry_p1 = db_pos["entry_price1"]
                entry_p2 = db_pos["entry_price2"]
                sign = 1 if db_pos["side"] == "long_spread" else -1
                pnl1 = db_pos["qty1"] * (price1 - entry_p1) * sign
                pnl2 = db_pos["qty2"] * (entry_p2 - price2) * sign
                pnl = round(pnl1 + pnl2, 4)
                db.close_position(db_pos["id"], price1, price2, pnl, req.exit_zscore)
                db.close_position_legs(db_pos["id"])

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
    Client → server (first message, JSON):
      symbol1, symbol2 — required (ccxt or raw ticker form, resolved via _resolve_pair).
      timeframe — default "1h".
      zscore_window — default 20.
      limit — candle count for PriceCache; server uses max(limit, zscore_window * 3).
      hedge_ratio — optional float. If omitted, OLS hedge is recomputed on the full
        window each push (same idea as /api/history and the alert monitor). If set,
        spread/Z use that fixed β.

    Broadcasts live OHLCV-derived fields on every kline (event-driven); 5 s wait timeout.
    Backed by price_cache / Binance kline WebSocket feeds.
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
            # Wait for next kline event (up to 5 s) — event-driven push
            await price_cache.wait_update(cache_key, timeout=5.0)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if cache_key is not None:
            price_cache.unsubscribe(cache_key)


@app.websocket("/ws/watchlist")
async def websocket_watchlist(websocket: WebSocket):
    """
    Event-driven watchlist z-score feed.  Replaces 5 s HTTP polling.

    Protocol:
      Client → Server:  JSON array of {sym1, sym2, timeframe, limit, zscore_window}
                        Send once on connect and again whenever the watchlist changes.
      Server → Client:  JSON array of {sym1, sym2, timeframe, current_zscore, spread}
                        Pushed immediately after each kline event from any subscribed feed.
    """
    await websocket.accept()
    subscribed_keys: dict[str, tuple] = {}   # tag → cache_key
    current_items: list[dict] = []
    send_lock = asyncio.Lock()

    def _item_tag(item: dict) -> str:
        sym1 = _normalise_symbol(item.get("sym1", ""))
        sym2 = _normalise_symbol(item.get("sym2", ""))
        tf   = item.get("timeframe", "1h")
        lim  = int(item.get("limit", 500))
        return f"{sym1}|{sym2}|{tf}|{lim}"

    def _reconcile(raw_items: list[dict]) -> None:
        nonlocal current_items
        current_items = raw_items
        new_tags: set[str] = set()
        for item in raw_items:
            sym1 = _normalise_symbol(item.get("sym1", ""))
            sym2 = _normalise_symbol(item.get("sym2", ""))
            tf   = item.get("timeframe", "1h")
            lim  = int(item.get("limit", 500))
            tag  = f"{sym1}|{sym2}|{tf}|{lim}"
            new_tags.add(tag)
            if tag not in subscribed_keys:
                subscribed_keys[tag] = price_cache.subscribe(sym1, sym2, tf, lim)
        for tag in set(subscribed_keys) - new_tags:
            price_cache.unsubscribe(subscribed_keys.pop(tag))

    async def _compute_and_send() -> None:
        if not current_items:
            return

        data_pairs = []
        for item in current_items:
            sym1 = _normalise_symbol(item.get("sym1", ""))
            sym2 = _normalise_symbol(item.get("sym2", ""))
            tf   = item.get("timeframe", "1h")
            lim  = int(item.get("limit", 500))
            zw   = int(item.get("zscore_window", 20))
            tag  = f"{sym1}|{sym2}|{tf}|{lim}"
            key  = subscribed_keys.get(tag)
            entry = price_cache.get(key) if key else None
            data_pairs.append((item, sym1, sym2, tf, zw, entry))

        def _batch():
            results = []
            for item, sym1, sym2, tf, zw, entry in data_pairs:
                out = {"sym1": item.get("sym1"), "sym2": item.get("sym2"),
                       "timeframe": tf, "current_zscore": None, "spread": None}
                if entry is not None:
                    try:
                        p1, p2 = entry["price1"], entry["price2"]
                        h  = strategy.calculate_hedge_ratio(p1, p2)
                        sp = strategy.calculate_spread(p1, p2, h)
                        zs = strategy.calculate_zscore(sp, window=zw)
                        zd = zs.dropna()
                        out["current_zscore"] = float(zd.iloc[-1]) if not zd.empty else None
                        out["spread"]         = float(sp.iloc[-1]) if not sp.empty else None
                    except Exception:
                        pass
                results.append(out)
            return results

        results = await _run_sync(_batch)
        async with send_lock:
            try:
                await websocket.send_text(json.dumps(_clean(results)))
            except Exception:
                pass

    async def _receive_task() -> None:
        """Receive updated watchlist from client and reconcile subscriptions."""
        try:
            async for msg in websocket.iter_text():
                _reconcile(json.loads(msg))
                await _compute_and_send()
        except Exception:
            pass

    async def _push_task() -> None:
        """Push z-score updates on every kline event from any subscribed feed."""
        while True:
            await price_cache.wait_any_update(list(subscribed_keys.values()), timeout=5.0)
            await _compute_and_send()

    recv = asyncio.create_task(_receive_task())
    push = asyncio.create_task(_push_task())
    try:
        await asyncio.gather(recv, push)
    except Exception:
        pass
    finally:
        recv.cancel()
        push.cancel()
        for key in subscribed_keys.values():
            price_cache.unsubscribe(key)


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
