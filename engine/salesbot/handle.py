"""handle.py — ядро обработки входящего сообщения. Без сети и без транспорта.

Философия: КОД решает (кто пришёл, какие гварды сработали, какой шаг),
МОДЕЛЬ только пишет текст. Логика отделена от мессенджера — поэтому её можно
гонять на полигоне без токена, без интернета и без трат.

Ход разговора:
  resolve_card   опознать человека: код с квиза → карточка, иначе по id, иначе холодная
    → first_touch   мгновенное «секунду, читаю» БЕЗ модели, чтобы тишина не съела обещание
    → plan_turn     гварды → шаг → промпт ИЛИ готовый ответ без модели
    → [транспорт зовёт brain]
    → apply_reply   обновить карточку: история, счётчики, шаг вперёд
"""
from __future__ import annotations
import re

import pipeline, prompt_builder, objections, escalation, limits, card as cardmod

# «код 4f8a», «карта 4f8a», «card 4f8a» — код из финала квиза
_CODE_RE = re.compile(r"(?:код|карта|карточка|card|kod)[\s\-_:]*([0-9a-zA-Z]{3,10})", re.IGNORECASE)

# Голый код: человек просто вставил его одной строкой.
# Требуем хотя бы одну цифру — иначе любое латинское слово будет считаться кодом.
# Кириллица сюда не попадает вовсе, поэтому «беру» или «дорого» не спутаются с кодом.
_BARE_CODE_RE = re.compile(r"^(?=.*\d)[0-9a-zA-Z]{4,10}$")

# Человек СООБЩАЕТ об оплате (не «хочу купить», а «уже перевёл»)
_PAID_MARKERS = ("оплатил", "оплатила", "оплачено", "оплату сделал", "скинул", "скинула",
                 "перевёл", "перевела", "перевел", "деньги отправил", "чек", "купил")


def extract_code(text: str) -> str | None:
    t = (text or "").strip()
    m = _CODE_RE.search(t)
    if m:
        return m.group(1)
    if _BARE_CODE_RE.match(t):
        return t
    return None


def _by_quiz_id(quiz_id: str, user_id, base):
    """Привязать карточку по коду.

    Защита личности: привязываем ТОЛЬКО свободную карточку или уже свою.
    Иначе один человек пришлёт чужой код и увидит чужие ответы про доход.
    """
    if not cardmod.is_valid_quiz_id(quiz_id):
        return None, False, None
    c = cardmod.load(base, quiz_id)
    if c is None:
        return None, False, None
    owner = c.get("user_id")
    if owner is None or str(owner) == str(user_id):
        first = owner is None
        c["user_id"] = user_id
        return c, first, None
    return None, False, f"Код {quiz_id} занят другим человеком. Пропускаю привязку."


def resolve_card(text, user_id, base, *, ref: str | None = None):
    """Опознать человека → (карточка, первое ли касание, заметка владельцу).

    Порядок:
      1. ref из ссылки — бесшовный путь, человек пришёл прямо с квиза
      2. код из текста — для тех, у кого ссылка потерялась
      3. карточка по id — вернулся без кода
      4. холодная болванка — квиз не проходил
    """
    if ref:
        c, first, _ = _by_quiz_id(ref, user_id, base)
        if c is not None:
            return c, first, None

    code = extract_code(text)
    if code:
        c, first, note = _by_quiz_id(code, user_id, base)
        if c is not None:
            return c, first, None
        if note:
            existing = cardmod.find_by_user_id(base, user_id)
            if existing is not None:
                return existing, False, note
            cold = cardmod.new_card(quiz_id=f"u{user_id}")
            cold["user_id"] = user_id
            return cold, False, note

    existing = cardmod.find_by_user_id(base, user_id)
    if existing is not None:
        return existing, False, None

    cold = cardmod.new_card(quiz_id=f"u{user_id}")
    cold["user_id"] = user_id
    return cold, False, None


def _ans(card, key):
    return (card.get("answers") or {}).get(key, "")


def first_touch_messages(card: dict, persona_name: str = "") -> list[str]:
    """Мост перед разбором, БЕЗ модели.

    Между переходом с квиза и первым ответом модели проходят секунды. В эти секунды
    человек сидит в пустом диалоге после обещания разбора. Два коротких сообщения
    закрывают дыру мгновенно.

    Пересказывать его ответы тут НЕ надо: узнавание — работа самого разбора,
    а пересказ читается как «робот подтверждает получение анкеты».
    """
    name = (card.get("name") or "").strip()
    hi = f"О, {name}, привет." if name else "О, привет."
    who = f" Я {persona_name}." if persona_name else ""
    return [
        f"{hi} Дай пару секунд, гляну твои ответы и разложу под тебя.",
        f"Отвечай развёрнуто, чем подробнее — тем жирнее разбор.{who}",
    ]


def _canned(kind: str) -> str:
    """Ответы без модели: для закрытых, перегруженных и упершихся в лимит.

    Молчание в этих случаях хуже простого ответа: человек думает, что его игнорируют.
    """
    if kind == "global_breaker":
        return "Слушай, я сейчас завален сообщениями. Отвечу чуть позже, не теряйся."
    if kind == "person_cap":
        return "На сегодня я тебя загрузил по полной. Давай продолжим завтра."
    if kind == "closed":
        return "По нашему вопросу всё сказал. Будут вопросы — пиши, я рядом."
    return "Понял тебя."


def plan_turn(card, text, *, persona, connections, links_fn, today, calls_today,
              draft_mode=False, track_hints=None):
    """Решить, что делать на этот ход. Без модели и без сети.

    Возвращает план:
      action     'llm' | 'canned' | 'escalate'
      to_client  что отправить человеку сразу
      to_owner   карточка владельцу
      system     промпт-персона (только для 'llm')
      user       промпт хода (только для 'llm')
      step, advance, deepen, draft
    """
    step = card.get("pipeline_step", "REVIEW")
    deepen = card.get("deepen_count", 0)
    low = (text or "").lower()

    # 1. Общий рубильник. Не молчим: ответ плюс алерт владельцу
    if not limits.global_ok(calls_today=calls_today):
        return {"action": "canned", "to_client": [_canned("global_breaker")],
                "to_owner": f"Общий предел вызовов достигнут ({calls_today}). Бот в режиме заглушки.",
                "step": step}

    # 2. Диалог закрыт: оплатил или дважды отказался
    if limits.is_closed(card):
        return {"action": "canned", "to_client": [_canned("closed")],
                "to_owner": None, "step": step}

    # 3. Сообщил об оплате → фиксируем, зовём владельца сверить, больше не продаём
    if any(m in low for m in _PAID_MARKERS):
        card.setdefault("paid", []).append("offer")
        return {"action": "canned",
                "to_client": ["Принял! Сверю оплату и вернусь с доступом."],
                "to_owner": "ОПЛАТА · " + escalation._facts(card) + f" · «{text}»",
                "step": step}

    # 4. Возражения. Опасное — сразу к живому человеку
    tag = objections.classify(text)
    if tag and objections.should_escalate(tag):
        return {"action": "escalate",
                "to_client": ["Услышал. Передаю человеку лично, он ответит."],
                "to_owner": escalation.render_escalation_card(
                    card, reason=f"возражение {tag}", user_text=text),
                "step": step}

    # Отказ ПОСЛЕ предложенного оффера — считаем. Два отказа закрывают диалог
    if tag in ("LATER", "PRICE") and card.get("offers"):
        card["rejections"] = card.get("rejections", 0) + 1

    # 5. Дневной предел на человека
    if not limits.allow_llm(card, today=today):
        return {"action": "canned", "to_client": [_canned("person_cap")],
                "to_owner": None, "step": step}

    # 6. Машина шага. Порядок важен: готовность купить сильнее «сошёл со скрипта»
    sc = pipeline.maybe_shortcut(text, step)
    buy_intent = pipeline.maybe_shortcut(text, "REVIEW") == "PITCH"
    hold = pipeline.should_hold(text) if not (sc or buy_intent) else False
    eff_step = sc or step

    directive = pipeline.directive(eff_step)
    if tag:
        directive += " [Возражение] " + objections.directive(tag)
    directive += links_fn(eff_step)
    if hold:
        directive += (" [Человек сошёл со сценария: встречный вопрос, спор или односложный ответ. "
                      "Ответь по сути, верни в русло, шаг не форсируй.]")

    system = prompt_builder.build_system(persona, connections, sphere=_ans(card, "Q2"))
    user = prompt_builder.build_user_prompt(card=card, step_directive=directive,
                                            history=card.get("history", []), user_text=text,
                                            track_hints=track_hints)
    return {"action": "llm", "to_client": [], "to_owner": None,
            "system": system, "user": user, "_user_text": text,
            "step": eff_step, "advance": not (sc or hold), "deepen": deepen,
            "draft": draft_mode}


def opening_plan(card, *, persona, connections, links_fn, track_hints=None) -> dict:
    """План первого касания, когда инициатива наша.

    Отличие от обычного хода: входящего текста нет, гвардов нет — человек только
    что перешёл с квиза, и разбор начинаем мы сами.
    """
    directive = pipeline.directive("REVIEW") + links_fn("REVIEW")
    system = prompt_builder.build_system(persona, connections, sphere=_ans(card, "Q2"))
    user = prompt_builder.build_user_prompt(card=card, step_directive=directive,
                                            history=card.get("history", []), user_text="",
                                            track_hints=track_hints)
    return {"action": "llm", "first_touch": first_touch_messages(card),
            "system": system, "user": user, "_user_text": "",
            "step": "REVIEW", "advance": True, "deepen": 0, "draft": False}


def apply_reply(card, plan, replies, *, today):
    """После ответа модели: обновить историю, счётчики и шаг."""
    card = limits.bump(card, today=today)
    hist = card.setdefault("history", [])
    if plan.get("_user_text"):
        hist.append({"role": "user", "text": plan["_user_text"]})
    for r in replies:
        hist.append({"role": "assistant", "text": r})

    # Оффер запоминаем один раз: живой торг не должен накручивать счётчик
    # и закрывать диалог тому, кто как раз собирался платить
    if plan["step"] == "PITCH" and "offer" not in card.get("offers", []):
        card.setdefault("offers", []).append("offer")

    if plan.get("advance"):
        nxt, deepen = pipeline.next_step(plan["step"], plan.get("deepen", 0))
        card["pipeline_step"], card["deepen_count"] = nxt, deepen
    else:
        card["pipeline_step"] = plan["step"]
    return replies, card
