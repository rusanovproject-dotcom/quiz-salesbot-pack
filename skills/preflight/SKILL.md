---
name: preflight
description: Use when человек просит начать, собрать воронку, квиз или ИИ-продажника и нужен безопасный единый вход в Funnel Studio.
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

# Единый вход Funnel Studio

Найди корень пака по `skills/preflight/SKILL.md`. Получи у пользователя либо
проверь безопасные `project_slug` и `funnel_slug`; рабочий корень всегда
`projects/<project_slug>/funnels/<funnel_slug>/`. Не создавай новый корневой
`PROJECT.md` и не записывай доменную фактуру в `engine/`, `examples/` или
`playbook/`.

Сначала определи `project_slug` и `funnel_slug`. Если пользователь их ещё не
подтвердил, спроси название проекта, затем название воронки — строго по одному
вопросу. Предложи безопасные латинские slug, покажи будущий путь и получи явное
подтверждение.

Для нового и для существующего экземпляра всегда выполняй из корня пака одну и
ту же идемпотентную проверку-инициализацию:

```bash
python3 scripts/init-funnel.py --project <project_slug> --funnel <funnel_slug>
```

Она создаст новый экземпляр либо проверит ownership markers и полноту уже
существующего. Не принимай каталог за валидный только по наличию четырёх файлов
и не собирай каталоги вручную. Если команда завершилась ошибкой, остановись,
объясни её простым языком и не переходи к следующему гейту.

После успешной инициализации передай управление `source-intake`. После него веди
только по графу:

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
