# Pair Trading Dashboard — CLAUDE.md

## Project Overview
Statistical arbitrage (pair trading) dashboard for Binance USDT-M Futures.
Monitors spread between two correlated assets, calculates cointegration statistics,
runs backtests, and executes live trades via Binance API.

## Project Structure
```
pair_trading/
├── backend/
│   ├── main.py              # FastAPI app — REST endpoints + WebSocket
│   ├── strategy.py          # Pair trading math (cointegration, z-score, backtest)
│   ├── binance_client.py    # ccxt async wrapper for Binance USDT-M Futures
│   └── requirements.txt
├── frontend/
│   └── index.html           # Single-file UI (Tailwind + Chart.js, no build step)
├── .env                     # API keys (not committed)
├── .env.example
├── start.sh                 # Launch script
└── .venv/                   # Python virtual environment
```

## Running the Project

### Backend
```bash
cd /Users/y.shvydak/Projects/pair_trading
.venv/bin/uvicorn backend/main:app --reload --port 8000
# or from backend/ directory:
cd backend && ../.venv/bin/uvicorn main:app --reload --port 8000
```

### Frontend
Open `frontend/index.html` directly in browser (no build/server needed).

### Virtual Environment
Always use `.venv/bin/python` and `.venv/bin/pip` — system Python is managed by Homebrew and blocks system-wide installs.

```bash
# Install new packages:
.venv/bin/pip install <package>
# Add to requirements.txt afterwards
```

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/symbols` | List all active USDT-M perpetual futures |
| GET | `/api/history` | OHLCV + spread/z-score + stats for a pair |
| GET | `/api/backtest` | Full backtest with equity curve and trades |
| GET | `/api/positions` | Open positions from Binance (requires API keys) |
| GET | `/api/balance` | USDT balance (requires API keys) |
| POST | `/api/trade` | Place market order pair trade |
| WS | `/ws/stream` | Live z-score updates every 5 seconds |

## Key Parameters for `/api/history`
- `symbol1`, `symbol2` — ccxt format, e.g. `BTC/USDT:USDT`
- `timeframe` — `1h`, `4h`, `1d`
- `limit` — number of candles (default 500, max 1500)
- `zscore_window` — rolling window for z-score (default 20)

## Strategy Logic (`strategy.py`)
- **Hedge ratio**: OLS regression on log prices — `log(P1) = β * log(P2) + α`
- **Spread**: `log(P1) - β * log(P2)`
- **Z-score**: rolling `(spread - mean) / std` with configurable window
- **Cointegration**: Engle-Granger test via `statsmodels.tsa.stattools.coint`
- **Half-life**: AR(1) on spread differences — `half_life = -log(2) / log(φ)`
- **Hurst exponent**: R/S analysis — H < 0.5 means mean-reverting
- **Backtest signals**: enter at `|z| > entry_threshold`, exit at `|z| < exit_threshold`

## Binance Client Notes (`binance_client.py`)
- Uses `ccxt.async_support.binanceusdm` (not `binance`)
- Symbol format: `BTC/USDT:USDT` (ccxt unified format, NOT `BTCUSDT`)
- The UI sends `BTCUSDT` → backend normalizes via `_normalise_symbol()`
- API keys are only injected into ccxt config if they are non-empty and non-placeholder
- Market type filter: `type in ("swap", "future")` — Binance perpetuals show as `swap`

## Frontend (`frontend/index.html`)
- Single HTML file, no build step, no npm
- Dependencies via CDN: Tailwind CSS, Chart.js 4.4.2, chartjs-plugin-annotation 3.0.1
- i18n: `I18N` object with `en`/`ru` keys, `t(key)` function, `applyLocale()` on load and lang switch
- Language stored in `localStorage` key `pt_lang`, default `ru`
- Tooltips: `position: fixed` with JS positioning — handles viewport clipping above/below
- API keys stored in `localStorage` (`binance_api_key`, `binance_secret`)

## Guide Drawer
- Triggered by "? Руководство / Guide" button in header
- Fixed right panel (520px), slides in with CSS transform transition
- Backdrop overlay closes on click; `Escape` key also closes
- Content defined in `GUIDE` JS object — bilingual (`ru`/`en`), switches with `currentLang`
- 8 sections: intro, pair selection, analysis, z-score, statistics, backtest, trading, risk
- Each section has optional `example` object — "Попробовать / Try example" button calls `applyGuideExample()` which fills the form and closes the drawer
- Previous/Next navigation at bottom of each section
- To add a section: add entry to both `GUIDE.ru[]` and `GUIDE.en[]` with `{id, title, content, example?}`

## Environment Variables (`.env`)
```
BINANCE_API_KEY=...
BINANCE_SECRET=...
```
Public endpoints (symbols, history, backtest) work without API keys.
Private endpoints (positions, balance, trade) require valid keys.

## Common Issues & Fixes
- **Empty symbols list**: ccxt returns Binance perpetuals as `type: "swap"`, not `"future"` — filter includes both
- **`pip` not found**: use `.venv/bin/pip` — Homebrew Python blocks system installs
- **CORS errors**: backend has CORS middleware allowing all origins including `file://`
- **NaN/Inf in JSON**: `_clean()` helper in `main.py` recursively strips non-serializable floats

## User Preferences
- Русский язык по умолчанию в UI
- Dark theme only
- No build tools — keep frontend as single HTML file
