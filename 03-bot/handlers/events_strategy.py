"""
handlers/events_strategy.py — вариант events.py с учётом бренд-стратегии.

Тестовый параллельный слой к events.py: тот же флоу (событие → форматы подачи → черновик),
но источник — cb_events_strategy (заполняется events_strategy_analyzer.py), где события
вычленяются с учётом strategy.md. Сравниваем с лёгким /events — если результат понравится
больше, останется как основной путь, /events — как быстрый запасной без стратегии.

Команды и callback-префиксы — с суффиксом _s, чтобы не пересекаться с events.py:
оба источника используют отдельные автоинкрементные id, без суффикса было бы неоднозначно,
какой таблице принадлежит event_id из callback_data.
"""

import asyncio
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from handlers.events import (
    _get_db,
    _event_text,
    _gen_event_formats_sync,
    _gen_event_formats_agy_sync,
    _EVENT_DRAFT_SYSTEM,
    _build_event_draft_prompt,
)

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

# user_id → {event_id, draft, format, engine}
_event_s_drafts: dict[int, dict] = {}
# user_id → {event_id, formats: list[dict]}
_pending_event_s_formats: dict[int, dict] = {}
# user_id → True (ждём текст/голос с комментарием для улучшения)
_awaiting_event_s_feedback: dict[int, bool] = {}


# ─── /events_strategy ──────────────────────────────────────────────────────────

@router.message(Command("events_strategy"))
async def cmd_events_strategy(message: Message):
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT id, title, description, status FROM cb_events_strategy "
            "WHERE status IN ('new', 'shown') ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    except Exception:
        rows = []
    conn.close()

    if not rows:
        await message.answer(
            "Нет новых событий (со стратегией).\n\n"
            "Запусти анализатор: /analyze_events_strategy",
            parse_mode="HTML",
        )
        return

    buttons = []
    lines = ["<b>События / инфоповоды (со стратегией):</b>\n"]
    for i, row in enumerate(rows, 1):
        event_id, title, description, status = row[0], row[1], row[2], row[3]
        short = description[:100] + "..." if len(description) > 100 else description
        tag = " ↩️" if status == "shown" else ""
        lines.append(f"<b>{i}. {title}{tag}</b>\n{short}\n")
        buttons.append([InlineKeyboardButton(text=f"{i}. {title[:38]}{tag}", callback_data=f"event_s:{event_id}")])

    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


def _event_s_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Форматы (Claude)", callback_data=f"gen_event_s_formats:claude:{event_id}"),
         InlineKeyboardButton(text="🤖 Форматы (Gemini)", callback_data=f"gen_event_s_formats:agy:{event_id}")],
        [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"dismiss_event_s:{event_id}"),
         InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_events_s")],
    ])


@router.callback_query(lambda c: c.data and c.data.startswith("event_s:"))
async def cb_select_event_s(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])
    conn = _get_db()
    row = conn.execute("SELECT id, title, description FROM cb_events_strategy WHERE id = ?", (event_id,)).fetchone()
    if not row:
        await callback.answer("Событие не найдено.")
        conn.close()
        return
    title, description = row[1], row[2]
    conn.execute("UPDATE cb_events_strategy SET status = 'shown' WHERE id = ? AND status = 'new'", (event_id,))
    conn.commit()
    conn.sync()
    conn.close()

    await callback.message.edit_text(
        _event_text(title, description),
        reply_markup=_event_s_keyboard(event_id),
        parse_mode="HTML",
    )
    await callback.answer()


# ─── Форматы подачи ───────────────────────────────────────────────────────────

async def _show_event_s_formats(callback: CallbackQuery, event_id: int, engine: str):
    conn = _get_db()
    row = conn.execute("SELECT title, description FROM cb_events_strategy WHERE id = ?", (event_id,)).fetchone()
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
    _pending_event_s_formats[user_id] = {"event_id": event_id, "formats": formats}

    lines = ["<b>Как это можно показать?</b>\n"]
    buttons = []
    for i, f in enumerate(formats):
        lines.append(f"<b>{i + 1}. {f['format']}</b>\n{f['angle']}\n")
        buttons.append([InlineKeyboardButton(text=f"{i + 1}. {f['format']}", callback_data=f"select_event_s_format:{event_id}:{i}")])
    buttons.append([
        InlineKeyboardButton(text="🔄 Ещё (Claude)", callback_data=f"gen_event_s_formats:claude:{event_id}"),
        InlineKeyboardButton(text="🤖 Ещё (Gemini)", callback_data=f"gen_event_s_formats:agy:{event_id}"),
    ])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"event_s:{event_id}")])

    await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("gen_event_s_formats:"))
async def cb_gen_event_s_formats(callback: CallbackQuery):
    parts = callback.data.split(":")
    engine, event_id = parts[1], int(parts[2])
    await _show_event_s_formats(callback, event_id, engine)


@router.callback_query(lambda c: c.data and c.data.startswith("select_event_s_format:"))
async def cb_select_event_s_format(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    event_id, idx = int(parts[1]), int(parts[2])

    pending = _pending_event_s_formats.get(user_id, {})
    formats = pending.get("formats", []) if pending.get("event_id") == event_id else []

    if not formats or idx >= len(formats):
        await callback.answer("Формат не найден — перегенерируй.")
        return

    selected = formats[idx]
    await callback.answer()
    await callback.message.answer(f"✍️ Пишу в формате «{selected['format']}»...")
    await _gen_event_s_draft(callback, event_id, selected, "claude")


# ─── Генерация черновика ──────────────────────────────────────────────────────

async def _gen_event_s_draft(callback: CallbackQuery, event_id: int, fmt: dict, engine: str):
    conn = _get_db()
    row = conn.execute("SELECT title, description FROM cb_events_strategy WHERE id = ?", (event_id,)).fetchone()
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
    _event_s_drafts[user_id] = {"event_id": event_id, "draft": text, "format": fmt, "engine": engine}
    await _send_event_s_draft(callback.message, text, event_id)


def _event_s_draft_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Улучшить", callback_data=f"improve_event_s:{event_id}")],
        [InlineKeyboardButton(text="🫀 Гуманизировать", callback_data=f"humanize_event_s:{event_id}"),
         InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"regen_event_s:{event_id}")],
        [InlineKeyboardButton(text="❌ Отклонить событие", callback_data=f"dismiss_event_s:{event_id}")],
    ])


async def _send_event_s_draft(message, text: str, event_id: int):
    keyboard = _event_s_draft_keyboard(event_id)
    preview = f"<b>Черновик:</b>\n\n{text}\n\n<i>({len(text)} символов)</i>"
    if len(preview) > 4096:
        await message.answer(text[:4000] + "...")
        await message.answer(f"<i>{len(text)} символов</i>", reply_markup=keyboard)
    else:
        await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("regen_event_s:"))
async def cb_regen_event_s(callback: CallbackQuery):
    user_id = callback.from_user.id
    event_id = int(callback.data.split(":")[1])
    stored = _event_s_drafts.get(user_id)
    if not stored or stored.get("event_id") != event_id:
        await callback.answer("Черновик не найден.")
        return
    await callback.answer()
    await callback.message.answer("🔄 Пишу другой вариант...")
    await _gen_event_s_draft(callback, event_id, stored["format"], stored.get("engine", "claude"))


@router.callback_query(lambda c: c.data and c.data.startswith("improve_event_s:"))
async def cb_improve_event_s(callback: CallbackQuery):
    user_id = callback.from_user.id
    event_id = int(callback.data.split(":")[1])
    stored = _event_s_drafts.get(user_id)
    if not stored or stored.get("event_id") != event_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return
    _awaiting_event_s_feedback[user_id] = True
    await callback.answer()
    await callback.message.answer("Напиши или надиктуй что изменить:")


@router.callback_query(lambda c: c.data and c.data.startswith("humanize_event_s:"))
async def cb_humanize_event_s(callback: CallbackQuery):
    user_id = callback.from_user.id
    event_id = int(callback.data.split(":")[1])
    stored = _event_s_drafts.get(user_id)
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
    _event_s_drafts[user_id] = {**stored, "draft": text}
    await _send_event_s_draft(callback.message, text, event_id)


async def process_event_s_feedback(message: Message, user_id: int, feedback: str):
    """Вызывается из post_writer.py при получении текста/голоса."""
    stored = _event_s_drafts.get(user_id)
    if not stored:
        _awaiting_event_s_feedback.pop(user_id, None)
        return
    _awaiting_event_s_feedback.pop(user_id)
    event_id = stored["event_id"]
    await message.answer("✏️ Улучшаю черновик...")

    from handlers.post_writer import _call_claude_sync
    prompt = f"Черновик:\n{stored['draft']}\n\nКомментарий — что нужно изменить:\n{feedback}\n\nУлучши, сохрани то что работает:"
    loop = asyncio.get_event_loop()
    text, _ = await loop.run_in_executor(None, _call_claude_sync, _EVENT_DRAFT_SYSTEM, prompt)
    if not text:
        await message.answer("Не удалось улучшить.")
        _awaiting_event_s_feedback[user_id] = True
        return
    _event_s_drafts[user_id] = {**stored, "draft": text}
    await _send_event_s_draft(message, text, event_id)


# ─── dismiss / back ───────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("dismiss_event_s:"))
async def cb_dismiss_event_s(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])
    conn = _get_db()
    conn.execute("UPDATE cb_events_strategy SET status = 'dismissed' WHERE id = ?", (event_id,))
    conn.commit()
    conn.sync()
    conn.close()
    await callback.answer("Событие отклонено.")
    await cmd_events_strategy(callback.message)


@router.callback_query(lambda c: c.data == "back_to_events_s")
async def cb_back_to_events_s(callback: CallbackQuery):
    await callback.answer()
    await cmd_events_strategy(callback.message)
