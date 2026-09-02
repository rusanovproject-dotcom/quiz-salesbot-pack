"""Тесты полигона: проверяем не текст модели, а маршрут по машине шагов.

Гоняется настоящий код-путь (тот же, что в бою), модель замокана.

    python3 -m pytest tests/ -q
"""
import json, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

import sim  # noqa: E402
import config as cfg  # noqa: E402

PERSONAS = os.path.join(BASE, "tools", "personas.json")


def шаги(path):
    """{id персонажа: [шаги, которые он прошёл]}"""
    personas = json.load(open(path, encoding="utf-8"))
    out = {}
    for p in personas:
        r = sim.run_persona(p, sim.mock_brain, links_fn=cfg.links_for_step)
        out[r["id"]] = [t["step"] for t in r["turns"]]
    return out


def test_полигон_доводит_хотя_бы_одного_до_pitch():
    """Не дошёл никто — платный оффер не проверен вообще.

    OFFER_NAME, OFFER_PRICE и OFFER_PAY_URL звучат только на PITCH. Зелёный
    полигон без него означает, что как бот продаёт — никто не видел.
    """
    прошли = шаги(PERSONAS)
    дошедшие = [pid for pid, s in прошли.items() if "PITCH" in s]
    assert дошедшие, f"до PITCH не дошёл никто: {прошли}"


def test_на_первых_ходах_оффера_нет_ни_у_кого():
    """Ранний оффер — самый частый способ потерять тёплого. Держим это тестом."""
    for pid, s in шаги(PERSONAS).items():
        assert "PITCH" not in s[:3], f"{pid} выпрыгнул в оффер на {s[:3]}"
