---
name: verify-funnel
description: Use when нужно локально проверить артефакты гейта или готовность воронки без публикации и внешних сервисов.
phase: verify-funnel
artifact-root: projects/<project_slug>/funnels/<funnel_slug>/
artifacts: FUNNEL-BRIEF.md, FUNNEL-BRIEF.meta.json, STATE.json, events.jsonl, quiz/, salesbot/, reports/
contracts: funnel-brief/v1, state/v1, studio-snapshot/v1, studio-event/v1
stop-conditions: missing-required-artifact, missing-owner-approval, blocked-readiness, unverified-domain-claim
domain-write-policy: project-artifacts-only
deployment-policy: never
---

# Локальная проверка воронки

Читай только выбранный `projects/<project_slug>/funnels/<funnel_slug>/` и
versioned contracts. Сначала валидируй companion metadata brief и state; затем
проверяй наличие артефактов текущего гейта и их соответствие утверждённой
ревизии `FUNNEL-BRIEF`. Не используй сеть, провайдеров, мессенджеры и deploy.

`prototype_only` означает только локальный прототип и никогда не даёт
production `PASS`. Отсутствующий артефакт, approval, подтверждение доменного
факта или заблокированная readiness дают fail-closed результат с одним
следующим действием. Не создавай approval и не исправляй файлы молча.

Отчёты проверки принадлежат `reports/` проектного funnel root. Методология из
`skills/` и техника из `playbook/`/`examples/` не являются источником доменного
контента. Этот навык только проверяет; публикация остаётся отдельным будущим
действием после `ProductionReadiness`.
