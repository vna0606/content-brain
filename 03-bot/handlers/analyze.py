"""
handlers/analyze.py — запуск анализаторов прямо из бота.

Команды:
  /analyze — запустить анализ дневника (смыслы) и получить новые идеи
  /analyze_fast — то же, но без NotebookLM
  /analyze_events — запустить анализ событий/инфоповодов (лёгкий, без стратегии)
  /analyze_events_strategy — то же, но с учётом strategy.md
"""

import asyncio
import os
import sys
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

_ANALYZER_PATH = Path(__file__).parent.parent.parent / "02-analyzer" / "analyzer.py"
_EVENTS_ANALYZER_PATH = Path(__file__).parent.parent.parent / "02-analyzer" / "events_analyzer.py"
_EVENTS_STRATEGY_ANALYZER_PATH = Path(__file__).parent.parent.parent / "02-analyzer" / "events_strategy_analyzer.py"


@router.message(Command("analyze_fast"))
async def cmd_analyze_fast(message: Message):
    """Запустить анализатор без NotebookLM (только дневник + Claude)."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", str(_ANALYZER_PATH),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if out.strip():
        await message.answer("⏳ Анализатор уже запущен. Когда закончит — пришлю /ideas.")
        return

    await message.answer("⚡️ Быстрый анализ (без NotebookLM)...\n\nТолько данные дневника + Claude. Пришлю уведомление когда готово.")
    asyncio.create_task(_run_analyzer_background(message, no_nlm=True))


@router.message(Command("analyze"))
async def cmd_analyze(message: Message):
    """Запустить анализатор в фоне и уведомить когда готово."""
    # Проверяем что анализатор не запущен уже
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", str(_ANALYZER_PATH),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if out.strip():
        await message.answer("⏳ Анализатор уже запущен. Когда закончит — пришлю /ideas.")
        return

    await message.answer("🚀 Запускаю анализ дневника...\n\nЭто займёт несколько минут. Пришлю уведомление когда готово.")

    asyncio.create_task(_run_analyzer_background(message))


@router.message(Command("analyze_events"))
async def cmd_analyze_events(message: Message):
    """Запустить анализатор событий/инфоповодов в фоне и уведомить когда готово."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", str(_EVENTS_ANALYZER_PATH),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if out.strip():
        await message.answer("⏳ Анализатор событий уже запущен. Когда закончит — пришлю /events.")
        return

    await message.answer("📍 Ищу события/инфоповоды за последние 7 дней дневника...\n\nПришлю уведомление когда готово.")
    asyncio.create_task(_run_events_analyzer_background(message))


@router.message(Command("analyze_events_strategy"))
async def cmd_analyze_events_strategy(message: Message):
    """Запустить анализатор событий с учётом strategy.md в фоне и уведомить когда готово."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", str(_EVENTS_STRATEGY_ANALYZER_PATH),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if out.strip():
        await message.answer("⏳ Анализатор событий (со стратегией) уже запущен. Когда закончит — пришлю /events_strategy.")
        return

    await message.answer("📍 Ищу события/инфоповоды за последние 7 дней с учётом стратегии...\n\nПришлю уведомление когда готово.")
    asyncio.create_task(_run_events_strategy_analyzer_background(message))


async def _run_analyzer_background(message: Message, no_nlm: bool = False):
    """Запускает анализатор смыслов в фоне и сообщает о результате."""
    cmd = [sys.executable, str(_ANALYZER_PATH)]
    if no_nlm:
        cmd.append("--no-nlm")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        await message.answer("❌ Анализатор завис (больше 15 минут). Проверь логи на сервере.")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[-600:]
        await message.answer(f"❌ Ошибка анализатора:\n<code>{err}</code>")
        return

    from handlers.ideas import cmd_ideas
    await cmd_ideas(message)


async def _run_events_analyzer_background(message: Message):
    """Запускает анализатор событий в фоне и сообщает о результате."""
    cmd = [sys.executable, str(_EVENTS_ANALYZER_PATH)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        await message.answer("❌ Анализатор событий завис (больше 15 минут). Проверь логи на сервере.")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[-600:]
        await message.answer(f"❌ Ошибка анализатора событий:\n<code>{err}</code>")
        return

    from handlers.events import cmd_events
    await cmd_events(message)


async def _run_events_strategy_analyzer_background(message: Message):
    """Запускает анализатор событий со стратегией в фоне и сообщает о результате."""
    cmd = [sys.executable, str(_EVENTS_STRATEGY_ANALYZER_PATH)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        await message.answer("❌ Анализатор событий (со стратегией) завис (больше 15 минут). Проверь логи на сервере.")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[-600:]
        await message.answer(f"❌ Ошибка анализатора событий (со стратегией):\n<code>{err}</code>")
        return

    from handlers.events_strategy import cmd_events_strategy
    await cmd_events_strategy(message)
