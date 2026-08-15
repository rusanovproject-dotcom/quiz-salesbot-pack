"""limits.py — предохранители. Чистые функции.

Три штуки, каждая закрывает свой способ спалить деньги и репутацию:
  allow_llm  — сколько раз в день зовём модель на одного человека
  is_closed  — диалог закрыт: оплатил или дважды отказался. Больше не дожимаем
  global_ok  — общий рубильник на день, защита от разгона по кругу
"""
from __future__ import annotations

PERSON_DAILY_CAP = 15

# Общий предел ставится ВЫШЕ ожидаемого потока. Это защита от разгона,
# а не нормальный рабочий потолок: сработает на пике — потеряешь лидов
# именно в тот день, когда они пришли.
GLOBAL_DAILY_CAP = 3000


def allow_llm(card: dict, *, today: str) -> bool:
    if card.get("last_day") != today:
        return True
    return card.get("msgs_today", 0) < PERSON_DAILY_CAP


def bump(card: dict, *, today: str) -> dict:
    if card.get("last_day") != today:
        card["last_day"], card["msgs_today"] = today, 0
    card["msgs_today"] = card.get("msgs_today", 0) + 1
    return card


def is_closed(card: dict) -> bool:
    """Оплатил → закрыт. Дважды отказался от одного оффера → тоже закрыт.

    Третий заход с тем же предложением не приносит продаж, зато приносит
    репутацию навязчивого бота.
    """
    if card.get("paid"):
        return True
    if card.get("rejections", 0) >= 2:
        return True
    offers = card.get("offers", [])
    return any(offers.count(o) >= 2 for o in set(offers))


def global_ok(*, calls_today: int, cap: int = GLOBAL_DAILY_CAP) -> bool:
    return calls_today < cap
