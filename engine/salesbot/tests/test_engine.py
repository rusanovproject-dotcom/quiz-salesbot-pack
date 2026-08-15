"""Тесты движка. Без сети, без модели, без токенов.

Проверяем то, что ломается молча и дорого: опознание человека, гварды,
маршрутизацию оффера, границы машины шагов.

    python3 -m pytest tests/ -q
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import card as cardmod          # noqa: E402
import handle, pipeline, limits, objections, prompt_builder  # noqa: E402

CLASS_MAP = {"solo": ["сам", "один"], "brigade": ["бригад"], "studio": ["студи"]}
TRACK_RULES = {"A": ["больше 10", "6–10"], "B": ["до 2", "3–5", "пропущу"]}


def links(step):
    return ""


# ── Опознание человека ───────────────────────────────────────────────

def test_код_из_текста_разными_способами():
    assert handle.extract_code("код 4f8a2") == "4f8a2"
    assert handle.extract_code("КАРТА x1y2z") == "x1y2z"
    assert handle.extract_code("m3k9p1") == "m3k9p1"          # голый код


def test_обычные_слова_не_принимаются_за_код():
    assert handle.extract_code("беру") is None
    assert handle.extract_code("дорого") is None
    assert handle.extract_code("привет как дела") is None
    assert handle.extract_code("hello") is None                # латиница без цифры


def test_чужой_код_не_отдаёт_чужую_карточку():
    """Иначе один человек пришлёт чужой код и увидит чужие ответы про доход."""
    with tempfile.TemporaryDirectory() as d:
        c = cardmod.new_card(quiz_id="abc12")
        c["user_id"] = 111
        c["answers"] = {"Q2": "ремонт"}
        cardmod.save(d, c)

        got, first, note = handle.resolve_card("код abc12", 222, d)
        assert got["quiz_id"] != "abc12"      # чужую не отдали
        assert note is not None               # владельцу сообщили


def test_свою_карточку_по_коду_отдаём():
    with tempfile.TemporaryDirectory() as d:
        c = cardmod.new_card(quiz_id="abc12")
        cardmod.save(d, c)
        got, first, _ = handle.resolve_card("код abc12", 333, d)
        assert got["quiz_id"] == "abc12"
        assert first is True                  # первое касание


def test_попытка_выйти_за_каталог_не_проходит():
    with tempfile.TemporaryDirectory() as d:
        got, _, _ = handle.resolve_card("", 1, d, ref="../../etc/passwd")
        assert got["quiz_id"].startswith("u")


# ── Гварды ───────────────────────────────────────────────────────────

def test_оплатившему_больше_не_продаём():
    c = cardmod.new_card(quiz_id="a1")
    c["paid"] = ["offer"]
    plan = handle.plan_turn(c, "а что ещё есть", persona="p", connections="",
                            links_fn=links, today="2030-01-01", calls_today=0)
    assert plan["action"] == "canned"


def test_два_отказа_закрывают_диалог():
    c = cardmod.new_card(quiz_id="a1")
    c["rejections"] = 2
    assert limits.is_closed(c)


def test_агрессия_уходит_к_живому_человеку():
    c = cardmod.new_card(quiz_id="a1")
    plan = handle.plan_turn(c, "отвали, бесишь", persona="p", connections="",
                            links_fn=links, today="2030-01-01", calls_today=0)
    assert plan["action"] == "escalate"
    assert plan["to_owner"]


def test_обвинение_в_разводе_тоже_эскалация():
    c = cardmod.new_card(quiz_id="a1")
    plan = handle.plan_turn(c, "да это развод какой-то", persona="p", connections="",
                            links_fn=links, today="2030-01-01", calls_today=0)
    assert plan["action"] == "escalate"


def test_общий_рубильник_не_молчит():
    """Молчание на пике = сожжённые лиды. Отвечаем всегда."""
    c = cardmod.new_card(quiz_id="a1")
    plan = handle.plan_turn(c, "привет", persona="p", connections="",
                            links_fn=links, today="2030-01-01", calls_today=999999)
    assert plan["action"] == "canned"
    assert plan["to_client"] and plan["to_client"][0]


def test_дневной_предел_на_человека():
    c = cardmod.new_card(quiz_id="a1")
    c["last_day"], c["msgs_today"] = "2030-01-01", limits.PERSON_DAILY_CAP
    plan = handle.plan_turn(c, "ещё вопрос про смету", persona="p", connections="",
                            links_fn=links, today="2030-01-01", calls_today=0)
    assert plan["action"] == "canned"


# ── Машина шагов ─────────────────────────────────────────────────────

def test_на_входе_оффер_не_подсовываем():
    """Ранний оффер — самый частый способ потерять тёплого."""
    assert pipeline.maybe_shortcut("сколько стоит", "REVIEW") is None
    assert pipeline.maybe_shortcut("беру", "DIRECT_Q") is None


def test_готовность_купить_ведёт_к_офферу():
    assert pipeline.maybe_shortcut("беру, куда платить", "FEEDBACK") == "PITCH"


def test_прошлые_покупки_не_считаются_намерением():
    assert pipeline.maybe_shortcut("да я уже оплатил кучу таких курсов", "FEEDBACK") is None
    assert pipeline.maybe_shortcut("не беру", "FEEDBACK") is None


def test_вопрос_про_сроки_не_путается_с_вопросом_про_цену():
    """«Сколько это займёт» — про сроки. Выпрыгивать в оффер посреди разбора нельзя."""
    assert pipeline.maybe_shortcut("а сколько это займёт по времени", "DEEPEN") != "PITCH"
    assert pipeline.maybe_shortcut("сколько стоит", "FEEDBACK") == "PITCH"


def test_вопрос_как_сделать_прекращает_допрос():
    assert pipeline.maybe_shortcut("а как это собрать?", "DEEPEN") == "FEEDBACK"


def test_углублений_не_больше_двух():
    step, deepen = "DEEPEN", pipeline.DEEPEN_CAP
    nxt, _ = pipeline.next_step(step, deepen)
    assert nxt == "FEEDBACK"


def test_встречный_вопрос_не_двигает_шаг():
    assert pipeline.should_hold("а ты вообще кто?")
    assert pipeline.should_hold("ага")
    assert not pipeline.should_hold("делаю ремонт квартир под ключ уже семь лет")


# ── Карточка и маршрутизация ─────────────────────────────────────────

def test_класс_из_первого_ответа():
    assert cardmod.derive_class({"Q1": "Работаю сам, руками"}, CLASS_MAP) == "solo"
    assert cardmod.derive_class({"Q1": "У меня бригада"}, CLASS_MAP) == "brigade"
    assert cardmod.derive_class({"Q1": "нечто своё"}, CLASS_MAP) == ""


def test_неизвестный_размер_идёт_на_бережную_дорожку():
    """Ошибиться в сторону «не давить» дешевле, чем в сторону «давить»."""
    assert cardmod.derive_track({"Q6": ""}, TRACK_RULES) == "B"
    assert cardmod.derive_track({"Q6": "непонятно что"}, TRACK_RULES) == "B"
    assert cardmod.derive_track({"Q6": "Больше 10"}, TRACK_RULES) == "A"


def test_доход_не_попадает_в_промпт_цифрой():
    """В промпте только дорожка. Цифра дохода не должна всплыть в разговоре."""
    c = cardmod.new_card(quiz_id="a1")
    c["answers"] = {"Q6": "300 000 рублей", "Q2": "ремонт"}
    c["track"] = "A"
    text = prompt_builder.facts(c)
    assert "300" not in text
    assert "дорожка" in text.lower()


def test_телефон_маскируется():
    assert cardmod.mask_phone("+79991234567") == "+7•••••4567"
    assert "999" not in cardmod.mask_phone("+79991234567")


def test_запись_не_затирает_чужие_правки():
    """Приёмник дописал телефон, пока бот думал. Телефон должен остаться."""
    with tempfile.TemporaryDirectory() as d:
        c = cardmod.new_card(quiz_id="a1")
        cardmod.save(d, c)

        def add_phone(fresh):
            fresh["phone"] = "+79990001122"
            return fresh
        cardmod.update(d, "a1", add_phone)

        def bot_writes(fresh):
            fresh["pipeline_step"] = "FEEDBACK"
            return fresh
        cardmod.update(d, "a1", bot_writes)

        got = cardmod.load(d, "a1")
        assert got["phone"] == "+79990001122"
        assert got["pipeline_step"] == "FEEDBACK"


# ── Промпт ───────────────────────────────────────────────────────────

def test_без_ответов_квиза_бот_не_цитирует_несуществующее():
    c = cardmod.new_card(quiz_id="a1")
    assert not prompt_builder.card_pulled(c)
    assert "ВХОД БЕЗ ОТВЕТОВ" in prompt_builder.entry_directive(c)


def test_берётся_блок_только_нужной_сферы():
    conn = ("## Ремонт, стройка\n- связка про ремонт\n\n"
            "## Фотограф, видеограф\n- связка про съёмку\n")
    block = prompt_builder.sphere_block(conn, "Ремонт под ключ")
    assert "про ремонт" in block
    assert "про съёмку" not in block


def test_редкая_сфера_уходит_в_фолбэк_а_не_в_пустоту():
    conn = ("## Ремонт, стройка\n- связка про ремонт\n\n"
            "## Сфера вне справочника\n- строй по формуле\n")
    block = prompt_builder.sphere_block(conn, "дрессировка соколов")
    assert "по формуле" in block


def test_общие_слова_не_утаскивают_чужой_блок():
    """«Услуги» есть в половине заголовков — по нему матчить нельзя."""
    conn = ("## Бьюти-услуги, маникюр\n- связка про маникюр\n\n"
            "## Сфера вне справочника\n- формула\n")
    block = prompt_builder.sphere_block(conn, "юридические услуги")
    assert "про маникюр" not in block


# ── Разбивка ответа ──────────────────────────────────────────────────

def test_ответ_режется_на_сообщения():
    import brain
    parts = brain.split_burst("первое /// второе /// третье")
    assert parts == ["первое", "второе", "третье"]


def test_служебные_строки_не_уезжают_клиенту():
    import brain
    assert "rename" not in brain.clean("📌 /rename ТЕМА\nПривет, разбираю твой случай")
