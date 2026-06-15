"""
handlers/capture.py — голосовой захват мыслей вне контекста фидбека.

Поток:
  голосовое → транскрибируем → Claude извлекает 1-3 идеи
  → сохраняем в cb_ideas (source_type='voice') + в cb_voice_captures
  → показываем извлечённые идеи с кнопками → пользователь выбирает платформу
"""

import asyncio
import io
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

_DB_DIR = Path(__file__).parent.parent

_EXTRACT_SYSTEM = """Ты помогаешь автору извлечь идеи для контента из голосовой заметки.

Задача: выдели 1-3 конкретных идеи из текста. Каждая идея — самостоятельная мысль,
которую можно раскрыть в посте, ролике или видео.

Правила:
- Берёшь только то, что реально есть в заметке
- Не добавляешь свои интерпретации
- title — короткий (до 60 символов), цепляющий
- thesis — 2-3 предложения: суть идеи + почему это важно

Ответ строго JSON-массивом без markdown:
[{"title": "...", "thesis": "..."}]"""


def _get_cb_db():
    conn = libsql.connect(
        str(_DB_DIR / "cb_bot_replica.db"),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


def _extract_ideas_sync(raw_text: str) -> list[dict]:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _EXTRACT_SYSTEM, "--model", CLAUDE_MODEL],
        input=f"Голосовая заметка:\n{raw_text}",
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(_DB_DIR),
    )
    text = result.stdout.strip()
    try:
        if "```" in text:
            start = text.index("[")
            end = text.rindex("]") + 1
            text = text[start:end]
        return json.loads(text)
    except Exception as e:
        print(f"[capture] json parse error: {e}, raw: {text[:200]}")
        return []


async def handle_voice_capture(message: Message, bot: Bot):
    """Обработать голосовое сообщение вне контекста фидбека — режим захвата мысли."""
    await message.answer("🎤 Записываю мысль...")

    buf = io.BytesIO()
    await bot.download(message.voice, destination=buf)
    from transcribe import transcribe_sync
    loop = asyncio.get_event_loop()
    raw_text = await loop.run_in_executor(None, transcribe_sync, buf.getvalue(), ".ogg")

    if not raw_text:
        await message.answer("Не удалось распознать — попробуй ещё раз.")
        return

    await message.answer(f"📝 Распознал:\n\n<i>{raw_text}</i>\n\n⏳ Извлекаю идеи...", parse_mode="HTML")

    # Сохраняем сырую запись
    conn = _get_cb_db()
    conn.execute(
        "INSERT INTO cb_voice_captures (raw_text, created_at) VALUES (?, ?)",
        (raw_text, datetime.utcnow().isoformat()),
    )
    conn.commit()

    # Извлекаем идеи через Claude
    ideas_data = await loop.run_in_executor(None, _extract_ideas_sync, raw_text)

    if not ideas_data:
        conn.sync()
        conn.close()
        await message.answer(
            "Не удалось извлечь идеи автоматически — заметка сохранена.\n"
            "Можешь посмотреть идеи через /ideas."
        )
        return

    # Сохраняем идеи
    saved_ids = []
    now = datetime.utcnow().isoformat()
    for item in ideas_data:
        conn.execute(
            "INSERT INTO cb_ideas "
            "(title, thesis, relevant_history, relevant_social, source_entries, source_type, status, created_at) "
            "VALUES (?, ?, '[]', '[]', '[]', 'voice', 'new', ?)",
            (item.get("title", "Без названия"), item.get("thesis", ""), now),
        )
        conn.commit()
        last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        saved_ids.append(last_id)

    conn.sync()
    conn.close()

    # Показываем извлечённые идеи с кнопками
    lines = [f"<b>Извлёк {len(ideas_data)} {'идею' if len(ideas_data) == 1 else 'идеи'}:</b>\n"]
    buttons = []
    for i, (item, idea_id) in enumerate(zip(ideas_data, saved_ids), 1):
        thesis_short = item["thesis"][:100] + ("..." if len(item["thesis"]) > 100 else "")
        lines.append(f"<b>{i}. {item['title']}</b>\n{thesis_short}\n")
        buttons.append([InlineKeyboardButton(
            text=f"{i}. {item['title'][:45]}",
            callback_data=f"idea:{idea_id}",
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("\n".join(lines), reply_markup=keyboard, parse_mode="HTML")
