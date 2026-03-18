"""
Tests for PriceCache in main.py — reference-counted OHLCV cache.

If ref-counting breaks, pairs won't unsubscribe (memory leak) or will
unsubscribe too early (missing data for WS / TP-SL monitor).
"""
import asyncio
from collections import deque

import pandas as pd
import pytest

from main import PriceCache


# ---------------------------------------------------------------------------
# subscribe / key structure
# ---------------------------------------------------------------------------

def test_subscribe_returns_correct_key():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    assert key == ("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)


def test_subscribe_creates_ref_entry():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    assert cache._refs[key] == 1


def test_subscribe_twice_increments_refcount():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    assert cache._refs[key] == 2


def test_subscribe_different_timeframes_are_separate_keys():
    cache = PriceCache()
    k1 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    k2 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "4h", 500)
    assert k1 != k2
    assert cache._refs[k1] == 1
    assert cache._refs[k2] == 1


def test_subscribe_different_limits_are_separate_keys():
    cache = PriceCache()
    k1 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    k2 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)
    assert k1 != k2


def test_subscribe_different_pairs_are_separate_keys():
    cache = PriceCache()
    k1 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    k2 = cache.subscribe("BTC/USDT:USDT", "LTC/USDT:USDT", "1h", 500)
    assert k1 != k2
    assert cache._refs[k1] == 1
    assert cache._refs[k2] == 1


# ---------------------------------------------------------------------------
# unsubscribe
# ---------------------------------------------------------------------------

def test_unsubscribe_decrements_refcount():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)  # ref = 2
    cache.unsubscribe(key)                                           # ref = 1
    assert cache._refs[key] == 1


def test_unsubscribe_last_removes_refs_entry():
    """ref goes to 0 → key removed from _refs."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache.unsubscribe(key)
    assert key not in cache._refs


def test_unsubscribe_last_removes_store_entry():
    """ref goes to 0 → cached data also cleared."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    # Inject fake data as if _refresh_one ran
    cache._store[key] = {
        "price1": pd.Series([100.0, 101.0]),
        "price2": pd.Series([50.0, 51.0]),
    }
    cache.unsubscribe(key)
    assert key not in cache._store


def test_unsubscribe_with_two_subscribers_keeps_data():
    """Second subscriber still active → data must NOT be cleared."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)  # ref = 2
    cache._store[key] = {
        "price1": pd.Series([100.0]),
        "price2": pd.Series([50.0]),
    }
    cache.unsubscribe(key)  # ref drops to 1 — data should stay
    assert key in cache._store
    assert key in cache._refs


def test_unsubscribe_nonexistent_key_no_error():
    """Unsubscribing a key that was never subscribed must not raise."""
    cache = PriceCache()
    cache.unsubscribe(("X/USDT:USDT", "Y/USDT:USDT", "1h", 100))  # no exception


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_returns_none_before_first_refresh():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    assert cache.get(key) is None


def test_get_returns_data_after_manual_inject():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    p1 = pd.Series([100.0, 101.0, 102.0])
    p2 = pd.Series([50.0, 51.0, 52.0])
    cache._store[key] = {"price1": p1, "price2": p2}

    data = cache.get(key)
    assert data is not None
    assert "price1" in data
    assert "price2" in data
    assert len(data["price1"]) == 3


def test_get_returns_none_after_full_unsubscribe():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache._store[key] = {
        "price1": pd.Series([100.0]),
        "price2": pd.Series([50.0]),
    }
    cache.unsubscribe(key)
    assert cache.get(key) is None


def test_get_returns_none_for_unknown_key():
    cache = PriceCache()
    assert cache.get(("A", "B", "1h", 500)) is None


# ---------------------------------------------------------------------------
# Integration: subscribe → inject → consumer reads → unsubscribe → gone
# ---------------------------------------------------------------------------

def test_full_lifecycle():
    """
    Simulates: WS subscribes, cache refreshes, WS reads, WS disconnects.
    After disconnect (unsubscribe) the entry must be gone.
    """
    cache = PriceCache()
    key = cache.subscribe("ETH/USDT:USDT", "BTC/USDT:USDT", "1h", 100)

    # Simulate _refresh_one result
    cache._store[key] = {
        "price1": pd.Series([3000.0, 3010.0]),
        "price2": pd.Series([50000.0, 50100.0]),
    }

    # Consumer reads
    data = cache.get(key)
    assert data is not None

    # WS disconnects
    cache.unsubscribe(key)

    assert cache.get(key) is None
    assert key not in cache._refs


# ---------------------------------------------------------------------------
# find_cached
# ---------------------------------------------------------------------------

def test_find_cached_exact_key():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache._store[key] = {"price1": pd.Series([1.0]), "price2": pd.Series([2.0])}
    assert cache.find_cached("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500) is not None


def test_find_cached_larger_limit():
    """Cache has 500 rows → request for 100 should hit."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache._store[key] = {"price1": pd.Series([1.0]), "price2": pd.Series([2.0])}
    result = cache.find_cached("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)
    assert result is not None


def test_find_cached_smaller_limit_misses():
    """Cache has 100 rows → request for 500 should miss."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)
    cache._store[key] = {"price1": pd.Series([1.0]), "price2": pd.Series([2.0])}
    result = cache.find_cached("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    assert result is None


def test_find_cached_different_timeframe_misses():
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache._store[key] = {"price1": pd.Series([1.0]), "price2": pd.Series([2.0])}
    result = cache.find_cached("BTC/USDT:USDT", "ETH/USDT:USDT", "4h", 500)
    assert result is None


def test_find_cached_empty_store():
    cache = PriceCache()
    assert cache.find_cached("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500) is None


def test_two_consumers_independent_unsubscribe():
    """
    WS + monitor both subscribe to the same key.
    After WS disconnects, monitor still gets data.
    After monitor also unsubscribes, data is gone.
    """
    cache = PriceCache()
    k_ws = cache.subscribe("ETH/USDT:USDT", "LTC/USDT:USDT", "5m", 500)
    k_mon = cache.subscribe("ETH/USDT:USDT", "LTC/USDT:USDT", "5m", 500)
    assert k_ws == k_mon  # same pair config → same key

    cache._store[k_ws] = {
        "price1": pd.Series([3000.0]),
        "price2": pd.Series([100.0]),
    }

    # WS disconnects (ref: 2 → 1)
    cache.unsubscribe(k_ws)
    assert cache.get(k_mon) is not None  # monitor still has data

    # Monitor closes (ref: 1 → 0)
    cache.unsubscribe(k_mon)
    assert cache.get(k_mon) is None


# ---------------------------------------------------------------------------
# SymbolFeed creation and symbol-level deduplication
# ---------------------------------------------------------------------------

def test_subscribe_creates_symbol_feeds():
    """subscribe() should create SymbolFeed entries in _feeds for both symbols."""
    cache = PriceCache()
    cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    assert ("BTC/USDT:USDT", "1h") in cache._feeds
    assert ("ETH/USDT:USDT", "1h") in cache._feeds


def test_subscribe_feed_ref_counts():
    """Each subscribe increments _feed_refs for both symbols."""
    cache = PriceCache()
    cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    assert cache._feed_refs[("BTC/USDT:USDT", "1h")] == 1
    assert cache._feed_refs[("ETH/USDT:USDT", "1h")] == 1


def test_symbol_deduplication_shared_feed():
    """
    BTC/ETH 1h and BTC/LTC 1h both need BTC → only ONE SymbolFeed for BTC.
    """
    cache = PriceCache()
    cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache.subscribe("BTC/USDT:USDT", "LTC/USDT:USDT", "1h", 500)
    # Three distinct symbols, three feeds
    assert len(cache._feeds) == 3
    assert cache._feed_refs[("BTC/USDT:USDT", "1h")] == 2  # shared by two pairs
    assert cache._feed_refs[("ETH/USDT:USDT", "1h")] == 1
    assert cache._feed_refs[("LTC/USDT:USDT", "1h")] == 1


def test_unsubscribe_stops_feed_when_no_more_refs():
    """When the last pair using a symbol is removed, its feed is stopped."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    feed_btc = cache._feeds[("BTC/USDT:USDT", "1h")]
    cache.unsubscribe(key)
    # Feed should be removed from _feeds
    assert ("BTC/USDT:USDT", "1h") not in cache._feeds
    # _stopped flag set
    assert feed_btc._stopped is True


def test_unsubscribe_keeps_feed_when_other_pair_still_uses_symbol():
    """
    BTC is used by two pairs. When one pair unsubscribes, BTC feed stays alive.
    """
    cache = PriceCache()
    k1 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache.subscribe("BTC/USDT:USDT", "LTC/USDT:USDT", "1h", 500)
    cache.unsubscribe(k1)
    # BTC feed should still exist (ref_count was 2 → 1)
    assert ("BTC/USDT:USDT", "1h") in cache._feeds
    assert cache._feed_refs[("BTC/USDT:USDT", "1h")] == 1


# ---------------------------------------------------------------------------
# _assemble_from_feeds
# ---------------------------------------------------------------------------

def _inject_candles(feed, rows: list[list]) -> None:
    """Helper: inject raw candle rows [ts_ms, o, h, l, c, v] into a SymbolFeed."""
    feed._candles.clear()
    feed._candles.extend(rows)


def _make_candle_rows(n: int, start_price: float) -> list[list]:
    """Generate n fake candle rows with sequential timestamps."""
    return [
        [i * 3_600_000, start_price + i, start_price + i + 1,
         start_price + i - 1, start_price + i, 100.0]
        for i in range(n)
    ]


def test_assemble_from_feeds_populates_store():
    """After injecting candles into both feeds, _assemble_from_feeds fills _store."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)

    feed1 = cache._feeds[("BTC/USDT:USDT", "1h")]
    feed2 = cache._feeds[("ETH/USDT:USDT", "1h")]
    _inject_candles(feed1, _make_candle_rows(200, 60_000))
    _inject_candles(feed2, _make_candle_rows(200, 3_000))

    cache._assemble_from_feeds(key)
    entry = cache.get(key)

    assert entry is not None
    assert "price1" in entry and "price2" in entry
    assert "df1" in entry and "df2" in entry
    assert len(entry["price1"]) == 200
    assert len(entry["price2"]) == 200


def test_assemble_from_feeds_aligns_on_common_timestamps():
    """Prices are aligned on inner join of timestamps."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)

    feed1 = cache._feeds[("BTC/USDT:USDT", "1h")]
    feed2 = cache._feeds[("ETH/USDT:USDT", "1h")]
    # feed1: timestamps 0,1,2,3 (ms × 3_600_000); feed2: timestamps 1,2,3,4
    _inject_candles(feed1, _make_candle_rows(4, 60_000))        # ts 0,1,2,3
    _inject_candles(feed2, [[r[0] + 3_600_000] + r[1:] for r in _make_candle_rows(4, 3_000)])  # ts 1,2,3,4

    cache._assemble_from_feeds(key)
    entry = cache.get(key)

    # Inner join → only timestamps 1,2,3 are common
    assert len(entry["price1"]) == 3
    assert len(entry["price2"]) == 3


def test_assemble_from_feeds_skips_if_feed_empty():
    """If a feed has no candles yet, _store should NOT be overwritten."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)

    # Inject data into one feed only; the other stays empty
    feed1 = cache._feeds[("BTC/USDT:USDT", "1h")]
    _inject_candles(feed1, _make_candle_rows(10, 60_000))

    cache._assemble_from_feeds(key)
    # _store should still be None because feed2 is empty
    assert cache.get(key) is None


# ---------------------------------------------------------------------------
# wait_update
# ---------------------------------------------------------------------------

def test_wait_update_unblocks_when_feed_fires():
    """wait_update should return after a kline event on one of the pair's feeds."""
    cache = PriceCache()
    key = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)

    feed1 = cache._feeds[("BTC/USDT:USDT", "1h")]
    feed2 = cache._feeds[("ETH/USDT:USDT", "1h")]
    _inject_candles(feed1, _make_candle_rows(10, 60_000))
    _inject_candles(feed2, _make_candle_rows(10, 3_000))
    cache._assemble_from_feeds(key)

    async def _run():
        async def _fire_kline():
            await asyncio.sleep(0.05)
            feed1._handle_kline({"t": 99_000_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"})

        asyncio.create_task(_fire_kline())
        await asyncio.wait_for(cache.wait_update(key, timeout=1.0), timeout=2.0)
        # After wait_update, store should be refreshed (new candle added)
        entry = cache.get(key)
        assert entry is not None

    asyncio.run(_run())


def test_wait_update_falls_back_to_timeout_when_no_feeds():
    """wait_update on a key with no feeds (never subscribed) sleeps for timeout."""
    cache = PriceCache()
    phantom_key = ("X/USDT:USDT", "Y/USDT:USDT", "1h", 100)

    async def _run():
        import time
        t0 = time.monotonic()
        await cache.wait_update(phantom_key, timeout=0.1)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.09

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# stop_all
# ---------------------------------------------------------------------------

def test_wait_any_update_unblocks_on_first_feed():
    """wait_any_update across multiple pairs unblocks when ANY feed fires."""
    cache = PriceCache()
    k1 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)
    k2 = cache.subscribe("SOL/USDT:USDT", "BNB/USDT:USDT", "1h", 100)
    feed_btc = cache._feeds[("BTC/USDT:USDT", "1h")]
    _inject_candles(feed_btc, _make_candle_rows(10, 60_000))
    _inject_candles(cache._feeds[("ETH/USDT:USDT", "1h")], _make_candle_rows(10, 3_000))
    _inject_candles(cache._feeds[("SOL/USDT:USDT", "1h")], _make_candle_rows(10, 100))
    _inject_candles(cache._feeds[("BNB/USDT:USDT", "1h")], _make_candle_rows(10, 300))
    cache._assemble_from_feeds(k1)
    cache._assemble_from_feeds(k2)

    async def _run():
        async def _fire():
            await asyncio.sleep(0.05)
            # Fire only feed_btc (one of 4 feeds across 2 pairs)
            feed_btc._handle_kline({"t": 99_000_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"})
        asyncio.create_task(_fire())
        await asyncio.wait_for(cache.wait_any_update([k1, k2], timeout=1.0), timeout=2.0)

    asyncio.run(_run())


def test_wait_any_update_refreshes_all_stores():
    """After wait_any_update, all pair stores are updated, not just the one that fired."""
    cache = PriceCache()
    k1 = cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 100)
    k2 = cache.subscribe("SOL/USDT:USDT", "BNB/USDT:USDT", "1h", 100)
    for sym in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT"):
        _inject_candles(cache._feeds[(sym, "1h")], _make_candle_rows(10, 1_000))
    # Neither pair assembled yet
    assert cache.get(k1) is None
    assert cache.get(k2) is None

    async def _run():
        async def _fire():
            await asyncio.sleep(0.05)
            cache._feeds[("BTC/USDT:USDT", "1h")]._notify()
        asyncio.create_task(_fire())
        await asyncio.wait_for(cache.wait_any_update([k1, k2], timeout=1.0), timeout=2.0)

    asyncio.run(_run())
    # Both stores should now be populated
    assert cache.get(k1) is not None
    assert cache.get(k2) is not None


def test_stop_all_sets_stopped_flag_on_all_feeds():
    """stop_all() must mark every SymbolFeed as stopped."""
    cache = PriceCache()
    cache.subscribe("BTC/USDT:USDT", "ETH/USDT:USDT", "1h", 500)
    cache.subscribe("SOL/USDT:USDT", "ETH/USDT:USDT", "4h", 200)

    asyncio.run(cache.stop_all())

    for feed in cache._feeds.values():
        assert feed._stopped is True
