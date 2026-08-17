"""intake.py — приёмник ответов квиза.

Квиз шлёт сюда ответы → тут собирается карточка → бот по коду поднимает её
и начинает с разбора, а не со «здравствуйте».

Запуск (стандартная библиотека, без зависимостей):
    LEADS_DIR=~/quiz-leads python3 intake.py 8087

Эндпоинт: POST /quiz-submit
Тело: {quiz_id, name?, phone?, answers: {Q1..QN}, klass?, funnel?}
Ответ: {ok, quiz_id, klass, track, link}

Про персональные данные: сюда приезжает ответ про доход или размер бизнеса,
иногда телефон. LEADS_DIR держи вне репозитория и вне публичных папок.
В лог телефон уходит только замаскированным.
"""
from __future__ import annotations
import json, logging, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import card as cardmod
import config as cfg
import storage_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("salesbot.intake")

MAX_BODY = 64 * 1024          # больше в ответы квиза не влезает при любой щедрости
MAX_FIELD = 2000              # обрезаем длинные текстовые ответы


class IngestError(ValueError):
    """Кривое тело запроса. Отвечаем 400 и живём дальше, не роняя приёмник."""


def build_lead_card(payload: dict) -> dict:
    """Тело запроса → карточка. Чистая функция: тестируется без сети и диска."""
    qid = str(payload.get("quiz_id", "")).strip()
    if not cardmod.is_valid_quiz_id(qid):
        raise IngestError("код квиза пустой или недопустимый")

    answers = payload.get("answers") or {}
    if not isinstance(answers, dict):
        raise IngestError("answers должен быть объектом Q1..QN")

    clean = {}
    for k, v in answers.items():
        if k in cardmod.QUIZ_KEYS and v not in (None, ""):
            clean[k] = str(v)[:MAX_FIELD]

    c = cardmod.new_card(quiz_id=qid,
                         name=str(payload.get("name", "") or "")[:200],
                         phone=str(payload.get("phone", "") or "")[:40])
    c["answers"] = clean
    c["source"] = str(payload.get("funnel", "") or "quiz")[:60]
    # класс мог посчитать фронт — берём его, иначе выводим из ответов
    if payload.get("klass"):
        c["klass"] = str(payload["klass"])[:60]
    cardmod.enrich_from_quiz(c, class_map=cfg.CLASS_MAP, track_rules=cfg.TRACK_RULES)
    return c


def bot_link(quiz_id: str) -> str:
    """Ссылка на диалог с ботом с кодом квиза — по ней бот узнает человека."""
    base = os.getenv("BOT_LINK", "").strip()
    if not base:
        return ""
    param = os.getenv("BOT_LINK_PARAM", "ref").strip()
    if not param:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{param}={quiz_id}"


def ingest(payload: dict, store: str) -> dict:
    """Принять ответы. Повторный приход того же кода дополняет карточку, не затирает.

    Дополняет, потому что квиз шлёт ответы дважды: сразу после прохождения и ещё раз,
    когда человек оставил телефон. Второй заход не должен обнулять первый.
    """
    fresh = build_lead_card(payload)

    def mutator(existing):
        if existing is None:
            return fresh
        # дополняем пустые поля, не трогая то, что уже наработал бот
        for k in ("name", "phone", "klass", "source"):
            if not existing.get(k) and fresh.get(k):
                existing[k] = fresh[k]
        merged = dict(existing.get("answers") or {})
        for k, v in (fresh.get("answers") or {}).items():
            if v:
                merged[k] = v
        existing["answers"] = merged
        if not existing.get("track"):
            existing["track"] = fresh.get("track")
        return existing

    saved = cardmod.update(store, fresh["quiz_id"], mutator)
    # Второй экземпляр в базу, если она настроена. Карточка-файл уже на диске:
    # база тут журнал, а не источник правды. Пишем в фоне — ответ квизу не ждёт
    # базу вовсе: фронт ждёт ~4 секунды, а недоступный хост ест пять на connect.
    storage_db.save_lead_async(saved)
    log.info("принято %s · сфера=%r · телефон=%s", saved["quiz_id"],
             (saved.get("answers") or {}).get("Q2", ""), cardmod.mask_phone(saved.get("phone", "")))
    return {"ok": True, "quiz_id": saved["quiz_id"], "klass": saved.get("klass", ""),
            "track": saved.get("track", ""), "link": bot_link(saved["quiz_id"])}


class Handler(BaseHTTPRequestHandler):
    store = cfg.LEADS_DIR

    def _send(self, code: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/quiz-submit":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"ok": False, "error": "bad_length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "bad_json"})
            return
        try:
            self._send(200, ingest(payload, self.store))
        except IngestError as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:                       # приёмник не падает никогда:
            log.exception("сбой приёма")             # упал он — потерялся живой лид
            self._send(500, {"ok": False, "error": "internal"})

    def log_message(self, fmt, *args):
        pass                                          # свой лог выше, стандартный шумит


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8087
    os.makedirs(cfg.LEADS_DIR, exist_ok=True)
    log.info("приёмник на :%d · карточки в %s", port, cfg.LEADS_DIR)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
