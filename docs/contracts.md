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
запись с `recorded_at` и `reason`. Поэтому 🔴 никогда не означает «опровергнуто».

## Состояние и гейты

Граф гейтов фиксирован: `FunnelFit → Factura → Offer → Meaning → QuizPreview →
SellerPreview → LocalVerify → ProductionReadiness`. Поле `gate` принимает
только одно из этих имён. Оси `phase`, `gate_status`, `technical_readiness` и
`market_readiness` проверяются раздельно.

`release_verdict` выводится валидатором, а не принимается из файла. `PASS`
возможен только для `RELEASE` на `ProductionReadiness`, когда гейт и обе
готовности имеют `PASS`. При `BLOCKED` у любой готовности или гейта вердикт
`BLOCKED`; поэтому технический `PASS` при рыночном `BLOCKED` не выпускает
релиз.

## Ошибки для потребителей

Валидатор собирает все обнаружимые ошибки в одном запуске, ничего не исправляя.
Каждая имеет стабильные `code`, JSON Pointer `path` и понятное `message`.
Список всегда отсортирован лексикографически по `(path, code, message)`.

| Код | Значение |
| --- | --- |
| `E_INVALID_JSON` | Файл не является UTF-8 JSON. |
| `E_UNKNOWN_SCHEMA_VERSION` | Версия не поддерживается. |
| `E_REQUIRED_FIELD` | Нет обязательного поля или раздела. |
| `E_INVALID_TYPE` | Значение имеет неверный тип. |
| `E_INVALID_FORMAT` | Значение не соответствует формату контракта. |
| `E_INVALID_ENUM` | Значение вне закрытого списка. |
| `E_UNKNOWN_FIELD` | Поле отсутствует в контракте v1. |
