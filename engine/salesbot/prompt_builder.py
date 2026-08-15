"""prompt_builder.py — сборка промпта. Экономно по токенам.

Три решения, каждое экономит деньги и повышает качество:
  1. В системный промпт НЕ грузим всю базу связок — только блок нужной сферы
  2. История диалога — окно последних реплик, а не весь разговор
  3. Никаких файлов проекта и глобальных настроек: модель зовётся в изоляции
"""
from __future__ import annotations

HISTORY_WINDOW = 10

# Заголовки-фолбэки в файле связок, когда сфера человека не нашлась дословно
_FALLBACK_HEADINGS = ("вне справочника", "не определ", "другое")

# Общие слова, по которым НЕЛЬЗЯ матчить обратно: иначе «услуги» или «клиент»
# в заголовке утащат чужой блок связок, и разбор будет не про то ремесло
_STOPWORDS = {"агент", "агентство", "онлайн", "клиент", "клиентов", "бизнес", "школа",
              "личный", "бренд", "услуги", "мастер", "специалист", "автор", "под", "ключ",
              "другое", "прочее"}


def _slice_heading(lines: list[str], match_fn) -> list[str]:
    out, capture = [], False
    for ln in lines:
        if ln.startswith("## "):
            capture = match_fn(ln.lower())
        if capture:
            out.append(ln)
    return out


def sphere_block(connections: str, sphere: str) -> str:
    """Вырезать из файла связок блок нужной сферы.

    Три попытки по убыванию точности:
      1. Слово сферы входит в заголовок
      2. Специфичное слово заголовка (от пяти букв, не из стоп-листа) входит в сферу
      3. Фолбэк-блок «вне справочника» — чтобы бот строил по формуле, а не молчал
         и не лил общие советы
    """
    if not sphere:
        return ""
    lines = connections.splitlines()
    key = sphere.lower()

    block = _slice_heading(lines, lambda h: key in h)
    if any(ln.strip() for ln in block):
        return "\n".join(block).strip()

    for ln in lines:
        if ln.startswith("## "):
            words = [w.strip(" ,/.()") for w in ln[3:].lower().split()]
            words = [w for w in words if len(w) >= 5 and w not in _STOPWORDS]
            if any(w in key for w in words):
                blk = _slice_heading(lines, lambda h, _t=ln.lower(): h == _t)
                if any(x.strip() for x in blk):
                    return "\n".join(blk).strip()

    fb = _slice_heading(lines, lambda h: any(f in h for f in _FALLBACK_HEADINGS))
    return "\n".join(fb).strip()


def build_system(persona: str, connections: str, *, sphere: str = "") -> str:
    block = sphere_block(connections, sphere)
    parts = [persona]
    if block:
        parts.append("\n# Связки под его сферу\n" + block)
    return "\n".join(parts)


def card_pulled(card: dict) -> bool:
    """Доехали ли ответы квиза.

    Если ключевые поля пусты — карточка не подтянулась, и цитировать «его слова»
    нельзя: их нет. Тогда первое сообщение строится по другой ветке.
    """
    a = card.get("answers", {})
    return any((a.get("Q2"), a.get("Q4"), a.get("Q7")))


def facts(card: dict, *, track_hints: dict | None = None) -> str:
    """Что бот «уже видел». Размер бизнеса и доход НЕ цифрой — только дорожкой."""
    a = card.get("answers", {})
    hints = track_hints or {
        "A": "плотная (платное предложение уместно)",
        "B": "бережная (только бесплатное, без дожима)",
    }
    parts = []
    name = (card.get("name") or "").strip()
    parts.append(f"Имя: {name}" if name else "Имя: неизвестно, по имени не обращайся")
    if card.get("klass"):
        parts.append(f"Класс из финала квиза: {card['klass']}")
    parts.append(f"Сфера (Q2): {a.get('Q2') or '?'}")
    if a.get("Q3"):
        parts.append(f"Уровень (Q3): {a['Q3']}")
    parts.append(f"Боль его словами (Q4): {a.get('Q4') or '?'}")
    if a.get("Q5"):
        parts.append(f"Хочет первым (Q5): {a['Q5']} ← под это выбирай, какую связку поднять")
    parts.append(f"Задача его словами (Q7): {a.get('Q7') or '— пропустил'}")
    parts.append(f"Дорожка оффера (человеку НЕ называть): {hints.get(card.get('track'), 'неизвестна')}")
    return ". ".join(parts) + "."


def entry_directive(card: dict) -> str:
    """Указание для первого сообщения. Две ветки: ответы доехали или нет."""
    if not card_pulled(card):
        return (
            "[ВХОД БЕЗ ОТВЕТОВ] Ответы квиза не доехали. НЕ цитируй несуществующие слова и "
            "ничего не выдумывай. Зайди живо, признай, что человек с квиза, и первым же ходом "
            "вытащи сферу и главную задачу одним вопросом. Разбор начнётся, как только ответит. "
            "Без дежурного «чем могу помочь».\n"
        )
    a = card.get("answers", {})
    skipped = not (a.get("Q7") or "").strip()
    extra = ("Задачу текстом он пропустил — опирайся на боль, сферу и то, что хочет первым, "
             "и первым ходом вытащи конкретику одним вопросом. Не лей разбор на пустых данных. "
             if skipped else "")
    return (
        "[ВХОД С КВИЗА] Первое сообщение. Человек только что ответил на вопросы, ты уже видел "
        "ответы. Узнавание, опора на его слова, ОДНА связка, ОДИН вопрос. Порядок и форма живые, "
        f"не шаблон. {extra}\n"
    )


def build_user_prompt(*, card: dict, step_directive: str, history: list[dict],
                      user_text: str, track_hints: dict | None = None) -> str:
    win = history[-HISTORY_WINDOW:]
    hist = "\n".join(f"{h['role']}: {h['text']}" for h in win)
    entry = "" if hist.strip() else entry_directive(card)
    return (
        f"[Карточка] {facts(card, track_hints=track_hints)}\n"
        f"{entry}"
        f"[Шаг] {step_directive}\n"
        f"[История]\n{hist}\n"
        f"[Сейчас написал] {user_text}\n\n"
        f"Ответь в своём голосе. Можно двумя-тремя сообщениями через ///:"
    )
