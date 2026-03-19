# Pair Trading Dashboard — CLAUDE.md

## Communication with the user

**Keep this file in English.** All future edits to CLAUDE.md should be in English only.

When the user asks for an explanation: keep it **simple and short**; avoid code and jargon; use **plain examples** in the context of this project (pair trading: two correlated assets, spread, entry/exit by z-score, TP/SL). Offer code or technical details only if the user asks for them.

**Professional trader role:** Think about the platform not only as a developer but also as a trader. Before implementing any trading-related feature, ask yourself: "what happens with multiple open positions?", "how does this behave on partial fills?", "what if symbols overlap across pairs?". If you spot an architectural risk — raise it before writing code. Discuss with the user as a beginner trader: explain trading consequences, not just technical ones.

## Changelog

This file needs to be updated with the latest bugs and fixes.
Changes history: [`CHANGELOG.md`](CHANGELOG.md)

## Key Architecture Docs

- **[`docs/POSITION_TRACKING.md`](docs/POSITION_TRACKING.md)** — **Read before touching `order_manager.py`, `db.py`, `main.py` close paths, or `user_data_feed.py`.** Covers: DB as source of truth, close direction/qty rules, reduceOnly, DUST flush, commission, PnL formula, background tasks, averaging, liquidation handling.

## Project Overview

Statistical arbitrage (pair trading) dashboard for Binance Futures with support for both USDT-M and USDC-M perpetuals.
Monitors spread between two correlated assets, calculates cointegration statistics,
runs backtests, and executes live trades via Binance API.

## Project Structure

```
pair_trading/
├── backend/
│   ├── main.py              # FastAPI app — REST endpoints + WebSocket
│   ├── symbol_feed.py       # Binance WS kline + bookTicker feeds (SymbolFeed, BookTickerFeed); feeds PriceCache
│   ├── user_data_feed.py    # Binance User Data Stream — real-time order fill notifications (UserDataFeed)
│   ├── strategy.py          # Pair trading math (cointegration, z-score, backtest)
│   ├── binance_client.py    # ccxt async wrapper for Binance Futures (USDT-M + USDC-M)
│   ├── order_manager.py     # Smart limit-order execution engine (state machine)
│   ├── db.py                # SQLite persistence — open_positions + closed_trades + triggers
│   ├── telegram_bot.py      # Telegram notifications + bot (aiogram v3); lifecycle: setup/start_polling/stop
│   ├── logger.py            # RotatingFileHandler setup → logs/pair_trading.log
│   └── requirements.txt
├── frontend/
│   └── index.html           # Single-file UI (Tailwind + Chart.js, no build step)
├── tests/
│   ├── conftest.py          # sys.path setup + tmp_db fixture (isolated temp SQLite per test)
│   ├── test_strategy.py     # 41 tests — all strategy math
│   ├── test_db.py           # 56 tests — SQLite persistence layer
│   ├── test_helpers.py      # 26 tests — _clean() / _safe_float() JSON helpers
│   ├── test_order_manager.py # 4 tests — Smart v2 repricing / semi-aggressive / dust rules
│   ├── test_price_cache.py  # 35 tests — PriceCache ref-counting, SymbolFeed assembly, wait_any_update
│   ├── test_symbol_feed.py  # 15 tests — SymbolFeed buffer, kline handling, event-driven updates
│   ├── test_watchlist.py    #  8 tests — WatchlistItem model validation
│   ├── test_telegram_bot.py # 56 tests — telegram_bot formatters, config, send(), notify_*
│   └── test_lifespan.py     #  5 tests — asyncio graceful shutdown
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

### Tests

```bash
cd /Users/y.shvydak/Projects/pair_trading
.venv/bin/pytest tests/ -v        # all 283 tests
.venv/bin/pytest tests/test_strategy.py -v   # strategy math only
```

### Virtual Environment

Always use `.venv/bin/python` and `.venv/bin/pip` — system Python is managed by Homebrew and blocks system-wide installs.

```bash
.venv/bin/pip install <package>   # add to requirements.txt afterwards
```

## API Endpoints

| Method | Path                         | Description                                                                                                     |
| ------ | ---------------------------- | --------------------------------------------------------------------------------------------------------------- |
| GET    | `/api/symbols`               | List all active USDT-M and USDC-M perpetual futures                                                             |
| GET    | `/api/history`               | OHLCV + spread/z-score + stats for a pair                                                                       |
| GET    | `/api/backtest`              | Full backtest; params: `sizing_method` (ols/atr/equal), `atr1`, `atr2`, `entry_threshold`, `exit_threshold`     |
| GET    | `/api/status`                | Binance connection status + supported futures balances (USDT + USDC)                                            |
| GET    | `/api/positions`             | Open positions from Binance (requires API keys)                                                                 |
| GET    | `/api/balance`               | Futures balances (all supported assets, or `?asset=USDT` / `USDC`)                                             |
| GET    | `/api/pre_trade_check`       | Validate balance, min notional, lot sizes, leverage before trade                                                |
| POST   | `/api/trade`                 | Place market order pair trade (instant, no retry)                                                               |
| POST   | `/api/trade/smart`           | Start smart limit-order execution in background; returns `exec_id`                                              |
| GET    | `/api/execution/{exec_id}`   | Poll smart execution state (call every 2s)                                                                      |
| DELETE | `/api/execution/{exec_id}`   | Request cancellation of a running smart execution                                                               |
| GET    | `/api/db/positions`          | Open positions saved by the strategy (with entry z-score, hedge ratio, etc.)                                    |
| GET    | `/api/db/history`            | Closed trade history from SQLite (`?limit=100`)                                                                 |
| GET    | `/api/db/positions/enriched` | Open positions from DB enriched with live Binance mark prices + unrealized PnL                                  |
| DELETE | `/api/db/positions/{id}`     | Delete a DB position record (does NOT close exchange positions)                                                 |
| GET    | `/api/all_positions`         | Single endpoint: one Binance call → returns `{strategy_positions: [...enriched], exchange_positions: [...raw]}` |
| GET    | `/api/dashboard`             | **Combined polling**: positions (enriched) + exchange positions + balances + recent alerts in one response      |
| GET    | `/api/triggers`              | All active TP/SL triggers (standalone, independent of positions)                                                |
| POST   | `/api/triggers`              | Create a new trigger: `{symbol1, symbol2, side, type, zscore, tp_smart, timeframe, zscore_window, alert_pct}`  |
| DELETE | `/api/triggers/{id}`         | Cancel an active trigger                                                                                        |
| GET    | `/api/alerts/recent`         | Alert triggers that fired within last N minutes (`?minutes=60`); used by frontend notification center          |
| GET    | `/api/executions`            | All active execution contexts (for inline progress monitoring in position rows)                                 |
| GET    | `/api/executions/history`    | Persisted terminal execution snapshots from SQLite (`?limit=100`)                                               |
| POST   | `/api/watchlist/data`        | Subscribe watchlist pairs to PriceCache; returns current z-score + spread for each pair (legacy HTTP fallback) |
| POST   | `/api/batch/sparklines`      | Batch z-score/spread data for multiple positions; uses PriceCache when available                                |
| WS     | `/ws/stream`                 | Live spread/price/Z-score updates, event-driven on each kline (≤5 s timeout) for the active analysed pair      |
| WS     | `/ws/watchlist`              | Event-driven watchlist z-score/spread feed; replaces 5 s HTTP polling; client sends full list, server pushes   |

### `GET /api/pre_trade_check` — query params

`symbol1`, `symbol2`, `size_usd`, `hedge_ratio`, `sizing_method`, `atr1`, `atr2`, `leverage`
Returns `{ok: bool, checks: [{name, ok, detail}], sizes: {qty1, qty2, rounded_qty1, rounded_qty2, notional1, notional2}, prices: {price1, price2}}`

### `POST /api/trade` — TradeRequest fields

| Field                | Type  | Default | Description                                                  |
| -------------------- | ----- | ------- | ------------------------------------------------------------ |
| `symbol1`, `symbol2` | str   | —       | ccxt or BTCUSDT format                                       |
| `action`             | str   | —       | `"open"` \| `"close"`                                        |
| `side`               | str   | —       | `"long_spread"` \| `"short_spread"`                          |
| `size_usd`           | float | —       | Dollar size of each leg                                      |
| `hedge_ratio`        | float | —       | OLS β from `/api/history`                                    |
| `sizing_method`      | str   | `"ols"` | `"ols"` \| `"atr"` \| `"equal"`                              |
| `atr1`, `atr2`       | float | null    | Required for ATR sizing                                      |
| `leverage`           | int   | `1`     | Futures leverage to set before opening                       |
| `entry_zscore`       | float | null    | Z-score at entry (saved to DB)                               |
| `exit_zscore`        | float | null    | Z-score at exit (saved to DB)                                |
| `timeframe`          | str   | `"1h"`  | Timeframe used for analysis (saved to DB, used in sparkline) |
| `candle_limit`       | int   | `500`   | Candle count for analysis window (saved to DB)               |
| `zscore_window`      | int   | `20`    | Rolling z-score window (saved to DB)                         |

### `POST /api/trade/smart` — SmartTradeRequest fields

Same as TradeRequest plus:
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `action` | str | `"open"` | `"open"` \| `"close"` |
| `passive_s` | float | `30.0` | Total dynamic passive window; starts at best bid/ask and reprices inside the window |
| `aggressive_s` | float | `20.0` | Total semi-aggressive window before market fallback |
| `allow_market` | bool | `true` | Use market order as final fallback |

When `action="close"`: finds DB position by (sym1, sym2), uses actual Binance qty (fallback to DB qty), reverses spread direction, runs smart execution that calls `db.close_position()` on success.

## Key Parameters for `/api/history`

- `symbol1`, `symbol2` — ccxt format, e.g. `BTC/USDT:USDT` or `BTC/USDC:USDC`
- `timeframe` — `5m`, `1h`, `4h`, `1d`
- `limit` — number of candles (default 500, max 1500)
- `zscore_window` — rolling window for z-score (default 20)
- **PriceCache optimization**: if pair is subscribed in PriceCache (watchlist/WS) with `cached_limit >= limit`, data is read from cache instead of fetching from Binance — makes analysis of watchlist pairs near-instant

## Strategy Logic (`strategy.py`)

- **Hedge ratio**: OLS via `numpy.linalg.lstsq` on log prices — `log(P1) = β * log(P2) + α` (replaced statsmodels OLS for ~10x speedup)
- **Spread**: `log(P1) - β * log(P2)`
- **Z-score**: rolling `(spread - mean) / std` with configurable window
- **Cointegration**: Engle-Granger test via `statsmodels.tsa.stattools.coint(maxlag=10)` — fixed maxlag avoids slow auto-selection
- **Half-life**: AR(1) via `numpy.linalg.lstsq` — `half_life = -log(2) / log(φ)` (replaced statsmodels OLS)
- **Hurst exponent**: variogram regression (not R/S despite docstring) — H < 0.5 means mean-reverting
- **Backtest signals**: enter at `|z| > entry_threshold`, exit at `|z| < exit_threshold`; PnL uses selected `sizing_method`
- **ATR**: `calculate_atr(df, period=14)` — average true range from OHLCV DataFrame
- **Position sizing** (`calculate_position_sizes`):
     - `size_usd` = **total position size** (both legs combined, value1 + value2 = size_usd)
     - `ols`: split proportionally by 1 : |β| → `qty1 = size / ((1+|β|)*P1)`, `qty2 = size*|β| / ((1+|β|)*P2)`
     - `atr`: `qty1 = size / (P1 + ratio*P2)`, `qty2 = qty1 * (ATR1/ATR2)` — equal dollar volatility per leg
     - `equal`: `qty1 = size / (2*P1)`, `qty2 = size / (2*P2)`

## Binance Client Notes (`binance_client.py`)

- Symbol format: `BTC/USDT:USDT` or `BTC/USDC:USDC` (ccxt unified format); UI sends `BTCUSDT` → backend normalizes via `_normalise_symbol()`
- Market type filter: `type in ("swap", "future")` — Binance perpetuals show as `swap`, not `future`
- `self.has_creds: bool` — True only if API keys are non-empty and non-placeholder
- `round_amount(symbol, amount)` — rounds to exchange stepSize; called before every order
- `check_min_notional(symbol, amount, price)` — validates against `limits.cost.min` from market data

## Frontend (`frontend/index.html`)

- Single HTML file, no build step, no npm
- Dependencies via CDN: Tailwind CSS, Chart.js 4.4.2, chartjs-plugin-annotation 3.0.1, chartjs-plugin-zoom 2.0.1, hammerjs 2.0.8
- i18n: `I18N` object with `en`/`ru` keys, `t(key)` function, `applyLocale()` on load and lang switch
- Language stored in `localStorage` key `pt_lang`, default `ru`

### Layout

Three-panel trading terminal:

- **Header**: Trade/Backtest mode, Binance status, Guide, language toggle
- **Trade mode**: three columns — Watchlist (left) | Charts (center) | Trading Panel (right, scroll)
- **Backtest mode**: charts with signals + controls + trades table
- **Bottom panel**: resizable drag handle, tabs: Positions | Alerts | Journal
- TP/SL displayed inline in position row (badges), no separate tab

### Key Frontend Patterns

- **Tailwind CDN gotcha**: dynamic classes via `classList.add()` don't work — CDN only generates CSS for classes present in HTML at parse time. Use `element.style.color` with hex constants (`C_GREEN`/`C_YELLOW`/`C_RED`)
- **Active pair highlight**: matches on **5 parameters** (sym1+sym2+timeframe+zscore_window+entryZ); normalized via `_wlNorm()`
- **PnL sub-label under Z**: reads from chart data (`dollarData.at(-1)`), NOT `z*std` formula
- **Sparklines in positions**: `_batchLoadSparklines(positions)` — if pair matches current analysis → reads from `state.historyData`; others → `POST /api/batch/sparklines` in one request (uses PriceCache on backend); throttle 30s per position
- **Trade markers**: use `yScaleID: 'ySpread'`, `yValue` = dollar PnL; timestamps via `_utcParse` (handles both formats: `2026-03-16 18:00:00` and `2026-03-16T18:17:28+00:00`)
- **Analysis state**: saved to `localStorage['pt_last']` after each Analyze; restored on page load
- **i18n**: `I18N` object (en/ru), `t(key)`, `applyLocale()`; tooltip keys: `tip_entry_z`, `tip_exit_z`, `tip_zwindow`
- **Chart zoom**: `chartjs-plugin-zoom` + `hammerjs`; `_syncCharts` syncs Spread and Price charts horizontally
- **Effective spread by sizing method**: `_effectiveSpreadData(data)` returns `{spread, dollarPerUnit}` depending on `sizingMethod` (OLS→β, Equal→β=1, ATR→β=atr1/atr2). Both `renderSpreadChart()` and `refreshLiveCharts()` must use this function — never read `data.spread` (OLS) directly, otherwise `_spreadDollarFactor.meanSpread` and chart data come from different bases → flat line on chart

### Watchlist

- `localStorage['pt_watchlist']` — saves **all analysis parameters** when adding a pair
- **Dedup key**: `(sym1, sym2, timeframe)` — same pair with different timeframe stored as separate entry; adding BTC/ETH 5m does not overwrite BTC/ETH 4h
- **Grouping by timeframe** in `renderWatchlist()`: section headers, order 5m→15m→30m→1h→2h→4h→8h→1d
- Z-score/spread updated **event-driven** via `WS /ws/watchlist` (fires on each Binance kline, max 5s delay); response contains `timeframe` for exact record matching
- `connectWatchlistWS()` opens WS on start; `_sendWatchlistToWs()` sends updated list on add/remove; reconnect with backoff (1s → 30s)
- `_applyWatchlistUpdate(data)` — common handler for incoming data (in-place DOM patch)
- Threshold indication: `|z| >= entryZ*0.75` → yellow; `|z| >= entryZ` → red + blinking
- Active pair highlight updates immediately after `runAnalyze()` (no wait for 5s tick)

### Positions Tab

- **Strategy Positions** (DB+live enriched) + **Exchange Positions** (raw Binance)
- `loadAllPositions()` → `GET /api/dashboard` — auto-refresh every 5s; returns positions + balances + recent alerts in one call; in-place DOM updates (no full rebuild)
- Actions: `↗` load pair | `✕ M` market close | `◎ S` smart close | `🗑` delete DB record
- Row click = `↗`; `pos.tp_smart` defaults `true` for positions without TP

### Trading Section

- Leverage 1–20x; execution mode: market / smart
- Smart settings: passive_s, aggressive_s, allow_market
- Pre-trade check validates balance, notional, lot size; blocks mixed-asset pairs
- `runAnalyze()` stores `state.historyLimit` → WS uses same window for consistency

## Guide Drawer

- Bilingual (ru/en) `GUIDE` JS object — 11 sections with optional `example` objects
- To add a section: add entry to both `GUIDE.ru[]` and `GUIDE.en[]` with `{id, title, content, example?}`

## Environment Variables (`.env`)

```
BINANCE_API_KEY=...
BINANCE_SECRET=...
TELEGRAM_BOT_TOKEN=       # from @BotFather
TELEGRAM_CHAT_ID=         # your chat/user ID (get via @userinfobot)
TELEGRAM_NOTIFY_OPENS=true
TELEGRAM_ALERT_RESET_Z=0.5
```

Public endpoints (symbols, history, backtest) work without API keys.
Private endpoints (positions, balance, trade) require valid keys.

## SymbolFeed (`backend/symbol_feed.py`)

Live OHLCV candle buffer per `(symbol, timeframe)` — the single source of price data for all consumers.

- Connects to `wss://fstream.binance.com/stream?streams={sym}@kline_{tf}` (one WS per symbol×timeframe)
- Loads initial history via REST once on startup; WS updates the current candle in-place or appends when it closes
- Auto-reconnect with exponential backoff (1s → 60s); REST refresh on each reconnect to fill gaps
- `get_dataframe() → pd.DataFrame | None` — returns deque as DataFrame; returns `None` if empty
- `wait_for_update(after_gen) → int` — async, event-driven; safe for N concurrent waiters ("replace event" pattern)
- `start()` / `stop()` — idempotent; `start()` is called from `PriceCache.run()`
- `_to_ws_symbol("BTC/USDT:USDT") → "btcusdt"` — ccxt format → Binance stream name
- Also contains `BookTickerFeed` — real-time best bid/ask via `{sym}@bookTicker`; `get_best() → (bid, ask)` or `(None, None)` before first message; same reconnect/start/stop/"replace event" pattern as SymbolFeed

## UserDataFeed (`backend/user_data_feed.py`)

Real-time order fill notifications via Binance Futures User Data Stream.

- `start() → bool` — returns False if no API credentials (graceful no-op)
- `register_order(order_id)` / `unregister_order(order_id)` — called around each limit order placed by order_manager
- `wait_for_order_update(after_gen) → int` — "replace event" pattern; wakes `_wait_for_fill_or_timeout` immediately on fill
- Keepalive loop runs every 30 min (listen key expires at 60 min)
- **`ACCOUNT_UPDATE` events**: LIQUIDATION → `_handle_liquidation` (sets `status=liquidated` in DB + Telegram); ADL → `_handle_adl`; FUNDING_FEE → `_handle_funding` (proportional distribution by notional across open positions)
- Commission in `ORDER_TRADE_UPDATE`: field `"n"` is per-fill delta; UserDataFeed accumulates to cumulative total per order in `_fill_data`
- Callbacks registered in lifespan: `on_liquidation(cb)`, `on_adl(cb)`, `on_funding(cb)`
- `_book_feeds: dict[str, BookTickerFeed]` in `main.py` — lazily created per symbol when smart execution starts; all stopped on lifespan shutdown

## Price Cache (`backend/main.py` — class `PriceCache`)

Centralised pair-level cache, assembled from SymbolFeed buffers. Single source of truth for all live consumers.

> **ARCHITECTURAL PRINCIPLE — always follow:**
> All live data (chart WS, watchlist WS, TP/SL monitor) reads from PriceCache, which is fed by Binance WS kline streams via SymbolFeed.
> Direct `client.fetch_ohlcv()` calls are only allowed in two cases:
>
> 1. Historical data for analysis/backtest (`/api/history`, `/api/backtest`)
> 2. Initial cache fill on cache miss (seed in watchlist HTTP endpoint)
>
> **Never create new polling timers on the frontend for live data — use `/ws/watchlist` or `/ws/stream`.**

- **Key**: `(sym1, sym2, timeframe, limit)` — one entry per unique pair config
- **Entry**: `{"price1": pd.Series, "price2": pd.Series, "df1": DataFrame, "df2": DataFrame}` — close prices + full OHLCV DataFrames, aligned on common timestamps
- **Symbol-level dedup**: `_feeds: dict[(sym, tf), SymbolFeed]` — BTC in 10 pairs = 1 WS connection; `_feed_refs` counts how many pair-keys use each feed
- `price_cache.subscribe(sym1, sym2, tf, limit) → key` — creates SymbolFeeds if needed; ref-counted
- `price_cache.unsubscribe(key)` — decrements refs; stops SymbolFeed when its ref count reaches 0; removes `_store[key]`
- `price_cache.get(key) → dict | None` — read-only; `None` if not yet assembled
- `price_cache.find_cached(sym1, sym2, tf, limit) → dict | None` — finds entry with matching `(sym1, sym2, tf)` and key limit `>= requested`; always verify `len(cached["price1"]) >= limit` before use — buffer may be incomplete shortly after server start
- `price_cache.run()` — background task in lifespan; calls `feed.start()` on all registered feeds; reassembles all pair stores every `ASSEMBLE_INTERVAL = 1s`
- `price_cache.wait_update(key, timeout=5.0)` — waits for next kline on either symbol of the pair; used by `/ws/stream` for event-driven push
- `price_cache.wait_any_update(keys, timeout=5.0)` — waits for ANY kline across a list of pairs (deduplicates feeds); used by `/ws/watchlist`
- `price_cache.stop_all()` — stops all SymbolFeed tasks; called in lifespan shutdown before `client.close()`
- **`/ws/stream`**: subscribes on connect, pushes on each kline event (`wait_update`, ≤5s timeout), unsubscribes in `finally`
- **`/ws/watchlist`**: per-connection subscriptions; two tasks: `_receive_task` reconciles pairs, `_push_task` pushes on `wait_any_update`; unsubscribes all on disconnect
- **`monitor_position_triggers`**: manages own `_monitor_keys: dict[tag → cache_key]` (local to the coroutine); subscribes active positions/triggers to PriceCache; reads from cache each 2s cycle — zero direct Binance calls
- **`POST /api/watchlist/data`** (HTTP, legacy): maintains module-level `_watchlist_keys`; on cache miss seeds `price_cache._store[key]` directly and spawns `_precompute_coint` background task

## Logging & Persistence

### Logging (`backend/logger.py`)

- `get_logger(name)` returns a logger with two handlers: `StreamHandler` (console) + `RotatingFileHandler`
- Log file: `logs/pair_trading.log` (relative to project root); max 10 MB × 5 rotating files, UTF-8
- Log format: `YYYY-MM-DD HH:MM:SS [LEVEL] name: message`

### SQLite Persistence (`backend/db.py`)

- DB file: `pair_trading.db` (project root, auto-created on first run via `db.init_db()` in lifespan)
- **Six tables**: `open_positions`, `closed_trades`, `triggers`, `execution_history`, `position_legs`, `funding_history`
- `open_positions` has `status` column: `open` | `partial_close` | `liquidated` | `adl_detected`; also `coint_pvalue`, `coint_checked_at`
- `find_open_position(sym1, sym2)` — excludes `liquidated`/`adl_detected`; returns `partial_close` (user may still want to close it)
- Position leg functions: `save_position_leg(...)`, `get_position_legs(pos_id)`, `close_position_legs(pos_id)`, `add_position_entry(pos_id, leg_number, new_qty, new_price)` — updates weighted avg price in `open_positions`
- Funding functions: `save_funding_history(pos_id, symbol, amount, asset)`, `get_funding_total(pos_id) → float`
- Status/health: `set_position_status(pos_id, status)`, `update_position_coint_health(pos_id, pvalue)`
- `close_position(...)` accepts `commission` and `commission_asset` kwargs; saved to `closed_trades`
- Key functions: `save_open_position(...)` → id, `close_position(...)`, `find_open_position(sym1, sym2)`, `get_open_positions()`, `get_closed_trades(limit)`, `delete_open_position(id)`
- Trigger functions: `save_trigger(...)` → id, `get_active_triggers()`, `cancel_trigger(id)`, `trigger_fired(id)`, `find_active_alert(sym1, sym2, zscore)` → dict|None, `alert_fired(id)`, `get_recent_alerts(minutes=60)` → list[dict]
- Execution history: `save_execution_history(...)` — `INSERT OR IGNORE` (idempotent); `get_execution_history(limit=100)`
- `save_open_position` raises `ValueError` if a position for (symbol1, symbol2) already exists — prevents duplicates
- On `action=open`: validates notional FIRST, then sets leverage, then places orders; qty saved is `order.get("amount")` (actual rounded qty from Binance)
- On `action=close`: DB position found by (sym1, sym2), PnL calculated from entry prices, record moved to `closed_trades`
- Triggers survive position deletion — user manages them explicitly via `/api/triggers` endpoints

## Order Manager (`order_manager.py`)

State machine: `PLACING → PASSIVE → AGGRESSIVE → FORCING → OPEN` or `→ ROLLBACK → DONE`

- **Smart v2**: PASSIVE = dynamic (best bid/ask, reprices every `reprice_s=4s`); AGGRESSIVE = semi-aggressive (25% into spread); FORCING = market fallback for residuals
- `ExecConfig`: `passive_s` (default **30s**), `aggressive_s` (20s), `allow_market` (True), `poll_s` (2s), `reprice_s` (**4s**)
- Both legs `FILLED`/`DUST` → `OPEN` (saves/closes DB record)
- **Open** partial: one leg filled, other not → `ROLLBACK` (market close of filled leg, `reduceOnly=True`) → `DONE`
- **Close** partial: one leg closed, other not → `partial_close` status in DB + Telegram alert; **no re-open** → `DONE`
- `ExecContext.is_close=True, close_db_id=N` — close mode; all orders get `reduceOnly=True`
- `ExecContext.is_average=True, average_position_id=N` — averaging mode; calls `add_position_entry` on success
- `clientOrderId = PT_{pos_id}_{leg}_{exec_id}` (max 36 chars) on every order — for crash recovery via `_reconcile_on_startup`
- DUST flush after close: `reduceOnly` market order for remainder; recalculates `leg.avg_price` as weighted average before saving PnL
- Commission: `LegState.commission` uses `max(self.commission, incoming)` — UserDataFeed stores cumulative per order; safe for both WS and REST sources
- `DUST` = remaining qty below exchange minimum; partial fill accepted, no new order for residual
- `_fetch_orderbooks` — prefers `BookTickerFeed.get_best()`, REST fallback; `_refresh_fills` — prefers UserDataFeed WS snapshot, REST fallback
- `active_executions` in `main.py`: terminal entries cleaned after 2h TTL; persisted to `execution_history` before cleanup via `_exec_saved_to_db` set
- `exec_id` is first 8 chars of UUID4

## Pre-trade Check & Monitor

- Balance check: `required_margin = size_usd / leverage * 1.1` — initial margin with 10% buffer
- Validation order: balance → min_notional → lot_size → leverage (informational)
- **monitor_position_triggers** runs every **2s** — reads from PriceCache (zero direct Binance calls); subscribes active positions/triggers, unsubscribes stale ones each cycle
- **Direction-agnostic TP/SL**: uses `abs(current_z)` — TP when `abs_z <= tp`, SL when `abs_z >= sl`; values always positive
- **Double-close prevention**: `closing_tags` (tag-based) + `closing_pairs` (pair-based) — prevents same pair closed simultaneously by position TP and standalone trigger
- **Cointegration cache**: `_coint_cache` with 10-min TTL; **background precompute** via `_precompute_coint` — watchlist pairs analyze instantly after ~15s priming
- `_run_sync(func, *args)` — CPU-bound functions run in thread-pool via `run_in_executor`
- **`reconcile_positions`** — background task every 5 min; compares DB positions vs exchange; detection only, no auto-fix; Telegram alert on mismatch
- **`health_check_coint`** — background task every 4h (120s initial delay); re-runs cointegration test per open position using PriceCache (REST fallback if not cached); updates `coint_pvalue` in DB; Telegram alert if p-value > 0.05
- **`_reconcile_on_startup`** — runs once on server start; queries Binance for open orders with `PT_` prefix to detect orphaned orders from crashed sessions; logs only, no auto-cancel
- **`_enrich_positions(db_positions, live_map)`** — single helper used by all enriched endpoints; PnL from `db["qty"] × (mark_price − entry_price)`, never from exchange position size — safe for overlapping symbols

## Telegram Bot (`telegram_bot.py`)

Lifecycle: `setup()` → `create_task(start_polling())` → `stop()` (on shutdown)

Notification functions: `notify_position_opened`, `notify_position_closed`, `notify_trigger_fired`, `notify_alert`, `notify_rollback`, `notify_execution_failed` — all non-blocking via `_fire()` → `create_task(send())`; `send()` never raises.

### Alert triggers (`type="alert"` in `triggers` table)

- Fires when `abs(current_z) >= alert_pct * abs(trig_z)`; stays `status="active"` — never auto-cancelled
- **Hysteresis**: `"idle"` → fires → `"alerted"` → `abs(z) <= ALERT_RESET_Z` → `"idle"` (ready to fire again)
- Created via 🔔 on watchlist item or `addAlertFromPanel()` button in Pair Config panel
- **Notification center**: `checkRecentAlerts()` piggybacked on the 5s positions interval (not a separate timer)

## Common Issues & Fixes

- **Position tracking architecture**: all issues from `POSITION_TRACKING_ISSUES.md` have been resolved. See [`docs/POSITION_TRACKING.md`](docs/POSITION_TRACKING.md) for current architecture
- **`partial_close` position in UI**: orange badge shown on position row; remaining leg must be closed manually on exchange — platform cannot auto-close it
- **Cointegration health dot is empty**: normal for first 4h after server start — `health_check_coint` has 120s initial delay then 4h interval
- **Empty symbols list**: ccxt returns Binance perpetuals as `type: "swap"`, not `"future"` — filter includes both
- **`pip` not found**: use `.venv/bin/pip` — Homebrew Python blocks system installs
- **CORS errors**: backend has CORS middleware allowing all origins including `file://`
- **NaN/Inf in JSON**: `_clean()` helper in `main.py` recursively strips non-serializable floats
- **Port 5000 on macOS**: reserved by AirPlay Receiver — use port 8080 instead
- **HTTP 400 "notional below minimum"**: `size_usd` too small; minimum depends on contract/margin market
- **Mixed pair won't trade**: one leg USDT-M + other USDC-M → analysis works, live trading blocked
- **Leverage set error (warning, not fatal)**: position already exists on Binance → WARNING, trade proceeds with current leverage
- **Smart execution stuck in PASSIVE**: intentionally dynamic up to `30s`; check events for `Reprice ... (passive)`
- **Rollback FAILED**: market order for filled leg also failed → manual action required; logged as ERROR "MANUAL ACTION REQUIRED"
- **Strategy Positions shows position but Exchange Positions is empty**: DB/exchange desync — use 🗑 to remove stale record or `✕ M` (backend detects no open positions and cleans DB)
- **Trade markers not showing on chart**: `loadTradeJournal()` must be called after `runAnalyze()` to populate `_cachedJournalTrades`; DB timestamps use `+00:00` format — `_utcParse` must handle timezone suffix without appending extra `Z`
- **Active pair highlight**: compares **5 params**: sym1+sym2+timeframe+zscore_window+entryZ — ticker alone is insufficient
- **`_pollAllExecutions` auto-opens popups**: `_execSeenIds` Set tracks shown popups; `_execFirstPoll` flag prevents opening old terminal popups on page reload; adaptive frequency: 2s with active executions, 5s idle (`setTimeout`-based)
- **TP fires immediately for short_spread**: fixed with `abs(current_z)` — direction-agnostic
- **Double close on TP fire**: fixed with `closing_pairs` set tracking `(sym1, sym2)`
- **TP/SL input only accepts positive numbers**: correct behavior with direction-agnostic logic — chart shows symmetric lines at ±threshold
- **Tailwind CDN dynamic classes don't work**: CDN only generates CSS for classes present in HTML at parse time — use `element.style.color` with explicit hex values (`C_GREEN`/`C_YELLOW`/`C_RED` constants)

## Tests (`tests/`)

283 unit tests (10 files), all pass in ~4–5s. Run: `.venv/bin/pytest tests/ -v`

| File                    | Tests | Coverage                                                    |
| ----------------------- | ----- | ----------------------------------------------------------- |
| `test_strategy.py`      | 41    | spread, zscore, sizing (OLS/ATR/Equal), signals, ATR, half-life, Hurst, coint, backtest |
| `test_db.py`            | 79    | positions, triggers, trade journal, duplicate guard, alert triggers, execution_history, position_legs, funding_history, coint_health, status |
| `test_helpers.py`       | 26    | `_clean()` / `_safe_float()` — NaN/Inf/np.float64 serialization |
| `test_order_manager.py` | 18    | Smart v2 repricing, semi-aggressive, dust, reduceOnly on close, clientOrderId, commission, partial_close rollback, DUST flush avg_price |
| `test_price_cache.py`   | 35    | subscribe/unsubscribe ref-counting, `find_cached`, `wait_update`, `wait_any_update`, `stop_all` |
| `test_symbol_feed.py`   | 15    | buffer update/append, `wait_for_update`, `start` idempotency |
| `test_watchlist.py`     | 8     | WatchlistItem Pydantic model validation                     |
| `test_telegram_bot.py`  | 56    | formatters, `send()` safety, all `notify_*` functions       |
| `test_lifespan.py`      | 5     | asyncio graceful shutdown pattern                           |

**`conftest.py`** — `tmp_db` fixture: `monkeypatch.setattr(db, "DB_PATH", tmp_path/"test.db")` + `db.init_db()` — isolated DB per test.

## User Preferences

- Russian language UI by default
- Dark theme only
- No build tools — keep frontend as single HTML file

## Guide Writing Rules

- **Write for beginners first.** Every technical term must be explained in plain language before showing formulas.
- **Explain the "why" before the "how".** First explain what problem it solves, then the formula.
- **Use concrete examples.** Every concept section must include a numerical example with realistic BTC/ETH prices.
- **Avoid jargon without explanation.** If unavoidable, immediately follow with a plain-language parenthetical.
