"""
handlers/capture.py — голосовой захват мыслей вне контекста фидбека.

Поток:
  голосовое/текст → транскрибируем (если голос) → сохраняем сырой текст в cb_voice_captures
  → спрашиваем режим и движок:
      💡 Идея
      📍 Событие (лёгкое)
      📍 Событие (со стратегией)
      🎯 Идея + событие (лёгкое)
      🎯 Идея + событие (со стратегией)
      🎯 Все три
    каждый — (Claude) / (Gemini)
  → извлекаем в выбранном режиме/движке → cb_ideas и/или cb_events и/или cb_events_strategy
  → показываем кнопки → ведут в обычные экраны идеи (/ideas), события (/events)
    или события со стратегией (/events_strategy)
"""

import asyncio
import io
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()
_DB_DIR = Path(__file__).parent.parent

# user_id → raw_text (ждём выбора режима обработки)
_pending_capture: dict[int, str] = {}

_EXTRACT_SYSTEM = """Ты помогаешь автору извлечь идеи для контента из голосовой заметки.

Задача: выдели 1-3 конкретных идеи из текста. Каждая идея — самостоятельная мысль,
которую можно раскрыть в посте, ролике или видео.

Правила:
- Берёшь только то, что реально есть в заметке
- Не добавляешь свои интерпретации
- Если ниже дана стратегия канала — отбирай и формулируй идеи так, чтобы они реально
  вписывались в темы и тон автора, а не любую мысль из заметки подряд
- title — короткий (до 60 символов), цепляющий
- thesis — 2-3 предложения: суть идеи + почему это важно

Ответ строго JSON-массивом без markdown:
[{"title": "...", "thesis": "..."}]"""


def _build_extract_system() -> str:
    from prompts import load_strategy
    strategy = load_strategy()
    if not strategy.strip():
        return _EXTRACT_SYSTEM
    return f"{_EXTRACT_SYSTEM}\n\n━━━ СТРАТЕГИЯ КАНАЛА ━━━\n{strategy}"


_EXTRACT_EVENT_SYSTEM = """Ты помогаешь автору вычленить из заметки конкретные ИНФОПОВОДЫ —
события и факты из жизни, из которых можно сделать лёгкий контент, без философии и выводов.

ЧТО СЧИТАЕТСЯ ИНФОПОВОДОМ:
- конкретное событие — что-то произошло, началось, закончилось, изменилось
- решение, которое было принято
- ситуация с естественным драматизмом или интересом (конфликт, неожиданность, прогресс)
- смена статуса, рутины, привычки, проекта
- заметный процесс — что-то делается прямо сейчас, есть стадии или прогресс

ЭТО НЕ ИНФОПОВОД:
- абстрактная мысль или философское рассуждение
- осознание/инсайт без конкретного действия или события
- общее настроение без привязки к ситуации

Выдели 1-3 инфоповода из заметки. Берёшь только то, что реально есть в тексте, без своих интерпретаций.
title — короткое название (до 60 символов), description — что именно произошло, конкретно, без выводов (2-4 предложения).

Если в заметке вообще нет инфоповодов — верни пустой массив [].

Ответ строго JSON-массивом без markdown:
[{"title": "...", "description": "..."}]"""


_EXTRACT_EVENT_STRATEGY_SYSTEM = """Ты помогаешь автору вычленить из заметки конкретные ИНФОПОВОДЫ —
события и факты из жизни, из которых можно сделать контент, без философии и выводов.

ЧТО СЧИТАЕТСЯ ИНФОПОВОДОМ:
- конкретное событие — что-то произошло, началось, закончилось, изменилось
- решение, которое было принято
- ситуация с естественным драматизмом или интересом (конфликт, неожиданность, прогресс)
- смена статуса, рутины, привычки, проекта
- заметный процесс — что-то делается прямо сейчас, есть стадии или прогресс

ЭТО НЕ ИНФОПОВОД:
- абстрактная мысль или философское рассуждение
- осознание/инсайт без конкретного действия или события
- общее настроение без привязки к ситуации

Если ниже дана стратегия канала — отбирай только те инфоповоды, которые реально подходят
автору по теме и тону, а не любой факт из заметки подряд.

Выдели 1-3 инфоповода из заметки. Берёшь только то, что реально есть в тексте, без своих интерпретаций.
title — короткое название (до 60 символов), description — что именно произошло, конкретно, без выводов (2-4 предложения).

Если в заметке вообще нет инфоповодов, подходящих по стратегии — верни пустой массив [].

Ответ строго JSON-массивом без markdown:
[{"title": "...", "description": "..."}]"""


def _build_extract_event_strategy_system() -> str:
    from prompts import load_strategy
    strategy = load_strategy()
    if not strategy.strip():
        return _EXTRACT_EVENT_STRATEGY_SYSTEM
    return f"{_EXTRACT_EVENT_STRATEGY_SYSTEM}\n\n━━━ СТРАТЕГИЯ КАНАЛА ━━━\n{strategy}"


def _get_cb_db():
    conn = libsql.connect(
        str(_DB_DIR / "cb_bot_replica.db"),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


def _parse_json_array(text: str) -> list[dict]:
    try:
        if "```" in text:
            start = text.index("[")
            end = text.rindex("]") + 1
            text = text[start:end]
        return json.loads(text)
    except Exception as e:
        print(f"[capture] json parse error: {e}, raw: {text[:200]}")
        return []


def _extract_ideas_sync(raw_text: str) -> list[dict]:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _build_extract_system(), "--model", CLAUDE_MODEL],
        input=f"Голосовая заметка:\n{raw_text}",
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(_DB_DIR),
    )
    return _parse_json_array(result.stdout.strip())


def _extract_ideas_agy_sync(raw_text: str) -> list[dict]:
    from handlers.post_writer import _call_agy_sync
    prompt = f"{_build_extract_system()}\n\n---\n\nГолосовая заметка:\n{raw_text}"
    text, _ = _call_agy_sync(prompt)
    return _parse_json_array(text)


def _extract_events_sync(raw_text: str) -> list[dict]:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _EXTRACT_EVENT_SYSTEM, "--model", CLAUDE_MODEL],
        input=f"Заметка:\n{raw_text}",
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(_DB_DIR),
    )
    return _parse_json_array(result.stdout.strip())


def _extract_events_agy_sync(raw_text: str) -> list[dict]:
    from handlers.post_writer import _call_agy_sync
    prompt = f"{_EXTRACT_EVENT_SYSTEM}\n\n---\n\nЗаметка:\n{raw_text}"
    text, _ = _call_agy_sync(prompt)
    return _parse_json_array(text)


def _extract_events_strategy_sync(raw_text: str) -> list[dict]:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _build_extract_event_strategy_system(), "--model", CLAUDE_MODEL],
        input=f"Заметка:\n{raw_text}",
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(_DB_DIR),
    )
    return _parse_json_array(result.stdout.strip())


def _extract_events_strategy_agy_sync(raw_text: str) -> list[dict]:
    from handlers.post_writer import _call_agy_sync
    prompt = f"{_build_extract_event_strategy_system()}\n\n---\n\nЗаметка:\n{raw_text}"
    text, _ = _call_agy_sync(prompt)
    return _parse_json_array(text)


async def handle_text_capture(message: Message):
    """Обработать текстовое сообщение вне контекста фидбека — режим захвата мысли."""
    raw_text = message.text.strip()
    if not raw_text:
        return
    await _save_and_ask_mode(message, raw_text)


async def handle_voice_capture(message: Message, bot: Bot):
    """Обработать голосовое сообщение вне контекста фидбека."""
    await message.answer("🎤 Записываю мысль...")
    buf = io.BytesIO()
    await bot.download(message.voice, destination=buf)
    from transcribe import transcribe_sync
    loop = asyncio.get_event_loop()
    raw_text = await loop.run_in_executor(None, transcribe_sync, buf.getvalue(), ".ogg")
    if not raw_text:
        await message.answer("Не удалось распознать — попробуй ещё раз.")
        return
    await message.answer(f"📝 Распознал:\n\n<i>{raw_text}</i>", parse_mode="HTML")
    await _save_and_ask_mode(message, raw_text)


def _mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💡 Идея (Claude)", callback_data="capture_mode:idea:claude"),
         InlineKeyboardButton(text="🤖 Идея (Gemini)", callback_data="capture_mode:idea:agy")],
        [InlineKeyboardButton(text="📍 Событие лёгкое (Claude)", callback_data="capture_mode:event:claude"),
         InlineKeyboardButton(text="🤖 Событие лёгкое (Gemini)", callback_data="capture_mode:event:agy")],
        [InlineKeyboardButton(text="📍 Событие+стратегия (Claude)", callback_data="capture_mode:event_s:claude"),
         InlineKeyboardButton(text="🤖 Событие+стратегия (Gemini)", callback_data="capture_mode:event_s:agy")],
        [InlineKeyboardButton(text="🎯 Идея+лёгкое (Claude)", callback_data="capture_mode:both:claude"),
         InlineKeyboardButton(text="🤖 Идея+лёгкое (Gemini)", callback_data="capture_mode:both:agy")],
        [InlineKeyboardButton(text="🎯 Идея+стратегия (Claude)", callback_data="capture_mode:both_s:claude"),
         InlineKeyboardButton(text="🤖 Идея+стратегия (Gemini)", callback_data="capture_mode:both_s:agy")],
        [InlineKeyboardButton(text="🎯 Все три (Claude)", callback_data="capture_mode:all:claude"),
         InlineKeyboardButton(text="🤖 Все три (Gemini)", callback_data="capture_mode:all:agy")],
    ])


async def _save_and_ask_mode(message: Message, raw_text: str):
    """Сохранить сырой текст и спросить, в каком режиме его обработать."""
    conn = _get_cb_db()
    conn.execute(
        "INSERT INTO cb_voice_captures (raw_text, created_at) VALUES (?, ?)",
        (raw_text, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.sync()
    conn.close()

    _pending_capture[message.from_user.id] = raw_text
    await message.answer("Как обработать эту мысль?", reply_markup=_mode_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("capture_mode:"))
async def cb_capture_mode(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    mode, engine = parts[1], parts[2]
    raw_text = _pending_capture.pop(user_id, None)
    if not raw_text:
        await callback.answer("Мысль не найдена — попробуй заново.")
        return

    await callback.answer()
    mode_labels = {
        "idea": "💡 Как идею",
        "event": "📍 Как событие (лёгкое)",
        "event_s": "📍 Как событие (со стратегией)",
        "both": "🎯 Идея + событие (лёгкое)",
        "both_s": "🎯 Идея + событие (со стратегией)",
        "all": "🎯 Идея + оба события",
    }
    engine_label = "🤖 Gemini" if engine == "agy" else "📓 Claude"
    await callback.message.edit_text(f"Обрабатываю ({engine_label}): {mode_labels.get(mode, mode)}...")

    if mode == "idea":
        await _extract_and_show_ideas(callback.message, raw_text, engine)
    elif mode == "event":
        await _extract_and_show_events(callback.message, raw_text, engine)
    elif mode == "event_s":
        await _extract_and_show_events_strategy(callback.message, raw_text, engine)
    elif mode == "both":
        await _extract_and_show_ideas(callback.message, raw_text, engine)
        await _extract_and_show_events(callback.message, raw_text, engine)
    elif mode == "both_s":
        await _extract_and_show_ideas(callback.message, raw_text, engine)
        await _extract_and_show_events_strategy(callback.message, raw_text, engine)
    elif mode == "all":
        await _extract_and_show_ideas(callback.message, raw_text, engine)
        await _extract_and_show_events(callback.message, raw_text, engine)
        await _extract_and_show_events_strategy(callback.message, raw_text, engine)


async def _extract_and_show_ideas(message: Message, raw_text: str, engine: str = "claude"):
    """Вычленить идеи (title+thesis) → cb_ideas → показать кнопки в экран идеи."""
    loop = asyncio.get_event_loop()
    fn = _extract_ideas_agy_sync if engine == "agy" else _extract_ideas_sync
    ideas_data = await loop.run_in_executor(None, fn, raw_text)

    if not ideas_data:
        await message.answer(
            "Не удалось извлечь идеи автоматически — заметка сохранена.\n"
            "Можешь посмотреть идеи через /ideas."
        )
        return

    conn = _get_cb_db()
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

    lines = [f"<b>💡 Извлёк {len(ideas_data)} {'идею' if len(ideas_data) == 1 else 'идеи'}:</b>\n"]
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


async def _extract_and_show_events(message: Message, raw_text: str, engine: str = "claude"):
    """Вычленить инфоповоды (title+description) → cb_events → показать кнопки в экран события."""
    loop = asyncio.get_event_loop()
    fn = _extract_events_agy_sync if engine == "agy" else _extract_events_sync
    events_data = await loop.run_in_executor(None, fn, raw_text)

    if not events_data:
        await message.answer("Не нашёл инфоповодов в этой заметке.")
        return

    conn = _get_cb_db()
    saved_ids = []
    now = datetime.utcnow().isoformat()
    for item in events_data:
        conn.execute(
            "INSERT INTO cb_events (title, description, source_entries, status, created_at) "
            "VALUES (?, ?, '[]', 'new', ?)",
            (item.get("title", "Без названия"), item.get("description", ""), now),
        )
        conn.commit()
        last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        saved_ids.append(last_id)
    conn.sync()
    conn.close()

    lines = [f"<b>📍 Нашёл {len(events_data)} {'инфоповод' if len(events_data) == 1 else 'инфоповода'}:</b>\n"]
    buttons = []
    for i, (item, event_id) in enumerate(zip(events_data, saved_ids), 1):
        desc_short = item["description"][:100] + ("..." if len(item["description"]) > 100 else "")
        lines.append(f"<b>{i}. {item['title']}</b>\n{desc_short}\n")
        buttons.append([InlineKeyboardButton(
            text=f"{i}. {item['title'][:45]}",
            callback_data=f"event:{event_id}",
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("\n".join(lines), reply_markup=keyboard, parse_mode="HTML")


async def _extract_and_show_events_strategy(message: Message, raw_text: str, engine: str = "claude"):
    """Вычленить инфоповоды (со стратегией) → cb_events_strategy → показать кнопки в экран события (стратегия)."""
    loop = asyncio.get_event_loop()
    fn = _extract_events_strategy_agy_sync if engine == "agy" else _extract_events_strategy_sync
    events_data = await loop.run_in_executor(None, fn, raw_text)

    if not events_data:
        await message.answer("Не нашёл инфоповодов (со стратегией) в этой заметке.")
        return

    conn = _get_cb_db()
    saved_ids = []
    now = datetime.utcnow().isoformat()
    for item in events_data:
        conn.execute(
            "INSERT INTO cb_events_strategy (title, description, source_entries, status, created_at) "
            "VALUES (?, ?, '[]', 'new', ?)",
            (item.get("title", "Без названия"), item.get("description", ""), now),
        )
        conn.commit()
        last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        saved_ids.append(last_id)
    conn.sync()
    conn.close()

    lines = [f"<b>🎯 Нашёл {len(events_data)} {'инфоповод' if len(events_data) == 1 else 'инфоповода'} (со стратегией):</b>\n"]
    buttons = []
    for i, (item, event_id) in enumerate(zip(events_data, saved_ids), 1):
        desc_short = item["description"][:100] + ("..." if len(item["description"]) > 100 else "")
        lines.append(f"<b>{i}. {item['title']}</b>\n{desc_short}\n")
        buttons.append([InlineKeyboardButton(
            text=f"{i}. {item['title'][:45]}",
            callback_data=f"event_s:{event_id}",
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("\n".join(lines), reply_markup=keyboard, parse_mode="HTML")
