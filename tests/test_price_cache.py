"""
Tests for PriceCache in main.py — reference-counted OHLCV cache.

If ref-counting breaks, pairs won't unsubscribe (memory leak) or will
unsubscribe too early (missing data for WS / TP-SL monitor).
"""
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
