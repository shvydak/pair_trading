import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import coint
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

        X = np.column_stack([np.ones(len(log2)), log2.values])
        coeffs, _, _, _ = np.linalg.lstsq(X, log1.values, rcond=None)
        return float(coeffs[1])

    @staticmethod
    def calculate_hedge_ratio_kalman(
        price1: pd.Series, price2: pd.Series, delta: float = 1e-4
    ) -> tuple:
        """
        Kalman Filter dynamic hedge ratio estimation.

        Instead of one fixed β for the whole history, returns a β per candle
        that adapts as the relationship between the pair changes over time.

        delta: process noise — controls how fast β is allowed to change.
               Lower (e.g. 1e-5) = slower adaptation, smoother β.
               Higher (e.g. 1e-3) = faster adaptation, noisier β.

        Returns:
            beta_series  — pd.Series of β values (one per candle, same index as prices)
            current_beta — float, the latest β (used for sizing / display)
        """
        log1 = np.log(price1.dropna())
        log2 = np.log(price2.dropna())
        log1, log2 = log1.align(log2, join="inner")
        n = len(log1)

        # Bootstrap state with OLS so the filter starts from a sensible β.
        # Without this, β=0 initialization causes 100-300 candle warm-up where
        # the spread is completely wrong → trending PnL and inflated half-life.
        X = np.column_stack([np.ones(n), log2.values])
        ols_coeffs, _, _, _ = np.linalg.lstsq(X, log1.values, rcond=None)
        # State vector: [β, intercept]
        theta = np.array([ols_coeffs[1], ols_coeffs[0]])

        # State covariance — tight at start (we trust the OLS seed)
        P = np.eye(2) * 1e-4

        # Process noise: how much β and intercept can shift per candle
        Q = delta * np.eye(2)

        # Observation noise: estimated from first-differences of log(price1)
        R = float(np.var(np.diff(log1.values))) if n > 1 else 1.0

        beta_arr = np.empty(n)

        for i in range(n):
            x = float(log2.iloc[i])
            H = np.array([[x, 1.0]])  # observation row, shape (1, 2)

            # Predict (random-walk state transition)
            P_pred = P + Q

            # Kalman gain
            S = float((H @ P_pred @ H.T)[0, 0]) + R  # scalar
            K = (P_pred @ H.T) / S                   # shape (2, 1)

            # Store PRIOR beta (before seeing this candle).
            # Using posterior beta would compress the spread to near-zero:
            # posterior_spread ≈ innovation * (1 - K[0]*log(p2)).
            # Since K[0]*log(p2) ≈ 0.98 for crypto, the spread is ~50x smaller
            # than the actual residual, making the rolling std tiny and z-score
            # meaningless (pure noise amplification).  Prior beta gives the proper
            # innovation scale, consistent with the OLS spread magnitude.
            beta_arr[i] = theta[0]

            # Update
            innovation = float(log1.iloc[i]) - float((H @ theta)[0])
            theta = theta + K.flatten() * innovation
            P = (np.eye(2) - K @ H) @ P_pred

        beta_series = pd.Series(beta_arr, index=log1.index, name="beta_kalman")
        return beta_series, float(theta[0])

    @staticmethod
    def calculate_spread(
        price1: pd.Series, price2: pd.Series, hedge_ratio
    ) -> pd.Series:
        """
        spread = log(price1) - hedge_ratio * log(price2)
        hedge_ratio can be a float (OLS) or a pd.Series (Kalman).
        """
        log1 = np.log(price1)
        log2 = np.log(price2)
        if isinstance(hedge_ratio, pd.Series):
            log1, hedge_ratio = log1.align(hedge_ratio, join="inner")
            log2 = log2.loc[log1.index]
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

        score, pvalue, critical_values = coint(log1, log2, maxlag=10)
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

        X = np.column_stack([np.ones(len(lag)), lag.values])
        coeffs, _, _, _ = np.linalg.lstsq(X, delta.values, rcond=None)
        beta = float(coeffs[1])
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

        size_usd is the TOTAL position size (both legs combined).

        method="ols"   — dollar-neutral adjusted by OLS hedge ratio β:
                          Split size_usd proportionally by 1 : |β|
                          leg1_usd = size_usd / (1 + |β|)
                          leg2_usd = size_usd * |β| / (1 + |β|)
                          qty1 = leg1_usd / price1
                          qty2 = leg2_usd / price2

        method="atr"   — volatility parity (ATR-based):
                          ratio = atr1 / atr2
                          qty1 = size_usd / (price1 + ratio * price2)
                          qty2 = qty1 * ratio
                          → equal price-unit volatility (qty1*ATR1 == qty2*ATR2)
                          → value1 + value2 = size_usd

        method="equal" — equal dollar exposure on both legs:
                          qty1 = size_usd / (2 * price1)
                          qty2 = size_usd / (2 * price2)
        """
        if method == "atr" and atr1 is not None and atr2 is not None and atr2 > 0:
            ratio = atr1 / atr2
            qty1 = size_usd / (price1 + ratio * price2)
            qty2 = qty1 * ratio
        elif method == "equal":
            qty1 = size_usd / (2 * price1)
            qty2 = size_usd / (2 * price2)
        else:  # ols (default)
            beta = abs(hedge_ratio)
            divisor = 1 + beta
            qty1 = size_usd / (divisor * price1)
            qty2 = (size_usd * beta) / (divisor * price2)

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
            elif position == 1:   # long spread: entered at z <= -entry, exit when z >= -exit (recovered from below)
                if z >= -exit_threshold:
                    position = 0
            elif position == -1:  # short spread: entered at z >= +entry, exit when z <= +exit (recovered from above)
                if z <= exit_threshold:
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
        sizing_method: str = "ols",
        atr1: float = None,
        atr2: float = None,
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

                # Dollar PnL using the selected sizing method
                if sizing_method == "atr" and atr1 is not None and atr2 is not None and atr2 > 0:
                    ratio = atr1 / atr2
                    _qty1 = position_size_usd / (entry_p1 + ratio * entry_p2)
                    _qty2 = _qty1 * ratio
                elif sizing_method == "equal":
                    _qty1 = position_size_usd / (2 * entry_p1)
                    _qty2 = position_size_usd / (2 * entry_p2)
                else:  # ols
                    beta = abs(hedge_ratio)
                    divisor = 1 + beta
                    _qty1 = position_size_usd / (divisor * entry_p1)
                    _qty2 = position_size_usd * beta / (divisor * entry_p2)
                qty1 = _qty1 * position
                qty2 = _qty2 * (-position)

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

        # Max drawdown as % of position_size_usd (capital at risk)
        # equity values are PnL relative to zero, so portfolio = position_size_usd + equity
        peak_portfolio = position_size_usd
        max_dd = 0.0
        for v in eq_values:
            portfolio_val = position_size_usd + v
            if portfolio_val > peak_portfolio:
                peak_portfolio = portfolio_val
            dd = (peak_portfolio - portfolio_val) / peak_portfolio
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
