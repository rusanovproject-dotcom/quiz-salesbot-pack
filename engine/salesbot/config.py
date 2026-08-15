"""config.py — всё, что меняется без переписывания промптов.

Читается из config.env рядом (он в .gitignore, туда же токены).
Правило простое: любой факт, который бот произносит вслух — цена, дата, ссылка —
живёт ЗДЕСЬ. В промпте фактов нет. Иначе бот однажды назовёт цену прошлого месяца.
"""
from __future__ import annotations
import json, os
from pathlib import Path

BASE = Path(__file__).parent


def load_env(path: Path) -> None:
    """Минимальный читатель .env — без зависимостей."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env(BASE / "config.env")


def _json_env(key: str, default):
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


# ── Куда складываются карточки. ВНЕ репозитория: там персональные данные ──
LEADS_DIR = os.path.expanduser(os.getenv("LEADS_DIR", "~/quiz-leads"))

# ── Транспорт ──
VK_TOKEN = os.getenv("VK_TOKEN", "")
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# Предохранитель на первые дни: задан — бот отвечает ТОЛЬКО на сообщения,
# начинающиеся с этой строки. Живые люди в это время бота не видят.
TEST_PREFIX = os.getenv("TEST_PREFIX", "").strip()

# Черновой режим: ответы уходят не человеку, а владельцу на утверждение
DRAFT_MODE = os.getenv("DRAFT_MODE", "0") == "1"

# Куда бот стучится, когда застрял. Свой id в том же мессенджере
OWNER_ID = os.getenv("OWNER_ID", "")

# ── Что бот говорит вслух. Нет значения → бот про это молчит ──
OFFER = {
    "name":      os.getenv("OFFER_NAME", ""),        # что продаём
    "price":     os.getenv("OFFER_PRICE", ""),       # сколько стоит
    "pay_url":   os.getenv("OFFER_PAY_URL", ""),     # куда платить
    "free_url":  os.getenv("FREE_URL", ""),          # бесплатное: гайд, запись, материал
    "event":     os.getenv("EVENT_NAME", ""),        # эфир, разбор, встреча
    "event_date": os.getenv("EVENT_DATE", ""),       # когда
}

# ── Класс-диагноз из первого ответа квиза ──
# {'ключ класса из quiz.config.js': ['слова', 'которые', 'ищем', 'в ответе']}
CLASS_MAP = _json_env("CLASS_MAP", {})

# ── Дорожки оффера по ответу про размер (Q6) ──
# A — можно предлагать платное. B — только бесплатное, без дожима.
# Человеку дорожка НЕ называется никогда.
TRACK_RULES = _json_env("TRACK_RULES", {})

TRACK_HINTS = _json_env("TRACK_HINTS", {
    "A": "плотная (платное предложение уместно)",
    "B": "бережная (только бесплатное, без дожима)",
})


def links_for_step(step: str) -> str:
    """Реальные факты в директиву шага — чтобы модель не писала «[ссылка здесь]».

    Отдаём только то, что заполнено. Пустое поле = бот про это не заикается.
    """
    bits = []
    if step in ("FEEDBACK", "BRIDGE"):
        if OFFER["free_url"]:
            bits.append(f"бесплатный материал: {OFFER['free_url']}")
        if OFFER["event"]:
            ev = OFFER["event"] + (f" — {OFFER['event_date']}" if OFFER["event_date"] else "")
            bits.append(ev)
    elif step == "PITCH":
        if OFFER["name"]:
            bits.append(f"предложение: {OFFER['name']}")
        if OFFER["price"]:
            bits.append(f"цена {OFFER['price']}")
        if OFFER["pay_url"]:
            bits.append(f"ссылка на оплату: {OFFER['pay_url']}")
    if not bits:
        return ""
    return " Используй ТОЧНО эти данные, не выдумывай: " + ", ".join(bits) + "."


def read_persona() -> str:
    p = BASE / "persona" / "persona.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_connections() -> str:
    p = BASE / "persona" / "connections.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""
