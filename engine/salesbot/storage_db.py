"""storage_db.py — журнал заявок в Postgres. Слой необязательный.

Зачем он есть: файловая карточка — рабочая память бота, её легко снести
неудачным rsync или затереть руками. База — журнал: заявка попадает туда
следом за карточкой и остаётся. Это защита от затирания карточек, НЕ бэкап:
том базы живёт на том же диске — бэкап делает pg_dump (см. навык leads-db).

Кто главный: карточка-файл ПЕРВИЧНА, бот читает только её. База — второй экземпляр.
Поэтому любая ошибка базы гасится здесь и наверх не поднимается: потерять живого
человека из-за упавшего Postgres нельзя. Честная граница: в журнал попадает то,
что прошло через карточку — если отказал сам файловый слой, в базу заявка тоже
не попадёт.

Включается одной переменной в config.env:
    DB_URL=postgresql://quiz:пароль@127.0.0.1:5432/quiz

Нет переменной — слой молча выключен, пак работает как раньше, на стандартной
библиотеке. DB_URL задан, а драйвера (psycopg / psycopg2) нет — это мисконфиг:
warning в лог на каждую заявку, приём при этом живёт.

Схема таблицы и запуск базы — db/schema.sql и db/docker-compose.yml рядом.
"""
from __future__ import annotations
import json, logging

import card as cardmod
import config as cfg

log = logging.getLogger("salesbot.db")

CONNECT_TIMEOUT = 5           # секунд на установку соединения
STATEMENT_TIMEOUT_MS = 5000   # и столько же на сам запрос: зависшая база не держит поток вечно

# Повторный приход того же кода ДОПОЛНЯЕТ строку. Приоритет здесь — у нового
# непустого (COALESCE/NULLIF), ответы сливаются (||). Семантика совпадает с
# файловой карточкой только потому, что на вход идёт УЖЕ слитая карточка
# (см. контракт save_lead): EXCLUDED — это результат файлового merge.
UPSERT = """
INSERT INTO leads (quiz_id, name, phone, klass, track, answers, funnel)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (quiz_id) DO UPDATE SET
    name    = COALESCE(NULLIF(EXCLUDED.name,   ''), leads.name),
    phone   = COALESCE(NULLIF(EXCLUDED.phone,  ''), leads.phone),
    klass   = COALESCE(NULLIF(EXCLUDED.klass,  ''), leads.klass),
    track   = COALESCE(NULLIF(EXCLUDED.track,  ''), leads.track),
    funnel  = COALESCE(NULLIF(EXCLUDED.funnel, ''), leads.funnel),
    answers = leads.answers || EXCLUDED.answers,
    updated_at = now()
"""


def _connect(url: str):
    """Драйвер импортируем ЗДЕСЬ, а не наверху файла.

    Пак стдлибный: у того, кто базу не поднимал, psycopg не установлен — и не
    должен быть нужен. Импорт внутри функции держит это обещание.
    """
    try:
        import psycopg                                   # psycopg 3
    except ImportError:
        import psycopg2 as psycopg                       # либо psycopg2-binary
    # options уезжает в libpq в обоих драйверах: statement_timeout режет
    # не только connect, но и execute/commit на зависшей базе
    return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT,
                           options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")


def _params(c: dict) -> tuple:
    # поле карточки source ← API-поле funnel (intake.build_lead_card) → колонка funnel.
    # Зигзаг унаследован от карточки; здесь просто возвращаем имя на место.
    return (c.get("quiz_id", ""), c.get("name", "") or "", c.get("phone", "") or "",
            c.get("klass", "") or "", c.get("track", "") or "",
            json.dumps(c.get("answers") or {}, ensure_ascii=False),
            c.get("source", "") or "")


def save_lead(c: dict, *, url: str | None = None, connect=None) -> bool:
    """Записать карточку в журнал. True — записали, False — слой выключен или база молчит.

    Контракт входа: сюда приходит УЖЕ слитая карточка — результат файлового
    мутатора, где старое непустое победило. Сырой payload сюда отдавать нельзя:
    UPSERT ниже даёт победить новому непустому, и на сыром входе приоритеты
    перевернутся ровно в recovery-сценарии, ради которого журнал существует.

    Наверх не летит ничего: ни исключения драйвера, ни отсутствие библиотеки.
    """
    dsn = (cfg.DB_URL if url is None else url).strip()
    if not dsn:
        return False
    connect = connect or _connect
    conn = None
    try:
        conn = connect(dsn)
        cur = conn.cursor()
        try:
            cur.execute(UPSERT, _params(c))
        finally:
            cur.close()
        conn.commit()
        return True
    except Exception as e:
        # телефон в лог только маской: строка лога переживает нас в journald.
        # Текст ошибки нужен целиком — «OperationalError» не отличает неверный
        # пароль от отсутствующей таблицы; пароль в тексты ошибок libpq не попадает.
        reason = (str(e).splitlines() or ["?"])[0][:200]
        log.warning("заявка %s не легла в базу (%s: %s) · телефон=%s · карточка-файл на месте",
                    c.get("quiz_id", "?"), e.__class__.__name__, reason,
                    cardmod.mask_phone(c.get("phone", "")))
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def save_lead_async(c: dict) -> None:
    """То же, но в фоновом потоке — для горячего пути приёма заявки.

    Ответ квизу не должен ждать базу ни секунды: фронт ждёт ответ ~4 секунды,
    а одна установка соединения к недоступному хосту может съесть все пять.
    Поток daemon: заявка уже в карточке-файле, журнал догоняет как успеет.
    """
    import threading
    t = threading.Thread(target=save_lead, args=(dict(c),), daemon=True)
    t.start()
    global _last_thread
    _last_thread = t


_last_thread = None   # тестам: дождаться фоновой записи через _last_thread.join()
