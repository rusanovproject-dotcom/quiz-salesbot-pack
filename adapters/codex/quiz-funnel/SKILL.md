---
name: quiz-funnel
description: Use when в Codex пользователь просит собрать квиз, квиз-воронку, воронку или ИИ-продажника и нужен общий Funnel Studio workflow.
canonical-entrypoint: preflight
skill-refs: preflight
public-phases: preflight, source-intake, FunnelFit, Factura, Offer, Meaning, QuizPreview, SellerPreview, LocalVerify, ProductionReadiness, verify-funnel
contracts: funnel-brief/v1, state/v1, studio-snapshot/v1, studio-event/v1
artifact-root: projects/<project_slug>/funnels/<funnel_slug>/
artifacts: FUNNEL-BRIEF.md, FUNNEL-BRIEF.meta.json, STATE.json, events.jsonl, quiz/, salesbot/, reports/
stop-conditions: missing-required-artifact, missing-owner-approval, blocked-readiness, unverified-domain-claim
trigger-map: хочу квиз=>preflight;собери воронку=>preflight;собери мне квиз-воронку=>preflight;ии-продажник=>preflight
---

# Codex adapter

Поднимайся от текущей рабочей директории до корня, содержащего
`skills/preflight/SKILL.md`. Фразы «хочу квиз», «собери воронку», «собери мне
квиз-воронку» и запрос на ИИ-продажника запускают один entrypoint — `preflight`.

Прочитай канонический навык полностью и следуй его ссылкам по мере надобности.
Все решения, гейты и stop conditions принадлежат `skills/`; этот adapter только
обнаруживает workspace и передаёт управление. Если корень или entrypoint не
найден, остановись и попроси открыть установленный workspace пака.
