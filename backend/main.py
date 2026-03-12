import asyncio
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

SUPPORTED_MARGIN_ASSETS = {"USDT", "USDC"}


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
    Entry = {"price1": pd.Series, "price2": pd.Series}
    """

    FEED_INTERVAL = 5  # seconds between refreshes

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

    async def _refresh_one(self, key: tuple) -> None:
        sym1, sym2, tf, limit = key
        df1, df2 = await asyncio.gather(
            client.fetch_ohlcv(sym1, tf, limit),
            client.fetch_ohlcv(sym2, tf, limit),
        )
        p1 = df1["close"]
        p2 = df2["close"]
        p1, p2 = p1.align(p2, join="inner")
        self._store[key] = {"price1": p1, "price2": p2}

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


async def _do_market_close(pos: dict, exit_zscore: Optional[float] = None) -> dict:
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

    cfg = ExecConfig(passive_s=10.0, aggressive_s=20.0, allow_market=True)
    ctx = ExecContext(
        sym1=sym1, sym2=sym2,
        side1=side1, side2=side2,
        qty1=qty1, qty2=qty2,
        config=cfg,
        is_close=True,
        close_db_id=pos["id"],
        entry_price1=pos.get("entry_price1"),
        entry_price2=pos.get("entry_price2"),
        exit_zscore=exit_zscore,
    )
    exec_id = uuid.uuid4().hex[:8]
    active_executions[exec_id] = ctx
    _exec_created_at[exec_id] = time.monotonic()
    asyncio.create_task(run_execution(ctx, client, db))
    return exec_id


_MONITOR_ZSCORE_WINDOW = 20
_MONITOR_TIMEFRAME = "1h"


async def monitor_position_triggers() -> None:
    """
    Background task: check TP/SL triggers every 5 s for all open positions.
    Runs at the same cadence as price_cache.run() so every cache update is
    checked immediately.  Reads from price_cache to avoid duplicate Binance
    requests.  Manages its own cache subscriptions keyed by DB position id.
    """
    await asyncio.sleep(15)  # wait for startup + first cache fill
    monitored_keys: dict[int, tuple] = {}  # pos_id → cache_key
    closing_ids: set[int] = set()  # positions currently being closed (smart)

    while True:
        try:
            positions = db.get_open_positions()
            current_ids: set[int] = set()

            for pos in positions:
                tp = pos.get("tp_zscore")
                sl = pos.get("sl_zscore")
                if not tp and not sl:
                    continue

                pos_id = pos["id"]
                current_ids.add(pos_id)

                if pos_id in closing_ids:
                    continue  # smart close already in flight

                # Subscribe to cache on first encounter
                if pos_id not in monitored_keys:
                    limit = max(_MONITOR_ZSCORE_WINDOW * 3, 60)
                    key = price_cache.subscribe(
                        pos["symbol1"], pos["symbol2"], _MONITOR_TIMEFRAME, limit
                    )
                    monitored_keys[pos_id] = key

                entry = price_cache.get(monitored_keys[pos_id])
                if entry is None:
                    continue  # cache not yet populated, skip this cycle

                try:
                    p1 = entry["price1"]
                    p2 = entry["price2"]
                    hedge = pos["hedge_ratio"]
                    spread = strategy.calculate_spread(p1, p2, hedge)
                    zscore_series = strategy.calculate_zscore(spread, window=_MONITOR_ZSCORE_WINDOW)
                    current_z = float(zscore_series.dropna().iloc[-1])

                    side = pos["side"]
                    trigger = None
                    if side == "long_spread":
                        if tp and current_z >= -tp:
                            trigger = "tp"
                        elif sl and current_z <= -sl:
                            trigger = "sl"
                    else:  # short_spread
                        if tp and current_z <= tp:
                            trigger = "tp"
                        elif sl and current_z >= sl:
                            trigger = "sl"

                    if trigger:
                        log.info(
                            f"TRIGGER {trigger.upper()} | pos={pos_id} "
                            f"{pos['symbol1']}/{pos['symbol2']} | z={current_z:.3f} | "
                            f"tp={tp} sl={sl}"
                        )
                        # Safety: check that the position still exists on the exchange.
                        # If closed manually, clean up the DB record instead of placing
                        # orders (which would open a new position in the wrong direction).
                        live_positions = await client.get_positions()
                        live_syms = {p["symbol"] for p in live_positions if p.get("size")}
                        sym1, sym2 = pos["symbol1"], pos["symbol2"]
                        if sym1 not in live_syms and sym2 not in live_syms:
                            log.warning(
                                f"TRIGGER {trigger.upper()} | pos={pos_id} | "
                                f"no live positions found — position was closed manually. "
                                f"Removing stale DB record."
                            )
                            db.delete_open_position(pos_id)
                            price_cache.unsubscribe(monitored_keys.pop(pos_id))
                            current_ids.discard(pos_id)
                            continue

                        use_smart = trigger == "tp" and bool(pos.get("tp_smart"))
                        if use_smart:
                            exec_id = await _do_smart_close_trigger(pos, exit_zscore=current_z)
                            closing_ids.add(pos_id)
                            log.info(
                                f"AUTO-SMART-CLOSE {trigger.upper()} | pos={pos_id} | "
                                f"exec_id={exec_id}"
                            )
                        else:
                            result = await _do_market_close(pos, exit_zscore=current_z)
                            log.info(
                                f"AUTO-CLOSE {trigger.upper()} | pos={pos_id} | "
                                f"pnl={result['pnl']}"
                            )
                        # Unsubscribe immediately after market close (smart close cleans up later)
                        if not use_smart:
                            price_cache.unsubscribe(monitored_keys.pop(pos_id))
                            current_ids.discard(pos_id)
                except Exception as e:
                    log.warning(f"monitor: error checking pos {pos_id}: {e}")

            # Remove closing_ids that are no longer in DB (smart close finished)
            closing_ids &= current_ids

            # Unsubscribe positions that were closed/deleted externally
            for pos_id in list(monitored_keys.keys()):
                if pos_id not in current_ids:
                    price_cache.unsubscribe(monitored_keys.pop(pos_id))

        except Exception as e:
            log.warning(f"monitor: outer error: {e}")

        # Clean up terminal executions older than TTL
        now = time.monotonic()
        for eid in list(active_executions.keys()):
            ctx = active_executions[eid]
            created = _exec_created_at.get(eid, now)
            if ctx.status.name in ("DONE", "CANCELLED", "FAILED") and (now - created) > _EXEC_TTL:
                active_executions.pop(eid, None)
                _exec_created_at.pop(eid, None)

        await asyncio.sleep(5)  # same cadence as price_cache refresh


async def lifespan(app: FastAPI):
    db.init_db()
    asyncio.create_task(price_cache.run())
    asyncio.create_task(monitor_position_triggers())
    log.info("Pair Trading backend started")
    yield
    await client.close()
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

        hedge_ratio = strategy.calculate_hedge_ratio(price1, price2)
        spread = strategy.calculate_spread(price1, price2, hedge_ratio)
        zscore = strategy.calculate_zscore(spread, window=zscore_window)
        coint_result = strategy.cointegration_test(price1, price2)
        half_life = strategy.calculate_half_life(spread)
        hurst = strategy.calculate_hurst_exponent(spread)
        correlation = strategy.calculate_correlation(price1, price2)
        atr1 = strategy.calculate_atr(df1)
        atr2 = strategy.calculate_atr(df2)

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
    tp_smart: bool = False


@app.post("/api/db/positions/{position_id}/triggers")
async def set_triggers(position_id: int, req: TriggerRequest):
    """Set TP/SL z-score triggers for an open position."""
    ok = db.set_position_triggers(position_id, req.tp_zscore, req.sl_zscore, req.tp_smart)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
    log.info(
        f"Triggers set for position {position_id}: "
        f"TP={req.tp_zscore} (smart={req.tp_smart}) SL={req.sl_zscore}"
    )
    return {
        "ok": True, "id": position_id,
        "tp_zscore": req.tp_zscore, "sl_zscore": req.sl_zscore, "tp_smart": req.tp_smart,
    }


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
    # Execution parameters
    passive_s: float = 10.0
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

            # Validate min notional
            ok1, notional1, min1 = await client.check_min_notional(sym1, qty1, price1)
            ok2, notional2, min2 = await client.check_min_notional(sym2, qty2, price2)
            if not ok1:
                raise HTTPException(400, f"{sym1}: notional ${notional1:.2f} < min ${min1:.2f}")
            if not ok2:
                raise HTTPException(400, f"{sym2}: notional ${notional2:.2f} < min ${min2:.2f}")

            # Set leverage (best-effort)
            for sym in (sym1, sym2):
                try:
                    await client.set_leverage(sym, req.leverage)
                except Exception as lev_err:
                    log.warning(f"Could not set leverage for {sym}: {lev_err}")

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
            )
            log.info(
                f"OPEN {req.side} | {sym1}/{sym2} | "
                f"qty1={qty1:.6f} qty2={qty2:.6f} | "
                f"price1={price1} price2={price2} | "
                f"z={req.entry_zscore} | lev={req.leverage}x | "
                f"sizing={req.sizing_method} | db_id={pos_id}"
            )

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
            await asyncio.sleep(5)

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
