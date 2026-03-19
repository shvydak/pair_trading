# Position Tracking — Полная спецификация

## Архитектурный принцип

> **DB — единственный источник правды для qty и direction. Биржа — только для цен, комиссий и PnL.**

Биржа существует в измерении символов. Платформа существует в измерении пар.
Все qty для открытия/закрытия берутся из DB, никогда с биржи напрямую.

---

## Блок 1 — Критические фиксы (без этого нельзя торговать несколькими парами)

### 1.1 Close direction из DB
**Файлы:** `backend/main.py` (~372, ~1967)

При закрытии не смотреть на биржу для определения направления ордера.
Всегда читать из DB `pos["side"]`:
- `long_spread` → закрытие: sell leg1, buy leg2
- `short_spread` → закрытие: buy leg1, sell leg2

Работает независимо от состояния биржи (даже если нога закрыта вручную).

### 1.2 Close qty из DB + `reduceOnly` на все close ордера
**Файлы:** `main.py:372` (`_do_market_close`), `main.py:413` (`_do_smart_close_trigger`)

Вместо `abs(p["size"])` с биржи — брать `pos["qty1"]` / `pos["qty2"]` из DB.
Добавить `reduceOnly=True` на все close ордера как страховка:
если qty в DB больше реального на бирже (из-за ручного вмешательства) — Binance автоматически ограничит ордер реальным размером.

### 1.3 Enriched PnL без матчинга к биржевому символу
**Файл:** `main.py` `/api/db/positions/enriched`

Убрать матчинг DB-записей к биржевым позициям по символу — это приводит к double-PnL если BTC в двух парах.
PnL считать только из DB qty × (mark_price − entry_price):
```
sign = 1 if side == "long_spread" else -1
pnl = qty1*(mark1 - entry1)*sign + qty2*(entry2 - mark2)*sign
```
Mark price брать отдельным запросом по символу, без привязки к конкретной Binance позиции.

### 1.4 DUST flush после smart close
**Файл:** `backend/order_manager.py`

После завершения smart close вычислить остаток: `ctx.leg.qty - ctx.leg.filled`.
Если остаток > 0 → place `reduceOnly` market ордер на точно этот amount.
Amount берётся из контекста исполнения, не с биржи — безопасно при overlapping symbols.

### 1.5 ROLLBACK логика для close операций
**Файл:** `backend/order_manager.py`

ROLLBACK придуман для открытия (откатить заполненную ногу). При закрытии семантика другая:
- leg1 закрылась, leg2 не закрылась → **не пытаться переоткрыть leg1**
- Алерт в Telegram: "leg2 не закрылась, требуется ручное вмешательство"
- Пометить позицию как `status="partial_close"` в DB
- Показать предупреждение в UI на этой позиции

---

## Блок 2 — Надёжность

### 2.1 `clientOrderId` на каждый ордер
**Файлы:** `backend/binance_client.py`, `backend/order_manager.py`

Формат: `PT_{position_id}_{leg}_{exec_id}` (до 36 символов, Binance поддерживает).
Пример: `PT_5_leg1_a3f2b1c4`

Зачем: при рестарте сервера можно найти все наши ордера через Binance API по префиксу `PT_`
и восстановить состояние — что открыто, что в процессе, что нужно откатить.

На старте: `client.fetch_open_orders()` → фильтр по clientOrderId prefix `PT_` → реконструкция.

### 2.2 `ACCOUNT_UPDATE` listener
**Файл:** `backend/user_data_feed.py`

Сейчас UserDataFeed слушает только `ORDER_TRADE_UPDATE`.
Добавить обработку `ACCOUNT_UPDATE` — Binance шлёт его при:
- **Ликвидации** (reason=`LIQUIDATION`) → алерт в Telegram + пометить ногу как `liquidated`
- **ADL** (reason=`ADL`) → алерт + обновить qty ноги в DB
- **Funding fee** (reason=`FUNDING_FEE`) → записать сумму в `funding_history` (см. Блок 4)

Поля события:
```
"m": reason (LIQUIDATION / ADL / FUNDING_FEE / ...)
"B": [{"a": asset, "wb": balance}]  ← изменение баланса
"P": [{"s": symbol, "pa": position_amount}]  ← новый размер позиции
```

### 2.3 Reconciliation loop
**Файл:** `backend/main.py`

Фоновая задача, запускается каждые 5 минут.
Для каждой открытой позиции в DB:
1. Запросить биржевые позиции по обоим символам
2. Если нога отсутствует на бирже → Telegram алерт "нога {symbol} закрыта без ведома платформы"
3. Если qty расходится более чем на 1 step size → WARNING в лог + Telegram
4. **Не авто-корректировать** — только обнаруживать и уведомлять

---

## Блок 3 — Мультипара и усреднение

### 3.1 Leg-level DB схема

Новая таблица `position_legs`:
```sql
CREATE TABLE position_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES open_positions(id),
    leg_number INTEGER,          -- 1 или 2
    symbol TEXT,
    side TEXT,                   -- "long" / "short"
    qty REAL,                    -- реально заполнено (из ctx.filled)
    entry_price REAL,
    client_order_id TEXT,        -- PT_{pos_id}_{leg}_{exec_id}
    status TEXT DEFAULT 'open',  -- open / closed / liquidated / partial_close
    opened_at TEXT,
    closed_at TEXT
)
```

Таблица `open_positions` остаётся как "шапка" пары (side, entry_zscore, TP/SL и т.д.).
`qty1`/`qty2` в `open_positions` становятся вычисляемыми: сумма всех активных legs.

### 3.2 Усреднение (pyramiding)

Второй вход в ту же пару = новая строка в `position_legs` с тем же `position_id`.
Средняя цена входа пересчитывается: `avg = sum(qty_i * price_i) / sum(qty_i)`.
При закрытии qty = сумма всех активных legs.

### 3.3 Параллельное закрытие пар с общим символом

После Блока 1 (qty из DB) — уже работает корректно.
Нет нужды в очереди `closing_symbols`.
`closing_pairs` остаётся для защиты от двойного закрытия **одной и той же пары**.

---

## Блок 4 — Дополнительно

### 4.1 Funding fee tracking

Новая таблица `funding_history`:
```sql
CREATE TABLE funding_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    symbol TEXT,
    amount REAL,        -- отрицательное = платим, положительное = получаем
    asset TEXT,         -- USDT / USDC
    paid_at TEXT
)
```

Источник данных: `ACCOUNT_UPDATE` с reason=`FUNDING_FEE` (из Блока 2.2).
Показывать в enriched positions как отдельную строку: "Funding: −$X.XX"
Итоговый реальный PnL = gross_pnl − комиссии − funding.

### 4.2 Комиссии из биржевых событий

Не считать самостоятельно — брать из `ORDER_TRADE_UPDATE`:
```
"n": "0.00012"    ← точная сумма комиссии за этот fill
"N": "USDC"       ← в чём списана
```

Накапливать в OrderManager контексте по каждому partial fill.
Итоговая комиссия за позицию = сумма комиссий всех fills обеих ног.
Сохранять в DB при закрытии, вычитать из PnL.

### 4.3 Cointegration health check

Фоновая задача каждые 4 часа.
Для каждой открытой позиции: перезапустить тест коинтеграции.
Если p-value > 0.05 → Telegram алерт "пара {sym1}/{sym2} возможно потеряла коинтеграцию".
Показывать индикатор здоровья в строке позиции в UI.

---

## Порядок реализации

1. **Блок 1** — фиксы (приоритет, разблокирует мультипару)
2. **Блок 2** — надёжность (параллельно или сразу после)
3. **Блок 3** — мультипара + усреднение (требует Блок 1 и Блок 2)
4. **Блок 4** — дополнительные фичи (в любое время)

---

## Что трогать не нужно

- PriceCache архитектура — правильная
- OrderManager state machine — правильный (с поправкой 1.5)
- WebSocket infrastructure — правильная
- TP/SL direction-agnostic логика (`abs(z)`) — правильная
- `closing_pairs` защита — оставить

