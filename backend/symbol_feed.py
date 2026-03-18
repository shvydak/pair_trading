"""
Live OHLCV candle feed for a single (symbol, timeframe) pair
via Binance fstream WebSocket kline stream.

Also provides BookTickerFeed — real-time best bid/ask for smart order execution.

Consumed by PriceCache to replace REST polling.
"""
import asyncio
import json
from collections import deque
from typing import Optional

import aiohttp
import pandas as pd

from logger import get_logger

log = get_logger("symbol_feed")


def _to_ws_symbol(ccxt_symbol: str) -> str:
    """Convert ccxt symbol format to Binance WS stream name.

    BTC/USDT:USDT  →  btcusdt
    ETH/USDC:USDC  →  ethusdc
    """
    # ccxt format: BASE/QUOTE:SETTLE  →  take BASE/QUOTE part
    base_quote = ccxt_symbol.split(":")[0]  # "BTC/USDT"
    return base_quote.replace("/", "").lower()  # "btcusdt"


class SymbolFeed:
    """
    Live OHLCV candle buffer for a single (symbol, timeframe).

    Flow:
      1. start() spawns a background asyncio.Task.
      2. Task loads initial history via REST (up to MAX_CANDLES candles).
      3. Task connects to Binance fstream WebSocket kline stream.
      4. Each kline message updates the current in-progress candle in-place
         or appends a new candle when the previous one closes.
      5. On disconnect: exponential back-off reconnect + REST history refresh
         to fill any gap.

    Consumers:
      - get_dataframe()       — read the live buffer as a DataFrame
      - wait_for_update(gen)  — await the next kline event (event-driven push)
    """

    WS_BASE = "wss://fstream.binance.com/stream"
    MAX_CANDLES = 1500

    def __init__(self, symbol: str, timeframe: str, client) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._client = client
        # Candle rows: [timestamp_ms, open, high, low, close, volume]
        self._candles: deque = deque(maxlen=self.MAX_CANDLES)
        # "Replace event" pattern — safe for N concurrent waiters.
        # Each update replaces _update_event with a fresh Event and sets the old one.
        self._generation: int = 0
        self._update_event: asyncio.Event = asyncio.Event()
        self._ready: asyncio.Event = asyncio.Event()  # set after first REST load
        self._task: Optional[asyncio.Task] = None
        self._stopped: bool = False

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background feed task (idempotent — safe to call repeatedly)."""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(
                self._run(), name=f"feed-{self.symbol}-{self.timeframe}"
            )

    def stop(self) -> None:
        """Request graceful shutdown."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def wait_ready(self) -> None:
        """Block until initial REST data is loaded."""
        await self._ready.wait()

    def get_dataframe(self) -> Optional[pd.DataFrame]:
        """Return current buffer as a DataFrame (snapshot). Returns None if empty."""
        if not self._candles:
            return None
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        df = pd.DataFrame(list(self._candles), columns=cols)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df.astype(float)

    async def wait_for_update(self, after_gen: int = -1) -> int:
        """
        Await the next kline update that comes *after* generation `after_gen`.
        Returns the new generation number.

        Pass the value returned by the previous call so you don't miss rapid updates.
        Multiple coroutines can safely call this concurrently.
        """
        while self._generation <= after_gen:
            evt = self._update_event
            await evt.wait()
        return self._generation

    # ── internals ─────────────────────────────────────────────────────────────

    def _notify(self) -> None:
        """Increment generation and wake all current waiters."""
        self._generation += 1
        old = self._update_event
        self._update_event = asyncio.Event()
        old.set()

    def _handle_kline(self, k: dict) -> None:
        """Process a kline payload dict from a WS message."""
        ts_ms = int(k["t"])
        candle = [
            ts_ms,
            float(k["o"]),
            float(k["h"]),
            float(k["l"]),
            float(k["c"]),
            float(k["v"]),
        ]
        if self._candles and self._candles[-1][0] == ts_ms:
            self._candles[-1] = candle  # update in-progress candle
        else:
            self._candles.append(candle)  # new candle
        self._notify()

    async def _load_initial(self) -> None:
        """Fetch up to MAX_CANDLES candles via REST and populate the buffer."""
        if self._client is None:
            self._ready.set()
            self._notify()
            return
        try:
            df = await self._client.fetch_ohlcv(
                self.symbol, self.timeframe, self.MAX_CANDLES
            )
            df_r = df.reset_index()
            rows = []
            for row in df_r.itertuples(index=False):
                ts_val = row.timestamp
                if hasattr(ts_val, "timestamp"):
                    ts_ms = int(ts_val.timestamp() * 1000)
                elif isinstance(ts_val, (int, float)):
                    # already milliseconds if large, else seconds
                    ts_ms = int(ts_val) if ts_val > 1e10 else int(ts_val * 1000)
                else:
                    ts_ms = int(pd.Timestamp(ts_val).timestamp() * 1000)
                rows.append([
                    ts_ms,
                    float(row.open),
                    float(row.high),
                    float(row.low),
                    float(row.close),
                    float(row.volume),
                ])
            self._candles.clear()
            self._candles.extend(rows)
            log.debug(
                f"SymbolFeed {self.symbol}/{self.timeframe}: "
                f"loaded {len(self._candles)} candles via REST"
            )
        except Exception as e:
            log.warning(
                f"SymbolFeed {self.symbol}/{self.timeframe}: REST load failed: {e}"
            )
        finally:
            self._ready.set()
            self._notify()  # signal initial data available

    async def _run(self) -> None:
        """Main loop: initial REST load then persistent WebSocket connection."""
        await self._load_initial()

        ws_sym = _to_ws_symbol(self.symbol)
        url = f"{self.WS_BASE}?streams={ws_sym}@kline_{self.timeframe}"
        backoff = 1.0

        while not self._stopped:
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        url, heartbeat=20, max_msg_size=0
                    ) as ws:
                        backoff = 1.0
                        log.info(
                            f"SymbolFeed {self.symbol}/{self.timeframe}: WS connected"
                        )
                        async for msg in ws:
                            if self._stopped:
                                return
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                # Combined stream wraps payload in {"stream":..., "data":{...}}
                                payload = data.get("data", data)
                                if payload.get("e") == "kline":
                                    self._handle_kline(payload["k"])
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.warning(
                                    f"SymbolFeed {self.symbol}/{self.timeframe}: "
                                    f"WS {msg.type.name}"
                                )
                                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning(
                    f"SymbolFeed {self.symbol}/{self.timeframe}: connection error: {e}"
                )

            if self._stopped:
                return
            log.info(
                f"SymbolFeed {self.symbol}/{self.timeframe}: "
                f"reconnecting in {backoff:.0f}s"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            # Refresh history on reconnect to fill any gap
            await self._load_initial()


class BookTickerFeed:
    """
    Real-time best bid/ask for a single symbol via Binance bookTicker stream.

    Consumed by order_manager to get fresh prices without REST round-trips.

    Flow:
      1. start() spawns a background asyncio.Task.
      2. Task connects to {sym}@bookTicker stream.
      3. Each message updates _bid/_ask in-place.
      4. Auto-reconnect with exponential backoff (1 s → 60 s).

    Usage:
      bid, ask = feed.get_best()   # None, None until first message arrives
    """

    WS_BASE = "wss://fstream.binance.com/stream"

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._stopped: bool = False

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background feed task (idempotent)."""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(
                self._run(), name=f"bookticker-{self.symbol}"
            )

    def stop(self) -> None:
        """Request graceful shutdown."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()

    def get_best(self) -> tuple[Optional[float], Optional[float]]:
        """Return (bid, ask) or (None, None) if no data yet."""
        return self._bid, self._ask

    # ── internals ─────────────────────────────────────────────────────────────

    def _handle_message(self, data: dict) -> None:
        payload = data.get("data", data)
        if payload.get("e") == "bookTicker":
            b = payload.get("b")
            a = payload.get("a")
            if b and a:
                self._bid = float(b)
                self._ask = float(a)

    async def _run(self) -> None:
        ws_sym = _to_ws_symbol(self.symbol)
        url = f"{self.WS_BASE}?streams={ws_sym}@bookTicker"
        backoff = 1.0

        while not self._stopped:
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        url, heartbeat=20, max_msg_size=0
                    ) as ws:
                        backoff = 1.0
                        log.info(f"BookTickerFeed {self.symbol}: WS connected")
                        async for msg in ws:
                            if self._stopped:
                                return
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_message(json.loads(msg.data))
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.warning(
                                    f"BookTickerFeed {self.symbol}: WS {msg.type.name}"
                                )
                                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning(f"BookTickerFeed {self.symbol}: error: {e}")

            if self._stopped:
                return
            log.info(
                f"BookTickerFeed {self.symbol}: reconnecting in {backoff:.0f}s"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
