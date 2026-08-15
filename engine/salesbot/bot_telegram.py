"""bot_telegram.py — транспорт Telegram. Та же логика, другой мессенджер.

Без сторонних библиотек: длинный опрос на стандартном http-клиенте. Ставить
ничего не нужно, работает из коробки.

Запуск:
    python3 bot_telegram.py

Нужно в config.env: TELEGRAM_TOKEN, LEADS_DIR.

Код квиза приезжает в /start: ссылка вида https://t.me/твой_бот?start=КОД
открывает диалог, и первым сообщением бот получает «/start КОД».
В quiz.config.js для этого поставь codeParam: 'start'.
"""
from __future__ import annotations
import asyncio, json, logging, urllib.parse, urllib.request
from datetime import datetime

import brain, handle, escalation, config as cfg, card as cardmod

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("salesbot.tg")

PERSONA = cfg.read_persona()
CONNECTIONS = cfg.read_connections()
API = "https://api.telegram.org/bot{token}/{method}"

BOT_FIELDS = ("user_id", "pipeline_step", "deepen_count", "offers", "paid",
              "rejections", "msgs_today", "last_day")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _call(method: str, **params):
    """Синхронный вызов Telegram. Зовётся из потока, чтобы не блокировать цикл."""
    url = API.format(token=cfg.TELEGRAM_TOKEN, method=method)
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=70) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("telegram %s: %s", method, e)
        return {"ok": False}


async def call(method: str, **params):
    return await asyncio.to_thread(_call, method, **params)


class Runtime:
    def __init__(self):
        self.day, self.calls_today = _today(), 0

    def refresh_day(self):
        t = _today()
        if self.day != t:
            self.day, self.calls_today = t, 0

    def tick(self):
        self.refresh_day()
        self.calls_today += 1
        return self.calls_today


async def send_owner(text: str):
    if not cfg.OWNER_ID:
        log.warning("ВЛАДЕЛЬЦУ ← %s", text)
        return
    await call("sendMessage", chat_id=cfg.OWNER_ID, text=text)


def _merge(store, updated, new_history=None):
    def mutator(fresh):
        if fresh is None:
            return updated
        for f in BOT_FIELDS:
            if f in updated:
                fresh[f] = updated[f]
        if new_history:
            hist = fresh.get("history", [])
            hist.extend(new_history)
            fresh["history"] = hist
        return fresh
    cardmod.update(store, updated["quiz_id"], mutator)


async def process(text: str, user_id, chat_id, rt: Runtime, *, ref=None) -> list[str]:
    today = _today()
    rt.refresh_day()
    store = cfg.LEADS_DIR

    card, first_touch, owner_note = handle.resolve_card(text, user_id, store, ref=ref)
    if owner_note:
        await send_owner(owner_note)

    out = []
    if first_touch:
        out += handle.first_touch_messages(card)

    plan = handle.plan_turn(card, text, persona=PERSONA, connections=CONNECTIONS,
                            links_fn=cfg.links_for_step, today=today,
                            calls_today=rt.calls_today, draft_mode=cfg.DRAFT_MODE,
                            track_hints=cfg.TRACK_HINTS)
    if plan["to_owner"]:
        await send_owner(plan["to_owner"])

    if plan["action"] in ("canned", "escalate"):
        _merge(store, card)
        return out + plan["to_client"]

    rt.tick()
    await call("sendChatAction", chat_id=chat_id, action="typing")
    try:
        replies = await brain.reply(plan["user"], plan["system"], timeout=120)
    except brain.BrainError as e:
        log.warning("мозг не ответил: %s", e)
        await send_owner(escalation.render_escalation_card(
            card, reason=f"мозг недоступен: {e}", user_text=text))
        return out + ["Секунду, отвлёкся. Вернусь через пару минут."]

    before = len(card.get("history", []))
    replies, updated = handle.apply_reply(card, plan, replies, today=today)
    _merge(store, updated, updated.get("history", [])[before:])

    if plan.get("draft"):
        await send_owner("ЧЕРНОВИК человеку:\n" + "\n///\n".join(replies))
        return out or ["Секунду."]
    return out + replies


async def run():
    if not cfg.TELEGRAM_TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN. Заполни config.env — см. config.env.example")

    removed = cardmod.cleanup_stale_locks(cfg.LEADS_DIR)
    if removed:
        log.info("подмёл %d забытых блокировок", removed)

    rt, offset = Runtime(), None
    me = await call("getMe")
    log.info("Старт: @%s · черновой режим=%s · предохранитель=%r",
             (me.get("result") or {}).get("username", "?"), cfg.DRAFT_MODE, cfg.TEST_PREFIX)

    while True:
        upd = await call("getUpdates", offset=offset, timeout=50)
        for u in (upd.get("result") or []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = (msg.get("chat") or {}).get("id")
            user_id = (msg.get("from") or {}).get("id")
            if not text or chat_id is None:
                continue

            # код квиза приезжает в /start КОД
            ref = None
            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                ref = parts[1].strip() if len(parts) > 1 else None
                text = "привет"

            if cfg.TEST_PREFIX:
                if not text.lower().startswith(cfg.TEST_PREFIX.lower()):
                    continue
                text = text[len(cfg.TEST_PREFIX):].strip()
                if not text:
                    continue

            log.info("входящее от %s: %s (ref=%r)", user_id, text[:120], ref)
            try:
                replies = await process(text, user_id, chat_id, rt, ref=ref)
            except Exception:
                log.exception("сбой хода")
                replies = ["Секунду, отвлёкся. Вернусь через пару минут."]
            for r in replies:
                await call("sendMessage", chat_id=chat_id, text=r)
            log.info("отправлено %s: %d сообщ.", user_id, len(replies))


if __name__ == "__main__":
    asyncio.run(run())
