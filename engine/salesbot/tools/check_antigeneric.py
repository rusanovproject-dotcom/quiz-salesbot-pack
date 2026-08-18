#!/usr/bin/env python3
"""check_antigeneric.py — проверка реплик бота на пустоту, канцелярит и длину.

Гоняется по выходу sim.py --live. Ловит не всё, но ловит самое стыдное:
робота видно по нескольким словам, и они всегда одни и те же.

    python3 tools/check_antigeneric.py /tmp/live.json
"""
import json, os, sys

# Канцелярит и обороты, по которым машинный текст виден сразу.
# Он от ниши не зависит — поэтому встроенный и никаким файлом не отключается
FORBIDDEN = ["в современном мире", "стоит отметить", "давайте разбер", "раскрой потенциал",
             "комплексный подход", "важно понимать", "в эпоху", "как известно",
             "не просто", "ключевую роль", "неотъемлем"]

# Советы, которые подходят кому угодно. Их наличие = разбор не про человека.
# ЭТО ЗАПАСНОЙ список, и он из чужой ниши. В твоей нише пустые советы другие —
# впиши их в tools/antigeneric-custom.txt, иначе гейт зелёный на любом тексте
GENERIC = ["начни с chatgpt", "изучай нейросети", "используй нейросети", "автоматизируй процессы",
           "оптимизируй бизнес-процессы", "внедри crm", "начни вести соцсети"]

CUSTOM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "antigeneric-custom.txt")

MAX_LEN = 600


def load_generic(path: str | None = None) -> tuple[list[str], str]:
    """Список пустых советов и откуда он взят.

    Файл рядом со скриптом: по строке на фразу, «#» — комментарий. Есть в нём
    хоть одна фраза — работаем по нему, встроенный не подмешиваем. Нет файла или
    в нём одни комментарии — остаётся встроенный, чтобы не проверять пустым списком.
    """
    label = path or "tools/antigeneric-custom.txt"
    path = CUSTOM_FILE if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return GENERIC, "встроенный список"

    phrases = [s.strip().lower() for s in lines if s.strip() and not s.strip().startswith("#")]
    return (phrases, label) if phrases else (GENERIC, "встроенный список")


def check_reply(text: str, generic: list[str] | None = None) -> list[str]:
    t = (text or "").lower()
    flags = []
    for p in FORBIDDEN:
        if p in t:
            flags.append(f"канцелярит: «{p}»")
    for p in (GENERIC if generic is None else generic):
        if p in t:
            flags.append(f"пустой совет: «{p}»")
    if t.count("это не ") >= 2:
        flags.append("штамп «это не X, это Y» больше одного раза")
    longest = max((len(part) for part in text.split("///")), default=0)
    if longest > MAX_LEN:
        flags.append(f"длина: {longest} знаков при пределе {MAX_LEN}")
    return flags


def main():
    if len(sys.argv) < 2:
        raise SystemExit("как звать: python3 tools/check_antigeneric.py /tmp/live.json")
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    generic, source = load_generic()
    print(f"пустые советы: {source} — фраз {len(generic)}\n")
    bad = 0
    for persona in data:
        for turn in persona["turns"]:
            flags = check_reply(turn.get("reply", ""), generic)
            if flags:
                bad += 1
                print(f"[{persona['id']}] шаг {turn['step']}: {'; '.join(flags)}")
    print(f"\n{'ЕСТЬ ЧТО ЧИНИТЬ' if bad else 'ЧИСТО'}: помечено реплик — {bad}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
