---
name: source-intake
description: Use when... нужно принять материалы владельца или legacy PROJECT.md и подготовить единый FUNNEL-BRIEF без выдуманных фактов.
phase: source-intake
artifact-root: projects/<project_slug>/funnels/<funnel_slug>/
artifacts: FUNNEL-BRIEF.md, FUNNEL-BRIEF.meta.json, STATE.json, events.jsonl, quiz/, salesbot/, reports/
contracts: funnel-brief/v1, state/v1, studio-snapshot/v1, studio-event/v1
stop-conditions: missing-required-artifact, missing-owner-approval, blocked-readiness, unverified-domain-claim
domain-write-policy: project-artifacts-only
deployment-policy: never
---
<!-- GENERATED from skills/source-intake/SKILL.md by scripts/check-adapters.sh --write. DO NOT EDIT. -->

# Приём источников

Работай только внутри `projects/<project_slug>/funnels/<funnel_slug>/`. Принимай
предоставленные владельцем материалы как источники, а не как автоматически
подтверждённые утверждения. Корневой `PROJECT.md` допустим только как legacy
input: прочитай его, сохрани provenance и никогда не редактируй.

Собирай человеческий документ в `FUNNEL-BRIEF.md`, а проверяемые факты — в
`FUNNEL-BRIEF.meta.json` по `funnel-brief/v1`. Используй только статусы
`confirmed`, `owner_hypothesis`, `synthetic`, `unknown`; неизвестное не
достраивай материалами из `playbook/`, `examples/` или моделью. Эти каталоги
дают форму и технику, но не доменные утверждения.

Если источников недостаточно, передай точечные вопросы навыку
`funnel-interview`. До валидного brief не переходи к `FunnelFit`. Ничего не
компилируй в `quiz/` или `salesbot/`, не меняй readiness и не деплой.
