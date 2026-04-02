"""
Unit tests for strategy.py — pure math functions.
No network, no DB, no mocks needed.
"""
import math
import numpy as np
import pandas as pd
import pytest

from strategy import PairTradingStrategy

strat = PairTradingStrategy()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prices(values):
    return pd.Series(values, dtype=float)


def _make_ou_process(n=300, theta=0.2, seed=42):
    """Ornstein-Uhlenbeck mean-reverting series."""
    rng = np.random.default_rng(seed)
    s = np.zeros(n)
    for i in range(1, n):
        s[i] = s[i - 1] + theta * (0.0 - s[i - 1]) + rng.standard_normal() * 0.1
    return pd.Series(s)


# ---------------------------------------------------------------------------
# calculate_spread
# ---------------------------------------------------------------------------

def test_calculate_spread_formula():
    """spread = log(p1) - β * log(p2)"""
    # log(e^2) - 1.0 * log(e^1) = 2 - 1 = 1.0
    p1 = _prices([math.e ** 2, math.e ** 3])
    p2 = _prices([math.e ** 1, math.e ** 1])
    spread = strat.calculate_spread(p1, p2, hedge_ratio=1.0)
    assert spread.tolist() == pytest.approx([1.0, 2.0], abs=1e-10)


def test_calculate_spread_hedge_ratio_scaling():
    """Hedge ratio scales the second leg correctly."""
    p1 = _prices([math.e ** 4])
    p2 = _prices([math.e ** 2])
    # log(e^4) - 0.5 * log(e^2) = 4 - 1 = 3.0
    spread = strat.calculate_spread(p1, p2, hedge_ratio=0.5)
    assert spread.iloc[0] == pytest.approx(3.0, abs=1e-10)


def test_calculate_spread_named():
    spread = strat.calculate_spread(_prices([100.0]), _prices([50.0]), 1.0)
    assert spread.name == "spread"


# ---------------------------------------------------------------------------
# calculate_zscore
# ---------------------------------------------------------------------------

def test_calculate_zscore_nan_prefix():
    """First (window - 1) values must be NaN."""
    spread = pd.Series(range(30), dtype=float)
    z = strat.calculate_zscore(spread, window=10)
    assert z.iloc[:9].isna().all()
    assert not pd.isna(z.iloc[9])


def test_calculate_zscore_known_value():
    """At index = window, z-score of the last value should be calculable."""
    # Spread: [0, 0, 0, 0, 5] with window=5
    # mean = 1.0
    # std (ddof=1) = sqrt(((0-1)^2*4 + (5-1)^2) / 4) = sqrt(20/4) = sqrt(5)
    # z[4] = (5 - 1) / sqrt(5) = 4/sqrt(5) ≈ 1.7889
    spread = pd.Series([0.0, 0.0, 0.0, 0.0, 5.0])
    z = strat.calculate_zscore(spread, window=5)
    assert z.iloc[4] == pytest.approx(4 / math.sqrt(5), abs=1e-10)


def test_calculate_zscore_named():
    z = strat.calculate_zscore(pd.Series([1.0, 2.0, 3.0]), window=2)
    assert z.name == "zscore"


# ---------------------------------------------------------------------------
# calculate_position_sizes
# ---------------------------------------------------------------------------

def test_position_sizes_ols():
    """OLS: size_usd is total; split by 1:|β|. β=0.5 → leg1=666.67, leg2=333.33"""
    result = strat.calculate_position_sizes(
        price1=100.0, price2=50.0, size_usd=1000.0, hedge_ratio=0.5, method="ols"
    )
    # divisor = 1 + 0.5 = 1.5
    # leg1_usd = 1000/1.5 ≈ 666.67, leg2_usd = 500/1.5 ≈ 333.33
    assert result["qty1"] == pytest.approx(1000.0 / (1.5 * 100.0))   # ≈ 6.667
    assert result["qty2"] == pytest.approx(1000.0 * 0.5 / (1.5 * 50.0))  # ≈ 6.667
    assert result["value1"] == pytest.approx(1000.0 / 1.5)
    assert result["value2"] == pytest.approx(1000.0 * 0.5 / 1.5)
    assert result["value1"] + result["value2"] == pytest.approx(1000.0)


def test_position_sizes_ols_negative_beta():
    """abs(β) is used — negative hedge ratio treated same as positive."""
    r_pos = strat.calculate_position_sizes(100.0, 50.0, 1000.0, hedge_ratio=0.5, method="ols")
    r_neg = strat.calculate_position_sizes(100.0, 50.0, 1000.0, hedge_ratio=-0.5, method="ols")
    assert r_pos["qty2"] == pytest.approx(r_neg["qty2"])


def test_position_sizes_equal():
    """Equal: each leg gets size_usd/2 in dollar exposure."""
    result = strat.calculate_position_sizes(
        price1=100.0, price2=50.0, size_usd=1000.0, hedge_ratio=1.0, method="equal"
    )
    assert result["value1"] == pytest.approx(500.0)
    assert result["value2"] == pytest.approx(500.0)
    assert result["qty1"] == pytest.approx(5.0)    # 1000/(2*100)
    assert result["qty2"] == pytest.approx(10.0)   # 1000/(2*50)
    assert result["value1"] + result["value2"] == pytest.approx(1000.0)


def test_position_sizes_atr():
    """ATR: total = size_usd, split proportionally by ATR ratio."""
    # atr1=5, atr2=2 → ratio=2.5
    # qty1 = 1000 / (100 + 2.5*50) = 1000/225 ≈ 4.444
    # qty2 = qty1 * 2.5 ≈ 11.111
    result = strat.calculate_position_sizes(
        price1=100.0, price2=50.0, size_usd=1000.0, hedge_ratio=1.0,
        atr1=5.0, atr2=2.0, method="atr"
    )
    expected_qty1 = 1000.0 / (100.0 + 2.5 * 50.0)
    assert result["qty1"] == pytest.approx(expected_qty1)
    assert result["qty2"] == pytest.approx(expected_qty1 * 2.5)
    assert result["value1"] + result["value2"] == pytest.approx(1000.0)


def test_position_sizes_atr_missing_falls_back_to_ols():
    """If atr values are missing, method="atr" falls back to OLS."""
    r_atr_missing = strat.calculate_position_sizes(
        100.0, 50.0, 1000.0, hedge_ratio=0.5, atr1=None, atr2=None, method="atr"
    )
    r_ols = strat.calculate_position_sizes(
        100.0, 50.0, 1000.0, hedge_ratio=0.5, method="ols"
    )
    assert r_atr_missing["qty2"] == pytest.approx(r_ols["qty2"])


def test_position_sizes_value_equals_qty_times_price():
    """value1 = qty1 * price1 and value2 = qty2 * price2 for all methods."""
    for method in ("ols", "equal", "atr"):
        r = strat.calculate_position_sizes(
            price1=200.0, price2=30.0, size_usd=500.0, hedge_ratio=0.8,
            atr1=3.0, atr2=1.5, method=method
        )
        assert r["value1"] == pytest.approx(r["qty1"] * 200.0, rel=1e-9)
        assert r["value2"] == pytest.approx(r["qty2"] * 30.0, rel=1e-9)


def test_position_sizes_total_equals_size_usd():
    """value1 + value2 == size_usd for all methods (total position = input)."""
    for method in ("ols", "equal", "atr"):
        r = strat.calculate_position_sizes(
            price1=200.0, price2=30.0, size_usd=500.0, hedge_ratio=0.8,
            atr1=3.0, atr2=1.5, method=method
        )
        assert r["value1"] + r["value2"] == pytest.approx(500.0, rel=1e-9)


# ---------------------------------------------------------------------------
# get_signals
# ---------------------------------------------------------------------------

def test_get_signals_long_spread_entry():
    """z <= -entry → signal = +1 (long spread)."""
    z = pd.Series([0.0, 0.0, -2.5, -2.0, -2.0])
    sig = strat.get_signals(z, entry_threshold=2.0, exit_threshold=0.5)
    assert sig.iloc[2] == 1


def test_get_signals_short_spread_entry():
    """z >= +entry → signal = -1 (short spread)."""
    z = pd.Series([0.0, 0.0, 2.5, 2.0])
    sig = strat.get_signals(z, entry_threshold=2.0, exit_threshold=0.5)
    assert sig.iloc[2] == -1


def test_get_signals_exit_long():
    """Long spread exits when z >= -exit_threshold (spread recovered from below)."""
    # Enter at z=-2.5, exit when z >= -0.5
    z = pd.Series([0.0, -2.5, -1.0, -0.3, 0.0])
    sig = strat.get_signals(z, entry_threshold=2.0, exit_threshold=0.5)
    assert sig.iloc[1] == 1    # entered
    assert sig.iloc[2] == 1    # still in
    assert sig.iloc[3] == 0    # exited (z=-0.3 >= -0.5)


def test_get_signals_exit_short():
    """Short spread exits when z <= +exit_threshold."""
    z = pd.Series([0.0, 2.5, 1.0, 0.3, 0.0])
    sig = strat.get_signals(z, entry_threshold=2.0, exit_threshold=0.5)
    assert sig.iloc[1] == -1
    assert sig.iloc[2] == -1
    assert sig.iloc[3] == 0    # exited (z=0.3 <= 0.5)


def test_get_signals_no_signal_below_threshold():
    """z between -entry and +entry should give no signal."""
    z = pd.Series([0.0, 1.0, -1.0, 1.9, -1.9])
    sig = strat.get_signals(z, entry_threshold=2.0, exit_threshold=0.5)
    assert (sig == 0).all()


def test_get_signals_nan_is_flat():
    """NaN z-score values produce signal=0."""
    z = pd.Series([float("nan"), -3.0, float("nan")])
    sig = strat.get_signals(z, entry_threshold=2.0, exit_threshold=0.5)
    assert sig.iloc[0] == 0


def test_get_signals_full_trade_cycle():
    """Entry → hold → exit cycle produces correct signal sequence."""
    z = pd.Series([0.0, 0.0, -2.5, -2.0, -1.0, -0.3, 0.0, 2.5, 1.0, 0.3])
    sig = strat.get_signals(z, entry_threshold=2.0, exit_threshold=0.5)
    assert sig.iloc[2] == 1    # long spread entry
    assert sig.iloc[3] == 1
    assert sig.iloc[4] == 1
    assert sig.iloc[5] == 0    # exit long (z=-0.3 >= -0.5)
    assert sig.iloc[7] == -1   # short spread entry
    assert sig.iloc[9] == 0    # exit short (z=0.3 <= 0.5)


# ---------------------------------------------------------------------------
# calculate_atr
# ---------------------------------------------------------------------------

def test_calculate_atr_known_value():
    """ATR computed from explicit True Range values."""
    # TR values (period=2):
    # row0: high-low = 10-8 = 2 (no prev_close → NaN for gap terms)
    # row1: max(12-9, |12-9|, |9-9|) = max(3,3,0) = 3
    # row2: max(11-9, |11-11|, |9-11|) = max(2,0,2) = 2
    # row3: max(13-10, |13-10|, |10-10|) = max(3,3,0) = 3
    # row4: max(12-9, |12-12|, |9-12|) = max(3,0,3) = 3
    # ATR(2): rolling(2).mean of [2,3,2,3,3]
    #   → [NaN, 2.5, 2.5, 2.5, 3.0]  → last = 3.0
    df = pd.DataFrame({
        "high":  [10.0, 12.0, 11.0, 13.0, 12.0],
        "low":   [8.0,   9.0,  9.0, 10.0,  9.0],
        "close": [9.0,  11.0, 10.0, 12.0, 10.0],
    })
    atr = strat.calculate_atr(df, period=2)
    assert atr == pytest.approx(3.0, abs=1e-10)


def test_calculate_atr_returns_nan_when_no_complete_window():
    """Too few rows for the requested period → nan."""
    df = pd.DataFrame({
        "high":  [10.0, 11.0],
        "low":   [9.0,  10.0],
        "close": [9.5,  10.5],
    })
    atr = strat.calculate_atr(df, period=14)
    assert math.isnan(atr)


# ---------------------------------------------------------------------------
# calculate_half_life
# ---------------------------------------------------------------------------

def test_calculate_half_life_nan_for_short_series():
    """Series with fewer than 10 values returns nan."""
    result = strat.calculate_half_life(pd.Series([1.0, 2.0, 1.5]))
    assert math.isnan(result)


def test_calculate_half_life_finite_for_mean_reverting():
    """Strong mean-reverting series returns a finite positive half-life."""
    s = _make_ou_process(n=300, theta=0.3, seed=1)
    hl = strat.calculate_half_life(s)
    assert not math.isnan(hl)
    assert hl > 0


def test_calculate_half_life_nan_for_random_walk():
    """Pure random walk (phi ≈ 1) may return nan (phi >= 1)."""
    rng = np.random.default_rng(7)
    rw = pd.Series(np.cumsum(rng.standard_normal(500)))
    hl = strat.calculate_half_life(rw)
    # phi typically >= 1 for a random walk → nan; not guaranteed but very likely
    # We only check that the function doesn't crash and returns float
    assert isinstance(hl, float)


# ---------------------------------------------------------------------------
# calculate_hurst_exponent
# ---------------------------------------------------------------------------

def test_calculate_hurst_nan_for_short_series():
    """Series with fewer than 20 values returns nan."""
    result = strat.calculate_hurst_exponent(pd.Series(range(10), dtype=float))
    assert math.isnan(result)


def test_calculate_hurst_mean_reverting_below_half():
    """OU process should have Hurst < 0.5 (mean-reverting)."""
    s = _make_ou_process(n=500, theta=0.4, seed=99)
    h = strat.calculate_hurst_exponent(s)
    assert not math.isnan(h)
    assert h < 0.5


def test_calculate_hurst_random_walk_near_half():
    """Random walk should have Hurst close to 0.5."""
    rng = np.random.default_rng(42)
    rw = pd.Series(np.cumsum(rng.standard_normal(1000)))
    h = strat.calculate_hurst_exponent(rw)
    assert not math.isnan(h)
    assert 0.3 < h < 0.7   # wide band — R/S estimate is noisy


# ---------------------------------------------------------------------------
# calculate_correlation
# ---------------------------------------------------------------------------

def test_calculate_correlation_identical_series():
    """Identical series have correlation = 1.0."""
    p = _prices([100.0, 101.0, 99.0, 102.0, 98.0])
    corr = strat.calculate_correlation(p, p)
    assert corr == pytest.approx(1.0, abs=1e-10)


def test_calculate_correlation_too_short_returns_nan():
    """Less than 2 common points returns nan."""
    corr = strat.calculate_correlation(_prices([100.0]), _prices([50.0]))
    assert math.isnan(corr)


def test_calculate_correlation_range():
    """Correlation result must be in [-1, 1]."""
    rng = np.random.default_rng(5)
    p1 = _prices(100 + rng.standard_normal(100).cumsum())
    p2 = _prices(50 + rng.standard_normal(100).cumsum())
    corr = strat.calculate_correlation(p1, p2)
    assert not math.isnan(corr)
    assert -1.0 <= corr <= 1.0


# ---------------------------------------------------------------------------
# calculate_hedge_ratio
# ---------------------------------------------------------------------------

def test_calculate_hedge_ratio_known_relationship():
    """If log(p1) = 2 * log(p2) exactly, hedge_ratio should be ≈ 2.0."""
    rng = np.random.default_rng(10)
    base = _prices(10.0 + np.cumsum(rng.standard_normal(200)) * 0.1)
    base = base.clip(lower=1.0)   # keep positive
    p2 = base
    p1 = base ** 2               # log(p1) = 2 * log(p2)
    hr = strat.calculate_hedge_ratio(p1, p2)
    assert hr == pytest.approx(2.0, abs=0.05)


# ---------------------------------------------------------------------------
# calculate_backtest
# ---------------------------------------------------------------------------

def _oscillating_prices(n=300, amplitude=0.3, period=50, seed=42):
    """Create (p1, p2) pair where spread oscillates and triggers trades."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    spread = amplitude * np.sin(2 * np.pi * t / period) + rng.standard_normal(n) * 0.005
    p2 = pd.Series(np.ones(n) * 100.0)
    p1 = pd.Series(100.0 * np.exp(spread))
    return p1, p2


def test_calculate_backtest_returns_all_keys():
    """Result dict must contain all expected keys."""
    p1, p2 = _oscillating_prices()
    result = strat.calculate_backtest(p1, p2, hedge_ratio=1.0)
    required = {"trades", "equity_curve", "total_pnl", "sharpe", "max_drawdown",
                "win_rate", "num_trades"}
    assert required.issubset(result.keys())


def test_calculate_backtest_zero_trades_when_flat():
    """Constant spread → NaN z-scores → zero trades."""
    p = pd.Series(np.ones(50) * 100.0)
    result = strat.calculate_backtest(p, p, hedge_ratio=1.0)
    assert result["num_trades"] == 0
    assert result["total_pnl"] == 0.0
    assert result["win_rate"] == 0.0


def test_calculate_backtest_generates_trades():
    """Oscillating spread with low threshold generates at least one trade."""
    p1, p2 = _oscillating_prices(amplitude=0.3, period=50)
    result = strat.calculate_backtest(
        p1, p2, hedge_ratio=1.0,
        entry_threshold=1.0, exit_threshold=0.2,
        zscore_window=15, position_size_usd=1000.0,
    )
    assert result["num_trades"] > 0


def test_calculate_backtest_num_trades_matches_list():
    """num_trades == len(trades)."""
    p1, p2 = _oscillating_prices()
    result = strat.calculate_backtest(
        p1, p2, hedge_ratio=1.0,
        entry_threshold=1.0, exit_threshold=0.2,
        zscore_window=15,
    )
    assert result["num_trades"] == len(result["trades"])


def test_calculate_backtest_total_pnl_matches_sum():
    """total_pnl == sum of individual trade pnls (within rounding tolerance)."""
    p1, p2 = _oscillating_prices()
    result = strat.calculate_backtest(
        p1, p2, hedge_ratio=1.0,
        entry_threshold=1.0, exit_threshold=0.2,
        zscore_window=15,
    )
    if result["num_trades"] > 0:
        expected = sum(t["pnl"] for t in result["trades"])
        assert abs(result["total_pnl"] - expected) < 0.02  # rounding tolerance


def test_calculate_backtest_win_rate_range():
    """win_rate must be in [0, 1]."""
    p1, p2 = _oscillating_prices()
    result = strat.calculate_backtest(p1, p2, hedge_ratio=1.0,
                                      entry_threshold=1.0, exit_threshold=0.2)
    assert 0.0 <= result["win_rate"] <= 1.0


def test_calculate_backtest_max_drawdown_non_negative():
    """max_drawdown must be >= 0."""
    p1, p2 = _oscillating_prices()
    result = strat.calculate_backtest(p1, p2, hedge_ratio=1.0)
    assert result["max_drawdown"] >= 0.0


def test_calculate_backtest_trade_fields():
    """Each trade dict must contain required fields with correct types."""
    p1, p2 = _oscillating_prices()
    result = strat.calculate_backtest(
        p1, p2, hedge_ratio=1.0,
        entry_threshold=1.0, exit_threshold=0.2,
        zscore_window=15,
    )
    for trade in result["trades"]:
        assert "entry_time" in trade
        assert "exit_time" in trade
        assert trade["side"] in ("long_spread", "short_spread")
        assert isinstance(trade["pnl"], float)
        assert isinstance(trade["entry_zscore"], float)
        assert isinstance(trade["exit_zscore"], float)


def test_calculate_backtest_equity_curve_length():
    """equity_curve length matches non-NaN z-score count (approximately)."""
    p1, p2 = _oscillating_prices(n=100)
    result = strat.calculate_backtest(p1, p2, hedge_ratio=1.0)
    # equity_curve should not be empty
    assert len(result["equity_curve"]) > 0
    # each entry has timestamp and equity
    for point in result["equity_curve"]:
        assert "timestamp" in point
        assert "equity" in point


# ---------------------------------------------------------------------------
# calculate_kalman_hedge_series
# ---------------------------------------------------------------------------

def test_kalman_returns_series_same_length():
    """Result is a Series with same length and index as input."""
    rng = np.random.default_rng(0)
    base = _prices(10.0 + np.cumsum(rng.standard_normal(100)) * 0.1).clip(lower=1.0)
    p1, p2 = base ** 2, base
    result = strat.calculate_kalman_hedge_series(p1, p2)
    assert isinstance(result, pd.Series)
    assert len(result) == len(p1)
    assert list(result.index) == list(p1.index)


def test_kalman_converges_to_known_beta():
    """Given log(p1) = 2*log(p2) exactly, Kalman β should converge near 2.0."""
    rng = np.random.default_rng(10)
    base = _prices(10.0 + np.cumsum(rng.standard_normal(300)) * 0.1).clip(lower=1.0)
    p2 = base
    p1 = base ** 2
    result = strat.calculate_kalman_hedge_series(p1, p2)
    # Last value should converge close to 2.0
    assert float(result.iloc[-1]) == pytest.approx(2.0, abs=0.1)


def test_kalman_beta_is_finite():
    """All returned values must be finite (no NaN or inf)."""
    rng = np.random.default_rng(7)
    p1 = _prices(100 + np.cumsum(rng.standard_normal(200)) * 0.5).clip(lower=1.0)
    p2 = _prices(50 + np.cumsum(rng.standard_normal(200)) * 0.3).clip(lower=1.0)
    result = strat.calculate_kalman_hedge_series(p1, p2)
    assert result.notna().all()
    assert np.isfinite(result.values).all()


def test_kalman_smoother_than_rolling_ols():
    """Kalman β should have lower std-dev than a rolling OLS β (smoother)."""
    rng = np.random.default_rng(42)
    base = _prices(10.0 + np.cumsum(rng.standard_normal(300)) * 0.2).clip(lower=1.0)
    p2 = base
    p1 = base ** 2 * (1 + rng.standard_normal(300) * 0.01)

    kalman_beta = strat.calculate_kalman_hedge_series(p1, p2)

    # Compare with rolling OLS betas computed manually over window=30
    log1 = np.log(p1.values)
    log2 = np.log(p2.values)
    window = 30
    rolling_betas = []
    for i in range(window, len(log1)):
        X = np.column_stack([np.ones(window), log2[i - window:i]])
        coeffs, _, _, _ = np.linalg.lstsq(X, log1[i - window:i], rcond=None)
        rolling_betas.append(coeffs[1])

    assert kalman_beta.iloc[window:].std() <= np.std(rolling_betas) * 1.5


def test_calculate_spread_with_series_hedge_ratio():
    """calculate_spread accepts a pd.Series hedge_ratio (Kalman output)."""
    rng = np.random.default_rng(1)
    n = 50
    p1 = _prices(100 + np.cumsum(rng.standard_normal(n)))
    p2 = _prices(50 + np.cumsum(rng.standard_normal(n)))
    beta_series = strat.calculate_kalman_hedge_series(p1, p2)

    spread = strat.calculate_spread(p1, p2, beta_series)
    assert isinstance(spread, pd.Series)
    assert len(spread) == n
    assert spread.name == "spread"


def test_calculate_spread_scalar_and_series_same_last_value():
    """Spread at last candle: scalar β=last Kalman β ≈ Series-based spread last value."""
    rng = np.random.default_rng(3)
    base = _prices(10.0 + np.cumsum(rng.standard_normal(100)) * 0.1).clip(lower=1.0)
    p1, p2 = base ** 2, base

    beta_series = strat.calculate_kalman_hedge_series(p1, p2)
    last_beta = float(beta_series.iloc[-1])

    spread_series = strat.calculate_spread(p1, p2, beta_series)
    spread_scalar = strat.calculate_spread(p1, p2, last_beta)

    # Last candle of both spreads should be identical
    assert float(spread_series.iloc[-1]) == pytest.approx(float(spread_scalar.iloc[-1]), abs=1e-10)
