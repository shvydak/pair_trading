import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import coint
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
from scipy import stats


class PairTradingStrategy:

    # ------------------------------------------------------------------
    # Core calculations
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_hedge_ratio(price1: pd.Series, price2: pd.Series) -> float:
        """
        OLS regression: log(price1) = hedge_ratio * log(price2) + intercept
        Returns the hedge_ratio (beta).
        """
        log1 = np.log(price1.dropna())
        log2 = np.log(price2.dropna())

        # Align on common index
        log1, log2 = log1.align(log2, join="inner")

        X = add_constant(log2)
        model = OLS(log1, X).fit()
        hedge_ratio = float(model.params.iloc[1])
        return hedge_ratio

    @staticmethod
    def calculate_spread(
        price1: pd.Series, price2: pd.Series, hedge_ratio: float
    ) -> pd.Series:
        """
        spread = log(price1) - hedge_ratio * log(price2)
        """
        log1 = np.log(price1)
        log2 = np.log(price2)
        spread = log1 - hedge_ratio * log2
        spread.name = "spread"
        return spread

    @staticmethod
    def calculate_zscore(spread: pd.Series, window: int = 20) -> pd.Series:
        """Rolling z-score of the spread."""
        rolling_mean = spread.rolling(window=window).mean()
        rolling_std = spread.rolling(window=window).std()
        zscore = (spread - rolling_mean) / rolling_std
        zscore.name = "zscore"
        return zscore

    @staticmethod
    def cointegration_test(price1: pd.Series, price2: pd.Series) -> dict:
        """
        Engle-Granger cointegration test.
        Returns: {cointegrated, pvalue, test_stat, critical_values}
        """
        log1 = np.log(price1.dropna())
        log2 = np.log(price2.dropna())
        log1, log2 = log1.align(log2, join="inner")

        score, pvalue, critical_values = coint(log1, log2)
        return {
            "cointegrated": bool(pvalue < 0.05),
            "pvalue": float(pvalue),
            "test_stat": float(score),
            "critical_values": {
                "1%": float(critical_values[0]),
                "5%": float(critical_values[1]),
                "10%": float(critical_values[2]),
            },
        }

    @staticmethod
    def calculate_half_life(spread: pd.Series) -> float:
        """
        Mean-reversion half-life via AR(1) model.
        half_life = -log(2) / log(phi)  where phi is AR(1) coefficient.
        """
        spread_clean = spread.dropna()
        if len(spread_clean) < 10:
            return float("nan")

        lag = spread_clean.shift(1).dropna()
        delta = spread_clean.diff().dropna()
        lag, delta = lag.align(delta, join="inner")

        X = add_constant(lag)
        model = OLS(delta, X).fit()
        # AR(1): delta_S = alpha + beta*S_{t-1} + eps
        # phi = 1 + beta  (the AR coefficient)
        beta = float(model.params.iloc[1])
        phi = 1.0 + beta

        if phi <= 0 or phi >= 1:
            return float("nan")

        half_life = -np.log(2) / np.log(phi)
        return float(half_life)

    @staticmethod
    def calculate_hurst_exponent(spread: pd.Series) -> float:
        """
        Hurst exponent via rescaled range (R/S) analysis.
        H < 0.5  -> mean reverting
        H ~ 0.5  -> random walk
        H > 0.5  -> trending
        """
        series = spread.dropna().values
        n = len(series)
        if n < 20:
            return float("nan")

        lags = range(2, min(100, n // 2))
        tau = []
        for lag in lags:
            # Standard deviation of differences at given lag
            diffs = np.subtract(series[lag:], series[:-lag])
            tau.append(np.std(diffs))

        tau = np.array(tau)
        lags_arr = np.array(list(lags))
        # log-log regression: log(tau) = H * log(lag) + const
        valid = tau > 0
        if valid.sum() < 2:
            return float("nan")
        slope, _, _, _, _ = stats.linregress(
            np.log(lags_arr[valid]), np.log(tau[valid])
        )
        hurst = float(slope)
        return hurst

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
        """
        Average True Range from OHLCV DataFrame.
        Returns the ATR of the last `period` bars as a float price value.
        """
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        val = atr.dropna()
        return float(val.iloc[-1]) if not val.empty else float("nan")

    @staticmethod
    def calculate_position_sizes(
        price1: float,
        price2: float,
        size_usd: float,
        hedge_ratio: float,
        atr1: float = None,
        atr2: float = None,
        method: str = "ols",
    ) -> dict:
        """
        Calculate position quantities for both legs.

        method="ols"   — dollar-neutral adjusted by OLS hedge ratio β:
                          qty1 = size_usd / price1
                          qty2 = size_usd * |β| / price2
                          → leg2 dollar exposure scales with β

        method="atr"   — volatility parity (ATR-based):
                          ratio = atr1 / atr2
                          qty1 = size_usd / price1
                          qty2 = qty1 * ratio
                          → both legs contribute equal dollar volatility
                            (qty1*ATR1 == qty2*ATR2)

        method="equal" — equal dollar exposure on both legs:
                          qty1 = size_usd / price1
                          qty2 = size_usd / price2
        """
        if method == "atr" and atr1 and atr2 and atr2 > 0:
            ratio = atr1 / atr2
            qty1 = size_usd / price1
            qty2 = qty1 * ratio
        elif method == "equal":
            qty1 = size_usd / price1
            qty2 = size_usd / price2
        else:  # ols (default)
            qty1 = size_usd / price1
            qty2 = (size_usd * abs(hedge_ratio)) / price2

        return {
            "qty1":   qty1,
            "qty2":   qty2,
            "value1": qty1 * price1,
            "value2": qty2 * price2,
        }

    @staticmethod
    def calculate_correlation(price1: pd.Series, price2: pd.Series) -> float:
        """Pearson correlation of log returns."""
        ret1 = np.log(price1).diff().dropna()
        ret2 = np.log(price2).diff().dropna()
        ret1, ret2 = ret1.align(ret2, join="inner")
        if len(ret1) < 2:
            return float("nan")
        corr = float(ret1.corr(ret2))
        return corr

    @staticmethod
    def get_signals(
        zscore: pd.Series, entry_threshold: float, exit_threshold: float
    ) -> pd.Series:
        """
        Generate trading signals from z-score.
        Returns a Series: 1 = long spread, -1 = short spread, 0 = flat
        """
        signals = pd.Series(0, index=zscore.index, name="signal")
        position = 0

        for i in range(len(zscore)):
            z = zscore.iloc[i]
            if pd.isna(z):
                signals.iloc[i] = 0
                continue

            if position == 0:
                if z <= -entry_threshold:
                    position = 1   # long spread (buy asset1, sell asset2)
                elif z >= entry_threshold:
                    position = -1  # short spread (sell asset1, buy asset2)
            elif position == 1:
                if z >= exit_threshold:
                    position = 0
            elif position == -1:
                if z <= -exit_threshold:
                    position = 0

            signals.iloc[i] = position

        return signals

    # ------------------------------------------------------------------
    # Backtest
    # ------------------------------------------------------------------

    def calculate_backtest(
        self,
        price1: pd.Series,
        price2: pd.Series,
        hedge_ratio: float,
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.5,
        position_size_usd: float = 1000.0,
        zscore_window: int = 20,
    ) -> dict:
        """
        Full vectorised backtest.

        Returns:
          trades        – list of trade dicts
          equity_curve  – list of {timestamp, equity}
          total_pnl     – float
          sharpe        – float
          max_drawdown  – float (as positive fraction)
          win_rate      – float (0-1)
          num_trades    – int
        """
        spread = self.calculate_spread(price1, price2, hedge_ratio)
        zscore = self.calculate_zscore(spread, window=zscore_window)
        signals = self.get_signals(zscore, entry_threshold, exit_threshold)

        # --- trade extraction ----------------------------------------
        trades = []
        equity = [0.0]
        timestamps = []

        position = 0
        entry_idx = None
        entry_zscore = None
        cumulative_pnl = 0.0
        equity_series = []

        price1_arr = price1.values
        price2_arr = price2.values
        z_arr = zscore.values
        sig_arr = signals.values
        idx = signals.index

        for i in range(len(sig_arr)):
            sig = sig_arr[i]
            if pd.isna(z_arr[i]):
                equity_series.append({"timestamp": str(idx[i]), "equity": cumulative_pnl})
                continue

            # Entry
            if position == 0 and sig != 0:
                position = sig
                entry_idx = i
                entry_zscore = float(z_arr[i])
                entry_p1 = price1_arr[i]
                entry_p2 = price2_arr[i]

            # Exit
            elif position != 0 and sig == 0:
                exit_p1 = price1_arr[i]
                exit_p2 = price2_arr[i]
                exit_zscore = float(z_arr[i])

                # Spread P&L in log terms scaled to USD
                entry_spread = np.log(entry_p1) - hedge_ratio * np.log(entry_p2)
                exit_spread = np.log(exit_p1) - hedge_ratio * np.log(exit_p2)
                spread_change = (exit_spread - entry_spread) * position

                # Dollar PnL using OLS-β sizing:
                # qty1 = size_usd / entry_p1  (long or short based on position)
                # qty2 = size_usd * |β| / entry_p2  (opposite leg)
                qty1 = (position_size_usd / entry_p1) * position
                qty2 = (position_size_usd * abs(hedge_ratio) / entry_p2) * (-position)

                pnl1 = qty1 * (exit_p1 - entry_p1)
                pnl2 = qty2 * (exit_p2 - entry_p2)
                pnl = float(pnl1 + pnl2)
                cumulative_pnl += pnl

                trades.append({
                    "entry_time": str(idx[entry_idx]),
                    "exit_time": str(idx[i]),
                    "side": "long_spread" if position == 1 else "short_spread",
                    "entry_zscore": round(entry_zscore, 4),
                    "exit_zscore": round(exit_zscore, 4),
                    "pnl": round(pnl, 4),
                })
                position = 0
                entry_idx = None

            equity_series.append({"timestamp": str(idx[i]), "equity": round(cumulative_pnl, 4)})

        # --- metrics --------------------------------------------------
        total_pnl = cumulative_pnl
        num_trades = len(trades)
        win_rate = (
            sum(1 for t in trades if t["pnl"] > 0) / num_trades
            if num_trades > 0
            else 0.0
        )

        # Sharpe from equity curve daily returns
        eq_values = np.array([e["equity"] for e in equity_series])
        pnl_changes = np.diff(eq_values)
        if len(pnl_changes) > 1 and pnl_changes.std() > 0:
            sharpe = float((pnl_changes.mean() / pnl_changes.std()) * np.sqrt(252))
        else:
            sharpe = 0.0

        # Max drawdown
        peak = eq_values[0]
        max_dd = 0.0
        for v in eq_values:
            if v > peak:
                peak = v
            dd = (peak - v) / (abs(peak) + 1e-9)
            if dd > max_dd:
                max_dd = dd

        return {
            "trades": trades,
            "equity_curve": equity_series,
            "total_pnl": round(total_pnl, 4),
            "sharpe": round(sharpe, 4),
            "max_drawdown": round(max_dd, 4),
            "win_rate": round(win_rate, 4),
            "num_trades": num_trades,
        }
