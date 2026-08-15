"""bot_vk.py — транспорт ВКонтакте. Тонкий слой: принять, позвать ядро, отправить.

Вся логика в handle.py, тут только мессенджер. Поэтому логику можно проверять
на полигоне, не имея ни токена, ни живых людей.

Запуск:
    python3 bot_vk.py

Нужно в config.env: VK_TOKEN, LEADS_DIR. Остальное по желанию.
"""
from __future__ import annotations
import asyncio, logging, random, time
from datetime import datetime

import brain, handle, escalation, config as cfg, card as cardmod

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("salesbot.vk")

PERSONA = cfg.read_persona()
CONNECTIONS = cfg.read_connections()

# Поля, которыми владеет бот. При записи переносим только их поверх свежей карточки,
# чтобы не затереть то, что мог дописать приёмник квиза
BOT_FIELDS = ("user_id", "pipeline_step", "deepen_count", "offers", "paid",
              "rejections", "msgs_today", "last_day")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Runtime:
    """Счётчик вызовов модели за день — для общего рубильника."""

    def __init__(self):
        self.day = _today()
        self.calls_today = 0

    def refresh_day(self):
        today = _today()
        if self.day != today:
            self.day, self.calls_today = today, 0

    def tick(self) -> int:
        self.refresh_day()
        self.calls_today += 1
        return self.calls_today


async def send_owner(bot, text: str) -> None:
    """Сообщение владельцу: эскалация, алерт, черновик.

    Не задан OWNER_ID — пишем в лог, чтобы не потерять. Логика эскалации работает
    в любом случае, ей нужен только адрес доставки.
    """
    if not cfg.OWNER_ID:
        log.warning("ВЛАДЕЛЬЦУ ← %s", text)
        return
    try:
        await bot.api.messages.send(peer_id=int(cfg.OWNER_ID), message=text,
                                    random_id=random.randint(1, 2_000_000_000))
    except Exception as e:
        log.warning("не смог написать владельцу (%s): %s", e, text)


async def _typing_keepalive(bot, peer_id, stop: asyncio.Event) -> None:
    """Держать «печатает» весь ход.

    Индикатор в ВК живёт около десяти секунд, а модель думает дольше. Без подкачки
    человек видит пустоту и уходит.
    """
    while not stop.is_set():
        try:
            await bot.api.messages.set_activity(peer_id=peer_id, type="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            continue


async def process(bot, text: str, user_id, rt: Runtime, *, ref: str | None = None) -> list[str]:
    """Один ход разговора. Возвращает сообщения человеку и сохраняет карточку."""
    today = _today()
    rt.refresh_day()
    store = cfg.LEADS_DIR

    card, first_touch, owner_note = handle.resolve_card(text, user_id, store, ref=ref)
    if owner_note:
        await send_owner(bot, owner_note)

    out: list[str] = []
    if first_touch:
        out += handle.first_touch_messages(card)

    plan = handle.plan_turn(card, text, persona=PERSONA, connections=CONNECTIONS,
                            links_fn=cfg.links_for_step, today=today,
                            calls_today=rt.calls_today, draft_mode=cfg.DRAFT_MODE,
                            track_hints=cfg.TRACK_HINTS)

    if plan["to_owner"]:
        await send_owner(bot, plan["to_owner"])

    if plan["action"] in ("canned", "escalate"):
        _merge(store, card, card)
        return out + plan["to_client"]

    rt.tick()
    try:
        replies = await brain.reply(plan["user"], plan["system"], timeout=120)
    except brain.BrainError as e:
        log.warning("мозг не ответил: %s", e)
        await send_owner(bot, escalation.render_escalation_card(
            card, reason=f"мозг недоступен: {e}", user_text=text))
        # Карточку не трогаем: пусть останется как есть, ход повторится
        return out + ["Секунду, отвлёкся. Вернусь через пару минут."]

    before = len(card.get("history", []))
    replies, updated = handle.apply_reply(card, plan, replies, today=today)
    _merge(store, updated, card, new_history=updated.get("history", [])[before:])

    if plan.get("draft"):
        await send_owner(bot, "ЧЕРНОВИК человеку:\n" + "\n///\n".join(replies))
        return out or ["Секунду."]
    return out + replies


def _merge(store, updated, original, new_history=None):
    """Записать карточку, не затирая чужие правки.

    Пока модель думала, приёмник мог дописать телефон или ответы. Поэтому берём
    свежую версию с диска и накладываем только те поля, которыми владеет бот.
    """
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


def main() -> None:
    from vkbottle.bot import Bot, Message

    if not cfg.VK_TOKEN:
        raise SystemExit("Нет VK_TOKEN. Заполни config.env — см. config.env.example")

    removed = cardmod.cleanup_stale_locks(cfg.LEADS_DIR)
    if removed:
        log.info("подмёл %d забытых блокировок", removed)

    rt = Runtime()
    bot = Bot(token=cfg.VK_TOKEN)
    log.info("Старт. черновой режим=%s, предохранитель=%r, карточки=%s",
             cfg.DRAFT_MODE, cfg.TEST_PREFIX, cfg.LEADS_DIR)

    @bot.on.message()
    async def on_message(message: Message):
        text = (message.text or "").strip()
        # ref из ссылки vk.me/clubXXX?ref=КОД — приезжает при первом «Начать»
        ref = getattr(message, "ref", None) or None
        log.info("входящее от %s: %s (ref=%r)", message.from_id, text[:120], ref)

        if cfg.TEST_PREFIX:
            if not text.lower().startswith(cfg.TEST_PREFIX.lower()):
                return          # живых подписчиков не трогаем
            text = text[len(cfg.TEST_PREFIX):].strip()
        if not text:
            return

        stop = asyncio.Event()
        keepalive = asyncio.create_task(_typing_keepalive(bot, message.peer_id, stop))
        try:
            replies = await process(bot, text, message.from_id, rt, ref=ref)
        finally:
            stop.set()
            await keepalive

        for r in replies:
            # Именно messages.send с peer_id. НЕ message.answer(): он уходит через
            # peer_ids, а это массовая рассылка — токен сообщества такого не даёт
            # и ответ падает с ошибкой доступа
            await bot.api.messages.send(peer_id=message.peer_id, message=r,
                                        random_id=random.randint(1, 2_000_000_000))
        log.info("отправлено %s: %d сообщ.", message.from_id, len(replies))

    bot.run_forever()


if __name__ == "__main__":
    main()
