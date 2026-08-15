#!/usr/bin/env python3
"""sim.py — полигон: гоняем бота на выдуманных клиентах.

Работает НАСТОЯЩИЙ код-путь — та же машина шагов, тот же сборщик промптов, что в бою.
Модель по умолчанию замокана: проверяем машину, ничего не тратим.

    python3 tools/sim.py tools/personas.json > /tmp/sim.json          # без трат
    python3 tools/sim.py tools/personas.json --live > /tmp/live.json  # с моделью
    python3 tools/check_antigeneric.py /tmp/live.json                 # проверка реплик

Живой прогон тратит деньги осознанно: только на нём видно, ушёл ли бот в общие
советы и не выгладился ли голос.
"""
import asyncio, json, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pipeline, prompt_builder, handle, card as cardmod, config as cfg  # noqa: E402

PERSONA = cfg.read_persona()
CONNECTIONS = cfg.read_connections()


def _check_ready():
    """Пустая персона или пустой справочник дают ЛОЖНО чистый результат.

    Промпт без фактуры не может уйти в общие советы просто потому, что в нём
    ничего нет. Молча пропустить это — значит поверить в проверку, которой не было.
    """
    problems = []
    if len(PERSONA.strip()) < 400 or "ШАБЛОН ПЕРСОНЫ" in PERSONA:
        problems.append("persona/persona.md — шаблон или пусто, заполни под себя")
    if CONNECTIONS.count("\n## ") < 2 or "[Сфера один" in CONNECTIONS:
        problems.append("persona/connections.md — шаблон или почти пуст, добавь связки")
    return problems


def mock_brain(system, user):
    """Заглушка вместо модели: показывает, ЧТО пришло бы в промпт."""
    step = user.split("[Шаг]")[1].split("\n")[0].strip()[:80] if "[Шаг]" in user else "?"
    return f"[заглушка] шаг: {step}"


async def live_brain(system, user):
    import brain
    parts = await brain.reply(user, system, timeout=120)
    return "///".join(parts)


def run_persona(persona, brain_fn, *, links_fn):
    """Прогнать одного выдуманного клиента через все его реплики."""
    c = cardmod.new_card(quiz_id=persona["id"], name=persona.get("name", ""))
    c["answers"] = dict(persona.get("quiz", {}))
    c["klass"] = persona.get("klass") or cardmod.derive_class(c["answers"], cfg.CLASS_MAP)
    c["track"] = persona.get("track") or cardmod.derive_track(c["answers"], cfg.TRACK_RULES)

    turns = []
    for msg in persona["messages"]:
        plan = handle.plan_turn(c, msg, persona=PERSONA, connections=CONNECTIONS,
                                links_fn=links_fn, today="2030-01-01", calls_today=0)
        if plan["action"] != "llm":
            turns.append({"in": msg, "step": plan["step"], "action": plan["action"],
                          "reply": " ".join(plan["to_client"]),
                          "to_owner": plan.get("to_owner")})
            continue
        reply = brain_fn(plan["system"], plan["user"])
        if asyncio.iscoroutine(reply):
            reply = asyncio.get_event_loop().run_until_complete(reply)
        turns.append({"in": msg, "step": plan["step"], "action": "llm", "reply": reply})
        handle.apply_reply(c, plan, reply.split("///"), today="2030-01-01")
    return {"id": persona["id"], "label": persona.get("label", ""),
            "klass": c["klass"], "track": c["track"], "turns": turns}


def main():
    if len(sys.argv) < 2:
        raise SystemExit("как звать: python3 tools/sim.py tools/personas.json [--live]")
    live = "--live" in sys.argv
    personas = json.load(open(sys.argv[1], encoding="utf-8"))

    problems = _check_ready()
    if problems:
        if live:
            raise SystemExit("Живой прогон бессмысленен:\n  · " + "\n  · ".join(problems))
        print("ВНИМАНИЕ, проверка неполная:\n  · " + "\n  · ".join(problems), file=sys.stderr)

    brain_fn = live_brain if live else mock_brain
    if live:
        asyncio.set_event_loop(asyncio.new_event_loop())

    out = [run_persona(p, brain_fn, links_fn=cfg.links_for_step) for p in personas]
    print(json.dumps(out, ensure_ascii=False, indent=2))

    # короткая сводка в stderr, чтобы не мешала перенаправлению
    for r in out:
        steps = " → ".join(t["step"] for t in r["turns"])
        print(f"{r['id']:14} класс={r['klass'] or '—':16} дорожка={r['track']}  {steps}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
