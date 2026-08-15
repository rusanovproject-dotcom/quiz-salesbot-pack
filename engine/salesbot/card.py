"""card.py — карточка человека, пришедшего с квиза.

Что в ней: ответы квиза, класс-диагноз, дорожка оффера, шаг диалога, история,
счётчики, метки предложенного и оплаченного.

ВАЖНО про персональные данные: здесь лежат телефон и ответ про размер бизнеса
или доход. Каталог с карточками держи ВНЕ репозитория и вне публичных папок.
Путь задаётся переменной LEADS_DIR. В логи телефон уходит только через mask_phone().
"""
from __future__ import annotations
import json, os, tempfile, time

# Ключи ответов квиза. Порядок задан движком квиза, см. engine/quiz/README.md
QUIZ_KEYS = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8")


def derive_class(answers: dict, class_map: dict) -> str:
    """Класс-диагноз из первого ответа.

    class_map приходит из конфига: {'ключ_класса': ['слова', 'для', 'матча']}.
    Пусто или не совпало → '' (бот зайдёт без класса, это допустимо).
    """
    t = (answers.get("Q1") or "").lower()
    if not t:
        return ""
    for klass, markers in (class_map or {}).items():
        if any(m.lower() in t for m in markers):
            return klass
    return ""


def derive_track(answers: dict, track_rules: dict) -> str:
    """Дорожка оффера из ответа про размер (Q6).

    track_rules из конфига: {'A': ['маркеры крупного'], 'B': ['маркеры мелкого']}.
    Дорожка НЕ озвучивается человеку — она только решает, что предложить в конце.
    Ничего не совпало → 'B' (бережная дорожка: безопаснее не давить оффером по ошибке).
    """
    t = (answers.get("Q6") or "").lower().replace(" ", "").replace(" ", "")
    if not t:
        return "B"
    for track, markers in (track_rules or {}).items():
        if any(m.lower().replace(" ", "") in t for m in markers):
            return track
    return "B"


def is_valid_quiz_id(qid: str) -> bool:
    """Контракт кода квиза: латиница с цифрами, до 10 символов.

    Проверяется и при записи, и при чтении. Симметрия закрывает две дыры:
    выход за пределы каталога через '../' и подстановку чужого длинного значения.
    """
    return bool(qid) and qid.isascii() and qid.isalnum() and 1 <= len(qid) <= 10


def new_card(*, quiz_id: str, name: str = "", phone: str = "") -> dict:
    return {
        "quiz_id": quiz_id, "name": name, "phone": phone, "user_id": None,
        "answers": {}, "klass": "", "track": None,
        "pipeline_step": "REVIEW", "deepen_count": 0,
        "offers": [], "paid": [], "rejections": 0,
        "history": [], "msgs_today": 0, "last_day": None,
        "created": int(time.time()),
    }


def enrich_from_quiz(card: dict, *, class_map: dict, track_rules: dict) -> dict:
    """Достроить класс и дорожку из ответов. Идемпотентно: повторный вызов не портит."""
    a = card.get("answers", {})
    if not card.get("klass"):
        card["klass"] = derive_class(a, class_map)
    if not card.get("track"):
        card["track"] = derive_track(a, track_rules)
    return card


# ── Хранение ─────────────────────────────────────────────────────────

def _path(base: str, quiz_id: str) -> str:
    return os.path.join(base, f"{quiz_id}.json")


def save(base: str, c: dict) -> None:
    """Атомарная запись: сначала во временный файл, потом подмена.

    Без этого одновременная запись из приёмника и из бота даёт битый JSON —
    и человек теряет свою карточку в самый неподходящий момент.
    """
    os.makedirs(base, exist_ok=True)
    path = _path(base, c["quiz_id"])
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def update(base: str, quiz_id: str, mutator):
    """Прочитать, изменить и записать карточку под блокировкой.

    Зачем: приёмник квиза и бот пишут в один файл. Пока модель думала свои восемь
    секунд, приёмник мог дописать телефон. Простой save() затрёт его — и владелец
    останется без контакта человека, который уже дал номер.

    mutator получает свежую карточку с диска (или None, если файла нет) и возвращает
    ту, что надо записать.
    """
    import fcntl
    os.makedirs(base, exist_ok=True)
    lock_path = _path(base, quiz_id) + ".lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            fresh = load(base, quiz_id)
            result = mutator(fresh)
            if result is not None:
                save(base, result)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def cleanup_stale_locks(base: str, max_age_hours: float = 2.0) -> int:
    """Подмести забытые .lock после аварийной остановки. Зовётся при старте бота."""
    if not os.path.isdir(base):
        return 0
    removed, now = 0, time.time()
    for fn in os.listdir(base):
        if not fn.endswith(".lock"):
            continue
        fp = os.path.join(base, fn)
        try:
            if now - os.path.getmtime(fp) > max_age_hours * 3600:
                os.unlink(fp)
                removed += 1
        except OSError:
            pass
    return removed


def load(base: str, quiz_id: str) -> dict | None:
    if not is_valid_quiz_id(quiz_id):
        return None
    p = _path(base, quiz_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def find_by_user_id(base: str, user_id) -> dict | None:
    """Найти карточку по привязанному человеку — для тех, кто вернулся без кода.

    Если карточек несколько (холодная плюс пришедшая с квиза), предпочитаем ту,
    что с квиза: в ней есть ответы. Среди равных — свежую.
    Перебор папки нормален на сотнях карточек; вырастет — понадобится индекс.
    """
    if user_id is None or not os.path.isdir(base):
        return None
    matches = []
    for fn in os.listdir(base):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        fp = os.path.join(base, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if str(c.get("user_id")) == str(user_id):
            is_cold = not (c.get("answers") or {})
            try:
                neg_mtime = -os.path.getmtime(fp)
            except OSError:
                neg_mtime = 0.0
            matches.append((is_cold, neg_mtime, c))
    if not matches:
        return None
    matches.sort(key=lambda x: (x[0], x[1]))
    return matches[0][2]


def mask_phone(phone: str) -> str:
    """+79991234567 → +7•••••4567. В логи и уведомления только так."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 5:
        return "•••••"
    return f"+{digits[0]}•••••{digits[-4:]}"
