"""Тесты журнала заявок в базе. Без живого Postgres — соединение подменяем.

Слой необязательный: нет DB_URL или нет драйвера — пак работает как раньше,
на файловых карточках. Поэтому главное, что тут проверяется: слой молчит, когда
его не звали, и НИКОГДА не роняет приём заявки.

    python3 -m pytest tests/ -q

Живая база подключается, только если задан TEST_DB_URL:
    TEST_DB_URL=postgresql://quiz:пароль@127.0.0.1:5432/quiz python3 -m pytest tests/ -q
"""
import json, logging, os, sys, tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import card as cardmod          # noqa: E402
import config as cfg            # noqa: E402
import intake                   # noqa: E402
import storage_db               # noqa: E402


# ── Подменное соединение ─────────────────────────────────────────────

class ФейкКурсор:
    def __init__(self, журнал, взрыв=None):
        self.журнал, self.взрыв = журнал, взрыв
        self.closed = False

    def execute(self, sql, params=None):
        if self.взрыв:
            raise self.взрыв
        self.журнал.append((sql, params))

    def close(self):
        self.closed = True


class ФейкСоединение:
    def __init__(self, журнал, взрыв=None):
        self.журнал, self.взрыв = журнал, взрыв
        self.commits, self.closed, self.курсор = 0, False, None

    def cursor(self):
        self.курсор = ФейкКурсор(self.журнал, self.взрыв)
        return self.курсор

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def фейк_коннект(журнал, взрыв=None, соединения=None):
    """Возвращает функцию-подмену _connect и копит созданные соединения."""
    def connect(url):
        c = ФейкСоединение(журнал, взрыв)
        if соединения is not None:
            соединения.append(c)
        return c
    return connect


def карточка(**поля):
    c = cardmod.new_card(quiz_id=поля.pop("quiz_id", "a1b2c"),
                         name=поля.pop("name", "Пётр"),
                         phone=поля.pop("phone", "+79990001122"))
    c["answers"] = поля.pop("answers", {"Q1": "работаю сам", "Q2": "ремонт"})
    c["klass"] = поля.pop("klass", "solo")
    c["track"] = поля.pop("track", "B")
    c["source"] = поля.pop("source", "quiz")
    c.update(поля)
    return c


# ── Слой выключается сам ─────────────────────────────────────────────

def test_без_db_url_слой_молчит():
    """Пак стдлибный: не настроил базу — работаешь на файлах, ничего не падает."""
    def нельзя_соединяться(url):
        raise AssertionError("без DB_URL соединяться нельзя")

    assert storage_db.save_lead(карточка(), url="", connect=нельзя_соединяться) is False


# ── Запись ───────────────────────────────────────────────────────────

def test_заявка_уходит_в_базу_одним_запросом():
    журнал = []
    ok = storage_db.save_lead(карточка(), url="postgresql://x/y",
                              connect=фейк_коннект(журнал))
    assert ok is True
    assert len(журнал) == 1
    sql, params = журнал[0]
    assert "leads" in sql
    assert "a1b2c" in params
    assert "+79990001122" in params
    assert "solo" in params
    # answers — шестой параметр (см. storage_db._params), не ищем по форме строки
    assert json.loads(params[5]) == {"Q1": "работаю сам", "Q2": "ремонт"}


def test_повторный_приход_дополняет_а_не_затирает():
    """Квиз шлёт дважды: сразу и после телефона. Второй заход не должен обнулить первый."""
    журнал = []
    storage_db.save_lead(карточка(), url="postgresql://x/y", connect=фейк_коннект(журнал))
    сжатый = "".join(журнал[0][0].upper().split())
    assert "ONCONFLICT(QUIZ_ID)DOUPDATE" in сжатый
    for поле in ("NAME", "PHONE", "KLASS", "TRACK"):
        # пустое новое значение не должно затирать уже записанное
        assert f"COALESCE(NULLIF(EXCLUDED.{поле},''),LEADS.{поле})" in сжатый
    assert "ANSWERS=LEADS.ANSWERS||EXCLUDED.ANSWERS" in сжатый    # слияние ответов


def test_соединение_закрывается_и_запись_подтверждается():
    журнал, соединения = [], []
    storage_db.save_lead(карточка(), url="postgresql://x/y",
                         connect=фейк_коннект(журнал, соединения=соединения))
    c = соединения[0]
    assert c.commits == 1
    assert c.closed is True
    assert c.курсор.closed is True


# ── База молчит — пак живёт ──────────────────────────────────────────

def test_нет_драйвера_слой_молча_выключен():
    """psycopg не установлен — это нормальный режим пака, а не авария."""
    def нет_библиотеки(url):
        raise ImportError("No module named 'psycopg'")

    assert storage_db.save_lead(карточка(), url="postgresql://x/y",
                                connect=нет_библиотеки) is False


def test_ошибка_базы_не_поднимается_наверх():
    журнал, соединения = [], []
    ok = storage_db.save_lead(
        карточка(), url="postgresql://x/y",
        connect=фейк_коннект(журнал, взрыв=OSError("сервер ушёл"), соединения=соединения))
    assert ok is False
    assert соединения[0].closed is True          # соединение не повисло
    assert соединения[0].курсор.closed is True   # и курсор тоже
    assert соединения[0].commits == 0


def test_телефон_в_логе_замаскирован(caplog):
    """Строка лога переживает нас в journald — номер туда попадать не должен."""
    with caplog.at_level(logging.WARNING, logger="salesbot.db"):
        storage_db.save_lead(карточка(phone="+79991234567"), url="postgresql://x/y",
                             connect=фейк_коннект([], взрыв=OSError("сервер ушёл")))
    текст = caplog.text
    assert "+79991234567" not in текст
    assert "9991234" not in текст
    assert "+7•••••4567" in текст


# ── Приём заявки: файл первичен, база вторична ───────────────────────

def test_заявка_ложится_и_в_файл_и_в_базу(monkeypatch):
    журнал = []
    monkeypatch.setattr(cfg, "DB_URL", "postgresql://x/y")
    monkeypatch.setattr(storage_db, "_connect", фейк_коннект(журнал))
    with tempfile.TemporaryDirectory() as d:
        ответ = intake.ingest({"quiz_id": "z9x8c", "name": "Пётр",
                               "answers": {"Q1": "работаю сам", "Q2": "ремонт"}}, d)
        assert ответ["ok"] is True
        assert cardmod.load(d, "z9x8c") is not None       # файл — рабочая память бота
    storage_db._last_thread.join(timeout=5)                # запись в базу — фоновая
    assert len(журнал) == 1                                # база — журнал
    assert "z9x8c" in журнал[0][1]


def test_повторный_приход_в_базу_несёт_слитую_карточку(monkeypatch):
    """Регресс-ловушка: в базу обязан ехать результат файлового merge (saved),
    а не сырой payload. Повтор без телефона не должен увезти в журнал пустоту."""
    журнал = []
    monkeypatch.setattr(cfg, "DB_URL", "postgresql://x/y")
    monkeypatch.setattr(storage_db, "_connect", фейк_коннект(журнал))
    with tempfile.TemporaryDirectory() as d:
        intake.ingest({"quiz_id": "z9x8c", "phone": "+79990001122",
                       "answers": {"Q2": "ремонт"}}, d)
        storage_db._last_thread.join(timeout=5)
        intake.ingest({"quiz_id": "z9x8c", "name": "Пётр",
                       "answers": {"Q4": "нет времени"}}, d)      # повтор БЕЗ телефона
        storage_db._last_thread.join(timeout=5)
    assert len(журнал) == 2
    параметры_повтора = журнал[1][1]
    assert "+79990001122" in параметры_повтора    # старый телефон уехал в журнал
    assert "Пётр" in параметры_повтора            # новое имя тоже


def test_упавшая_база_не_роняет_приём_заявки(monkeypatch):
    """Потерять живого человека из-за упавшего Postgres нельзя."""
    def взрыв(url):
        raise OSError("connection refused")

    monkeypatch.setattr(cfg, "DB_URL", "postgresql://x/y")
    monkeypatch.setattr(storage_db, "_connect", взрыв)
    with tempfile.TemporaryDirectory() as d:
        ответ = intake.ingest({"quiz_id": "z9x8c", "phone": "+79990001122",
                               "answers": {"Q2": "ремонт"}}, d)
        assert ответ["ok"] is True
        assert cardmod.load(d, "z9x8c")["phone"] == "+79990001122"
        storage_db._last_thread.join(timeout=5)   # фоновая запись отработала и погасла


# ── Живая база (только если задан TEST_DB_URL) ───────────────────────

TEST_DB_URL = os.getenv("TEST_DB_URL", "").strip()


СХЕМА = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "db", "schema.sql")


@pytest.mark.skipif(not TEST_DB_URL, reason="нет TEST_DB_URL — живая база не проверяется")
def test_живая_база_дополняет_карточку_а_не_затирает():
    """Квиз шлёт заявку дважды: сначала ответы, потом телефон. Оба должны остаться."""
    conn = storage_db._connect(TEST_DB_URL)
    cur = conn.cursor()
    qid = "t" + os.urandom(3).hex()
    try:
        with open(СХЕМА, encoding="utf-8") as f:
            # файл целиком одним execute: резать по точкам с запятой опасно —
            # символ «;» внутри комментария или тела функции ломает нарезку
            cur.execute(f.read())
        conn.commit()

        первый = карточка(quiz_id=qid, phone="", answers={"Q1": "работаю сам", "Q2": "ремонт"})
        assert storage_db.save_lead(первый, url=TEST_DB_URL) is True

        второй = карточка(quiz_id=qid, name="", phone="+79990001122",
                          klass="", track="", answers={"Q7": "нужна смета"})
        assert storage_db.save_lead(второй, url=TEST_DB_URL) is True

        cur.execute("SELECT name, phone, klass, answers, created_at <= updated_at "
                    "FROM leads WHERE quiz_id = %s", (qid,))
        name, phone, klass, answers, порядок_времени = cur.fetchone()
        if isinstance(answers, str):
            answers = json.loads(answers)
        assert name == "Пётр"                      # пустое имя не затёрло записанное
        assert phone == "+79990001122"             # телефон второго захода дописан
        assert klass == "solo"
        assert answers == {"Q1": "работаю сам", "Q2": "ремонт", "Q7": "нужна смета"}
        assert порядок_времени is True
    finally:
        try:
            cur.execute("DELETE FROM leads WHERE quiz_id = %s", (qid,))
            conn.commit()
        finally:
            conn.close()
