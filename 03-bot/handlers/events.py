"""
handlers/events.py — отдельный лёгкий флоу: события/инфоповоды → форматы подачи → черновик.

Параллельный поток к ideas.py. Без суть/линзы — просто событие → формат → текст.
Источник: таблица cb_events (заполняется events_analyzer.py, 02-analyzer).

Флоу:
  /events → список → выбрал событие → [Подобрать форматы]
  → список форматов (по образцу "10 форматов из одного инфоповода")
  → выбрал формат → черновик → [Улучшить | Гуманизировать | Перегенерировать]
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from handlers.event_formats_prompts import EVENT_FORMATS_SYSTEM_ACTIVE as _EVENT_FORMATS_SYSTEM

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()
_DB_DIR = Path(__file__).parent.parent

# user_id → {event_id, draft, format, engine}
_event_drafts: dict[int, dict] = {}
# user_id → {event_id, formats: list[dict]}
_pending_event_formats: dict[int, dict] = {}
# user_id → True  (ждём текст/голос с комментарием для улучшения)
_awaiting_event_feedback: dict[int, bool] = {}


def _get_db():
    conn = libsql.connect(
        str(_DB_DIR / "cb_bot_replica.db"),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


# ─── /events ──────────────────────────────────────────────────────────────────

@router.message(Command("events"))
async def cmd_events(message: Message):
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT id, title, description, status FROM cb_events "
            "WHERE status IN ('new', 'shown') ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    except Exception:
        rows = []
    conn.close()

    if not rows:
        await message.answer(
            "Нет новых событий.\n\n"
            "Запусти анализатор: /analyze_events",
            parse_mode="HTML",
        )
        return

    buttons = []
    lines = ["<b>События / инфоповоды:</b>\n"]
    for i, row in enumerate(rows, 1):
        event_id, title, description, status = row[0], row[1], row[2], row[3]
        short = description[:100] + "..." if len(description) > 100 else description
        tag = " ↩️" if status == "shown" else ""
        lines.append(f"<b>{i}. {title}{tag}</b>\n{short}\n")
        buttons.append([InlineKeyboardButton(text=f"{i}. {title[:38]}{tag}", callback_data=f"event:{event_id}")])

    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


def _event_text(title: str, description: str) -> str:
    return f"<b>{title}</b>\n\n{description}"


def _event_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Форматы (Claude)", callback_data=f"gen_event_formats:claude:{event_id}"),
         InlineKeyboardButton(text="🤖 Форматы (Gemini)", callback_data=f"gen_event_formats:agy:{event_id}")],
        [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"dismiss_event:{event_id}"),
         InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_events")],
    ])


@router.callback_query(lambda c: c.data and c.data.startswith("event:"))
async def cb_select_event(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])
    conn = _get_db()
    row = conn.execute("SELECT id, title, description FROM cb_events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        await callback.answer("Событие не найдено.")
        conn.close()
        return
    title, description = row[1], row[2]
    conn.execute("UPDATE cb_events SET status = 'shown' WHERE id = ? AND status = 'new'", (event_id,))
    conn.commit()
    conn.sync()
    conn.close()

    await callback.message.edit_text(
        _event_text(title, description),
        reply_markup=_event_keyboard(event_id),
        parse_mode="HTML",
    )
    await callback.answer()


# ─── Форматы подачи ───────────────────────────────────────────────────────────

def _parse_formats(text: str) -> list[dict]:
    try:
        if "```" in text:
            text = text[text.index("["):text.rindex("]") + 1]
        return json.loads(text)
    except Exception as e:
        print(f"[event-formats] parse error: {e}, raw: {text[:200]}")
        return []


def _gen_event_formats_sync(title: str, description: str) -> list[dict]:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    inp = f"Событие: {title}\nЧто произошло: {description}"
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _EVENT_FORMATS_SYSTEM, "--model", CLAUDE_MODEL],
        input=inp, capture_output=True, text=True, env=env, timeout=60, cwd=str(_DB_DIR),
    )
    return _parse_formats(result.stdout.strip())


def _gen_event_formats_agy_sync(title: str, description: str) -> list[dict]:
    from handlers.post_writer import _call_agy_sync
    prompt = f"{_EVENT_FORMATS_SYSTEM}\n\n---\n\nСобытие: {title}\nЧто произошло: {description}"
    text, _ = _call_agy_sync(prompt)
    return _parse_formats(text)


async def _show_event_formats(callback: CallbackQuery, event_id: int, engine: str):
    conn = _get_db()
    row = conn.execute("SELECT title, description FROM cb_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Событие не найдено.")
        return
    title, description = row[0], row[1]

    await callback.answer()
    tag = "🤖 [Gemini]" if engine == "agy" else "📓 [Claude]"
    msg = await callback.message.answer(f"{tag} Подбираю форматы...")

    loop = asyncio.get_event_loop()
    if engine == "agy":
        formats = await loop.run_in_executor(None, _gen_event_formats_agy_sync, title, description)
    else:
        formats = await loop.run_in_executor(None, _gen_event_formats_sync, title, description)

    if not formats:
        await msg.edit_text("Не удалось подобрать форматы — попробуй снова.")
        return

    user_id = callback.from_user.id
    _pending_event_formats[user_id] = {"event_id": event_id, "formats": formats}

    lines = ["<b>Как это можно показать?</b>\n"]
    buttons = []
    for i, f in enumerate(formats):
        lines.append(f"<b>{i + 1}. {f['format']}</b>\n{f['angle']}\n")
        buttons.append([InlineKeyboardButton(text=f"{i + 1}. {f['format']}", callback_data=f"select_event_format:{event_id}:{i}")])
    buttons.append([
        InlineKeyboardButton(text="🔄 Ещё (Claude)", callback_data=f"gen_event_formats:claude:{event_id}"),
        InlineKeyboardButton(text="🤖 Ещё (Gemini)", callback_data=f"gen_event_formats:agy:{event_id}"),
    ])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"event:{event_id}")])

    await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("gen_event_formats:"))
async def cb_gen_event_formats(callback: CallbackQuery):
    parts = callback.data.split(":")
    engine, event_id = parts[1], int(parts[2])
    await _show_event_formats(callback, event_id, engine)


@router.callback_query(lambda c: c.data and c.data.startswith("select_event_format:"))
async def cb_select_event_format(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    event_id, idx = int(parts[1]), int(parts[2])

    pending = _pending_event_formats.get(user_id, {})
    formats = pending.get("formats", []) if pending.get("event_id") == event_id else []

    if not formats or idx >= len(formats):
        await callback.answer("Формат не найден — перегенерируй.")
        return

    selected = formats[idx]
    await callback.answer()
    await callback.message.answer(f"✍️ Пишу в формате «{selected['format']}»...")
    await _gen_event_draft(callback, event_id, selected, "claude")


# ─── Генерация черновика ──────────────────────────────────────────────────────

_EVENT_DRAFT_SYSTEM = """Ты пишешь короткий текст от лица Никиты — сценарий или подпись под контент про конкретное событие из его жизни.

ВАЖНО: это НЕ философский пост с выводом. Это просто живой рассказ о том, что происходит —
без "и вот что я понял", без урока, без обобщения на тему "как устроена реальность".
Просто показываешь момент/процесс/событие так, как его можно рассказать в выбранном формате.

Пиши от первого лица, разговорным языком. Короткие абзацы (1-3 предложения), пустая строка между ними.
Никаких нумерованных списков, никакого AI-канцелярита.
Не объясняй мораль. Закончи естественно — не обязательно вопросом или выводом."""


def _build_event_draft_prompt(title: str, description: str, fmt: dict) -> str:
    return f"""Событие: {title}
Что произошло: {description}

Формат подачи: {fmt['format']}
Угол: {fmt['angle']}

Напиши текст в этом формате:"""


async def _gen_event_draft(callback: CallbackQuery, event_id: int, fmt: dict, engine: str):
    conn = _get_db()
    row = conn.execute("SELECT title, description FROM cb_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    if not row:
        await callback.message.answer("Событие не найдено.")
        return
    title, description = row[0], row[1]

    user_prompt = _build_event_draft_prompt(title, description, fmt)
    loop = asyncio.get_event_loop()
    if engine == "gemini":
        from handlers.post_writer import _call_agy_sync
        text, _ = await loop.run_in_executor(None, _call_agy_sync, f"{_EVENT_DRAFT_SYSTEM}\n\n---\n\n{user_prompt}")
    else:
        from handlers.post_writer import _call_claude_sync
        text, _ = await loop.run_in_executor(None, _call_claude_sync, _EVENT_DRAFT_SYSTEM, user_prompt)

    if not text:
        await callback.message.answer("Не удалось сгенерировать — попробуй снова.")
        return

    user_id = callback.from_user.id
    _event_drafts[user_id] = {"event_id": event_id, "draft": text, "format": fmt, "engine": engine}
    await _send_event_draft(callback.message, text, event_id)


def _event_draft_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Улучшить", callback_data=f"improve_event:{event_id}")],
        [InlineKeyboardButton(text="🫀 Гуманизировать", callback_data=f"humanize_event:{event_id}"),
         InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"regen_event:{event_id}")],
        [InlineKeyboardButton(text="❌ Отклонить событие", callback_data=f"dismiss_event:{event_id}")],
    ])


async def _send_event_draft(message, text: str, event_id: int):
    keyboard = _event_draft_keyboard(event_id)
    preview = f"<b>Черновик:</b>\n\n{text}\n\n<i>({len(text)} символов)</i>"
    if len(preview) > 4096:
        await message.answer(text[:4000] + "...")
        await message.answer(f"<i>{len(text)} символов</i>", reply_markup=keyboard)
    else:
        await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("regen_event:"))
async def cb_regen_event(callback: CallbackQuery):
    user_id = callback.from_user.id
    event_id = int(callback.data.split(":")[1])
    stored = _event_drafts.get(user_id)
    if not stored or stored.get("event_id") != event_id:
        await callback.answer("Черновик не найден.")
        return
    await callback.answer()
    await callback.message.answer("🔄 Пишу другой вариант...")
    await _gen_event_draft(callback, event_id, stored["format"], stored.get("engine", "claude"))


@router.callback_query(lambda c: c.data and c.data.startswith("improve_event:"))
async def cb_improve_event(callback: CallbackQuery):
    user_id = callback.from_user.id
    event_id = int(callback.data.split(":")[1])
    stored = _event_drafts.get(user_id)
    if not stored or stored.get("event_id") != event_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return
    _awaiting_event_feedback[user_id] = True
    await callback.answer()
    await callback.message.answer("Напиши или надиктуй что изменить:")


@router.callback_query(lambda c: c.data and c.data.startswith("humanize_event:"))
async def cb_humanize_event(callback: CallbackQuery):
    user_id = callback.from_user.id
    event_id = int(callback.data.split(":")[1])
    stored = _event_drafts.get(user_id)
    if not stored or stored.get("event_id") != event_id:
        await callback.answer("Черновик не найден.")
        return
    await callback.answer()
    await callback.message.answer("🫀 Гуманизирую...")

    from handlers.post_writer import _build_humanizer_system, _call_claude_sync
    system = _build_humanizer_system()
    prompt = f"Гуманизируй этот текст. Убери AI-паттерны, сохрани смысл и голос автора:\n\n{stored['draft']}"
    loop = asyncio.get_event_loop()
    text, _ = await loop.run_in_executor(None, _call_claude_sync, system, prompt)
    if not text:
        await callback.message.answer("Не удалось гуманизировать.")
        return
    _event_drafts[user_id] = {**stored, "draft": text}
    await _send_event_draft(callback.message, text, event_id)


async def process_event_feedback(message: Message, user_id: int, feedback: str):
    """Вызывается из post_writer.py при получении текста/голоса."""
    stored = _event_drafts.get(user_id)
    if not stored:
        _awaiting_event_feedback.pop(user_id, None)
        return
    _awaiting_event_feedback.pop(user_id)
    event_id = stored["event_id"]
    await message.answer("✏️ Улучшаю черновик...")

    from handlers.post_writer import _call_claude_sync
    prompt = f"Черновик:\n{stored['draft']}\n\nКомментарий — что нужно изменить:\n{feedback}\n\nУлучши, сохрани то что работает:"
    loop = asyncio.get_event_loop()
    text, _ = await loop.run_in_executor(None, _call_claude_sync, _EVENT_DRAFT_SYSTEM, prompt)
    if not text:
        await message.answer("Не удалось улучшить.")
        _awaiting_event_feedback[user_id] = True
        return
    _event_drafts[user_id] = {**stored, "draft": text}
    await _send_event_draft(message, text, event_id)


# ─── dismiss / back ───────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("dismiss_event:"))
async def cb_dismiss_event(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])
    conn = _get_db()
    conn.execute("UPDATE cb_events SET status = 'dismissed' WHERE id = ?", (event_id,))
    conn.commit()
    conn.sync()
    conn.close()
    await callback.answer("Событие отклонено.")
    await cmd_events(callback.message)


@router.callback_query(lambda c: c.data == "back_to_events")
async def cb_back_to_events(callback: CallbackQuery):
    await callback.answer()
    await cmd_events(callback.message)
