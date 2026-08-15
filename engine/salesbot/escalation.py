"""escalation.py — карточки владельцу: когда бот не уверен и когда нужен человек.

Два случая:
  черновик  — бот сгенерил ответ, но отправляет его не человеку, а владельцу на утверждение.
              Режим для первых дней работы: видно, что бот пишет, до того как это увидят люди
  эскалация — бот застрял (жалоба, агрессия, нет факта). Человеку короткое «передаю», владельцу карточка
"""
from __future__ import annotations


def _facts(card: dict) -> str:
    a = card.get("answers") or {}
    name = card.get("name") or "без имени"
    return f"{name} · {a.get('Q2', '?')} · хочет: {a.get('Q7', '?')}"


def render_draft_card(card: dict, *, variants: list[str], user_text: str) -> str:
    vs = "\n".join(f"{i + 1}) {v}" for i, v in enumerate(variants))
    return (f"ЧЕРНОВИК · {_facts(card)}\n"
            f"человек: «{user_text}»\n\n{vs}\n\n"
            f"ответь цифрой или поправь текст")


def render_escalation_card(card: dict, *, reason: str, user_text: str) -> str:
    return (f"НУЖЕН ТЫ · {_facts(card)}\n"
            f"человек: «{user_text}»\n"
            f"почему застрял: {reason}\n"
            f"что ответить? твой ответ передам ему")
