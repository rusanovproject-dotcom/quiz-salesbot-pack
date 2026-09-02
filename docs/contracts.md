# Контракты Funnel Studio v1

`FUNNEL-BRIEF.md` — человекочитаемый документ. Его companion metadata —
`FUNNEL-BRIEF.meta.json`; именно его проверяет `validate_brief`. `STATE.json`
проверяет `validate_state`. Оба валидатора используют только стандартную
библиотеку Python, не вызывают модели, сеть или мессенджеры и ничего не
перезаписывают.

Схемы лежат в `schemas/funnel-brief-v1.json` и `schemas/state-v1.json`.
Текущие версии: `funnel-brief/v1` и `state/v1`. Неизвестная версия возвращает
`E_UNKNOWN_SCHEMA_VERSION`; миграция — отдельная явная операция, не побочный
эффект проверки.

## Факты

Допустимы ровно четыре значения `status`:

- `confirmed` — 🟢 подтверждённый факт.
- `owner_hypothesis` — 🟡 гипотеза владельца.
- `synthetic` — 🔴 синтетическое утверждение, требующее проверки.
- `unknown` — ⚪ достоверность ещё не определена.

Опровержение не является статусом: оно хранится в `refutations` как отдельная
запись с `recorded_at` и `reason`. `recorded_at` — строгий RFC 3339 `date-time`:
дата и время разделены `T`, у времени обязателен timezone (`Z` либо `±HH:MM`).
Поэтому 🔴 никогда не означает «опровергнуто».

## Состояние и гейты

Граф гейтов фиксирован: `FunnelFit → Factura → Offer → Meaning → QuizPreview →
SellerPreview → LocalVerify → ProductionReadiness`. Поле `gate` принимает
только одно из этих имён. Оси `phase`, `gate_status`, `technical_readiness` и
`market_readiness` проверяются раздельно. `prototype_only` допустим только на
оси `technical_readiness`: это рабочий прототип, не фаза и не гейт; он никогда
не даёт релизный `PASS`.

`release_verdict` выводится валидатором, а не принимается из файла. `PASS`
возможен только для `RELEASE` на `ProductionReadiness`, когда гейт и обе
готовности имеют `PASS`. При `BLOCKED` у любой готовности или гейта вердикт
`BLOCKED`; поэтому технический `PASS` при рыночном `BLOCKED` не выпускает
релиз.

## Ошибки для потребителей

Валидатор собирает все обнаружимые ошибки в одном запуске, ничего не исправляя.
Каждая имеет стабильные `code`, JSON Pointer `path` и понятное `message`.
Список всегда отсортирован лексикографически по `(path, code, message)`.

| Условие | Code | JSON Pointer pattern |
| --- | --- | --- |
| JSON-файл не читается как UTF-8 JSON | `E_INVALID_JSON` | `/` |
| Отсутствует поле верхнего уровня | `E_REQUIRED_FIELD` | `/{field}` |
| Отсутствует раздел brief | `E_REQUIRED_FIELD` | `/sections/{section}` |
| Отсутствует поле факта или опровержения | `E_REQUIRED_FIELD` | `/facts/{index}/{field}` или `/facts/{index}/refutations/{index}/{field}` |
| Значение имеет неверный JSON-тип | `E_INVALID_TYPE` | путь самого поля, включая `/schema_version`, `/{axis}` и пути facts |
| `recorded_at` не RFC 3339 date-time | `E_INVALID_FORMAT` | `/facts/{index}/refutations/{index}/recorded_at` |
| Строковая schema version не поддержана | `E_UNKNOWN_SCHEMA_VERSION` | `/schema_version` |
| Строковое значение вне закрытого списка | `E_INVALID_ENUM` | `/phase`, `/gate`, `/gate_status`, `/technical_readiness`, `/market_readiness`, `/facts/{index}/status` |
| Поле не описано v1 | `E_UNKNOWN_FIELD` | `/{field}`, `/sections/{field}`, `/facts/{index}/{field}` или `/facts/{index}/refutations/{index}/{field}` |
