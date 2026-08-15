"""brain.py — мозг бота: вызов Claude Code без инструментов.

Почему именно так:
  · claude -p — по подписке, отдельный ключ API не нужен
  · без инструментов и без MCP — боту нужен только текст. Иначе каждый ответ
    тянет десятки лишних инструментов и тормозит на секунды
  · cwd в стороне от проекта — чтобы модель не подхватывала файлы и настройки
    твоего рабочего каталога и не приносила их в диалог с клиентом
  · упал или молчит — поднимаем BrainError, а не возвращаем пустую строку.
    Пустой ответ в проде выглядит как «бот проигнорировал человека»
"""
from __future__ import annotations
import asyncio, logging, os
from typing import Awaitable, Callable

log = logging.getLogger("salesbot.brain")

BURST = "///"          # маркер разбивки на несколько сообщений подряд
NOTOOLS = ["--allowedTools", "", "--strict-mcp-config"]
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
SANDBOX_CWD = os.getenv("BRAIN_CWD", "/tmp")


class BrainError(Exception):
    """Мозг упал, завис или вернул пусто. Наверху это либо повтор, либо алерт владельцу."""


RawRunner = Callable[..., Awaitable[str]]


def clean(text: str) -> str:
    """Срезать служебное, что иногда подмешивают глобальные настройки Claude Code.

    Живой пример: у владельца в личных настройках стоит «начинай ответ со строки /rename» —
    и эта строка уезжает клиенту в переписку. Фильтр дешевле, чем объяснения потом.
    """
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith(("📌", "/rename", "_подключился")):
            continue
        if s and set(s) <= set("-—–*_= "):    # голая горизонтальная черта — не стиль переписки
            continue
        out.append(ln)
    return "\n".join(out).strip()


def split_burst(raw: str, max_parts: int = 4) -> list[str]:
    """Сырой ответ → список сообщений по маркеру ///.

    Два коротких сообщения читаются живее одного длинного. Ограничение сверху —
    чтобы бот не превратился в пулемёт и не словил ограничение мессенджера.
    """
    text = clean(raw)
    parts = [p.strip() for p in text.split(BURST)]
    parts = [p for p in parts if p]
    return (parts or [text])[:max_parts]


async def _run_claude(prompt: str, system: str, timeout: float) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", *NOTOOLS, "--model", MODEL,
            "--append-system-prompt", system,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=SANDBOX_CWD,
        )
    except Exception as e:
        raise BrainError(f"не смог запустить claude: {e}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise BrainError(f"claude не ответил за {timeout} с") from e
    if proc.returncode != 0:
        raise BrainError(f"claude вернул код {proc.returncode}: {err.decode()[:200]}")
    raw = out.decode()
    if not raw.strip():
        raise BrainError("claude вернул пусто")
    return raw


async def reply(prompt: str, system: str, *, timeout: float = 45.0,
                runner: RawRunner | None = None) -> list[str]:
    """Ответ человеку как список сообщений.

    runner подменяется в тестах и на полигоне — поэтому разбор ответа проверяется
    без запуска модели и без трат.
    """
    run = runner or _run_claude
    raw = await run(prompt, system, timeout)
    parts = split_burst(raw)
    if not parts:
        raise BrainError("мозг вернул пустой ответ")
    return parts
