# Changelog — Pair Trading Dashboard

---

## 2026-03-22 — Smart execution: сохранение partial fill при перепрайсе остатка

### Что изменено

- **`backend/order_manager.py` (`LegState.absorb_order`)**: учёт исполнения ноги сделан **кумулятивным across reprices**. Если старый ордер частично исполнился, а новый ордер выставляется только на остаток, `filled` больше не может откатиться назад к нулю из-за данных нового ордера. `remaining` теперь пересчитывается от целевого `qty`, а не слепо копируется из последнего order snapshot.
- **Подтверждённый runtime-сценарий, который исправлен**: partial fill `0.007` на первой LTC-неге + новый residual order `0.414` больше не приводит к тому, что бот закрывает только `0.414` и оставляет `0.007` orphan на бирже.
- **`tests/test_order_manager.py`**: добавлен тест на сценарий partial fill + reprice остатка (`0.007 + 0.414 = 0.421`), чтобы future changes не вернули регрессию.
- **Диагностическая instrumentation оставлена** в `backend/order_manager.py`, `backend/main.py`, `backend/db.py` для будущих расследований auto-trading инцидентов. Она уже помогла подтвердить:
  - потерю partial fill между старым и новым residual order,
  - расхождение `qty mismatch` между DB и биржей,
  - последующий `BOT ORPHAN` как следствие незакрытого хвоста.

### Что проверено после фикса

- Свежие runtime-логи после рестарта backend показали новые сделки с **partial → filled** без потери накопленного fill.
- `reconcile_positions` больше не фиксировал новых `qty mismatch` / `BOT ORPHAN` для свежих позиций после загрузки фикса.
- Полный набор тестов: **`368 passed`**.

---

## 2026-03-21 — Telegram: ясный текст алерта (порог входа vs уровень срабатывания)

### Что изменено

- **`backend/telegram_bot.py` (`notify_alert`)**: опциональный аргумент `fire_at` — фактический порог `|Z|` при `alert_pct` &lt; 100%; в сообщении отдельно показаны **Entry Z** и строка **«Срабатывание при |Z| ≥ …»**, плюс проценты от входного порога и от уровня срабатывания.
- **`backend/main.py`**: при срабатывании alert-триггера передаётся `fire_at=thresh` (`alert_pct * |entry_z|`).
- **`tests/test_telegram_bot.py`**: тест на отображение уровня срабатывания.

---

## 2026-03-21 — Telegram-алерт: не слать сразу при уже пробитом пороге

### Что изменено

- **`backend/main.py` (`monitor_position_triggers`)**: для `type=alert` при **первом** появлении триггера в этом процессе, если `|z|` уже ≥ `alert_pct * |entry_z|`, состояние гистерезиса выставляется в `alerted` **без** `notify_alert` и без `alert_fired`. Уведомление уйдёт только после опускания `|z|` ниже `TELEGRAM_ALERT_RESET_Z` и нового пересечения порога. В лог добавлены `tf` / `zscore_window` / `candle_limit` при срабатывании и при «sync».
- **`CLAUDE.md`**: описание этого поведения в разделе alert triggers.

---

## 2026-03-20 — Watchlist: индикация алерта + dedup алертов по TF / окну Z

### Что изменено

- **`backend/db.py`**: `find_active_alert` учитывает `timeframe`, `zscore_window` и **`candle_limit` (История свечей)**; две конфигурации с одной парой и порогом Z, но разными `candle_limit` и/или окном Z — **разные** алерты; замена при `POST` только при полном совпадении ключа (включая lookback).
- **`backend/main.py`**: в `find_active_alert` передаётся `req.candle_limit` (раньше дубликат не учитывал длину истории).
- **`tests/test_db.py`**: тесты на несовпадение по TF, по `zscore_window`, по `candle_limit`, сценарий 500/50 vs 1000/100.
- **`frontend/index.html`**: кэш триггеров при старте (`refreshTriggersCache` + `Promise.all` с `initWatchlist`); колокольчик на карточке watchlist всегда виден, если есть подходящий активный алерт; подписи `wl_alert_btn` / `wl_alert_active` (i18n); после добавления/отмены алерта — обновление списка и watchlist; из watchlist в `POST` алерта передаётся `candle_limit` (раньше API отклонял запрос без него).

---

## 2026-03-18 — Binance WebSocket kline streams + /ws/watchlist (событийная архитектура)

### Что изменено

**Новый файл `backend/symbol_feed.py`:**
- **`SymbolFeed`** — одно WS-соединение на `(symbol, timeframe)`: подписывается на `wss://fstream.binance.com/stream?streams={sym}@kline_{tf}` (USDT-M и USDC-M через один fstream)
- Начальная история загружается через REST один раз при старте; WS обновляет последнюю свечу live или добавляет новую при закрытии
- Auto-reconnect с экспоненциальным backoff (1 с → 60 с); после переподключения история обновляется через REST
- `wait_for_update(after_gen)` — event-driven уведомление потребителей (паттерн «replace event»: safe для N concurrent waiters)
- Дедупликация на уровне символа: 10 пар с BTC = 1 WS-соединение

**Backend (`backend/main.py`) — PriceCache:**
- `_feeds: dict[(sym, tf), SymbolFeed]` + `_feed_refs` — двухуровневый ref-count (символ + пара)
- `subscribe()` создаёт SymbolFeed, `run()` запускает их; `unsubscribe()` останавливает feed при ref=0
- `_assemble_from_feeds(key)` — собирает данные пары из буферов двух SymbolFeed (было: REST poll)
- `wait_update(key, timeout=5.0)` — event-driven ожидание для `/ws/stream`
- `wait_any_update(keys, timeout=5.0)` — ждёт обновления любого feed из набора пар; для `/ws/watchlist`
- `stop_all()` — graceful shutdown всех WS-фидов; вызывается в lifespan

**Backend (`backend/main.py`) — монитор TP/SL:**
- Удалён batch `asyncio.gather(fetch_ohlcv)` — больше нет прямых обращений к Binance
- Добавлен `_monitor_keys: dict[tag → cache_key]` (локальный для корутины): подписывает активные позиции/триггеры в PriceCache, автоматически отписывает стёртые

**Backend (`backend/main.py`) — новый WebSocket `/ws/watchlist`:**
- Заменяет `POST /api/watchlist/data` polling (5 с HTTP интервал → event-driven WS push)
- Протокол: клиент присылает полный список при подключении и при любом изменении; сервер пушит z-score/spread на каждое kline-событие
- Два concurrent tasks на соединение: `_receive_task` (реконсиляция подписок) + `_push_task` (wait_any_update → push)
- `send_lock` предотвращает concurrent sends

**Frontend (`frontend/index.html`):**
- `connectWatchlistWS()` заменяет `startWatchlistUpdater()` + `setInterval`
- `_sendWatchlistToWs()` — отправляет текущий список при изменениях (добавление/удаление пары)
- `_applyWatchlistUpdate(data)` — обрабатывает входящие WS сообщения (in-place DOM patch)
- Reconnect backoff `_wlWsBackoff` (1 с → 30 с) при обрыве соединения

### Эффект на архитектуру

| До | После |
|----|-------|
| PriceCache: REST poll Binance каждые 2 с × N пар | 1 WS на символ, event-driven |
| Watchlist: HTTP 5 с polling | WebSocket push, sub-second |
| Монитор: N×2 REST fetch_ohlcv/цикл | 0 REST, читает PriceCache |
| /ws/stream: asyncio.sleep(2) | wait_update, событийно |
| Binance rate limit: ~N×2 вызовов/2 с | 1 REST при старте, WS далее |

### Новые тесты

**`tests/test_symbol_feed.py`** (15 тестов):
- `_to_ws_symbol_*` — конвертация ccxt-символа в Binance stream name
- `_handle_kline_*` — обновление in-place vs append новой свечи
- `_wait_for_update_*` — event-driven уведомление, N concurrent waiters
- `test_load_initial_with_none_client_sets_ready` — guard для тестов без клиента

**`tests/test_price_cache.py`** (22 → 33 тестов):
- `test_subscribe_creates_symbol_feeds`, `test_symbol_deduplication_shared_feed`
- `test_unsubscribe_stops_feed_when_no_more_refs`, `test_unsubscribe_keeps_feed_when_other_pair_still_uses_symbol`
- `test_assemble_from_feeds_*` (populate, align, skip if empty)
- `test_wait_update_*`, `test_wait_any_update_*`, `test_stop_all_sets_stopped_flag_on_all_feeds`

### Итого тестов: 246 (+28)

---

## 2026-03-18 — Оптимизация производительности: numpy OLS, кеш коинтеграции, фоновый пресчёт

### Что изменено

**Backend (`strategy.py`):**
- **numpy вместо statsmodels OLS** — `calculate_hedge_ratio` и `calculate_half_life` заменены с `statsmodels.OLS` на `numpy.linalg.lstsq`. Результат идентичный, скорость ~10x выше (убраны стандартные ошибки, t-тесты и прочее что не используется). Импорты `OLS` и `add_constant` удалены.
- **`maxlag=10` в тесте коинтеграции** — `coint(log1, log2, maxlag=10)` вместо авто-подбора лага. Сокращает время теста в 2–3x без потери точности для финансовых рядов.

**Backend (`main.py`):**
- **`_coint_cache`** — кеш результатов коинтеграции с TTL 10 минут: `(sym1, sym2, tf, limit) → (result, timestamp)`. Повторный анализ той же пары пропускает самую тяжёлую операцию.
- **`_precompute_coint`** — фоновая async-задача: вычисляет коинтеграцию в thread pool и кладёт в кеш. Запускается через `asyncio.create_task`, не блокирует ответ.
- **Фоновый пресчёт в `get_watchlist_data`** — после каждого опроса вотчлиста (каждые 5 сек) для пар с устаревшим или отсутствующим кешем коинтеграции запускается `_precompute_coint`. Через 5–15 сек после добавления пары в вотчлист анализ становится мгновенным.
- **`asyncio.get_running_loop()`** — заменено устаревшее `get_event_loop()` в `_run_sync`.

**Frontend (`index.html`):**
- Удалена лишняя переменная `_wlNorm` в `updateWatchlistZScores` (объявлялась но не использовалась).

### Эффект на производительность

| Операция | До | После |
|---|---|---|
| Первый анализ пары (M1 Pro) | 3–4 сек | 1.5–2 сек |
| Повторный анализ (кеш) | 3–4 сек | <0.5 сек |
| Переключение между парами вотчлиста | 3–4 сек | <0.5 сек (после ~15 сек прогрева) |
| Raspberry Pi 5: первый анализ | ~15 сек | ~5–7 сек |
| Raspberry Pi 5: повторный анализ | ~15 сек | <1 сек |

---

## 2026-03-18 — Оптимизация запросов: dashboard endpoint, batch sparklines, PriceCache для анализа

### Что изменено

**Backend (`main.py`):**
- **`GET /api/dashboard`** — новый объединённый эндпоинт: возвращает позиции (enriched) + балансы + недавние алерты одним запросом. Binance вызовы (`get_positions` + `get_all_balances`) выполняются параллельно через `asyncio.gather`
- **`POST /api/batch/sparklines`** — новый batch эндпоинт: принимает массив `(sym1, sym2, timeframe, limit, zscore_window)`, возвращает z-score/spread/hedge_ratio для каждой позиции. Использует PriceCache когда данные доступны; остальные запрашивает параллельно с Binance. Все вычисления в одном `_run_sync` вызове
- **PriceCache хранит полные DataFrames** — `_store[key]` теперь содержит `df1`/`df2` (full OHLCV) помимо `price1`/`price2` (close). Это позволяет `/api/history` использовать кеш для расчёта ATR
- **`PriceCache.find_cached(sym1, sym2, tf, limit)`** — ищет кешированные данные для пары; возвращает entry с `cached_limit >= requested_limit`
- **`/api/history` использует PriceCache** — если пара уже подписана в кеше (watchlist/WS), анализ берёт данные из кеша мгновенно вместо запроса к Binance (2-3 сек → <100мс)

**Frontend (`index.html`):**
- **`loadAllPositions()` → один `/api/dashboard`** — вместо 3 параллельных запросов (`/api/all_positions` + `/api/balance` + `/api/alerts/recent`). Алерты обрабатываются inline
- **`_batchLoadSparklines(positions)`** — вместо N отдельных `/api/history` вызовов, все non-current позиции запрашиваются одним `POST /api/batch/sparklines`
- **Adaptive exec poller** — 2 сек при активных исполнениях, 5 сек в покое (`setTimeout` вместо `setInterval`)

### Влияние на запросы

| До | После |
|----|-------|
| ~88 запросов/мин к backend | ~24 запросов/мин |
| 3 запроса × 12/мин (positions+balance+alerts) | 1 × 12/мин (dashboard) |
| N × 2/мин (sparklines per position) | 1 × 2/мин (batch) |
| 30/мин (exec poller) | 12/мин (adaptive) |
| Анализ watchlist-пары: 2-3 сек (Binance API) | <100мс (PriceCache) |

### Новые тесты
- `test_find_cached_exact_key` — точное совпадение ключа
- `test_find_cached_larger_limit` — кеш с 500 строками покрывает запрос на 100
- `test_find_cached_smaller_limit_misses` — кеш с 100 строками не покрывает запрос на 500
- `test_find_cached_different_timeframe_misses` — другой таймфрейм не матчится
- `test_find_cached_empty_store` — пустой кеш возвращает None

### Итого тестов: 218 (+5)

---

## 2026-03-18 — Производительность, исправление двойного закрытия, direction-agnostic TP/SL

### Оптимизация производительности

**Frontend (`index.html`):**
- **Sparkline throttle (30 с)** — `_loadSparkline()` теперь пропускает API-запрос `/api/history` если данные для позиции были получены менее 30 секунд назад; кэш `_sparklineLastFetch` с TTL 30 с
- **Убран двойной вызов `loadAllPositions()`** — в 5-секундном интервале вызывались и `loadStrategyPositions()` и `refreshPositions()` (оба alias на `loadAllPositions()`); теперь один вызов
- **In-place обновление Z-score в watchlist** — `updateWatchlistZScores()` обновляет только `<span id="wl-z-${i}">` вместо полного `renderWatchlist()` при каждом тике; fallback на полный rebuild если DOM не найден

**Backend (`main.py`):**
- **`_run_sync()` — CPU-bound расчёты в thread-pool** — OLS, коинтеграция, z-score больше не блокируют asyncio event loop; используется `loop.run_in_executor()` для `/api/history` и `/api/watchlist/data`
- **Batch вычисления для watchlist** — 14 пар в watchlist обрабатываются одним вызовом `_run_sync(_batch_calc)` вместо 14 последовательных
- **Параллельные вызовы Binance в smart trade** — `check_min_notional` (2 leg) и `set_leverage` (2 symbol) выполняются через `asyncio.gather`
- **Параллельные fetch OHLCV в мониторе** — все позиции и триггеры собирают fetch-спеки, дедуплицируются, выполняются одним `asyncio.gather`
- **Кэш hedge ratio (60 с)** — standalone триггеры не пересчитывают hedge ratio каждые 2 с; `_hedge_cache` с TTL 60 с

### Критический баг-фикс: двойное закрытие позиции

**Проблема:** При срабатывании TP позиция закрывалась дважды — один раз через legacy `tp_zscore` поля позиции, второй через standalone trigger в таблице `triggers`. Разные `tag` ключи (`pos_X` vs `trig_Y`) означали что `closing_tags` не предотвращал дубль.

**Решение (`main.py`):** Добавлен `closing_pairs: set[tuple]` — отслеживает `(sym1, sym2)` пары в процессе закрытия. Проверяется до запуска любого закрытия (и для position triggers, и для standalone triggers).

### Критический баг-фикс: TP срабатывал сразу для short_spread

**Проблема:** Условие `current_z <= tp` (напр. `-2.8 <= 2.2`) всегда истинно когда z отрицательный → TP срабатывал мгновенно после установки.

**Решение — direction-agnostic TP/SL:**
- **Backend `monitor_position_triggers`**: `abs_z = abs(current_z)`; TP когда `abs_z <= tp`, SL когда `abs_z >= sl`
- **Frontend `_checkWsTriggers`**: `absZ = Math.abs(currentZ)`; аналогичная логика
- TP/SL теперь задаются только положительными числами (напр. TP=0.5, SL=4.0)
- На графике отображаются две симметричные горизонтальные линии (±threshold)

### Исправление popup исполнения

- **Popup не появлялся при открытии сделки**: добавлен `_execSeenIds` Set + немедленный первый poll
- **Popup не появлялся при срабатывании TP**: backend-initiated closes обнаруживались только если poller активен; `_startExecPoller()` теперь вызывается всегда
- **Старые popup при перезагрузке страницы**: `_execFirstPoll` flag — первый poll только открывает non-terminal executions
- **Poller timing gap**: убрана 2с задержка до первого poll; убрана auto-stop логика

### Итого тестов: 213 (без изменений)

---

## 2026-03-18 — UI: короткие символы без USDT/USDC суффикса

### Что изменено

**Frontend (`index.html`):**
- **Новый хелпер `_dispSym(sym)`** — `"BTC/USDT:USDT"` / `"BTCUSDT"` → `"BTC"` (только для отображения)
- **Новый хелпер `_expandSym(s)`** — `"BTC"` → `"BTCUSDT"` или `"BTCUSDC"` по активному market filter (перед отправкой в API)
- **Все места отображения** используют `_dispSym()`: watchlist карточки, таблица Strategy Positions, таблица Exchange Positions, вкладка Alerts, журнал, popup умного исполнения, price chart labels, confirm-диалоги закрытия/удаления
- **Все читки инпутов для API** обёрнуты в `_expandSym()`: `runAnalyze`, `runBacktest`, `executeTrade`, `fetchPreTradeCheck`, `startSmartExecution`, `addCurrentPairToWatchlist`, `addAlertFromPanel`, `addWatchlistItem`, `updateSizePreview`, `saveAnalysisState`, `getCurrentPairMeta`
- **Все сеттеры инпутов** используют `_dispSym()` при загрузке пары из кода: `loadPairIntoAnalysis`, `_loadPosIntoAnalysis`, `_loadAlertIntoAnalysis`, `restoreAnalysisState`, `applyGuideExample` — backward-compatible (старые сохранённые состояния с `"BTCUSDT"` корректно конвертируются в `"BTC"` при восстановлении)
- **Все нормализующие лямбды** (`_wlNorm`, `_spNorm`, `_alNorm`, `normSym` в `_loadSparkline` и `updateLiveData`) дополнены `.replace(/USDT$|USDC$/,'')` — сравнения `"BTC" === "BTC"` работают для значений из инпута и из БД одновременно
- **`populateDatalist`** — при filter=USDT или USDC показывает короткие символы (`BTC`, `ETH`...); при ALL — полные (иначе два одинаковых "BTC" из USDT-M и USDC-M)
- **HTML плейсхолдеры** обновлены: `BTCUSDT` → `BTC`, `ETHUSDT` → `ETH` (sym1-input, sym2-input, wl-sym1, wl-sym2)

### Итого тестов: 213 (без изменений — все изменения в frontend JS)

---

## 2026-03-18 — Watchlist: несколько таймфреймов для одной пары

### Что изменено

**Frontend (`index.html`):**
- **`_addToWatchlist`** — ключ дедупликации изменён с `(sym1, sym2)` на `(sym1, sym2, timeframe)`. Одна и та же пара с разными таймфреймами теперь хранится как отдельные записи; добавление BTC/ETH на 5m больше не перезаписывает BTC/ETH на 4h
- **`renderWatchlist`** — визуальная группировка по таймфреймам: заголовок-разделитель (`5m`, `1h`, `4h` и т.д.) перед каждой группой; порядок групп: 5m → 15m → 30m → 1h → 2h → 4h → 8h → 1d. Таймфрейм убран из каждой строки (виден из заголовка группы)
- **`updateWatchlistZScores`** — матчинг ответа API изменён с `(sym1, sym2)` на `(sym1, sym2, timeframe)`: без этого обе записи BTC/ETH обновлялись бы первым попавшимся значением

**Backend (`main.py`):**
- **`GET /api/watchlist/data`** — ответ теперь включает поле `timeframe` в каждом объекте; без него фронтенд не мог понять, к какой записи относится значение z-score

### Итого тестов: 213 (без изменений — все изменения в frontend JS и ответе API)

---

## 2026-03-17 — UI оптимизация: компактные stat-карточки, маркеры на PnL-оси, Binance в header

### Что изменено

**Frontend (`index.html`):**

#### 1. Redesign stat-карточек
- Новый вертикальный макет: лейбл сверху, значение снизу (`flex-col`)
- Лейблы сокращены в i18n: "Коэф. хеджа" → "β", "Полураспад" → "HL", "Показ. Хёрста" → "H", "Корреляция" → "ρ", "Коинтегрированы" → "p-val", "Z-счёт" → "Z"
- **Цветовая кодировка через `style.color`** (НЕ Tailwind classList — CDN не генерирует CSS для классов, добавленных только через JS):
  - Z-score: зелёный (≤−2), красный (≥+2), жёлтый (иначе)
  - Half-Life: зелёный (10–50 баров), жёлтый (5–10 или 50–200), красный (<5 или >200)
  - Hurst: зелёный (<0.4), жёлтый (0.4–0.6), красный (>0.6)
  - Корреляция: зелёный (≥0.75), жёлтый (0.5–0.75), красный (<0.5)
  - p-val (коинтеграция): зелёный (p<0.05), жёлтый (p<0.10), красный (иначе) — показывает только число, убран текст Yes/No
- Константы цветов: `C_GREEN='#3fb950'`, `C_YELLOW='#d29922'`, `C_RED='#f85149'`
- Хелпер `_statColor(el, color)` устанавливает `el.style.color`
- PnL-sublabel под Z: берётся из последней точки синей линии графика (`dollarData.at(-1)`), НЕ из формулы `z*std`; синхронизируется в `renderSpreadChart` и `refreshLiveCharts` (каждые 2 с)

#### 2. Кнопки Long/Short перенесены в строку stat-карточек
- Удалены из правой торговой панели (включая обёртки с tooltip и кнопку "Close All Positions")
- Добавлены в правый конец строки stat-карточек с `ml-auto`
- Tooltip с кнопок удалён
- Кнопка "Close All Positions" полностью удалена
- JS: `'close-btn'` удалён из массива enable/disable

#### 3. Статус Binance перенесён в header
- Удалена полная секция-карточка из правой панели
- Добавлен компактный элемент в header между WS-статусом и кнопкой Guide
- `renderApiStatus` теперь рендерит компактно: `● USDT·$424  USDC·$426` (connected), или точка+лейбл (другие состояния)
- Использует `style` для точек (не Tailwind) для надёжности

#### 4. Цвет WS-лейбла
- `setWsStatus(connected)` устанавливает `lbl.style.color` через `C_GREEN`/`C_RED`
- Обновляется в `applyLocale()` при смене языка

#### 5. Частота поллинга алертов
- Было: отдельный `setInterval(checkRecentAlerts, 60000)`
- Стало: `checkRecentAlerts()` вызывается внутри существующего 5-секундного интервала позиций
- Один таймер вместо двух; максимальная задержка 5 с вместо 60 с
- При обнаружении нового алерта → `loadAlertsTab()` вызывается немедленно

#### 6. Маркеры сделок привязаны к PnL-оси
- Все маркеры (▲▼✕ + лейблы) изменены с `yScaleID: 'yZ'` на `yScaleID: 'ySpread'`
- `yValue` изменён с z-score значения на фактический долларовый PnL в этот timestamp
- Новый хелпер `_pnlAtLabel(lbl)`: читает напрямую из `state.spreadChart.data.datasets[0].data` (гарантированно то же, что на графике)
- Лейбл входа изменён с "Вход Z +2.73" на "Вход +$0.52" (долларовое значение)
- Исправлено: scale ID был `'ySpread'` а не `'y'` — маркеры падали на 0

#### 7. Исправление PnL в таблице позиций
- Было: `pnlPerZ = spread_std * size_usd` (неправильная формула на основе z-score)
- Стало: вычисляет `dollarPerUnit = size_usd / (1 + |beta|)`, `meanSpread` из полного массива, `dollarData = (spread - mean) * dollarPerUnit` — идентично основному графику
- PnL входа: находит ближайший timestamp в `data.timestamps` к `pos.opened_at`, возвращает `dollarData[idx]`
- Текущий PnL: последняя точка `dollarData`
- Работает для текущей пары (`state.historyData`) и для других пар (полученные данные)

### Ключевой баг-фикс (архитектурный)
**Tailwind CDN не генерирует CSS для классов, добавленных только через `classList.add()` в JS** — только для классов, присутствующих в HTML при парсинге. Цвета stat-карточек не работали через динамические Tailwind-классы. Решение: использовать `element.style.color` с явными hex-значениями.

### Итого тестов: 213 (без изменений)

---

## 2026-03-16 — size_usd = total position, PnL ($) ось на графике

### Что изменено

**Backend (`strategy.py`):**
- **`size_usd` теперь означает ОБЩИЙ размер позиции** (обе ноги вместе), а не размер первой ноги
  - OLS: пропорциональное разделение `1 : |β|` → `leg1 = size / (1+|β|)`, `leg2 = size*|β| / (1+|β|)`
  - Equal: каждая нога = `size / 2` (раньше каждая = `size`)
  - ATR: `qty1 = size / (P1 + ratio*P2)`, `qty2 = qty1 * ratio`
- **`calculate_backtest()`** — PnL формула обновлена для total-size семантики

**Frontend (`index.html`):**
- **`updateSizePreview()`** — зеркалит новую логику бэкенда
- **Ось спреда → PnL ($)** — левая Y-ось графика теперь показывает доллары вместо лог-значений
  - Формула: `(spread - mean) * size_usd / (1 + |β|)`
  - `$0` соответствует z-score = 0 (спред на среднем)
  - Пересчитывается при «Анализировать» и при live-обновлениях через WebSocket
- **Выравнивание нулей осей** — обе оси (PnL и Z-score) принудительно симметричны относительно нуля, чтобы `$0` и `z=0` были на одной горизонтальной линии
- **Тултипы обновлены** (EN + RU) — объясняют как читать PnL ось

**Документация:**
- `CLAUDE.md` — обновлены формулы сайзинга, количество тестов

### Новые тесты
- `test_position_sizes_total_equals_size_usd` — проверяет что `value1 + value2 == size_usd` для всех методов

### Итого тестов: 213 (+1)

---

## 2026-03-16 — Фикс: бесконечный popup при выставлении TP/SL

### Исправленные баги

**Frontend:**
- **Popup исполнения постоянно всплывал при наличии TP/SL** — `_startExecPoller()` запускался когда у любой позиции выставлен TP/SL; `_pollAllExecutions` вызывал `_openExecPopup()` для **всех** записей из `GET /api/executions`, включая терминальные (`OPEN`/`DONE`/`CANCELLED`/`FAILED`), которые висят в памяти до 2 часов (TTL). Popup автоматически закрывался через 15 сек → через 2 сек поллер снова открывал → бесконечная петля. Фикс: `_pollAllExecutions` теперь авто-открывает popup **только** для не-терминальных (активно работающих) исполнений; терминальные обновляются если popup уже открыт, но никогда не создают новый

### Итого тестов: 212 (без изменений)

---

## 2026-03-18 — Execution history, фикс TP-линий, фикс ложного toast TP

### Что добавлено

**Backend:**
- **Персистентная история исполнений** — новая таблица `execution_history` в SQLite: сохраняет финальный снапшот каждого завершённого исполнения (`exec_id`, `db_id`, `close_db_id`, `is_close`, `status`, `symbol1/2`, `data_json`, `completed_at`)
- `db.save_execution_history(...)` — `INSERT OR IGNORE` (идемпотентно); `db.get_execution_history(limit=100)` — сортировка по `completed_at DESC`
- Новый endpoint `GET /api/executions/history` — список всех сохранённых исполнений
- Хук в мониторе: терминальные контексты (`DONE`/`CANCELLED`/`FAILED`/`OPEN`) сохраняются в `execution_history` перед TTL-очисткой (`_exec_saved_to_db: set` гарантирует однократную запись)
- **`OPEN` добавлен в TTL-cleanup** — ранее контексты со статусом `OPEN` никогда не удалялись, что вызывало бесконечную петлю переоткрытия popup. Теперь удаляются через 2 ч наравне с остальными терминальными статусами

**Frontend:**
- **`loadExecHistory()`** — загружает `GET /api/executions/history` при старте страницы; заполняет `_execHistory` и `_execHistoryByDbId` (Map db_id → exec_id); обновляет кнопки 📋 во всех строках позиций
- **`_execStatusHtml(exec, posId)`** — принимает `posId`; всегда показывает 📋 Лог если в `_execHistoryByDbId` есть запись для данной позиции, даже после перезагрузки страницы
- **`_startExecPoller()` в `loadAllPositions()`** — автоматически запускает поллинг когда хоть у одной позиции выставлен TP/SL; позволяет поймать закрытие, инициированное бэкендом

### Исправленные баги

**Frontend:**
- **TP-линии исчезали сразу после нажатия «Уст.»** — гонка условий: `loadAllPositions()` внутри `_setTriggers` запускал fetch до того, как POST сохранял `tp_zscore`; возвращался старый `null` → `_updatePositionAnnotations()` прятал линии. Фикс: убран вызов `loadAllPositions()` из `_setTriggers` и `_cancelTrigger`; локальное состояние обновляется напрямую (`_stratPosMap[id].tp_zscore = tp` + `_tpslBadgesHtml()`)
- **Кнопка 📋 не появлялась после перезагрузки страницы** — в in-place ветке обновления строки позиции кнопка не рендерилась. Фикс: `_execStatusHtml` перемещена в div `exec-status-{id}`, который обновляется in-place; после сохранения в `_execHistoryByDbId` div немедленно перерисовывается
- **Ложный toast «Take Profit достигнут — идёт закрытие»** — `_checkWsTriggers` на фронтенде вычислял z на датасете из 1000 свечей, монитор бэкенда — на `max(zscore_window*3, 60)` свечей (например 300 при window=100). Разный датасет → разный mean/std → разный z → фронтенд видел пересечение порога там, где бэкенд не видел. Фикс: монитор использует `min(candle_limit, 500)` вместо `max(zscore_window*3, 60)` (читает `candle_limit` из позиции в БД)
- **Текст toast вводил в заблуждение** — «идёт закрытие» подразумевало немедленное действие, но фронтенд только уведомляет, закрытие делает только монитор. Фикс: RU → «порог достигнут — монитор закрывает», EN → «threshold reached — monitor will close»

### Тесты

- `test_db.py`: +7 тестов для `execution_history` — `save/get basic`, `idempotent INSERT OR IGNORE`, `is_close=True`, `empty list`, `limit param`, `newest first order`, `all 4 terminal statuses`

### Итого тестов: 212 (+7 от предыдущих 204, +1 за test_order_manager.py → 4)

---

## 2026-03-17 — Подсветка активной пары, фикс graceful shutdown

### Что добавлено

**Frontend:**
- **Подсветка активной пары** в watchlist, вкладке Позиции и вкладке Alerts: активная пара выделяется синим фоном (`bg-blue-950/40`) и синей левой рамкой; в watchlist дополнительно — синяя точка вместо threshold-индикатора
- Критерий совпадения — **5 параметров**: sym1 + sym2 + timeframe + zscore_window + entryZ (с допуском 0.01 для float). Только тикер недостаточен: одна пара может быть в watchlist с разными timeframe/z-порогами
- Подсветка обновляется **сразу** после `runAnalyze()` (не ждёт 5-секундного тика watchlist)
- `_updateStrategyPosHighlights()` — обновляет класс на существующих строках позиций без full rebuild
- `_cachedAlerts` — кэш последних загруженных алертов; позволяет перерисовать Alerts без дополнительного API-запроса при смене пары

### Исправленные баги

**Backend:**
- **Uvicorn не останавливался по Cmd+C**: lifespan не сохранял ссылки на background tasks (`price_cache.run()`, `monitor_position_triggers`, `tg_bot.start_polling()`). При SIGTERM нечего было отменять — бесконечные циклы зависали. Fix: `_bg_tasks = [asyncio.create_task(...), ...]`; shutdown делает `for t in _bg_tasks: t.cancel()` + `await asyncio.gather(*_bg_tasks, return_exceptions=True)`

**Frontend:**
- **Новый алерт не появлялся сразу** при создании через кнопку `🔔 Alert` в панели Настройки пары (`addAlertFromPanel`): `loadAlertsTab()` вызывался только если пользователь кликал на toast, а не сразу. Fix: `loadAlertsTab()` вызывается сразу после `res.ok`
- **Нормализация символов в highlight**: `sym1-input` мог содержать ccxt-формат `BTC/USDT:USDT`, а watchlist хранит `BTCUSDT` → подсветка не срабатывала. Fix: `_wlNorm()` применяется и к значениям из input-полей

### Тесты
- `test_lifespan.py` — 5 новых тестов для паттерна asyncio graceful shutdown: бесконечные задачи отменяются, `CancelledError` поглощается `return_exceptions=True`, already-done задачи не ломаются, смешанные типы задач

### Итого тестов: 204

---

## 2026-03-16 — Исправление двойного триггера, монитор 2с, синхронизация z-score

### Исправленные баги

**Backend:**
- **Двойной триггер** — монитор мог сработать дважды за один цикл (два Telegram-сообщения, открытие обратной позиции на бирже). Причина: `closing_tags.add(tag)` мог пропуститься при исключении. Фикс: `db.set_position_triggers(pos_id, None, None, False)` вызывается до старта закрытия — следующий цикл видит `tp is None and sl is None → continue`; защита на уровне БД, не памяти
- **Монитор использовал хардкод `timeframe="1h"` и `zscore_window=20`** вместо параметров позиции из БД. TP/SL срабатывали при неверном уровне z-score (особенно критично при торговле на 5m с большим окном). Фикс: монитор читает `pos.get("timeframe")` и `pos.get("zscore_window")` из каждой позиции

**Frontend:**
- **Z-score в строке позиции не совпадал с шапкой** — `_loadSparkline` делал запрос с `pos.candle_limit` (из БД), а WebSocket использовал `state.historyLimit` (текущий анализ). Разный размер датасета → разный mean/std → разный z. Фикс: если пара совпадает с текущим анализом, `_loadSparkline` читает данные из `state.historyData` напрямую (без лишнего запроса), гарантируя идентичный z-score
- **TP/SL badge не появлялся сразу** после выставления — `_setTriggers()` не обновлял UI. Фикс: `loadAllPositions()` вызывается после успешного сохранения
- **Badge не обновлялся при отмене** — in-place ветка `renderStrategyPositions` обновляла только PnL-ячейку. Фикс: добавлен `id="tpsl-badges-{id}"` + `_tpslBadgesHtml()` helper, обновляется in-place
- **TP=0 нельзя было сохранить** — `parseFloat('0') || null` → null (JS falsy). Фикс: явная проверка на пустую строку
- **Кнопка ◎ (smart) всегда серая** по умолчанию — CSS класс был хардкодным. Фикс: класс зависит от `pos.tp_smart`; для новых позиций без TP `pos.tp_smart` устанавливается `true` (дефолтный режим)

### Что добавлено

**Backend:**
- Новый endpoint `GET /api/executions` — список всех активных контекстов исполнения (для frontend-мониторинга прогресса)
- `order_manager.ExecContext.to_dict()` теперь включает `close_db_id`

**Frontend:**
- Z-score в строке позиции обновляется в реальном времени: `updateLiveData()` при каждом WS-сообщении обновляет `z-cur-{id}` для совпадающей пары — синхронно с шапкой
- Удалена вкладка **«Ордера TP/SL»** (была источником путаницы: TP/SL позиций видны в строке позиции, алерты — во вкладке Alerts)
- `_toggleTpSmart` автоматически сохраняет изменение если TP уже выставлен — нет нужды перевыставлять вручную
- Прогресс smart-исполнения показывается inline в строке позиции (`id="exec-status-{id}"`)
- Тосты: `toast_tp_hit`, `toast_sl_hit`, `toast_exec_closed`, `toast_exec_rollback`, `toast_exec_failed`

### Тесты

- `test_db.py`: +1 тест — `test_set_position_triggers_clear_all_resets_tp_smart` — паттерн анти-двойного-триггера: вызов `(None, None, False)` обнуляет и tp_zscore, и sl_zscore, и tp_smart

---

## 2026-03-15 — Notification center + кнопка Alert в панели настройки пары

### Что добавлено

**Backend:**
- `db.py`: колонка `last_fired_at TEXT` в triggers; `alert_fired(id)` — записывает timestamp срабатывания, сохраняет `status='active'` (гистерезис продолжает работать); `get_recent_alerts(minutes=60)` — алерты, сработавшие за последние N минут
- `main.py`: монитор вызывает `db.alert_fired(trig_id)` при каждом срабатывании; новый endpoint `GET /api/alerts/recent?minutes=60`

**Frontend:**
- **Notification center**: при загрузке страницы и каждые 60 с вызывается `checkRecentAlerts()` → кликабельный toast с парой и временем (`🔔 Алерт: BNB/USDC / SOL/USDC — 5 мин назад`), клик открывает вкладку Alerts
- **Badge** на кнопке 🔔 Alerts — жёлтый кружок с количеством; исчезает при открытии вкладки
- **Подсветка строк** в таблице Alerts: жёлтый фон + `⚡ X мин назад` в колонке "Last fired" для срабатываний последнего часа
- **Кнопка `🔔 Alert`** рядом с `★ В Watchlist` в панели Настройки пары — `addAlertFromPanel()` берёт sym1/sym2/timeframe/zscore_window/entry-z из текущего состояния анализа; позволяет создавать алерты без добавления пары в watchlist
- Toast "Анализ завершён" убран — больше не вытесняет алерт-уведомление
- `showToast()` расширен: параметры `duration` (мс) и `onClick` (callback при клике)

**Tests:**
- `test_db.py`: +9 новых тестов — `alert_fired` (updates last_fired_at, keeps status active, false on missing/cancelled), `get_recent_alerts` (returns fired, excludes unfired/cancelled/tp-sl/old, multiple)

### Итого тестов: 194

---

## 2026-03-15 — Telegram алерты: отдельная вкладка, настраиваемый порог, timeframe-aware мониторинг

### Что добавлено

**Backend:**
- `db.py`: новые колонки в таблице `triggers` — `timeframe TEXT DEFAULT '1h'`, `zscore_window INTEGER DEFAULT 20`, `alert_pct REAL DEFAULT 1.0`
- `db.py`: новая функция `find_active_alert(sym1, sym2, zscore)` — поиск существующего активного алерта для dedup-логики
- `db.py`: `save_trigger()` принимает `timeframe`, `zscore_window`, `alert_pct`
- `main.py`: `TriggerCreateRequest` расширен полями `timeframe`, `zscore_window`, `alert_pct`
- `main.py`: `POST /api/triggers` для `type="alert"` — заменяет дубликат (same sym1/sym2/zscore) вместо создания второй записи; разные zscore — разные алерты
- `main.py`: монитор теперь использует `trig["timeframe"]` / `trig["zscore_window"]` для подписки на PriceCache и расчёта z-score (раньше — фиксированные `1h`/`20`)
- `main.py`: монитор использует `alert_pct * abs(trig_z)` как порог вместо захардкоженных 90%

**Frontend:**
- Новая вкладка **🔔 Alerts** в нижней панели (между TP/SL и Journal)
- `loadAlertsTab()` / `renderAlerts(alerts)` — отдельный рендер алертов из `GET /api/triggers` (только `type=alert`)
- `loadOrdersTab()` теперь показывает только `tp`/`sl` — алерты вынесены из этой вкладки
- В таблице Alerts: Pair, Timeframe, Z-score, Z-window, **Threshold** (процент + реальный z-score срабатывания), Status, Created, Cancel
- Клик на строку алерта → `_loadAlertIntoAnalysis(trig)` — загружает пару в основной график с сохранёнными параметрами (sym1/sym2, timeframe, zscore_window, entry-z)
- При создании алерта (кнопка 🔔 в watchlist) — prompt с вопросом «при каком % от порога?», по умолчанию **100%**; диапазон 1–200%
- После создания алерта — автоматический переход на вкладку Alerts

**Tests:**
- `test_db.py`: +9 новых тестов — `save_trigger` defaults/custom для timeframe/zscore_window/alert_pct, `find_active_alert` (match, miss, different zscore, cancelled, multiple)

### Исправленные баги

- **Монитор использовал фиксированный `1h`/`20` для всех standalone-триггеров** — теперь каждый триггер хранит собственные параметры и монитор подписывается на PriceCache с ними; z-score в мониторе совпадает с z-score, который видит пользователь в watchlist
- **Алерт срабатывал жёстко на 90%** — теперь настраивается при создании (default 100%)
- **Дубли алертов** — при создании алерта с тем же (sym1, sym2, zscore) старый автоматически отменяется

### Итого тестов: 185

---

## 2026-03-15 — Telegram Bot: уведомления + алерты из watchlist

### Что добавлено

**`backend/telegram_bot.py`** (новый файл, ~260 строк):
- Интеграция через `aiogram v3` (asyncio-native, пригоден для будущего bot-управления)
- `setup()` / `start_polling()` / `stop()` — lifecycle, вызывается из `main.py` lifespan
- `send(text)` — никогда не выбрасывает исключение; ошибки логируются, торговля не прерывается
- `_fire(text)` — `asyncio.create_task(send(text))`, non-blocking
- Функции уведомлений:

| Функция | Триггер |
|---|---|
| `notify_position_opened` | `/api/trade` open + order_manager OPEN state |
| `notify_position_closed` | `_do_market_close` + `/api/trade` close + smart close |
| `notify_trigger_fired` | monitor — перед закрытием по TP/SL |
| `notify_alert` | monitor — при достижении порога z-score алерта |
| `notify_rollback` | order_manager — частичное исполнение, откат |
| `notify_execution_failed` | order_manager — критическая ошибка |

- `notify_position_opened` управляется флагом `TELEGRAM_NOTIFY_OPENS`
- `/start` и `/status` команды-заглушки (основа для будущего bot-управления)

**`.env` / `.env.example`** — новые переменные:
```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_NOTIFY_OPENS=true
TELEGRAM_ALERT_RESET_Z=0.5
```

**Алерты (тип `"alert"` в таблице `triggers`):**
- Создаются кнопкой 🔔 в watchlist
- Monitor обрабатывает алерты отдельно, никогда не закрывает позиции
- Гистерезис: `alert_states: dict[str, str]` — `"idle"` → пересечение порога → `"alerted"` → z возвращается ниже `ALERT_RESET_Z` → `"idle"`

**`tests/test_telegram_bot.py`** (новый файл, 56 тестов):
- Форматтеры (`_fmt_pair`, `_fmt_side`, `_fmt_pnl`), `is_configured`, `send()`, все `notify_*`
- Паттерн: `_capture_fire(monkeypatch)` заменяет `_fire` синхронным коллектором
- Без `pytest-asyncio` — используется `asyncio.run()`

### Исправленные баги

- **`_do_smart_close_trigger` передавал неправильные kwargs** в `ExecContext`: `sym1=`, `side1=`, `qty1=` → исправлено на `exec_id=`, `leg1=LegState(...)`, `leg2=LegState(...)`
- **`_fmt_pnl(-50.0)` возвращал `"$-50.00"` вместо `"-$50.00"`** — исправлено через `f"-${abs(pnl):.2f}"`

### Итого тестов: 177

---
