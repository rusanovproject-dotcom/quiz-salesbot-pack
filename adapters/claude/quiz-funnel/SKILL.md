---
name: quiz-funnel
description: Use when... в Claude Code пользователь говорит «хочу квиз» или «собери воронку» и нужен общий Funnel Studio workflow.
canonical-entrypoint: preflight
skill-refs: preflight
public-phases: preflight, source-intake, FunnelFit, Factura, Offer, Meaning, QuizPreview, SellerPreview, LocalVerify, ProductionReadiness, verify-funnel
contracts: funnel-brief/v1, state/v1, studio-snapshot/v1, studio-event/v1
artifact-root: projects/<project_slug>/funnels/<funnel_slug>/
artifacts: FUNNEL-BRIEF.md, FUNNEL-BRIEF.meta.json, STATE.json, events.jsonl, quiz/, salesbot/, reports/
stop-conditions: missing-required-artifact, missing-owner-approval, blocked-readiness, unverified-domain-claim
trigger-map: хочу квиз=>preflight;собери воронку=>preflight
---

# Claude Code adapter

От корня текущего workspace найди `skills/preflight/SKILL.md`. Фразы «хочу
квиз» и «собери воронку» запускают один entrypoint — `preflight`.

Загрузи канонический навык полностью и следуй его ссылкам по мере надобности.
Все решения, гейты и stop conditions принадлежат `skills/`; этот adapter только
обнаруживает workspace и передаёт управление. Если корень или entrypoint не
найден, остановись и попроси открыть установленный workspace пака.
