# Pair Trading Dashboard — CLAUDE.md

## Communication with the user

When the user asks for an explanation: keep it **simple and short**; avoid code and jargon; use **plain examples** in the context of this project (pair trading: two correlated assets, spread, entry/exit by z-score, TP/SL). Offer code or technical details only if the user asks for them.

## Changelog

This file need to be updated with the latest bugs and fixes.
Changes history: [`CHANGELOG.md`](CHANGELOG.md)

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
│   ├── db.py                # SQLite persistence — open_positions + closed_trades + triggers
│   ├── telegram_bot.py      # Telegram notifications + bot (aiogram v3); lifecycle: setup/start_polling/stop
│   ├── logger.py            # RotatingFileHandler setup → logs/pair_trading.log
│   └── requirements.txt
├── frontend/
│   └── index.html           # Single-file UI (Tailwind + Chart.js, no build step)
├── tests/
│   ├── conftest.py          # sys.path setup + tmp_db fixture (isolated temp SQLite per test)
│   ├── test_strategy.py     # 40 tests — all strategy math
│   ├── test_db.py           # 55 tests — SQLite persistence layer
│   ├── test_helpers.py      # 26 tests — _clean() / _safe_float() JSON helpers
│   ├── test_order_manager.py # 4 tests — Smart v2 repricing / semi-aggressive / dust rules
│   ├── test_price_cache.py  # 17 tests — PriceCache ref-counting
│   ├── test_watchlist.py    #  8 tests — WatchlistItem model validation
│   └── test_telegram_bot.py # 56 tests — telegram_bot formatters, config, send(), notify_*
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
.venv/bin/pytest tests/ -v        # all 213 tests
.venv/bin/pytest tests/test_strategy.py -v   # strategy math only
```

### Virtual Environment

Always use `.venv/bin/python` and `.venv/bin/pip` — system Python is managed by Homebrew and blocks system-wide installs.

```bash
# Install new packages:
.venv/bin/pip install <package>
# Add to requirements.txt afterwards
```

## API Endpoints

| Method | Path                         | Description                                                                                                     |
| ------ | ---------------------------- | --------------------------------------------------------------------------------------------------------------- |
| GET    | `/api/symbols`               | List all active USDT-M and USDC-M perpetual futures                                                             |
| GET    | `/api/history`               | OHLCV + spread/z-score + stats for a pair                                                                       |
| GET    | `/api/backtest`              | Full backtest with equity curve and trades                                                                      |
| GET    | `/api/status`                | Binance connection status + supported futures balances (USDT + USDC)                                            |
| GET    | `/api/positions`             | Open positions from Binance (requires API keys)                                                                 |
| GET    | `/api/balance`               | Futures balances (all supported assets, or `?asset=USDT` / `USDC`)                                              |
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
| GET    | `/api/triggers`              | All active TP/SL triggers (standalone, independent of positions)                                                |
| POST   | `/api/triggers`              | Create a new trigger: `{symbol1, symbol2, side, type, zscore, tp_smart, timeframe, zscore_window, alert_pct}`   |
| DELETE | `/api/triggers/{id}`         | Cancel an active trigger                                                                                        |
| GET    | `/api/alerts/recent`         | Alert triggers that fired within last N minutes (`?minutes=60`); used by frontend notification center           |
| GET    | `/api/executions`            | All active execution contexts (for inline progress monitoring in position rows)                                 |
| GET    | `/api/executions/history`    | Persisted terminal execution snapshots from SQLite (`?limit=100`)                                               |
| POST   | `/api/watchlist/data`        | Subscribe watchlist pairs to PriceCache; returns current z-score + spread for each pair                         |
| WS     | `/ws/stream`                 | Live spread/price/Z-score updates every 2 seconds for the active analysed pair                                  |

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
     - `size_usd` = **total position size** (both legs combined, value1 + value2 = size_usd)
     - `ols`: split proportionally by 1 : |β| → `qty1 = size / ((1+|β|)*P1)`, `qty2 = size*|β| / ((1+|β|)*P2)`
     - `atr`: `qty1 = size / (P1 + ratio*P2)`, `qty2 = qty1 * (ATR1/ATR2)` — equal dollar volatility per leg
     - `equal`: `qty1 = size / (2*P1)`, `qty2 = size / (2*P2)`

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
- Dependencies via CDN: Tailwind CSS, Chart.js 4.4.2, chartjs-plugin-annotation 3.0.1, chartjs-plugin-zoom 2.0.1, hammerjs 2.0.8
- i18n: `I18N` object with `en`/`ru` keys, `t(key)` function, `applyLocale()` on load and lang switch
- Language stored in `localStorage` key `pt_lang`, default `ru`

### Layout

Трёхпанельный торговый терминал:

- **Header**: Trade/Backtest mode, Binance status, Guide, язык
- **Trade mode**: три колонки — Watchlist (left) | Charts (center) | Trading Panel (right, scroll)
- **Backtest mode**: charts с сигналами + контролы + таблица сделок
- **Bottom panel**: resizable drag handle, вкладки: Позиции | Alerts | Журнал
- TP/SL отображаются inline в строке позиции (badges), отдельной вкладки нет

### Key Frontend Patterns

- **Tailwind CDN gotcha**: динамические классы через `classList.add()` не работают — CDN генерирует CSS только для классов в HTML. Используй `element.style.color` с hex-константами (`C_GREEN`/`C_YELLOW`/`C_RED`)
- **Active pair highlight**: совпадение по **5 параметрам** (sym1+sym2+timeframe+zscore_window+entryZ); нормализация через `_wlNorm()`
- **PnL sub-label under Z**: читает из данных графика (`dollarData.at(-1)`), NOT `z*std` формула
- **Sparklines в позициях**: если пара совпадает с текущим анализом → читает из `state.historyData` (нет API-запроса); иначе → `GET /api/history` с параметрами из БД
- **Trade markers**: используют `yScaleID: 'ySpread'`, `yValue` = dollar PnL; timestamps через `_utcParse` (обрабатывает оба формата: `2026-03-16 18:00:00` и `2026-03-16T18:17:28+00:00`)
- **Analysis state**: сохраняется в `localStorage['pt_last']` после каждого Analyze; восстанавливается при загрузке
- **i18n**: `I18N` object (en/ru), `t(key)`, `applyLocale()`; tooltip keys: `tip_entry_z`, `tip_exit_z`, `tip_zwindow`
- **Chart zoom**: `chartjs-plugin-zoom` + `hammerjs`; `_syncCharts` синхронизирует Spread и Price горизонтально
- **Effective spread по методу сайзинга**: `_effectiveSpreadData(data)` возвращает `{spread, dollarPerUnit}` в зависимости от `sizingMethod` (OLS→β, Equal→β=1, ATR→β=atr1/atr2). `renderSpreadChart()` и `refreshLiveCharts()` **оба** должны использовать эту функцию — нельзя брать `data.spread` (OLS) напрямую, иначе `_spreadDollarFactor.meanSpread` и данные окажутся из разных баз → плоская линия на графике

### Watchlist

- `localStorage['pt_watchlist']` — сохраняет **все параметры анализа** при добавлении пары
- **Ключ дедупликации**: `(sym1, sym2, timeframe)` — одна и та же пара с разным таймфреймом хранится как отдельная запись; добавление BTC/ETH 5m не перезаписывает BTC/ETH 4h
- **Группировка по таймфреймам** в `renderWatchlist()`: заголовки-разделители, порядок 5m→15m→30m→1h→2h→4h→8h→1d
- Z-score/spread обновляются каждые **5 сек** через `POST /api/watchlist/data` (PriceCache); ответ содержит `timeframe` для точного матчинга записей
- Threshold индикация: `|z| >= entryZ*0.75` → жёлтый; `|z| >= entryZ` → красный + мигание
- Подсветка активной пары обновляется сразу после `runAnalyze()` (не ждёт 5s тик)

### Positions Tab

- **Strategy Positions** (DB+live enriched) + **Exchange Positions** (raw Binance)
- `loadAllPositions()` → `GET /api/all_positions` — auto-refresh каждые 5 с; in-place DOM updates (без полного rebuild)
- Actions: `↗` load pair | `✕ M` market close | `◎ S` smart close | `🗑` delete DB record
- Row click = `↗`; `pos.tp_smart` defaults `true` для позиций без TP

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
```

Public endpoints (symbols, history, backtest) work without API keys.
Private endpoints (positions, balance, trade) require valid keys.

## Price Cache (`backend/main.py` — class `PriceCache`)

Centralised OHLCV data feed shared by **WebSocket** and **watchlist** consumers.

> **АРХИТЕКТУРНЫЙ ПРИНЦИП — соблюдать всегда:**
> WebSocket и watchlist читают live-данные из PriceCache.
> Монитор TP/SL (`monitor_position_triggers`) делает **прямые** `fetch_ohlcv` каждые 2 с — он намеренно NOT использует PriceCache, чтобы не быть ограниченным его циклом обновления.
> Прямые вызовы `client.fetch_ohlcv()` допустимы только в трёх случаях:
>
> 1.   Исторические данные для анализа/бэктеста (`/api/history`, `/api/backtest`)
> 2.   Первичное заполнение кэша, если он ещё пуст (seed на первом вызове)
> 3.   Монитор TP/SL — маленькие запросы (`min(candle_limit, 500)` свечей) каждые 2 с
>      **Никогда не создавай новые polling-таймеры на фронтенде, делающие прямые запросы к `/api/history` для получения live-данных — всегда используй `/api/watchlist/data` или WebSocket.**

- **Key**: `(sym1, sym2, timeframe, limit)` — one entry per unique pair config
- **Entry**: `{"price1": pd.Series, "price2": pd.Series}` — raw close prices, aligned
- `price_cache.subscribe(sym1, sym2, tf, limit) → key` — registers pair; reference-counted
- `price_cache.unsubscribe(key)` — decrements ref; entry removed when count reaches 0
- `price_cache.get(key) → dict | None` — read without network call; `None` if not yet populated
- `price_cache.run()` — background `asyncio.Task` started in `lifespan`; refreshes all subscribed keys every **2 s** (`FEED_INTERVAL = 2`) via `fetch_ohlcv × 2`
- **WebSocket** (`/ws/stream`): subscribes on connect, reads from cache each **2 s** loop, unsubscribes in `finally`
- **monitor_position_triggers**: does **NOT** use PriceCache; fetches OHLCV directly every 2 s with limit `min(candle_limit, 500)` (reads `candle_limit` from DB position); no `monitored_keys` dict
- **Watchlist** (`POST /api/watchlist/data`): maintains `_watchlist_keys: dict[tag → cache_key]` at module level; subscribes each pair on first call, unsubscribes pairs removed from watchlist; frontend polls every 5 s — one request for the whole watchlist; on cache miss seeds `price_cache._store[key]` directly before returning

## Logging & Persistence

### Logging (`backend/logger.py`)

- `get_logger(name)` returns a logger with two handlers: `StreamHandler` (console) + `RotatingFileHandler`
- Log file: `logs/pair_trading.log` (relative to project root); max 10 MB × 5 rotating files, UTF-8
- Log format: `YYYY-MM-DD HH:MM:SS [LEVEL] name: message`
- Logged events: backend start/stop, leverage set, OPEN/CLOSE trade (pair, qty, prices, z-score, lever, sizing, db_id), trade errors with `exc_info=True`

### SQLite Persistence (`backend/db.py`)

- DB file: `pair_trading.db` (project root, auto-created on first run via `db.init_db()` in lifespan)
- Four tables:
     - `open_positions` — active strategy positions; columns: symbol1/2, side, qty1/2, hedge_ratio, entry_zscore, entry_price1/2, size_usd, sizing_method, leverage, tp_zscore, sl_zscore, tp_smart, timeframe, candle_limit, zscore_window, opened_at
     - `closed_trades` — full history; same + exit_price1/2, exit_zscore, pnl, closed_at
     - `triggers` — standalone TP/SL/alert orders, independent of positions; columns: symbol1/2, side, type (tp|sl|alert), zscore, tp_smart, status (active|triggered|cancelled), timeframe, zscore_window, alert_pct, last_fired_at, created_at, triggered_at
     - `execution_history` — persisted terminal smart execution snapshots; columns: exec_id (UNIQUE), db_id, close_db_id, is_close, status, symbol1/2, data_json, completed_at
- Key functions: `save_open_position(...)` → id, `close_position(...)`, `find_open_position(sym1, sym2)`, `get_open_positions()`, `get_closed_trades(limit)`, `delete_open_position(id)`
- Trigger functions: `save_trigger(sym1, sym2, side, type, zscore, tp_smart, timeframe, zscore_window, alert_pct)` → id, `get_active_triggers()`, `get_triggers_for_pair(sym1, sym2)`, `cancel_trigger(id)`, `trigger_fired(id)`, `find_active_alert(sym1, sym2, zscore)` → dict|None, `alert_fired(id)` → bool (sets `last_fired_at`, keeps status=active), `get_recent_alerts(minutes=60)` → list[dict]
- Execution history functions: `save_execution_history(exec_id, db_id, close_db_id, is_close, status, symbol1, symbol2, data_json)` — `INSERT OR IGNORE` (idempotent); `get_execution_history(limit=100)` → list[dict] ordered by `completed_at DESC`
- Triggers survive position deletion — user manages them explicitly via `/api/triggers` endpoints
- `save_open_position` raises `ValueError` if a position for (symbol1, symbol2) already exists — prevents duplicate DB records
- On `action=open`: validates notional FIRST, then sets leverage, then places orders; qty saved is `order.get("amount")` (actual rounded qty from Binance, not pre-rounding calculated value)
- On `action=close`: DB position found by (sym1, sym2), PnL calculated from entry prices, record moved to `closed_trades`
- Backward-compatible: if no DB record found on close, trades still execute (PnL field is null)

## Order Manager (`order_manager.py`)

State machine: `PLACING → PASSIVE → AGGRESSIVE → FORCING → OPEN` or `→ ROLLBACK → DONE`

- **Smart v2 (balanced profile) is now live**:
     - `PASSIVE` = dynamic passive: places at best bid/ask, checks every `poll_s=2s`, reprices at most once per `reprice_s=4s`
     - `AGGRESSIVE` = semi-aggressive: still uses limit orders, but moves to `25%` into the spread instead of jumping straight to taker
     - `FORCING` = market fallback only for still-placeable residual size
- `ExecConfig`: `passive_s` (default **30s**), `aggressive_s` (20s), `allow_market` (True), `poll_s` (2s), `reprice_s` (**4s**)
- `LegState`: tracks `order_id`, `status` (`WAITING`/`PARTIAL`/`FILLED`/`DUST`/`CANCELLED`/`FAILED`), `filled`, `remaining`, `avg_price`, `working_price`, `last_reprice_at`, `hold_until_stage_end`; `absorb_order(order_dict)` syncs from ccxt order
- `ExecContext`: holds both legs, config, events log, `cancel_req` flag, `db_id`, `is_close`, `close_db_id`, `entry_price1/2`, `exit_zscore`; `to_dict()` includes `is_close`; OPEN terminal state branches on `is_close` to call `close_position()` vs `save_open_position()`
- `run_execution(ctx, client, db_module)` runs as `asyncio.create_task()`: places passive limits → polls/reprices within `passive_s` window → rebuilds as semi-aggressive within `aggressive_s` window → market fallback for residuals. Both legs `FILLED`/`DUST` → `OPEN` (saves/closes DB record); one exposed leg partial → `ROLLBACK` (close filled leg at market) → `DONE`.
- Passive price: buy@bid, sell@ask (maker side, 0% fee on USDC-M)
- Semi-aggressive price: buy at `bid + 25% of spread`, sell at `ask - 25% of spread`
- `DUST` means the leg already has a partial fill, but the remaining qty is below exchange minimum; the partial fill is accepted and persisted, and no new order is placed for the residual
- `active_executions` dict in `main.py` maps `exec_id → ExecContext`; terminal entries (DONE/CANCELLED/FAILED/**OPEN**) are cleaned up after 2h TTL in the monitor loop; before cleanup, each terminal entry is persisted to `execution_history` via `_exec_saved_to_db` set (ensures single write)
- `_exec_saved_to_db: set[str]` — tracks which exec_ids have already been written to DB; `discard(eid)` on TTL removal
- `_exec_created_at: dict[str, float]` — monotonic timestamps for TTL cleanup
- `exec_id` is first 8 chars of UUID4

## Pre-trade Check (`GET /api/pre_trade_check`)

- Balance check formula: `required_margin = size_usd / leverage * 1.1` — initial margin with 10% buffer
- Order of validation: balance → min_notional → lot_size → leverage (informational)
- monitor_position_triggers runs every **2 s** — direct `fetch_ohlcv` with limit `min(candle_limit, 500)`; uses `pos.timeframe`/`pos.zscore_window`/`pos.candle_limit` from DB (not hardcoded constants)
- Strategy Positions table shows `liq_price1`/`liq_price2` in orange — from `/api/db/positions/enriched`

## Telegram Bot (`telegram_bot.py`)

### Configuration (`.env`)

```
TELEGRAM_BOT_TOKEN=       # from @BotFather
TELEGRAM_CHAT_ID=         # your chat/user ID (get via @userinfobot)
TELEGRAM_NOTIFY_OPENS=true        # send notification when position is opened
TELEGRAM_ALERT_RESET_Z=0.5        # abs(z) below which alert state resets (ready to fire again)
```

### Lifecycle (integrated in `main.py` lifespan)

```python
await tg_bot.setup()                           # init Bot + Dispatcher
asyncio.create_task(tg_bot.start_polling())    # long-polling background task
# ... after yield ...
await tg_bot.stop()                            # stop polling + close session
```

### Notification functions

| Function                                                                                | When called                                                          | Respects toggle         |
| --------------------------------------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------- |
| `notify_position_opened(sym1, sym2, side, entry_z, price1, price2, size_usd, leverage)` | Market open (main.py) + smart open terminal state (order_manager.py) | `TELEGRAM_NOTIFY_OPENS` |
| `notify_position_closed(sym1, sym2, side, pnl, exit_z, reason)`                         | `_do_market_close`, `/api/trade` close, smart close terminal state   | always                  |
| `notify_trigger_fired(sym1, sym2, side, trigger_type, current_z, threshold_z)`          | Monitor — before TP/SL close starts                                  | always                  |
| `notify_alert(sym1, sym2, current_z, threshold_z)`                                      | Monitor — alert trigger at `alert_pct * trig_z` threshold            | always                  |
| `notify_rollback(sym1, sym2, exec_id)`                                                  | order_manager.py — partial fill rollback                             | always                  |
| `notify_execution_failed(sym1, sym2, exec_id, reason)`                                  | order_manager.py — unrecoverable error                               | always                  |

- `_fire(text)` — schedules `asyncio.create_task(send(text))`, non-blocking
- `send(text)` — never raises; exceptions are logged and swallowed so trading is unaffected

### Alert triggers (`type="alert"` in `triggers` table)

- Created via 🔔 button on watchlist items → `POST /api/triggers`; user chooses `alert_pct` (1–200%, default 100%) via browser prompt
- Stored with `timeframe`, `zscore_window`, `alert_pct` from the watchlist item at creation time
- Monitor subscribes to PriceCache using each trigger's own `timeframe`/`zscore_window` (not global `_MONITOR_TIMEFRAME`)
- Fires when `abs(current_z) >= alert_pct * abs(trig_z)`
- **Dedup**: `POST /api/triggers` with `type="alert"` cancels any existing active alert with same (sym1, sym2, zscore) before inserting; different zscore = different alert (allowed to coexist)
- **Hysteresis** (in-memory `alert_states: dict[str, str]` in monitor):
     - `"idle"` → threshold crossed → send notification → `"alerted"`
     - `"alerted"` → `abs(current_z) <= ALERT_RESET_Z` → `"idle"` (ready to fire again)
- `alert_states` cleaned up alongside `monitored_keys` in monitor cleanup loop
- Alert trigger stays `status="active"` in DB — never transitions to `"triggered"`; user cancels manually
- When alert fires: `db.alert_fired(id)` updates `last_fired_at` (status stays active for hysteresis)
- Separate **🔔 Alerts** tab in bottom panel: `loadAlertsTab()` / `renderAlerts()`; click row → `_loadAlertIntoAnalysis(trig)` restores pair + timeframe + zscore_window + entry-z and calls `runAnalyze()`
- `loadOrdersTab()` only shows `tp`/`sl` types — alerts are excluded
- **Notification center**: `checkRecentAlerts()` polls `GET /api/alerts/recent` on startup + piggybacked on the 5 s positions interval (not a separate 60 s timer); shows clickable toast + badge on Alerts tab; recently fired rows highlighted yellow with `⚡ X мин назад` in "Last fired" column
- **Creating alerts**: (1) 🔔 button on watchlist item — uses watchlist params; (2) `addAlertFromPanel()` button next to `★ В Watchlist` in Pair Config panel — uses current analysis params (sym1/sym2/timeframe/zscore_window/entry-z); both show pct prompt, switch to Alerts tab after creation

## Common Issues & Fixes

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
- **`_pollAllExecutions` auto-opens popups**: only for non-terminal executions; terminal entries (OPEN/DONE/CANCELLED/FAILED) update if popup already open but never create new one
- **Tailwind CDN dynamic classes don't work**: CDN only generates CSS for classes present in HTML at parse time — classes added only via `classList.add()` in JS produce no CSS rules. Use `element.style.color` with explicit hex values instead (see `_statColor`, `C_GREEN`/`C_YELLOW`/`C_RED` constants)

## Tests (`tests/`)

213 unit-тестов (9 файлов), все проходят ~3.0–4.0 сек. Запуск: `.venv/bin/pytest tests/ -v`

| Файл                    | Тестов | Покрытие                                                                                                                                                                                                                                                                                                                                                                              |
| ----------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_strategy.py`      | 41     | spread, zscore, position sizing (OLS/ATR/Equal), signals, ATR, half-life, Hurst, correlation, hedge ratio, backtest                                                                                                                                                                                                                                                                   |
| `test_db.py`            | 56     | save/find/close/delete positions, TP/SL triggers (tp_smart), trade journal, duplicate guard, analysis params (timeframe/candle_limit/zscore_window), alert trigger params (timeframe/zscore_window/alert_pct), find_active_alert, alert_fired, get_recent_alerts, **double-trigger clear pattern**, **execution_history** (save, idempotent, is_close, empty, limit, order, statuses) |
| `test_helpers.py`       | 26     | `_clean()` / `_safe_float()` — NaN/Inf/np.float64/np.int64 сериализация                                                                                                                                                                                                                                                                                                               |
| `test_order_manager.py` | 4      | Smart v2 dynamic passive repricing, semi-aggressive stage pricing, non-placeable residual hold-until-stage-end, dust acceptance                                                                                                                                                                                                                                                       |
| `test_price_cache.py`   | 17     | PriceCache: subscribe/unsubscribe ref-counting, key isolation, two-consumer lifecycle                                                                                                                                                                                                                                                                                                 |
| `test_watchlist.py`     | 8      | WatchlistItem Pydantic model: defaults, custom fields, required fields validation                                                                                                                                                                                                                                                                                                     |
| `test_telegram_bot.py`  | 56     | Formatters, is*configured, send() safety, all notify*\* content (via `_fire` mock); uses `asyncio.run()` — no pytest-asyncio needed                                                                                                                                                                                                                                                   |
| `test_lifespan.py`      | 5      | asyncio graceful shutdown pattern: infinite tasks cancelled, `CancelledError` absorbed by `return_exceptions=True`, already-done tasks unharmed, mixed task types                                                                                                                                                                                                                     |

**`conftest.py`** — `tmp_db` fixture: `monkeypatch.setattr(db, "DB_PATH", tmp_path/"test.db")` + `db.init_db()` — изолированная БД на каждый тест.

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
