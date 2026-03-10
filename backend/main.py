import asyncio
import json
import math
import os
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from binance_client import BinanceClient
from strategy import PairTradingStrategy

load_dotenv()

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await client.close()


app = FastAPI(title="Pair Trading API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
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
    """Return list of available USDT-M perpetual futures symbols."""
    try:
        symbols = await client.get_available_futures()
        # Return short names (e.g. BTC/USDT -> BTCUSDT) for convenience
        short = [s.replace("/", "") for s in symbols]
        return {"symbols": short}
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
        sym1 = _normalise_symbol(symbol1)
        sym2 = _normalise_symbol(symbol2)

        df1, df2 = await asyncio.gather(
            client.fetch_ohlcv(sym1, timeframe, limit),
            client.fetch_ohlcv(sym2, timeframe, limit),
        )

        price1 = df1["close"]
        price2 = df2["close"]

        # Align on common timestamps
        price1, price2 = price1.align(price2, join="inner")

        hedge_ratio = strategy.calculate_hedge_ratio(price1, price2)
        spread = strategy.calculate_spread(price1, price2, hedge_ratio)
        zscore = strategy.calculate_zscore(spread, window=zscore_window)
        coint_result = strategy.cointegration_test(price1, price2)
        half_life = strategy.calculate_half_life(spread)
        hurst = strategy.calculate_hurst_exponent(spread)
        correlation = strategy.calculate_correlation(price1, price2)

        timestamps = [str(ts) for ts in price1.index]

        return _clean({
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
        sym1 = _normalise_symbol(symbol1)
        sym2 = _normalise_symbol(symbol2)

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


@app.get("/api/positions")
async def get_positions():
    """Return open Binance futures positions."""
    try:
        positions = await client.get_positions()
        return {"positions": _clean(positions)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/balance")
async def get_balance():
    """Return USDT balance."""
    try:
        balance = await client.get_balance()
        return _clean(balance)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class TradeRequest(BaseModel):
    symbol1: str
    symbol2: str
    action: str          # "open" | "close"
    side: str            # "long_spread" | "short_spread"
    size_usd: float
    hedge_ratio: float


@app.post("/api/trade")
async def execute_trade(req: TradeRequest):
    """
    Execute a pair trade.
    long_spread  -> buy symbol1, sell symbol2
    short_spread -> sell symbol1, buy symbol2
    action=close -> flatten both legs
    """
    try:
        sym1 = _normalise_symbol(req.symbol1)
        sym2 = _normalise_symbol(req.symbol2)

        ticker1, ticker2 = await asyncio.gather(
            client.fetch_ticker(sym1),
            client.fetch_ticker(sym2),
        )
        price1 = ticker1["last"]
        price2 = ticker2["last"]

        half_size = req.size_usd / 2.0
        qty1 = half_size / price1
        qty2 = (half_size * abs(req.hedge_ratio)) / price2

        if req.action == "open":
            if req.side == "long_spread":
                side1, side2 = "buy", "sell"
            else:
                side1, side2 = "sell", "buy"
        else:  # close
            # Determine current position and reverse
            positions = await client.get_positions()
            pos_map = {p["symbol"]: p for p in positions}
            p1 = pos_map.get(sym1)
            p2 = pos_map.get(sym2)

            if p1 is None and p2 is None:
                return {"status": "no open positions to close"}

            side1 = "sell" if (p1 and p1["side"] == "long") else "buy"
            side2 = "buy" if (p2 and p2["side"] == "short") else "sell"
            qty1 = abs(p1["size"]) if p1 else qty1
            qty2 = abs(p2["size"]) if p2 else qty2

        order1, order2 = await asyncio.gather(
            client.place_order(sym1, side1, qty1),
            client.place_order(sym2, side2, qty2),
        )

        return _clean({
            "status": "ok",
            "order1": order1,
            "order2": order2,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------------

@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    """
    Accept {symbol1, symbol2, timeframe, zscore_window} and broadcast
    live updates every 5 seconds.
    """
    await websocket.accept()
    try:
        data = await websocket.receive_text()
        params = json.loads(data)
        symbol1 = _normalise_symbol(params.get("symbol1", "BTC/USDT"))
        symbol2 = _normalise_symbol(params.get("symbol2", "ETH/USDT"))
        timeframe = params.get("timeframe", "1h")
        zscore_window = int(params.get("zscore_window", 20))

        while True:
            try:
                df1, df2 = await asyncio.gather(
                    client.fetch_ohlcv(symbol1, timeframe, zscore_window * 3),
                    client.fetch_ohlcv(symbol2, timeframe, zscore_window * 3),
                )
                price1 = df1["close"]
                price2 = df2["close"]
                price1, price2 = price1.align(price2, join="inner")

                hedge_ratio = strategy.calculate_hedge_ratio(price1, price2)
                spread = strategy.calculate_spread(price1, price2, hedge_ratio)
                zscore = strategy.calculate_zscore(spread, window=zscore_window)

                current_p1 = float(price1.iloc[-1])
                current_p2 = float(price2.iloc[-1])
                current_spread = float(spread.iloc[-1])
                current_z = float(zscore.dropna().iloc[-1]) if not zscore.dropna().empty else 0.0

                payload = _clean({
                    "timestamp": str(price1.index[-1]),
                    "price1": current_p1,
                    "price2": current_p2,
                    "spread": current_spread,
                    "zscore": current_z,
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


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _normalise_symbol(symbol: str) -> str:
    """Convert BTCUSDT -> BTC/USDT for ccxt."""
    symbol = symbol.upper().strip()
    if "/" not in symbol:
        if symbol.endswith("USDT"):
            return symbol[:-4] + "/USDT"
        if symbol.endswith("BUSD"):
            return symbol[:-4] + "/BUSD"
    return symbol
