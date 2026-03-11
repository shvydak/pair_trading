# Pair Trading Dashboard

Statistical arbitrage dashboard for Binance Futures with support for both USDT-M and USDC-M perpetuals.

Monitors the spread between two correlated assets in real time, runs backtests, and executes trades via the Binance API.

---

## Features

- **Pair analysis** — cointegration, hedge ratio, half-life, Hurst exponent, correlation
- **Spread & Z-score chart** — entry/exit threshold lines update live as you type, and the latest point refreshes via WebSocket every 5 seconds after analysis
- **Normalised price chart** — see how both assets move relative to each other, with the latest point refreshing every 5 seconds after analysis
- **Backtesting** — equity curve, Sharpe ratio, max drawdown, trade log
- **Live trading** — Long/Short spread with one click via Binance Futures API
- **Position sizing** — three methods: OLS β (default), ATR volatility parity, Equal dollar
- **Position preview** — see exact quantities and dollar values before placing a trade
- **Built-in guide** — slide-in panel with 8 sections, SVG charts, examples and "Try it" buttons
- **EN / RU interface** — language toggle in the header

---

## Quick Start

### 1. Clone / download the project

```bash
cd /path/to/pair_trading
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

### 3. Configure API keys (optional)

API keys are **not required** for analysis and backtesting. They are only needed for the Positions tab and live trading.

```bash
cp .env.example .env
# Edit .env and fill in your keys:
# BINANCE_API_KEY=your_key
# BINANCE_SECRET=your_secret
```

> Binance keys must have **Futures Trading** permission enabled. IP restriction is recommended.

### 4. Start the backend

```bash
cd backend
../.venv/bin/uvicorn main:app --reload --port 8080
```

Or via the launch script (requires `pip` on system PATH):

```bash
./start.sh
```

### 5. Open the UI

Open `frontend/index.html` directly in your browser — double-click it or run:

```bash
open frontend/index.html
```

---

## Usage

### Pair Analysis

1. Enter two symbols in **Symbol 1** and **Symbol 2**
   Examples: `BTC/USDT:USDT`, `ETH/USDT:USDT`, `BTC/USDC:USDC`, `ETHUSDC`
2. Select a timeframe: `5m` / `1h` / `4h` / `1d`
3. Set the lookback (candles) and Z-score window
4. Click **Analyze Pair**
5. After analysis, the dashboard keeps the last chart point updated every ~5 seconds via WebSocket without rerunning backtest

### Statistics Reference

| Metric              | Description                               | Good value                 |
| ------------------- | ----------------------------------------- | -------------------------- |
| **Z-score**         | Spread deviation from rolling mean in σ   | \|Z\| > 2 — entry signal   |
| **Hedge Ratio (β)** | Position size ratio between legs          | —                          |
| **Half-Life**       | Bars for spread to revert halfway to mean | 5–50 bars                  |
| **Hurst Exponent**  | Nature of the spread process              | H < 0.5 — mean reverting ✓ |
| **Correlation**     | Pearson correlation of log returns        | ≥ 0.7 ✓                    |
| **Cointegrated**    | Long-run equilibrium exists               | Yes (p < 0.05) ✓           |

### Backtesting

1. Set **Entry Z-score** (default 2.0) and **Exit Z-score** (default 0.5)
2. Set **Position Size** in USD
3. Click **Run Backtest**
4. Review: equity curve, Sharpe ratio, max drawdown, win rate, trade list

### Trading (requires API keys)

- **Long Spread** — buy S1, sell S2 (when Z-score < −2, S1 is undervalued)
- **Short Spread** — sell S1, buy S2 (when Z-score > +2, S1 is overvalued)
- **Close All** — closes both legs

**Position sizing methods:**

| Method              | Formula                                     | When to use                     |
| ------------------- | ------------------------------------------- | ------------------------------- |
| **OLS β** (default) | `qty1 = size/P1`, `qty2 = size×\|β\|/P2`    | Most pairs                      |
| **ATR**             | `qty1 = size/P1`, `qty2 = qty1×(ATR1/ATR2)` | Equal dollar-volatility per leg |
| **Equal $**         | `qty1 = size/P1`, `qty2 = size/P2`          | Equal dollar exposure           |

> The **Position Preview** panel shows exact quantities and values before you click Long/Short. Live trading requires both legs to use the same margin market, e.g. both `USDT-M` or both `USDC-M`.

---

## Strategy

```
Spread  = log(Price₁) − β × log(Price₂)
Z-score = (Spread − Mean) / StdDev

Enter Long  spread: Z < −2  → S1 cheap relative to S2
Enter Short spread: Z > +2  → S1 expensive relative to S2
Exit:              |Z| < 0.5 → spread reverted to mean
```

The strategy relies on **cointegration** — the spread must have a tendency to revert to a long-run equilibrium.

---

## Project Structure

```
pair_trading/
├── backend/
│   ├── main.py              # FastAPI: REST API + WebSocket
│   ├── strategy.py          # Math: cointegration, z-score, backtest
│   ├── binance_client.py    # ccxt wrapper for Binance Futures
│   └── requirements.txt
├── frontend/
│   └── index.html           # Full UI in a single file (no build step)
├── .env                     # Your API keys (never commit!)
├── .env.example             # Template
└── start.sh                 # Launch script
```

---

## Tech Stack

**Backend**

- Python 3.10+
- FastAPI + uvicorn
- ccxt (Binance Futures: USDT-M + USDC-M)
- pandas, numpy, statsmodels, scipy

**Frontend**

- Vanilla JS (no framework, no build step)
- Tailwind CSS (CDN)
- Chart.js + chartjs-plugin-annotation (CDN)
- Built-in guide drawer — 8 sections, bilingual (EN / RU)

---

## FAQ

**How to run tests?**

```bash
cd /path/to/pair_trading
.venv/bin/pytest tests/ -v
```

**Do I need API keys to view charts?**
No. Keys are only required for the Positions tab and trading buttons.

**What symbol format should I use?**
Use ccxt unified format like `BTC/USDT:USDT` or `BTC/USDC:USDC`. You can also type short symbols like `BTCUSDT` or `BTCUSDC` — the backend converts them automatically.

**How do I pick a good pair?**
Look for: cointegration (p < 0.05), Hurst exponent < 0.5, correlation > 0.7, half-life between 10–50 bars.

**WebSocket shows "Disconnected"?**
Make sure the backend is running on `localhost:8080`. The connection is established after clicking **Analyze Pair**.

**Port conflict on macOS?**
Port 5000 is reserved by macOS AirPlay Receiver. Use port 8080 (default) or any other free port.

---

## Disclaimer

> Pair trading does not guarantee profit. Cointegration can break down. Always use stop-losses and test with small position sizes before trading real capital.
