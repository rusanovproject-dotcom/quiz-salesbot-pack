"""Тесты детектора пустых советов. Без сети и без модели.

Главное, что тут проверяется: список пустых советов берётся из ниши владельца.
Прошитый список чужой ниши даёт зелёный гейт на любом тексте — то есть проверку,
которой не было.

    python3 -m pytest tests/ -q
"""
import os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import check_antigeneric as ag  # noqa: E402


def _файл(dirname, text):
    path = os.path.join(dirname, "antigeneric-custom.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_кастомный_список_подхватывается_вместо_встроенного():
    with tempfile.TemporaryDirectory() as d:
        path = _файл(d, "# пустые советы мастерской\nотдай в мастерскую\nпочини сам\n")
        generic, source = ag.load_generic(path)
        assert generic == ["отдай в мастерскую", "почини сам"]
        assert source == path


def test_комментарии_и_пустые_строки_не_попадают_в_список():
    with tempfile.TemporaryDirectory() as d:
        path = _файл(d, "# шапка\n\n   \nотдай в мастерскую\n\n# хвост\n")
        generic, _ = ag.load_generic(path)
        assert generic == ["отдай в мастерскую"]


def test_фразы_приводятся_к_нижнему_регистру():
    """Реплики сравниваются в нижнем регистре: «Почини Сам» иначе не найдётся никогда."""
    with tempfile.TemporaryDirectory() as d:
        path = _файл(d, "Почини Сам\n")
        generic, _ = ag.load_generic(path)
        assert generic == ["почини сам"]


def test_файл_без_фраз_отдаёт_встроенный_список():
    """Одни комментарии — это не список. Молча проверять пустым нельзя."""
    with tempfile.TemporaryDirectory() as d:
        path = _файл(d, "# сюда впиши свои пустые советы\n\n")
        generic, source = ag.load_generic(path)
        assert generic == ag.GENERIC
        assert source == "встроенный список"


def test_без_файла_берётся_встроенный_список():
    with tempfile.TemporaryDirectory() as d:
        generic, source = ag.load_generic(os.path.join(d, "нет-такого.txt"))
        assert generic == ag.GENERIC
        assert source == "встроенный список"


def test_совет_из_кастомного_списка_помечается():
    flags = ag.check_reply("просто отдай в мастерскую и всё", generic=["отдай в мастерскую"])
    assert any("пустой совет" in f for f in flags)


def test_канцелярит_ловится_всегда_мимо_кастомного_списка():
    """Канцелярит от ниши не зависит — он встроенный и списком не отключается."""
    flags = ag.check_reply("в современном мире это важно понимать", generic=["отдай в мастерскую"])
    assert any("канцелярит" in f for f in flags)


def test_шаблон_рядом_со_скриптом_приезжает_без_живых_фраз():
    """Ученик должен увидеть файл, но НЕ получить чужие фразы как свои.

    Живые фразы в шаблоне скрипт считает списком владельца: проверка зелёная,
    а гейта нет. Поэтому в шаблоне они закомментированы.
    """
    assert os.path.exists(ag.CUSTOM_FILE)
    generic, source = ag.load_generic()
    assert source == "встроенный список"
    assert generic == ag.GENERIC


def test_встроенный_список_предупреждает_про_чужую_нишу():
    """Молчаливый фолбэк = ученик думает, что защищён."""
    warning = ag.generic_warning("встроенный список")
    assert "чужой ниши" in warning
    assert "antigeneric-custom.txt" in warning


def test_свой_список_не_предупреждает():
    with tempfile.TemporaryDirectory() as d:
        path = _файл(d, "отдай в мастерскую\nпочини сам\n")
        _, source = ag.load_generic(path)
        assert ag.generic_warning(source) == ""
