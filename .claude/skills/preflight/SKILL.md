---
name: preflight
description: Use when... человек просит начать, собрать воронку или квиз и нужен безопасный единый вход в Funnel Studio.
phase: preflight
public-phases: preflight, source-intake, FunnelFit, Factura, Offer, Meaning, QuizPreview, SellerPreview, LocalVerify, ProductionReadiness, verify-funnel
gate-graph: FunnelFit, Factura, Offer, Meaning, QuizPreview, SellerPreview, LocalVerify, ProductionReadiness
skill-refs: preflight, source-intake, funnel-interview, audience-factura, wordstat-mining, quiz-offer, quiz-meaning, build-quiz, quiz-design, build-salesbot, leads-db, offer-testing, verify-funnel
artifact-root: projects/<project_slug>/funnels/<funnel_slug>/
artifacts: FUNNEL-BRIEF.md, FUNNEL-BRIEF.meta.json, STATE.json, events.jsonl, quiz/, salesbot/, reports/
contracts: funnel-brief/v1, state/v1, studio-snapshot/v1, studio-event/v1
stop-conditions: missing-required-artifact, missing-owner-approval, blocked-readiness, unverified-domain-claim
domain-write-policy: project-artifacts-only
deployment-policy: never
---
<!-- GENERATED from skills/preflight/SKILL.md by scripts/check-adapters.sh --write. DO NOT EDIT. -->

# Единый вход Funnel Studio

Найди корень пака по `skills/preflight/SKILL.md`. Получи у пользователя либо
проверь безопасные `project_slug` и `funnel_slug`; рабочий корень всегда
`projects/<project_slug>/funnels/<funnel_slug>/`. Не создавай новый корневой
`PROJECT.md` и не записывай доменную фактуру в `engine/`, `examples/` или
`playbook/`.

Сначала проверь наличие четырёх контрактных артефактов: `FUNNEL-BRIEF.md`,
`FUNNEL-BRIEF.meta.json`, `STATE.json`, `events.jsonl`. Если экземпляра ещё нет,
остановись на одном следующем действии — безопасной инициализации через
проектный слой `studio`; сам не собирай каталоги вручную.

Дальше передай управление `source-intake`. После него веди только по графу:

`FunnelFit → Factura → Offer → Meaning → QuizPreview → SellerPreview → LocalVerify → ProductionReadiness`.

| Гейт | Канонические навыки |
| --- | --- |
| FunnelFit | `funnel-interview` |
| Factura | `audience-factura`, при необходимости `wordstat-mining` |
| Offer | `quiz-offer` |
| Meaning | `quiz-meaning` |
| QuizPreview | `build-quiz`, затем `quiz-design` |
| SellerPreview | `build-salesbot` |
| LocalVerify | `verify-funnel` |
| ProductionReadiness | `leads-db`, затем `verify-funnel` |

На каждом переходе сверяй `STATE.json` через контракт `state/v1`. Нет требуемого
артефакта, явного approval, подтверждённой фактуры либо readiness заблокирована
— остановись и покажи ровно одно следующее действие. Adapter не создаёт approval
и не повышает readiness. Этот навык ничего не деплоит.
