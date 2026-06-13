"""
handlers/analyze.py — запуск анализатора прямо из бота.

Команды:
  /analyze — запустить анализ дневника и получить новые идеи
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


@router.message(Command("analyze_fast"))
async def cmd_analyze_fast(message: Message):
    """Запустить анализатор без NotebookLM (только дневник + Claude)."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", "analyzer.py",
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
        "pgrep", "-f", "analyzer.py",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if out.strip():
        await message.answer("⏳ Анализатор уже запущен. Когда закончит — пришлю /ideas.")
        return

    await message.answer("🚀 Запускаю анализ дневника...\n\nЭто займёт несколько минут. Пришлю уведомление когда готово.")

    asyncio.create_task(_run_analyzer_background(message))


async def _run_analyzer_background(message: Message, no_nlm: bool = False):
    """Запускает анализатор в фоне и сообщает о результате."""
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
