# Changelog — Pair Trading Dashboard

---

## 2026-03-10 — Фикс: check_min_notional всегда возвращал $0.00

### Проблема

`check_min_notional` искал данные через `self.exchange.markets.get(symbol, {})`,
где `symbol` — нормализованный `BTC/USDT`. Но ccxt хранит ключ как `BTC/USDT:USDT`
(с указанием расчётной валюты). Прямой lookup возвращал `{}` → `limits.cost.min` = None
→ `min_notional = 0.0`.

В «Проверке перед сделкой» отображалось `min required: $0.00` при реальном минимуме $100
для BTC/USDT.

### Что изменено

| Место | Было | Стало |
|---|---|---|
| `binance_client.check_min_notional` | `markets.get(symbol, {})` — прямой dict lookup | `self.exchange.market(symbol)` — ccxt-метод с резолвингом символа |

### Почему это работает

`exchange.market('BTC/USDT')` внутри ccxt резолвит символ в правильный ключ
`BTC/USDT:USDT`, а `limits.cost.min` содержит реальное значение ($100 для BTC/USDT).
Добавлен fallback на `market["info"]["filters"]` с `filterType: "MIN_NOTIONAL"`
на случай если unified field отсутствует.

---

## 2026-03-10 — Умное исполнение ордеров (Smart Limit Execution)

### Задача

Режим маркет-ордеров всегда платит тейкерскую комиссию (~0.05% на USDT-M, 0.045% на USDC-M).
На USDC-M Binance предлагает 0% мейкерскую комиссию для всех пользователей.
Нужен механизм, который пробует сначала выставить лимитные ордера (мейкер),
и только при неудаче переключается на более агрессивный режим.

### Что добавлено

**`backend/order_manager.py`** — новый файл, engine умного исполнения:

Стейт-машина:
```
PLACING → PASSIVE → AGGRESSIVE → FORCING → OPEN
                                         ↘ ROLLBACK → DONE
                                         ↘ CANCELLED
```

| Фаза | Поведение | Комиссия |
|---|---|---|
| PASSIVE | Покупка по bid / продажа по ask — ждём исполнения N секунд | 0% (мейкер) |
| AGGRESSIVE | Переставляем на другую сторону стакана (buy@ask, sell@bid) | ~0.045% (тейкер) |
| FORCING | Рыночный ордер для остатка | ~0.045–0.05% |
| ROLLBACK | Если нога A исполнена, нога B провалилась — закрываем A по рынку | нейтрализация риска |

Ключевые классы:
- `ExecConfig` — настройки: `passive_s=10`, `aggressive_s=20`, `allow_market=True`, `poll_s=2`
- `LegState` — состояние ноги: `order_id`, `filled`, `remaining`, `avg_price`, `status`
- `ExecContext` — контекст исполнения: обе ноги, лог событий, `cancel_req`, `db_id`

**`backend/binance_client.py`** — 4 новых метода:
- `fetch_order_book(symbol, limit=5)` → `{bid, ask, spread_pct}`
- `place_limit_order(symbol, side, amount, price)` — с округлением amount и price
- `cancel_order(symbol, order_id)`
- `fetch_order(symbol, order_id)`

**`backend/main.py`** — 5 новых эндпоинтов:

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/pre_trade_check` | Валидация баланса, номинала, лотов, плеча |
| POST | `/api/trade/smart` | Старт умного исполнения, возвращает `exec_id` |
| GET | `/api/execution/{id}` | Опрос состояния (каждые 2с) |
| DELETE | `/api/execution/{id}` | Запрос отмены |

Новая модель `SmartTradeRequest`: те же поля что `TradeRequest` + `passive_s`, `aggressive_s`, `allow_market`.

Глобальный dict `active_executions: {exec_id → ExecContext}` — никогда не чистится, хранит историю.

**`frontend/index.html`** — новый UI в секции «Торговля»:
- Поле **Плечо** (1–20×)
- Переключатель **Маркет / Умный лимит** (`setExecMode`)
- Панель **Настройки умного лимита** (скрыта в режиме маркет): passive/aggressive timeout, Market fallback checkbox
- Кнопка **«Проверка перед сделкой»**: показывает ✓/✗ по каждому пункту + точные объёмы после округления
- Панель **Монитор исполнения** (появляется при старте умного исполнения): статус-бейдж, % заполнения ног, лог событий, кнопка «Отменить»

Новые JS-функции:
- `setExecMode(mode)` — переключение режима
- `fetchPreTradeCheck()` / `renderPreTrade(data)` — проверка перед сделкой
- `startSmartExecution(side)` — старт + запуск поллинга
- `pollExecution(execId)` / `renderExecution(data)` — обновление монитора
- `cancelCurrentExecution()` — отмена

Новые i18n-ключи (ru/en): `leverage_label`, `exec_mode`, `exec_market`, `exec_smart`,
`smart_settings`, `passive_timeout`, `aggressive_timeout`, `allow_market_fallback`,
`pretrade_check`, `pretrade_ok`, `pretrade_fail`, `exec_monitor`, `exec_cancel`,
`exec_cancel_confirm`, `toast_smart_started`, `toast_smart_error`, `leg_label`.

---

## 2026-03-10 — Persistence, logging, leverage, min notional

### Что добавлено

**`backend/db.py`** — новый файл, SQLite-журнал сделок:
- Таблица `open_positions` — запись при открытии позиции
- Таблица `closed_trades` — перенос при закрытии + P&L
- Функции: `save_open_position`, `close_position`, `find_open_position`, `get_open_positions`, `get_closed_trades`
- Файл БД: `pair_trading.db` в корне проекта, создаётся автоматически при старте

**`backend/logger.py`** — новый файл:
- `get_logger(name)` → `StreamHandler` + `RotatingFileHandler`
- Лог: `logs/pair_trading.log` (10 МБ × 5 файлов, UTF-8)
- Формат: `YYYY-MM-DD HH:MM:SS [LEVEL] name: message`

**`backend/binance_client.py`** — новые методы:
- `_ensure_markets()` — ленивая загрузка market data перед precision-операциями
- `round_amount(symbol, amount)` → float — через `amount_to_precision`
- `set_leverage(symbol, leverage)` — best-effort, ошибка логируется как WARNING
- `check_min_notional(symbol, amount, price)` → `(ok, actual, min)` — через `limits.cost.min`
- `place_order()` — автоматическое округление через `amount_to_precision` перед отправкой

**`backend/main.py`** — доработки:
- Lifespan: `db.init_db()` при старте, `client.close()` при остановке
- Новые поля `TradeRequest`: `leverage`, `entry_zscore`, `exit_zscore`
- При `action=open`: `set_leverage` (best-effort), `check_min_notional` (HTTP 400 при провале), `save_open_position`
- При `action=close`: поиск позиции в БД, расчёт P&L, `close_position`
- Новые эндпоинты: `GET /api/db/positions`, `GET /api/db/history`

### Известные нюансы

- `set_leverage` выдаёт WARNING (не ERROR) если позиция уже открыта на бирже — торговля продолжается с текущим плечом
- `find_open_position` ищет по `(symbol1, symbol2)` — если пара менялась, запись не найдётся и P&L будет null

---

## 2026-03-10 — Документация и руководство пользователя

### CLAUDE.md

- Добавлен `order_manager.py` в структуру проекта
- Расширена таблица API-эндпоинтов (5 новых строк)
- Добавлена таблица полей `SmartTradeRequest`
- Расширен раздел Binance Client Notes (4 новых метода)
- Добавлен подраздел «Trading Section» во Frontend с описанием всех новых UI-элементов
- Добавлен раздел «Order Manager» — стейт-машина, rollback, exec_id
- Добавлены 2 новых пункта в Common Issues

### Guide (GUIDE.ru/GUIDE.en, section 8 — Открытие сделок)

Полная переработка раздела:
- Два режима исполнения с объяснением для начинающих
- Три фазы умного лимита (пассивная → агрессивная → маркет-фолбэк)
- Описание настроек умного лимита
- Описание «Проверки перед сделкой» (что проверяется и что делать при красном)
- Описание монитора исполнения

### Guide (section 10 — Защита ордеров)

- Добавлен раздел «Защита от частичного исполнения (Rollback)»
- Схема стейт-машины для пользователя (OPEN / ROLLBACK → DONE / CANCELLED)
- Обновлён пример лог-файла с execution ID

---

## 2026-03-10 — Начальная структура проекта

Создан проект с нуля.

### Backend
- `FastAPI` + `ccxt.async_support.binanceusdm` для Binance USDT-M Futures
- Эндпоинты: `/api/symbols`, `/api/history`, `/api/backtest`, `/api/status`, `/api/positions`, `/api/balance`, `POST /api/trade`, `/ws/stream`
- `strategy.py`: hedge ratio (OLS), spread, z-score, cointegration (Engle-Granger), half-life, Hurst exponent, ATR, backtest, position sizing (OLS/ATR/Equal)
- Нормализация символа: `BTCUSDT` → `BTC/USDT` через `_normalise_symbol()`
- `_clean()` — рекурсивная замена NaN/Inf для JSON-сериализации

### Frontend
- Single HTML file: Tailwind CDN + Chart.js 4.4.2 + chartjs-plugin-annotation
- i18n: `I18N` объект, `ru`/`en`, дефолт `ru`, localStorage `pt_lang`
- Spread-chart с аннотациями (entry/exit пороги, обновляются без перезапроса)
- Price chart (нормализованные цены, база=100)
- Backtest: equity curve + таблица сделок (цветные строки, cumulative P&L)
- Positions tab: баланс, открытые позиции с unrealized P&L
- WebSocket стрим: обновление z-score каждые 5с
- Статус Binance API в сайдбаре: no_keys / connected / auth_error / network_error
- Руководство (Guide drawer): 10 разделов, двуязычный, с примерами SVG-схем
- Тултипы на всех статистических картах со встроенными SVG-иллюстрациями
- Предпросмотр позиции (Position Preview) с расчётом объёмов на клиенте

---

Каждая запись: что сломано или добавлено, причина, что именно изменено.
