# Pair Trading Dashboard — CLAUDE.md

## Communication with the user

**Keep this file in English.** All future edits to CLAUDE.md should be in English only.

When the user asks for an explanation: keep it **simple and short**; avoid code and jargon; use **plain examples** in the context of this project (pair trading: two correlated assets, spread, entry/exit by z-score, TP/SL). Offer code or technical details only if the user asks for them.

**Professional trader role:** Think about the platform not only as a developer but also as a trader. Before implementing any trading-related feature, ask yourself: "what happens with multiple open positions?", "how does this behave on partial fills?", "what if symbols overlap across pairs?". If you spot an architectural risk — raise it before writing code. Discuss with the user as a beginner trader: explain trading consequences, not just technical ones.

## When to Use Sequential Thinking MCP

Before writing any code, call `mcp__MCP_DOCKER__sequentialthinking` when the task involves:

- Changes to `order_manager.py`, `db.py`, or position close/open logic
- New trading features (triggers, averaging, partial fills, multi-leg scenarios)
- Architectural decisions (new endpoints, new background tasks, PriceCache changes)
- Debugging unexpected trading behavior (wrong PnL, double-close, stuck execution)

Skip for: UI text/style changes, simple endpoint additions, typo fixes.

## When to Check Context7 Documentation

Before modifying files that use external libraries, fetch current API docs via Context7 MCP:

| File                                    | Library                | Context7 ID                  |
| --------------------------------------- | ---------------------- | ---------------------------- |
| `binance_client.py`, `order_manager.py` | ccxt (Binance Futures) | `/ccxt/ccxt`                 |
| `symbol_feed.py`, `user_data_feed.py`   | ccxt WebSocket         | `/ccxt/ccxt`                 |
| `telegram_bot.py`                       | aiogram v3             | `/aiogram/aiogram`           |
| `main.py` (WebSocket endpoints)         | FastAPI                | `/websites/fastapi_tiangolo` |

Skip for: typo fixes, UI text changes, or when you already have current docs loaded in context.

A hookify rule (`check-context7-docs`) will remind you automatically when editing these files.

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
│   ├── main.py              # FastAPI app — REST endpoints + WebSocket + PriceCache + monitor
│   ├── symbol_feed.py       # Binance WS kline + bookTicker feeds (SymbolFeed, BookTickerFeed)
│   ├── user_data_feed.py    # Binance User Data Stream — fills, liquidations, funding
│   ├── strategy.py          # Pair trading math (cointegration, z-score, backtest)
│   ├── binance_client.py    # ccxt async wrapper for Binance Futures (USDT-M + USDC-M)
│   ├── order_manager.py     # Smart limit-order execution engine (state machine)
│   ├── db.py                # SQLite persistence — 8 tables
│   ├── telegram_bot.py      # Telegram notifications + bot (aiogram v3)
│   └── logger.py            # RotatingFileHandler → logs/pair_trading.log (10 MB × 5)
├── frontend/
│   └── index.html           # Single-file UI (Tailwind + Chart.js, no build step)
├── tests/                   # 334 tests (see Tests section)
├── pair_trading.db          # SQLite trade journal (auto-created)
├── .env                     # API keys (not committed)
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

Open `http://localhost:8080` in browser — FastAPI serves `frontend/index.html` at `GET /`.

### Tests

```bash
cd /Users/y.shvydak/Projects/pair_trading
.venv/bin/pytest tests/ -v        # all 310 tests
.venv/bin/pytest tests/test_strategy.py -v   # strategy math only
```

### Virtual Environment

Always use `.venv/bin/python` and `.venv/bin/pip` — system Python is managed by Homebrew and blocks system-wide installs.

```bash
.venv/bin/pip install <package>   # add to requirements.txt afterwards
```

## API Endpoints

> Full field schemas: see Pydantic models in `main.py` (`TradeRequest`, `SmartTradeRequest`, `WatchlistItemDB`).

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/symbols` | List all active USDT-M and USDC-M perpetual futures |
| GET | `/api/history` | OHLCV + spread/z-score + stats; PriceCache used if pair subscribed (`cached_limit >= limit`) |
| GET | `/api/backtest` | Full backtest; params: `sizing_method` (ols/atr/equal), `entry_threshold`, `exit_threshold` |
| GET | `/api/status` | Binance connection status + futures balances |
| GET | `/api/balance` | Futures balances (`?asset=USDT` / `USDC`) |
| GET | `/api/pre_trade_check` | Validate balance, min notional, lot sizes, leverage; params: `symbol1/2`, `size_usd`, `hedge_ratio`, `sizing_method`, `leverage` |
| POST | `/api/trade` | Market order pair trade (instant, no retry) |
| POST | `/api/trade/smart` | Smart limit-order execution; returns `exec_id`; smart params: `passive_s`(30s), `aggressive_s`(20s), `allow_market`(true) |
| GET | `/api/execution/{exec_id}` | Poll smart execution state (call every 2s) |
| DELETE | `/api/execution/{exec_id}` | Cancel a running smart execution |
| GET | `/api/db/positions` | Open positions from DB (entry z-score, hedge ratio, etc.) |
| GET | `/api/db/history` | Closed trade history (`?limit=100`) |
| GET | `/api/db/positions/enriched` | DB positions enriched with live mark prices + PnL |
| DELETE | `/api/db/positions/{id}` | Delete DB position record (does NOT close exchange positions) |
| GET | `/api/dashboard` | **Combined polling**: enriched positions + exchange positions + balances + recent alerts |
| GET | `/api/triggers` | All active TP/SL triggers |
| POST | `/api/triggers` | Create trigger: `{symbol1, symbol2, side, type, zscore, tp_smart, timeframe, zscore_window, alert_pct, candle_limit}`; `candle_limit` required for `type="alert"`; same-params alert replaces existing |
| DELETE | `/api/triggers/{id}` | Hard-delete a trigger (gone permanently) |
| GET | `/api/alerts/recent` | Alert triggers fired within last N minutes (`?minutes=60`) |
| GET | `/api/executions` | All active execution contexts |
| GET | `/api/executions/history` | Persisted terminal execution snapshots (`?limit=100`) |
| GET | `/api/watchlist` | All saved watchlist items |
| POST | `/api/watchlist` | Add or update watchlist item (upsert by sym1+sym2+timeframe) |
| DELETE | `/api/watchlist/{id}` | Remove watchlist item |
| PATCH | `/api/watchlist/{id}/stats` | Persist computed stats (half_life, hurst, corr, pval); called after each Analyze |
| POST | `/api/watchlist/data` | Subscribe pairs to PriceCache; return current z-score + spread (legacy HTTP fallback) |
| POST | `/api/batch/sparklines` | Batch z-score/spread for multiple positions; uses PriceCache when available |
| GET | `/api/bot/configs` | All bot configs (enriched with `signal_first_seen_at` from memory) |
| POST | `/api/bot/configs` | Create or update bot config (upsert by `watchlist_id`) |
| DELETE | `/api/bot/configs/{id}` | Delete bot config |
| PATCH | `/api/bot/configs/{id}/enable` | Set bot status = `waiting` |
| PATCH | `/api/bot/configs/{id}/disable` | Set bot status = `disabled` |
| WS | `/ws/stream` | Live spread/price/Z-score updates, event-driven on each kline (≤5s timeout) |
| WS | `/ws/watchlist` | Event-driven watchlist z-score/spread feed; client sends full list, server pushes |

## Strategy Logic (`strategy.py`)

- **Hedge ratio**: OLS via `numpy.linalg.lstsq` on log prices — `log(P1) = β * log(P2) + α`
- **Spread**: `log(P1) - β * log(P2)`; **Z-score**: rolling `(spread - mean) / std`
- **Cointegration**: Engle-Granger `coint(maxlag=10)` — fixed maxlag avoids slow auto-selection
- **Hurst exponent**: variogram regression (not R/S despite docstring) — H < 0.5 means mean-reverting
- **Position sizing** (`calculate_position_sizes`): `size_usd` = total (both legs); `ols`: 1:|β| split; `atr`: equal dollar volatility; `equal`: 50/50

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

- `watchlist` SQLite table — stores all analysis parameters per pair; managed via `GET/POST/DELETE /api/watchlist`; `_watchlistItems` in-memory array populated on page load via `initWatchlist()`
- **Startup**: `Promise.all([initWatchlist(), refreshTriggersCache(), refreshBotConfigs()])` then `renderWatchlist()` + `connectWatchlistWS()` — alert list and bot configs must load before first paint
- **BOT badge**: `_cachedBotConfigs` — fetched via `refreshBotConfigs()` → `GET /api/bot/configs`; matched to watchlist row by `watchlist_id` (not symbol pair); refreshed every 5s in `loadAllPositions()`; after save/toggle call `renderSpreadChart(state.historyData)` to update chart annotations
- **Dedup key**: `(sym1, sym2, timeframe)` — same pair with different timeframe stored as separate entry; adding BTC/ETH 5m does not overwrite BTC/ETH 4h
- **Grouping by timeframe** in `renderWatchlist()`: section headers, order 5m→15m→30m→1h→2h→4h→8h→1d
- **Telegram alert (🔔)**: `addAlertTrigger` / `addAlertFromPanel` → `POST /api/triggers` with `candle_limit` (from row `limit` or `#limit-input`). `_watchlistItemHasAlert(w, _cachedAlerts)` — bell stays visible (yellow, always-on opacity) when an active `type=alert` matches the row: normalised sym pair, `timeframe`, `zscore_window`, entry Z (`|tr.zscore|` ≈ `entryZ`), and `candle_limit` when both row and trigger have it. After add/cancel alert: `loadAlertsTab()` (or equivalent cache refresh) + `renderWatchlist()`. i18n: `wl_alert_btn`, `wl_alert_active`
- Z-score/spread updated **event-driven** via `WS /ws/watchlist` (fires on each Binance kline, max 5s delay); response contains `timeframe` for exact record matching
- `connectWatchlistWS()` opens WS on start; `_sendWatchlistToWs()` sends updated list on add/remove; reconnect with backoff (1s → 30s)
- `_applyWatchlistUpdate(data)` — common handler for incoming data (in-place DOM patch)
- Threshold indication: `|z| >= entryZ*0.75` → yellow; `|z| >= entryZ` → red + blinking
- Active pair highlight updates immediately after `runAnalyze()` (no wait for 5s tick)
- **localStorage (remaining):** `pt_lang` (ru/en), `pt_mode` (trade/backtest), `pt_price_chart_h` (chart height px), `pt_last` (last analysis state: sym1/sym2/tf/limit/zwindow/entryZ/exitZ/sizing/leverage) — **not shared across browsers/devices**; watchlist acts as the persistent cross-device state for saved pairs

### Alerts tab (frontend)

- `refreshTriggersCache()` fetches `GET /api/triggers` into `_cachedAlerts` (alerts only); `loadAlertsTab(clearBadge=true)` refreshes the table; pass `false` when calling from background poller to avoid clearing badge set by `loadAllPositions()`
- **Row highlight** (“current analysis”): same criteria as watchlist bell — includes **`candle_limit` vs `#limit-input`** so two alerts that differ only by lookback are not both highlighted
- **`_loadAlertIntoAnalysis`**: applies symbols, timeframe, z-window, **lookback** (`limit-input` from `trig.candle_limit` when set), entry Z → `runAnalyze()`

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
> 1.   Historical data for analysis/backtest (`/api/history`, `/api/backtest`)
> 2.   Initial cache fill on cache miss (seed in watchlist HTTP endpoint)
>
> **Never create new polling timers on the frontend for live data — use `/ws/watchlist` or `/ws/stream`.**

- **Key**: `(sym1, sym2, timeframe, limit)` — one entry per unique pair config
- **Entry**: `{"price1": pd.Series, "price2": pd.Series, "df1": DataFrame, "df2": DataFrame}` — close prices + full OHLCV DataFrames, aligned on common timestamps
- **Symbol-level dedup**: `_feeds: dict[(sym, tf), SymbolFeed]` — BTC in 10 pairs = 1 WS connection; `_feed_refs` counts how many pair-keys use each feed
- `price_cache.subscribe(sym1, sym2, tf, limit) → key` — creates SymbolFeeds if needed; ref-counted
- `price_cache.unsubscribe(key)` — decrements refs; stops SymbolFeed when its ref count reaches 0; removes `_store[key]`
- `price_cache.get(key) → dict | None` — read-only; `None` if not yet assembled
- `price_cache.find_cached(sym1, sym2, tf, limit) → dict | None` — finds entry with matching `(sym1, sym2, tf)` and key limit `>= requested`; always verify `len(cached["price1"]) >= limit` before use — buffer may be incomplete shortly after server start
- **`_assemble_from_feeds` ignores the `limit` in the cache key** — always stores the full SymbolFeed buffer. Consumers that need exactly `limit` rows must slice explicitly: `entry["price1"].iloc[-limit:]`. `/api/history` does this; any new WS endpoint must too. Mismatch between consumer's slice and the OLS window causes a different β → PnL line goes flat.
- **`find_cached` miss when user's limit > watchlist's saved `candle_limit`**: `find_cached(sym1, sym2, tf, 500)` returns `None` if only a `(sym1, sym2, tf, 200)` key exists (200 < 500) → `/api/history` falls back to REST. Meanwhile `/ws/stream` subscribes fresh PriceCache entry — slightly different time window → different β → flat PnL line. **Fix: always send `hedge_ratio: state.hedgeRatio` from the frontend in the `/ws/stream` WS subscription payload** so the backend uses a fixed β (backend already supports `fixed_hedge_ratio` param). This makes spread computation consistent regardless of PriceCache vs REST source.
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

`get_logger(name)` → `StreamHandler` + `RotatingFileHandler`; file: `logs/pair_trading.log`; max 10 MB × 5 files; format: `YYYY-MM-DD HH:MM:SS [LEVEL] name: message`

### SQLite Persistence (`backend/db.py`)

- DB file: `pair_trading.db` (project root, auto-created on first run via `db.init_db()` in lifespan)
- **Eight tables**: `open_positions`, `closed_trades`, `triggers`, `execution_history`, `position_legs`, `funding_history`, `watchlist`, `bot_configs`
- `bot_configs` — per-pair auto-trading config; `UNIQUE(watchlist_id)`, `ON DELETE CASCADE`; statuses: `disabled` | `waiting` | `in_position` | `paused_after_sl`; `last_close_reason` written by `monitor_position_triggers` before closing; `avg_in_progress` flag blocks TP/SL during averaging
- `_conn()` includes `PRAGMA foreign_keys = ON` — required for `ON DELETE CASCADE`; must be kept
- **Enriching DB rows in endpoints**: `sqlite3.Row` is read-only — convert first: `[dict(c) for c in db.get_X()]`, then add fields
- Watchlist functions: `get_watchlist()`, `save_watchlist_item(...)` → id (upsert by sym1+sym2+timeframe), `delete_watchlist_item(id)` → bool, `update_watchlist_stats(item_id, half_life, hurst, corr, pval)` — called after each analysis to persist computed stats
- `open_positions` has `status` column: `open` | `partial_close` | `liquidated` | `adl_detected`; also `coint_pvalue`, `coint_checked_at`
- `find_open_position(sym1, sym2)` — excludes `liquidated`/`adl_detected`; returns `partial_close` (user may still want to close it)
- Position leg functions: `save_position_leg(...)`, `get_position_legs(pos_id)`, `close_position_legs(pos_id)`, `add_position_entry(pos_id, leg_number, new_qty, new_price)` — updates weighted avg price in `open_positions`
- Funding functions: `save_funding_history(pos_id, symbol, amount, asset)`, `get_funding_total(pos_id) → float`
- Status/health: `set_position_status(pos_id, status)`, `update_position_coint_health(pos_id, pvalue)`
- `close_position(...)` accepts `commission` and `commission_asset` kwargs; saved to `closed_trades`
- Key functions: `save_open_position(...)` → id, `close_position(...)`, `find_open_position(sym1, sym2)`, `get_open_positions()`, `get_closed_trades(limit)`, `delete_open_position(id)`
- Trigger functions: `save_trigger(...)` → id, `get_active_triggers()`, `cancel_trigger(id)` (**hard DELETE** — row gone immediately; no soft-delete/cancelled state), `trigger_fired(id)`, `find_active_alert(sym1, sym2, zscore, timeframe, zscore_window, candle_limit)` → dict|None (dedup on alert create matches TF + z-window + lookback + z threshold), `alert_fired(id)`, `get_recent_alerts(minutes=60)` → list[dict]; `candle_limit` stored per trigger (required for `type="alert"`); monitor uses `trig["candle_limit"] or max(zw*3, 60)`
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
- `clientOrderId = PT_{pos_id}_{leg}_{uuid8}` (max 36 chars) on every order — **unique per placement** (fresh UUID each call, not per-execution constant); prevents Binance stale cache returns; for crash recovery via `_reconcile_on_startup`
- DUST flush after close: `reduceOnly` market order for remainder; recalculates `leg.avg_price` as weighted average before saving PnL
- Commission: `LegState.commission` uses `max(self.commission, incoming)` — UserDataFeed stores cumulative per order; safe for both WS and REST sources
- `DUST` = remaining qty below exchange minimum; partial fill accepted, no new order for residual
- `_fetch_orderbooks` — prefers `BookTickerFeed.get_best()`, REST fallback; `_refresh_fills` — prefers UserDataFeed WS snapshot, REST fallback
- `active_executions` in `main.py`: terminal entries cleaned after 2h TTL; persisted to `execution_history` before cleanup via `_exec_saved_to_db` set
- `exec_id` is first 8 chars of UUID4

## Pre-trade Check & Monitor

- Balance check: `required_margin = size_usd / leverage * 1.1` — initial margin with 10% buffer
- Validation order: balance → min_notional → lot_size → leverage (informational)
- **monitor_position_triggers** runs every **2s** — reads from PriceCache (zero direct Binance calls); subscribes active positions/triggers, unsubscribes stale ones each cycle; writes `last_close_reason` to `bot_configs` before closing; skips close up to 70s if `avg_in_progress = 1`; uses **fresh OLS hedge** (`strategy.calculate_hedge_ratio`) every cycle — never `pos["hedge_ratio"]` from DB (it drifts from entry and produces a z inconsistent with the chart, causing false TP/SL); **stale check guard**: if `get_positions()` returns empty, checks `active_executions` for a recent opening execution (`ctx.db_id == pos_id`, `not ctx.is_close`, `status=OPEN`, created < 120s ago) — if found, skips deletion; additionally `_stale_counts[pos_id]` requires **2 consecutive** "not on exchange" cycles before `delete_open_position` is called; every stale check occurrence logs `STALE CHECK | ...` at WARNING with z, threshold, recently_opened flag, and exec snapshot
- **monitor_auto_trading** runs every **2s** — handles bot entry detection, position adoption, z-score confirmation timer, averaging; `_bot_signal_seen_at: dict[int, str]` module-level (cfg_id → HH:MM UTC) exposed via `GET /api/bot/configs`
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

Notification functions: `notify_position_opened`, `notify_position_closed`, `notify_trigger_fired`, `notify_alert`, `notify_rollback`, `notify_execution_failed`, `notify_liquidation`, `notify_adl`, `notify_coint_breakdown`, `notify_reconcile_mismatch` — all non-blocking via `_fire()` → `create_task(send())`; `send()` never raises.

**Rule:** `notify_alert` is ONLY for watchlist z-score threshold alerts. Use dedicated functions for all other events — never pass dummy `0.0, 0.0` args.

### Alert triggers (`type="alert"` in `triggers` table)

- **Monitor** recomputes z like analysis: PriceCache closes for `(sym1, sym2, trig timeframe, candle_limit)`, OLS hedge on that series, rolling z with `trig["zscore_window"]`. Telegram when `abs(current_z) >= alert_pct * abs(trig_z)` while state is `"idle"`. `notify_alert(..., fire_at=thresh)` separates **Entry Z** from the **actual trip level** when `alert_pct < 1` so the message is not confused with “% of ±entry” only.
- **First subscription (per process):** if `|z|` is **already** past that gate, the FSM starts in `"alerted"` **without** Telegram — avoids instant ping on create; after `abs(z) <= TELEGRAM_ALERT_RESET_Z` (default 0.5) state returns to `"idle"` and the next breach sends `notify_alert`.
- Row stays in DB while active (fires repeatedly via hysteresis); user Cancel = hard DELETE.
- **Hysteresis**: `"idle"` → fires → `"alerted"` → `abs(z) <= ALERT_RESET_Z` → `"idle"` (ready to fire again)
- Created via 🔔 on watchlist item or `addAlertFromPanel()` button in Pair Config panel
- **Notification center**: `checkRecentAlerts()` piggybacked on the 5s positions interval (not a separate timer)

## Common Issues & Fixes

- **Symbol format mismatch (DB vs exchange)**: DB stores symbols as `SOLUSDC`; ccxt/Binance returns `SOL/USDC:USDC`. Never compare them directly. Always use `_normalise_symbol(sym)` (→ `SOL/USDC`) and build lookup maps with both full and base keys (`sym.split(":")[0]`). Affected anywhere we call `get_positions()` and match against DB: `reconcile_positions`, `monitor_position_triggers` (live check before close), `_build_live_map` / `_enrich_positions`. Use `_build_live_map()` to construct all `live_map` dicts — it handles all variants.

- **When fixing a bug — check propagation**: After fixing a bug, always grep for the same pattern across the entire codebase before closing. Ask: "where else does code do the same thing?" A symbol format bug found in `reconcile_positions` was silently present in `monitor_position_triggers`, `_enrich_positions`, and two other endpoints — causing wrong PnL display and phantom TP failures.

- **FOREIGN KEY constraint failed on position close/delete**: `position_legs` references `open_positions(id)` without `ON DELETE CASCADE` — always `DELETE FROM position_legs WHERE position_id = ?` before `DELETE FROM open_positions`; applies to both `close_position()` and `delete_open_position()`
- **`partial_close` position in UI**: orange badge; remaining leg must be closed manually on exchange
- **Cointegration health dot is empty**: normal for first 4h after server start — `health_check_coint` has 120s initial delay then 4h interval
- **HTTP 400 "notional below minimum"**: `size_usd` too small for the contract
- **Strategy Positions shows position but Exchange Positions is empty**: DB/exchange desync — use 🗑 to remove stale record or `✕ M`
- **Trade markers not showing on chart**: `loadTradeJournal()` must be called after `runAnalyze()`; `_utcParse` handles both `2026-03-16 18:00:00` and `2026-03-16T18:17:28+00:00` formats
- **Active pair highlight**: compares **5 params**: sym1+sym2+timeframe+zscore_window+entryZ — ticker alone is insufficient
- **`_pollAllExecutions` auto-opens popups**: `_execSeenIds` + `_execFirstPoll` prevent stale popups on reload; adaptive 2s/5s frequency
- **Tailwind CDN dynamic classes don't work**: CDN only generates CSS for classes present at parse time — use `element.style.color` with `C_GREEN`/`C_YELLOW`/`C_RED` constants
- **SQLite upsert `lastrowid` unreliable**: after `INSERT ... ON CONFLICT DO UPDATE`, always use a follow-up `SELECT` to get the actual ID
- **SQLite datetime format**: always use `strftime("%Y-%m-%d %H:%M:%S")` — ISO format with `T` and `+00:00` breaks `datetime()` range queries
- **Alert z vs chart z may differ**: monitor uses trigger's `candle_limit`/`zscore_window`/`timeframe`; chart uses current UI values — load pair from watchlist to align
- **WebSocket requires absolute URL**: `new WebSocket('/ws/path')` throws — use `_wsUrl(path)` helper
- **`ecosystem.config.js` path resolution**: use `path.join(__dirname, '.venv/bin/uvicorn')` — PM2 resolves `script` relative to `cwd`
- **PnL line goes flat after loading pair from watchlist then changing limit**: watchlist's saved `candle_limit` < new limit → `find_cached` misses → `/api/history` uses REST, WS uses PriceCache → β mismatch → flat line. Root fix: send `hedge_ratio: state.hedgeRatio` in `/ws/stream` WS payload (already fixed in frontend).
- **Bot TP/SL fires seconds after position opens (API lag)**: Binance `get_positions()` returns empty 1-3s after fill. Stale check guard: `_recently_opened = any(ctx.db_id == pos_id and not ctx.is_close and ctx.status.name == "OPEN" and elapsed < 120 ...)` over `active_executions`. Do NOT use time-based `pos_age_s` — it parses `opened_at` string from DB and is fragile. Pattern: gate on execution knowledge, not wall-clock. Additionally, `_stale_counts[pos_id]` requires 2 consecutive "not on exchange" cycles before `delete_open_position` is called — single-cycle false positives are silently skipped. The diagnostic `STALE CHECK | ...` log at WARNING level captures z, threshold, `recently_opened`, and active exec snapshot each time it fires.
- **`monitor_auto_trading` stale `cfg` snapshot causes wrong bot status**: `active_cfgs` is loaded once per cycle; `cfg["last_close_reason"]` can be None if `monitor_position_triggers` set it after the snapshot. Fix: when `pos is None` in `in_position` branch, reload via `db.get_bot_config_by_pair(sym1, sym2)` before reading `last_close_reason` — otherwise bot always goes `paused_after_sl` instead of `waiting` when TP fires.
- **Orphaned exchange position after stale deletion**: when bot goes to `paused_after_sl` and DB position is gone, `monitor_auto_trading` now calls `client.get_positions()` and logs `BOT ORPHAN` error + Telegram alert if exchange still holds the position. This guards against manual close being required on exchange.
- **Stored `hedge_ratio` drift causes false TP**: `pos["hedge_ratio"]` is saved at entry time. Even minutes later, fresh OLS gives a different β → different z. If monitor uses stored β but bot ticker uses fresh OLS, they compute different z-scores — monitor can see `abs_z < tp` when chart shows `abs_z >> tp` → premature TP fires. Always use fresh OLS in monitor.

## Tests (`tests/`)

367 unit tests, all pass in ~7s. Run: `.venv/bin/pytest tests/ -v`

- `test_strategy.py`(41), `test_db.py`(103), `test_helpers.py`(26), `test_order_manager.py`(18), `test_price_cache.py`(35), `test_symbol_feed.py`(15), `test_watchlist.py`(8), `test_telegram_bot.py`(70), `test_lifespan.py`(5), `test_bot_monitor.py`(46), `test_user_data_feed.py`(19), `test_symbol_helpers.py`(14)
- `conftest.py`: `tmp_db` fixture — `monkeypatch.setattr(db, "DB_PATH", tmp_path/"test.db")` + `db.init_db()` — isolated DB per test
- **`main.py` cannot be imported in tests** — side effects at import time (BinanceClient, PriceCache, .env). Test pure helpers by copying logic inline with a comment, as done in `test_symbol_helpers.py`.

## Deployment

- **`ecosystem.config.js`** — PM2 config; runs uvicorn on port 8080; uses `path.join(__dirname, ...)` for reliable path resolution regardless of where `pm2 start` is called from
- **`.github/workflows/deploy.yml`** — GitHub Actions self-hosted runner on Raspberry Pi; no build step; runs `.venv/bin/pip install -r backend/requirements.txt` then `pm2 reload`; `clean: false` preserves `.env` between deploys
- **`docs/DEPLOYMENT.md`** — full setup guide (venv creation, PM2 startup, Cloudflare Tunnel)
- Cloudflare Tunnel: one tunnel `pair-trading.shvydak.com → localhost:8080` — serves both API and frontend (no separate static server)
- Local dev: `cd backend && ../.venv/bin/uvicorn main:app --reload --port 8080` → open `http://localhost:8080`

## User Preferences

- Russian language UI by default
- Dark theme only
- No build tools — keep frontend as single HTML file

## Guide Writing Rules

- **Write for beginners first.** Every technical term must be explained in plain language before showing formulas.
- **Explain the "why" before the "how".** First explain what problem it solves, then the formula.
- **Use concrete examples.** Every concept section must include a numerical example with realistic BTC/ETH prices.
- **Avoid jargon without explanation.** If unavoidable, immediately follow with a plain-language parenthetical.
