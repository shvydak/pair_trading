"""
Tests for SymbolFeed (symbol_feed.py).

All tests are sync-only using asyncio.run() — no pytest-asyncio needed.
SymbolFeed's _client is mocked or set to None; WS connections are never made.
"""
import asyncio
import sys
import os

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from symbol_feed import SymbolFeed, _to_ws_symbol


# ---------------------------------------------------------------------------
# _to_ws_symbol helper
# ---------------------------------------------------------------------------

def test_to_ws_symbol_usdt():
    assert _to_ws_symbol("BTC/USDT:USDT") == "btcusdt"


def test_to_ws_symbol_usdc():
    assert _to_ws_symbol("ETH/USDC:USDC") == "ethusdc"


def test_to_ws_symbol_alt():
    assert _to_ws_symbol("SOL/USDT:USDT") == "solusdt"


# ---------------------------------------------------------------------------
# SymbolFeed — buffer management
# ---------------------------------------------------------------------------

def _make_feed() -> SymbolFeed:
    return SymbolFeed("BTC/USDT:USDT", "1h", client=None)


def test_get_dataframe_empty():
    feed = _make_feed()
    assert feed.get_dataframe() is None


def test_handle_kline_appends_candle():
    feed = _make_feed()
    feed._handle_kline({"t": 1000000, "o": "60000", "h": "60100", "l": "59900", "c": "60050", "v": "10"})
    df = feed.get_dataframe()
    assert df is not None
    assert len(df) == 1
    assert float(df["close"].iloc[0]) == 60050.0


def test_handle_kline_updates_in_progress_candle():
    """Same timestamp → update last candle in-place."""
    feed = _make_feed()
    feed._handle_kline({"t": 1000000, "o": "60000", "h": "60100", "l": "59900", "c": "60010", "v": "5"})
    feed._handle_kline({"t": 1000000, "o": "60000", "h": "60200", "l": "59800", "c": "60050", "v": "10"})
    df = feed.get_dataframe()
    assert len(df) == 1
    assert float(df["close"].iloc[0]) == 60050.0


def test_handle_kline_appends_new_candle():
    """Different timestamp → append new candle."""
    feed = _make_feed()
    feed._handle_kline({"t": 1000000, "o": "60000", "h": "60100", "l": "59900", "c": "60010", "v": "5"})
    feed._handle_kline({"t": 2000000, "o": "60010", "h": "60300", "l": "59950", "c": "60200", "v": "8"})
    df = feed.get_dataframe()
    assert len(df) == 2
    assert float(df["close"].iloc[-1]) == 60200.0


def test_get_dataframe_returns_correct_columns():
    feed = _make_feed()
    feed._handle_kline({"t": 1000000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "100"})
    df = feed.get_dataframe()
    assert set(df.columns) == {"open", "high", "low", "close", "volume"}
    assert df.index.name == "timestamp"


def test_generation_increments_on_kline():
    feed = _make_feed()
    assert feed._generation == 0
    feed._handle_kline({"t": 1000000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"})
    assert feed._generation == 1
    feed._handle_kline({"t": 2000000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"})
    assert feed._generation == 2


# ---------------------------------------------------------------------------
# SymbolFeed — wait_for_update (event-driven notification)
# ---------------------------------------------------------------------------

def test_wait_for_update_unblocks_on_kline():
    """wait_for_update should return when a kline arrives."""
    feed = _make_feed()

    async def _run():
        # Schedule a kline after a short delay
        async def _fire():
            await asyncio.sleep(0.05)
            feed._handle_kline({"t": 1000000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"})
        asyncio.create_task(_fire())
        gen = await asyncio.wait_for(feed.wait_for_update(after_gen=0), timeout=1.0)
        assert gen == 1

    asyncio.run(_run())


def test_wait_for_update_multiple_waiters():
    """Multiple coroutines waiting on the same feed all get notified."""
    feed = _make_feed()

    async def _run():
        results = []

        async def _waiter():
            gen = await asyncio.wait_for(feed.wait_for_update(after_gen=0), timeout=1.0)
            results.append(gen)

        tasks = [asyncio.create_task(_waiter()) for _ in range(3)]
        await asyncio.sleep(0.05)
        feed._handle_kline({"t": 1000000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"})
        await asyncio.gather(*tasks)
        assert results == [1, 1, 1]

    asyncio.run(_run())


def test_wait_for_update_already_past_gen():
    """If generation already exceeds after_gen, wait_for_update returns immediately."""
    feed = _make_feed()
    feed._handle_kline({"t": 1000000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"})

    async def _run():
        # gen=1 > after_gen=0 → should return without blocking
        gen = await asyncio.wait_for(feed.wait_for_update(after_gen=0), timeout=0.1)
        assert gen == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# SymbolFeed — _load_initial with client=None
# ---------------------------------------------------------------------------

def test_load_initial_with_none_client_sets_ready():
    """When client=None, _load_initial should still set _ready without error."""
    feed = _make_feed()
    assert not feed._ready.is_set()

    asyncio.run(feed._load_initial())

    assert feed._ready.is_set()
    assert feed._generation == 1  # _notify() called
    assert len(feed._candles) == 0  # no data loaded (client=None)


# ---------------------------------------------------------------------------
# SymbolFeed — stop
# ---------------------------------------------------------------------------

def test_stop_without_start_no_error():
    """stop() on a never-started feed must not raise."""
    feed = _make_feed()
    feed.stop()  # should be a no-op


def test_start_is_idempotent():
    """Calling start() twice should not create two tasks."""
    async def _run():
        feed = SymbolFeed("BTC/USDT:USDT", "1h", client=None)
        feed.start()
        task1 = feed._task
        feed.start()  # second call
        task2 = feed._task
        assert task1 is task2  # same task, not a new one
        feed.stop()
        await asyncio.gather(feed._task, return_exceptions=True)

    asyncio.run(_run())
