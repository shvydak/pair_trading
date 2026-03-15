"""
Unit tests for the WatchlistItem Pydantic model (POST /api/watchlist/data).
Tests model validation, default values, and field constraints.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from pydantic import ValidationError


# Import WatchlistItem directly to avoid triggering FastAPI app startup
# We import lazily to isolate from ccxt / dotenv side effects in main.py
def _get_model():
    # Temporarily redirect ccxt import so we don't need real API keys
    import unittest.mock as mock
    with mock.patch.dict("sys.modules", {
        "ccxt": mock.MagicMock(),
        "ccxt.async_support": mock.MagicMock(),
        "dotenv": mock.MagicMock(),
    }):
        # Remove cached module if already imported
        sys.modules.pop("main", None)
        import main as m
        return m.WatchlistItem


@pytest.fixture(scope="module")
def WatchlistItem():
    return _get_model()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_watchlist_item_defaults(WatchlistItem):
    """sym1/sym2 required; timeframe/limit/zscore_window have defaults."""
    item = WatchlistItem(sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT")
    assert item.sym1 == "BTC/USDT:USDT"
    assert item.sym2 == "ETH/USDT:USDT"
    assert item.timeframe == "1h"
    assert item.limit == 100
    assert item.zscore_window == 20


# ---------------------------------------------------------------------------
# Custom values
# ---------------------------------------------------------------------------

def test_watchlist_item_custom_timeframe(WatchlistItem):
    item = WatchlistItem(sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT", timeframe="4h")
    assert item.timeframe == "4h"


def test_watchlist_item_custom_limit(WatchlistItem):
    item = WatchlistItem(sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT", limit=200)
    assert item.limit == 200


def test_watchlist_item_custom_zscore_window(WatchlistItem):
    item = WatchlistItem(sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT", zscore_window=50)
    assert item.zscore_window == 50


def test_watchlist_item_all_fields(WatchlistItem):
    item = WatchlistItem(
        sym1="BTC/USDC:USDC",
        sym2="ETH/USDC:USDC",
        timeframe="5m",
        limit=300,
        zscore_window=10,
    )
    assert item.sym1 == "BTC/USDC:USDC"
    assert item.sym2 == "ETH/USDC:USDC"
    assert item.timeframe == "5m"
    assert item.limit == 300
    assert item.zscore_window == 10


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------

def test_watchlist_item_missing_sym1_raises(WatchlistItem):
    with pytest.raises((ValidationError, TypeError)):
        WatchlistItem(sym2="ETH/USDT:USDT")


def test_watchlist_item_missing_sym2_raises(WatchlistItem):
    with pytest.raises((ValidationError, TypeError)):
        WatchlistItem(sym1="BTC/USDT:USDT")


def test_watchlist_item_missing_both_raises(WatchlistItem):
    with pytest.raises((ValidationError, TypeError)):
        WatchlistItem()
