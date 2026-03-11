"""
Tests for JSON-serialisation helpers in main.py: _clean() and _safe_float().

Regression coverage for the silent NaN/Inf bug: if these functions break,
API responses fail to serialise and the frontend receives malformed JSON —
an error that's hard to trace back to source.
"""
import math
import numpy as np
import pytest

from main import _clean, _safe_float


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

def test_safe_float_nan_returns_none():
    assert _safe_float(float("nan")) is None


def test_safe_float_positive_inf_returns_none():
    assert _safe_float(float("inf")) is None


def test_safe_float_negative_inf_returns_none():
    assert _safe_float(float("-inf")) is None


def test_safe_float_valid_float():
    assert _safe_float(3.14) == pytest.approx(3.14)


def test_safe_float_zero():
    assert _safe_float(0.0) == 0.0


def test_safe_float_negative():
    assert _safe_float(-7.5) == pytest.approx(-7.5)


def test_safe_float_integer_input():
    result = _safe_float(42)
    assert result == 42.0
    assert isinstance(result, float)


def test_safe_float_invalid_string_returns_none():
    assert _safe_float("not_a_number") is None


def test_safe_float_none_returns_none():
    assert _safe_float(None) is None


# ---------------------------------------------------------------------------
# _clean — primitive types
# ---------------------------------------------------------------------------

def test_clean_nan_float_returns_none():
    assert _clean(float("nan")) is None


def test_clean_inf_float_returns_none():
    assert _clean(float("inf")) is None
    assert _clean(float("-inf")) is None


def test_clean_valid_float_passthrough():
    assert _clean(1.5) == pytest.approx(1.5)


def test_clean_string_passthrough():
    assert _clean("hello") == "hello"


def test_clean_none_passthrough():
    assert _clean(None) is None


def test_clean_int_passthrough():
    assert _clean(5) == 5


# ---------------------------------------------------------------------------
# _clean — numpy types (the main regression case: strategy returns np.float64)
# ---------------------------------------------------------------------------

def test_clean_numpy_nan_returns_none():
    assert _clean(np.float64("nan")) is None


def test_clean_numpy_inf_returns_none():
    assert _clean(np.float64("inf")) is None
    assert _clean(np.float64("-inf")) is None


def test_clean_numpy_float_valid():
    result = _clean(np.float64(2.5))
    assert result == pytest.approx(2.5)


def test_clean_numpy_integer_becomes_python_int():
    result = _clean(np.int64(7))
    assert result == 7
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# _clean — containers
# ---------------------------------------------------------------------------

def test_clean_dict_with_nan_values():
    result = _clean({"a": float("nan"), "b": 1.5, "c": float("inf")})
    assert result["a"] is None
    assert result["b"] == pytest.approx(1.5)
    assert result["c"] is None


def test_clean_list_with_nan_values():
    result = _clean([float("inf"), 1.0, float("-inf"), 2.0])
    assert result[0] is None
    assert result[1] == pytest.approx(1.0)
    assert result[2] is None
    assert result[3] == pytest.approx(2.0)


def test_clean_nested_dict_in_list():
    """Typical shape: equity_curve = [{timestamp, equity}, ...]"""
    data = [
        {"timestamp": "2024-01-01", "equity": float("nan")},
        {"timestamp": "2024-01-02", "equity": 50.0},
    ]
    result = _clean(data)
    assert result[0]["equity"] is None
    assert result[1]["equity"] == pytest.approx(50.0)


def test_clean_deeply_nested():
    """Backtest response shape: {trades: [{pnl: nan, ...}], total_pnl: nan}"""
    data = {
        "trades": [{"pnl": float("nan")}, {"pnl": 5.0}],
        "total_pnl": float("nan"),
        "sharpe": 1.2,
    }
    result = _clean(data)
    assert result["trades"][0]["pnl"] is None
    assert result["trades"][1]["pnl"] == pytest.approx(5.0)
    assert result["total_pnl"] is None
    assert result["sharpe"] == pytest.approx(1.2)


def test_clean_empty_dict():
    assert _clean({}) == {}


def test_clean_empty_list():
    assert _clean([]) == []


def test_clean_mixed_numpy_in_dict():
    """Stats dict from strategy returns mix of np.float64 and Python floats."""
    data = {
        "half_life": np.float64("nan"),
        "hurst": np.float64(0.35),
        "num_trades": np.int64(12),
        "pvalue": 0.03,
    }
    result = _clean(data)
    assert result["half_life"] is None
    assert result["hurst"] == pytest.approx(0.35)
    assert result["num_trades"] == 12
    assert isinstance(result["num_trades"], int)
    assert result["pvalue"] == pytest.approx(0.03)
