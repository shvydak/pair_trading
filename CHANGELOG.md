# Changelog — Pair Trading Dashboard

---

## 2026-03-11 — Аудит и исправление критических ошибок

### 1. Исправлен расчёт баланса в `/api/pre_trade_check`

**Было:** `free >= size_usd * 0.5` — не учитывало leverage, давало ложные результаты во всех случаях.
**Стало:** `free >= size_usd / leverage * 1.1` — корректный margin requirement с 10%-буфером.

| Файл | Строка | Изменение |
|---|---|---|
| `backend/main.py` | pre_trade_check | `size_usd * 0.5` → `size_usd / leverage * 1.1` |

---

### 2. Защита от дублирующихся позиций в DB

`save_open_position()` теперь проверяет, есть ли уже открытая позиция для этой пары. Если да — выбрасывает `ValueError` вместо создания второй записи-дубля, которая бы никогда не закрылась.

| Файл | Изменение |
|---|---|
| `backend/db.py` | Проверка `SELECT id FROM open_positions WHERE symbol1=? AND symbol2=?` перед INSERT |

---

### 3. Порядок операций при открытии позиции

**Было:** Set leverage → check notional → place orders (если notional провалил, leverage уже выставлен).
**Стало:** Check notional → set leverage → place orders.

---

### 4. Market mode: в DB сохраняется реальный rounded qty

**Было:** `qty1=qty1` — расчётное значение до округления Binance.
**Стало:** `qty1=float(order1.get("amount") or qty1)` — фактический объём из ответа биржи.

---

### 5. Monitor TP/SL: интервал 10s → 5s

Теперь совпадает с частотой обновления `price_cache`. Каждое обновление кэша проверяется немедленно, без пропуска цикла.

---

### 6. Очистка `active_executions` по TTL (2 часа)

Завершённые executions (DONE/CANCELLED/FAILED) теперь удаляются из памяти через 2 часа. Ранее они накапливались бесконечно.

---

### 7. Ликвидационная цена в таблице Strategy Positions

Поле `liq_price1`/`liq_price2` из `/api/db/positions/enriched` теперь отображается в таблице (оранжевый цвет). Данные берутся с Binance при каждом обновлении вкладки Positions.

---

### 8. ATR sizing docstring: исправлена вводящая в заблуждение формулировка

Было: "equal dollar volatility". Стало: "equal price-unit volatility (qty1×ATR1 == qty2×ATR2)" с пояснением, что dollar exposure ног может отличаться.

---

## 2026-03-11 — Централизованный кэш данных (PriceCache)

### Проблема

Каждый компонент самостоятельно делал запросы к Binance:
- WebSocket — `fetch_ohlcv × 2` каждые 5 с (пока открыт браузер)
- `monitor_position_triggers` — `fetch_ohlcv × 2` **на каждую позицию** каждые 10 с

При 3 позициях с TP/SL + открытом браузере: до 8 запросов каждые 5 с.
Если анализируемая пара совпадала с позицией — данные скачивались дважды.

### Что добавлено

**`backend/main.py`** — класс `PriceCache`:

- Ключ: `(sym1, sym2, timeframe, limit)` — одна запись на уникальную конфигурацию пары
- `subscribe(sym1, sym2, tf, limit) → key` — регистрирует пару, возвращает ключ; reference-counting
- `unsubscribe(key)` — уменьшает счётчик; при 0 — запись удаляется из кэша
- `get(key) → {"price1": pd.Series, "price2": pd.Series} | None` — чтение без сетевого запроса
- `run()` — фоновый `asyncio.Task`; обходит все подписки и делает `fetch_ohlcv` раз в 5 с

**WebSocket `/ws/stream`**:
- Подписывается на кэш при старте, отписывается в `finally` при дисконнекте
- Читает `price1/price2` из кэша → считает hedge_ratio/spread/zscore локально
- Прямых вызовов `client.fetch_ohlcv` больше нет

**`monitor_position_triggers`**:
- Поддерживает `monitored_keys: dict[pos_id → cache_key]`
- При появлении новой позиции с TP/SL — подписывается; при закрытии — отписывается
- Читает данные из кэша (не делает свои запросы к Binance)
- При пустом кэше (первый цикл) — пропускает позицию, не падает

**`lifespan`**: `price_cache.run()` запускается первым, до монитора.

### Результат

| Сценарий | Запросов к Binance / 5 с |
|---|---|
| 1 WS-пара = 1 позиция с TP/SL | **2** (было 4) |
| 1 WS-пара + 3 позиции (разные пары) | 8 (как раньше, но теперь из одного места) |
| Браузер закрыт, 3 позиции с TP/SL | 6 (без изменений — монитор владеет подписками) |

### Совместимость с watchlist (будущая фича)

Watchlist вызовет `price_cache.subscribe()` для каждой карточки — данные появятся
автоматически без новых запросов к Binance, если пара уже отслеживается.

---

## 2026-03-11 — Фикс: UI сайзинга и пропадающая линия Z-score на графике

### Проблема 1 — Кнопки «OLS β / ATR / Equal» отображались в две строки

CSS-класс `.tooltip-container` задаёт `display: inline-block`, что нарушало `flex` раскладку
дочерних кнопок (`flex-1`). Проблема воспроизводилась в блоках **Sizing Method**, **Leverage**
и **Execution Mode**.

**Фикс**: добавлен `style="display:block"` к `tooltip-container`-обёрткам, которые являются
блочными элементами с flex-детьми.

---

### Проблема 2 — Оранжевая линия Z-score исчезала с графика спреда

TP/SL аннотации инициализировались с `yMin: 9999`, что заставляло Chart.js масштабировать
правую ось Y до ±10 000. Реальные значения Z-score (диапазон ±4) становились невидимы.

**Фикс**: TP/SL аннотации теперь инициализируются с `display: false`. `updateTriggerLines()`
переключает `display: true/false` вместо записи экстремальных координат.

| Файл | Изменение |
|---|---|
| `frontend/index.html` | `tooltip-container` + `style="display:block"` для 3 блоков |
| `frontend/index.html` | `renderSpreadChart`: `tpHigh/tpLow/slHigh/slLow` → `display: false` |
| `frontend/index.html` | `updateTriggerLines()`: переключение через `display`, а не 9999 |

---

## 2026-03-11 — Tooltips по всему UI

Добавлены информационные всплывающие подсказки (tooltip) к ключевым полям интерфейса.
Каждый tooltip содержит объяснение параметра, конкретный числовой пример и рекомендации.

### Новые ключи i18n (EN + RU)

| Ключ | Поле |
|---|---|
| `tip_market_filter` | Фильтр рынка (USDT-M / USDC-M / All) |
| `tip_timeframe` | Таймфрейм анализа |
| `tip_lookback` | Глубина истории (количество свечей) |
| `tip_pos_size` | Размер позиции в USD |
| `tip_sizing` | Метод сайзинга (OLS / ATR / Equal) |
| `tip_leverage` | Кредитное плечо |
| `tip_exec_mode` | Режим исполнения (Market / Smart Limit) |
| `tip_passive_s` | Время ожидания на пассивной цене |
| `tip_aggressive_s` | Время ожидания на агрессивной цене |
| `tip_tp_sl` | Take Profit / Stop Loss по Z-score |
| `tip_long_spread` | Направление Long Spread |
| `tip_short_spread` | Направление Short Spread |

**Дополнительно**: `initTooltips()` получил guard от двойной инициализации (`container._tooltipInit`)
и вызывается после `renderStrategyPositions()`.

---

## 2026-03-11 — Auto-close по z-score: TP/SL триггеры (#4)

### Реализовано

**Backend (`backend/main.py`, `backend/db.py`)**:
- `POST /api/db/positions/{id}/triggers` — сохраняет `tp_zscore` и `sl_zscore` к позиции
- `monitor_position_triggers()` — фоновый `asyncio.Task`, запускается при старте приложения;
  каждые 10 секунд проверяет все открытые позиции с заданными триггерами;
  при срабатывании вызывает market close (тот же механизм, что кнопка `✕ M`)
- `db.set_position_triggers(id, tp, sl)` — UPDATE в таблице `open_positions`

**Frontend (`frontend/index.html`)**:
- В таблице **Strategy Positions**: поля `TP z` и `SL z` per-row с текущими значениями из DB
- Кнопка **Set** → `_setTriggers(posId)` → POST на `/api/db/positions/{id}/triggers`
- После `↗ Load into Analysis` значения TP/SL из DB сохраняются в `state.pendingTriggers`
  и применяются на графике как только `renderSpreadChart()` завершится
- `updateTriggerLines(tp, sl)` — рисует 4 горизонтальные линии на графике спреда:
  - зелёные пунктиры `TP` на ±tp_zscore
  - красные пунктиры `SL` на ±sl_zscore

---

## 2026-03-11 — Фикс: скачок спреда от пересчёта hedge_ratio в WebSocket

### Проблема

На графике «Спред и Z-счёт» в самом правом конце возникал резкий шип — последняя точка
сильно отличалась от предыдущих. Причина: WS-бэкенд каждые 5 секунд заново вычислял
`hedge_ratio` из свежих свечей. Даже небольшое изменение β (например 1.503 → 1.511)
давало заметный скачок в значении спреда, так как `log(ETH) ≈ 7.55` является большим
множителем: `Δspread = 0.008 × 7.55 ≈ 0.06`.

Вся историческая часть графика строилась с оригинальным β из Analyze, последняя точка —
с новым. Визуально это выглядело как внезапный шип.

### Что изменено

| Файл | Изменение |
|---|---|
| `frontend/index.html` | `connectWebSocket()` теперь передаёт `hedge_ratio: state.hedgeRatio` в WS-параметрах |
| `backend/main.py` | WS-обработчик принимает `hedge_ratio` из параметров; если передан — использует его фиксированным, иначе пересчитывает |

---

## 2026-03-11 — Восстановление состояния при перезагрузке (#2)

### Задача

При перезагрузке страницы все поля (символы, таймфрейм, z-score окно, leverage, sizing method,
market filter и др.) сбрасывались. Пользователь вынужден был заново вводить параметры и нажимать
Analyze. Кнопка ↗ в таблице позиций переносила только символы, без остальных параметров.

### Что добавлено

**`frontend/index.html`**:

- `saveAnalysisState()` — вызывается после каждого успешного `runAnalyze()`; сохраняет в
  `localStorage['pt_last']`: sym1, sym2, timeframe, limit, zscore_window, entryZ, exitZ,
  posSize, sizingMethod, leverage, marketFilter
- `restoreAnalysisState()` — вызывается при `DOMContentLoaded`; восстанавливает все поля и
  автоматически вызывает `runAnalyze()`. Если сохранённого состояния нет (первый запуск) —
  просто выставляет timeframe `1h`
- `_loadPosIntoAnalysis(id)` расширена: теперь дополнительно восстанавливает `sizing_method` и
  `leverage` из DB-записи и сразу вызывает `runAnalyze()` (ранее требовалось нажать Analyze вручную)

### Результат

| До | После |
|---|---|
| Перезагрузка → пустые поля | Перезагрузка → все поля заполнены, график загружается автоматически |
| ↗ переносит только символы | ↗ переносит символы + leverage + sizing и сразу показывает график |
| Market filter сбрасывается на "Все" | Market filter восстанавливается из сохранённого состояния |
| Параметры стратегии (entryZ, exitZ, posSize) сбрасываются | Все параметры сохраняются между сессиями |

---

## 2026-03-11 — Управление открытыми позициями: таб Positions

### Задача

После открытия сделки не было способа управлять ею из UI: кнопка «Закрыть» в сайдбаре
требовала вручную помнить параметры пары, при перезагрузке страницы всё терялось.
Не было истории закрытых сделок. Не было способа устранить рассинхрон между DB и биржей.

### Что добавлено

**`backend/db.py`**:
- `delete_open_position(position_id)` → bool — удаляет запись из `open_positions` без действий на бирже

**`backend/main.py`** — 2 новых эндпоинта:

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/db/positions/enriched` | DB-позиции + live mark prices от Binance + unrealized PnL |
| DELETE | `/api/db/positions/{id}` | Удаление DB-записи (биржа не затрагивается) |

`SmartTradeRequest` получил поля `action: str = "open"` и `exit_zscore`.
`POST /api/trade/smart` с `action="close"`:
1. Находит DB-позицию по (sym1, sym2)
2. Получает актуальные qty с Binance (fallback → qty из DB)
3. Разворачивает стороны (long_spread → sell/buy, short_spread → buy/sell)
4. Запускает `run_execution` с `is_close=True`

**`backend/order_manager.py`**:
- `ExecContext` новые поля: `is_close`, `close_db_id`, `entry_price1/2`, `exit_zscore`
- `to_dict()` включает `is_close`
- В терминальном состоянии OPEN: если `is_close=True` → вычисляет PnL и вызывает `db.close_position()`; иначе → `save_open_position()` (прежнее поведение)

**`frontend/index.html`** — таб Positions полностью переработан:

**Секция «Стратегические позиции»** (DB + live Binance):
- Колонки: Пара, Сторона, Цены входа, Entry Z, Z-score (sparkline + текущее значение), Нереализ. PnL, Длительность, Действия
- Sparkline: Chart.js canvas 90×28px, последние 50 точек z-score (1h таймфрейм, окно=20), нулевая линия
- Текущий Z покрашен по уровню: серый (<1.5σ), жёлтый (1.5–2.5σ), красный (>2.5σ)
- 4 кнопки на строку: `↗` Load into Analysis, `✕ M` Market Close, `◎ S` Smart Close, `🗑` Delete DB Record

**Секция «Позиции на бирже»** — прежняя таблица Binance, переименована

**Секция «Журнал сделок»** — новая, сворачиваемая:
- `GET /api/db/history?limit=50` → закрытые сделки с PnL, Entry/Exit Z, датами
- Строки покрашены зелёным/красным tint по знаку PnL

**Другие улучшения**:
- `pollExecution` при любом терминальном статусе → обновляет обе таблицы позиций
- `_loadPosIntoAnalysis` заполняет символы + устанавливает `state.hedgeRatio` из DB (сайдбар-закрытие работает без повторного анализа)
- Toast при смарт-исполнении различает открытие («Позиция открыта») и закрытие («Позиция закрыта»)

### Что решено

| Проблема | Решение |
|---|---|
| Нет закрытия из UI | Кнопки ✕ M / ◎ S в каждой строке Strategy Positions |
| Параметры теряются при перезагрузке | DB хранит все параметры; ↗ Load восстанавливает всё за 1 клик |
| Нет истории сделок | Секция «Журнал сделок» показывает все закрытые позиции |
| Рассинхрон DB ↔ биржа | Кнопка 🗑 удаляет стале DB-записи с явным предупреждением |
| Z-score позиции непонятен без анализа | Sparkline прямо в таблице показывает тренд за последние 50 баров |

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
