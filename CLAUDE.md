# Pair Trading Dashboard — CLAUDE.md

## Changelog

История изменений: [`CHANGELOG.md`](CHANGELOG.md)

## Project Overview
Statistical arbitrage (pair trading) dashboard for Binance Futures with support for both USDT-M and USDC-M perpetuals.
Monitors spread between two correlated assets, calculates cointegration statistics,
runs backtests, and executes live trades via Binance API.

## Project Structure
```
pair_trading/
├── backend/
│   ├── main.py              # FastAPI app — REST endpoints + WebSocket
│   ├── strategy.py          # Pair trading math (cointegration, z-score, backtest)
│   ├── binance_client.py    # ccxt async wrapper for Binance Futures (USDT-M + USDC-M)
│   ├── order_manager.py     # Smart limit-order execution engine (state machine)
│   ├── db.py                # SQLite persistence — open_positions + closed_trades
│   ├── logger.py            # RotatingFileHandler setup → logs/pair_trading.log
│   └── requirements.txt
├── frontend/
│   └── index.html           # Single-file UI (Tailwind + Chart.js, no build step)
├── logs/
│   └── pair_trading.log     # Rotating log (10 MB × 5 files)
├── pair_trading.db          # SQLite trade journal (auto-created on first run)
├── .env                     # API keys (not committed)
├── .env.example
├── start.sh                 # Launch script
└── .venv/                   # Python virtual environment
```

## Running the Project

### Backend
```bash
cd /Users/y.shvydak/Projects/pair_trading
.venv/bin/uvicorn backend/main:app --reload --port 8080
# or from backend/ directory:
cd backend && ../.venv/bin/uvicorn main:app --reload --port 8080
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
| GET | `/api/symbols` | List all active USDT-M and USDC-M perpetual futures |
| GET | `/api/history` | OHLCV + spread/z-score + stats for a pair |
| GET | `/api/backtest` | Full backtest with equity curve and trades |
| GET | `/api/status` | Binance connection status + supported futures balances (USDT + USDC) |
| GET | `/api/positions` | Open positions from Binance (requires API keys) |
| GET | `/api/balance` | Futures balances (all supported assets, or `?asset=USDT|USDC`) |
| GET | `/api/pre_trade_check` | Validate balance, min notional, lot sizes, leverage before trade |
| POST | `/api/trade` | Place market order pair trade (instant, no retry) |
| POST | `/api/trade/smart` | Start smart limit-order execution in background; returns `exec_id` |
| GET | `/api/execution/{exec_id}` | Poll smart execution state (call every 2s) |
| DELETE | `/api/execution/{exec_id}` | Request cancellation of a running smart execution |
| GET | `/api/db/positions` | Open positions saved by the strategy (with entry z-score, hedge ratio, etc.) |
| GET | `/api/db/history` | Closed trade history from SQLite (`?limit=100`) |
| WS | `/ws/stream` | Live spread/price/Z-score updates every 5 seconds for the active analysed pair |

### `GET /api/pre_trade_check` — query params
`symbol1`, `symbol2`, `size_usd`, `hedge_ratio`, `sizing_method`, `atr1`, `atr2`, `leverage`
Returns `{ok: bool, checks: [{name, ok, detail}], sizes: {qty1, qty2, rounded_qty1, rounded_qty2, notional1, notional2}, prices: {price1, price2}}`

### `POST /api/trade` — TradeRequest fields
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol1`, `symbol2` | str | — | ccxt or BTCUSDT format |
| `action` | str | — | `"open"` \| `"close"` |
| `side` | str | — | `"long_spread"` \| `"short_spread"` |
| `size_usd` | float | — | Dollar size of each leg |
| `hedge_ratio` | float | — | OLS β from `/api/history` |
| `sizing_method` | str | `"ols"` | `"ols"` \| `"atr"` \| `"equal"` |
| `atr1`, `atr2` | float | null | Required for ATR sizing |
| `leverage` | int | `1` | Futures leverage to set before opening |
| `entry_zscore` | float | null | Z-score at entry (saved to DB) |
| `exit_zscore` | float | null | Z-score at exit (saved to DB) |

### `POST /api/trade/smart` — SmartTradeRequest fields
Same as TradeRequest (minus `action`/`exit_zscore`) plus:
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `passive_s` | float | `10.0` | Seconds to wait at bid/ask before chasing |
| `aggressive_s` | float | `20.0` | Seconds at taker side before market fallback |
| `allow_market` | bool | `true` | Use market order as final fallback |

## Key Parameters for `/api/history`
- `symbol1`, `symbol2` — ccxt format, e.g. `BTC/USDT:USDT` or `BTC/USDC:USDC`
- `timeframe` — `5m`, `1h`, `4h`, `1d`
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
- **ATR**: `calculate_atr(df, period=14)` — average true range from OHLCV DataFrame
- **Position sizing** (`calculate_position_sizes`):
  - `ols`: `qty1 = size/P1`, `qty2 = size*|β|/P2`
  - `atr`: `qty1 = size/P1`, `qty2 = qty1 * (ATR1/ATR2)` — equal dollar volatility per leg
  - `equal`: `qty1 = size/P1`, `qty2 = size/P2`
  - ⚠️ ATR formula does NOT include `* (P1/P2)` — that was a bug, correct is `qty2 = qty1 * ratio`

## Binance Client Notes (`binance_client.py`)
- Uses `ccxt.async_support.binanceusdm` (not `binance`)
- Symbol format: `BTC/USDT:USDT` or `BTC/USDC:USDC` (ccxt unified format)
- The UI can send `BTCUSDT` / `BTCUSDC` → backend normalizes via `_normalise_symbol()`
- API keys are only injected into ccxt config if they are non-empty and non-placeholder
- `self.has_creds: bool` — exposed for use in `/api/status`
- `get_available_futures_meta()` → list[dict] — returns market metadata for the UI symbol filter (`ALL` / `USDT-M` / `USDC-M`)
- `get_balance(asset)` / `get_all_balances()` — return futures balances by margin asset
- Market type filter: `type in ("swap", "future")` — Binance perpetuals show as `swap`
- `_ensure_markets()` — loads market data if not yet cached (called before any precision/order op)
- `round_amount(symbol, amount)` → float — rounds to exchange stepSize via `amount_to_precision`
- `place_order()` — market orders; automatically calls `amount_to_precision` before submitting
- `set_leverage(symbol, leverage)` → dict — sets cross-margin leverage; called per-symbol before open
- `check_min_notional(symbol, amount, price)` → `(ok: bool, actual: float, min: float)` — validates against `limits.cost.min` from market data
- `fetch_order_book(symbol, limit=5)` → `{bid, ask, spread_pct}` — top-of-book snapshot
- `place_limit_order(symbol, side, amount, price)` → order dict — rounds both amount and price to exchange precision
- `cancel_order(symbol, order_id)` → dict — cancels by order id
- `fetch_order(symbol, order_id)` → dict — polls single order status

## Frontend (`frontend/index.html`)
- Single HTML file, no build step, no npm
- Dependencies via CDN: Tailwind CSS, Chart.js 4.4.2, chartjs-plugin-annotation 3.0.1
- i18n: `I18N` object with `en`/`ru` keys, `t(key)` function, `applyLocale()` on load and lang switch
- Language stored in `localStorage` key `pt_lang`, default `ru`
- Tooltips: `position: fixed` with JS positioning — handles viewport clipping above/below
- **Market filter** in pair config — `setMarketFilter('ALL'|'USDT'|'USDC')` filters symbol suggestions for `Symbol 1/2`
- **Market context** card in pair config — auto-detects whether the current pair is `USDT-M`, `USDC-M`, or mixed; mixed pairs can be analysed but live trading is blocked
- **Binance status section** in sidebar — `checkApiStatus()` calls `/api/status` on page load and on refresh button click; `renderApiStatus(data)` renders supported futures balances and highlights the active market asset; states: `no_keys` (grey) / connected (green) / `auth_error` (red) / network error (yellow)
- **Live chart updates**: after `Analyze`, `connectWebSocket()` subscribes to `/ws/stream`; every ~5s the frontend updates the last spread, z-score, and normalised price points in-place without rebuilding charts
- **Live threshold lines**: `updateThresholdLines()` — called `oninput` on entry/exit Z-score fields; updates chart annotations immediately via `chart.update('none')` without re-fetching data
- **Position sizing**: `sizingMethod` global (`ols`/`atr`/`equal`), `updateSizePreview()` computes qty/value client-side from `state.historyData` prices + ATR
- **state** includes: `historyData`, `historyLimit`, `hedgeRatio`, `atr1`, `atr2`, `pairMeta`, `balances`, `markets`, `marketFilter`, `spreadChart`, `priceChart`, `ws`
- **Tooltips with i18n**: tooltip HTML is stored in `I18N.en.tip_*` / `I18N.ru.tip_*` keys; `tooltip-box` div uses `data-i18n-html` attribute; `applyLocale()` sets `innerHTML` for these. Existing tooltip keys: `tip_entry_z`, `tip_exit_z`, `tip_zwindow`
- **Trades table**: colored rows (green/red bg tint), `+/-` PnL signs, legs column showing ▲/▼ per symbol, cumulative PnL column. Populated in `runBacktest()` from `data.trades`
- **Guide nav**: redesigned as sidebar-style list on left of drawer (not a cramped top bar); numbered sections with readable text

### Trading Section (sidebar)
- **Leverage input**: `#leverage-input` (1–20x); passed to both market and smart execution
- **Execution mode toggle**: `setExecMode('market'|'smart')` — globals `execMode`, updates button styles and shows/hides smart settings
- **Smart settings panel** (`#smart-settings`): shown only in smart mode; inputs `#passive-s-input`, `#aggressive-s-input`, `#allow-market-input`
- **Pre-trade check**: `fetchPreTradeCheck()` → GET `/api/pre_trade_check` → `renderPreTrade(data)` renders ✓/✗ per check + rounded quantities in `#pretrade-results`
- **Pre-trade market validation**: shared margin asset is required for live trading; `renderPreTrade(data)` shows `USDT-M` / `USDC-M` context and blocks mixed-asset pairs
- **Execution monitor** (`#exec-monitor`): shown during/after smart execution; status badge, per-leg fill %, event log; `cancelCurrentExecution()` sends DELETE
- **Smart execution globals**: `execMode`, `currentExecId`, `execPollTimer`
- **Smart execution flow**: `executeTrade()` routes to `startSmartExecution(side)` in smart mode → POST `/api/trade/smart` → starts `setInterval(pollExecution, 2000)` → `renderExecution(data)` → stops on terminal status
- **History/WS consistency**: `runAnalyze()` stores the active history length in `state.historyLimit`, and `connectWebSocket()` passes that `limit` to `/ws/stream` so live updates are calculated on the same window as the original chart (prevents the last point from jumping on first refresh)

## Guide Drawer
- Triggered by "? Руководство / Guide" button in header
- Fixed right panel (520px), slides in with CSS transform transition
- Backdrop overlay closes on click; `Escape` key also closes
- Content defined in `GUIDE` JS object — bilingual (`ru`/`en`), switches with `currentLang`
- 10 sections: intro, theory, pair-selection, analysis, zscore, stats, backtest, trading, risk, trade-safety
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

## Logging & Persistence

### Logging (`backend/logger.py`)
- `get_logger(name)` returns a logger with two handlers: `StreamHandler` (console) + `RotatingFileHandler`
- Log file: `logs/pair_trading.log` (relative to project root); max 10 MB × 5 rotating files, UTF-8
- Log format: `YYYY-MM-DD HH:MM:SS [LEVEL] name: message`
- Logged events: backend start/stop, leverage set, OPEN/CLOSE trade (pair, qty, prices, z-score, lever, sizing, db_id), trade errors with `exc_info=True`

### SQLite Persistence (`backend/db.py`)
- DB file: `pair_trading.db` (project root, auto-created on first run via `db.init_db()` in lifespan)
- Two tables:
  - `open_positions` — active strategy positions; columns: symbol1/2, side, qty1/2, hedge_ratio, entry_zscore, entry_price1/2, size_usd, sizing_method, leverage, opened_at
  - `closed_trades` — full history; same + exit_price1/2, exit_zscore, pnl, closed_at
- Key functions: `save_open_position(...)` → id, `close_position(id, exit_p1, exit_p2, pnl, exit_zscore)`, `find_open_position(sym1, sym2)` → dict|None, `get_open_positions()`, `get_closed_trades(limit)`
- On `action=open`: position saved with entry prices, z-score from request, `db_id` returned in response
- On `action=close`: DB position found by (sym1, sym2), PnL calculated from entry prices, record moved to `closed_trades`
- Backward-compatible: if no DB record found on close, trades still execute (PnL field is null)

## Order Manager (`order_manager.py`)
State machine: `PLACING → PASSIVE → AGGRESSIVE → FORCING → OPEN` or `→ ROLLBACK → DONE`

- `ExecConfig`: `passive_s` (default 10s), `aggressive_s` (20s), `allow_market` (True), `poll_s` (2s)
- `LegState`: tracks `order_id`, `status` (WAITING/PARTIAL/FILLED/CANCELLED/FAILED), `filled`, `remaining`, `avg_price`; `absorb_order(order_dict)` syncs from ccxt order
- `ExecContext`: holds both legs, config, events log, `cancel_req` flag, `db_id`; `to_dict()` → serializable snapshot for polling
- `run_execution(ctx, client, db_module)` runs as `asyncio.create_task()`:
  1. Fetch both orderbooks, place passive limits simultaneously via `asyncio.gather`
  2. Poll every `poll_s`: check cancel flag → refresh fills → check timeouts
  3. Passive timeout: cancel+replace at taker prices (`_chase_to_taker`)
  4. Aggressive timeout: cancel+market (`_force_market`) → break
  5. Both filled → `OPEN`, save to DB
  6. Partial fill → `ROLLBACK` (close filled leg at market) → `DONE`
- Passive price: buy@bid, sell@ask (maker side, 0% fee on USDC-M)
- Aggressive price: buy@ask, sell@bid (taker side, crosses spread)
- `active_executions` dict in `main.py` maps `exec_id → ExecContext`; never cleaned up (keep for review)
- `exec_id` is first 8 chars of UUID4

## Common Issues & Fixes
- **Empty symbols list**: ccxt returns Binance perpetuals as `type: "swap"`, not `"future"` — filter includes both
- **`pip` not found**: use `.venv/bin/pip` — Homebrew Python blocks system installs
- **CORS errors**: backend has CORS middleware allowing all origins including `file://`
- **NaN/Inf in JSON**: `_clean()` helper in `main.py` recursively strips non-serializable floats
- **Port 5000 on macOS**: reserved by AirPlay Receiver (Control Center) — use port 8080 instead
- **HTTP 400 "notional below minimum"**: position `size_usd` is too small — increase it; minimum depends on the exact contract and margin market (`USDT-M` / `USDC-M`)
- **Mixed pair won't trade**: if one leg is `USDT-M` and the other is `USDC-M`, analysis still works but live trading is rejected until both legs use the same margin asset
- **Leverage set error (warning, not fatal)**: if a position already exists on Binance, `set_leverage` fails — logged as WARNING, trade still proceeds with current exchange leverage
- **`amount_to_precision` KeyError**: markets not loaded — fixed by `_ensure_markets()` guard in `place_order`
- **Smart execution stuck in PASSIVE**: passive_s too long, or orders not visible in `fetch_order` — check log events in execution monitor
- **Rollback FAILED**: market order for the filled leg also failed — requires manual action; logged as ERROR with "MANUAL ACTION REQUIRED"

## User Preferences
- Русский язык по умолчанию в UI
- Dark theme only
- No build tools — keep frontend as single HTML file

## Guide Writing Rules
- **Write for beginners first.** The target user may not know what OLS, ATR, cointegration, or hedge ratio mean. Every technical term must be explained in plain language before showing formulas.
- **Explain the "why" before the "how".** Don't just show a formula — first explain what problem it solves in one sentence (e.g. "You can't buy equal dollars of both assets because they move with different force").
- **Use concrete examples.** Every concept section must include a numerical example with realistic BTC/ETH prices and clear input/output.
- **Avoid jargon without explanation.** If a technical term is unavoidable, immediately follow it with a plain-language parenthetical or sentence.
- Each guide section should be readable by someone who has never traded before.
