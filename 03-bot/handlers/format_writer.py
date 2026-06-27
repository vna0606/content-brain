"""
handlers/format_writer.py — генерация Reels/Shorts и YouTube сценариев.

Поддерживает Claude и Gemini для обоих форматов.
Не модифицирует post_writer.py — полностью аддитивный модуль.
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

_DB_DIR = Path(__file__).parent.parent

# State dicts
_reels_drafts: dict[int, dict] = {}    # user_id → {idea_id, draft, engine, session_id, conv_id}
_youtube_drafts: dict[int, dict] = {}  # user_id → {idea_id, draft, engine, session_id, conv_id}
_awaiting_reels_feedback: dict[int, bool] = {}
_awaiting_youtube_feedback: dict[int, bool] = {}
_pending_reels_formats: dict[int, dict] = {}  # user_id → {idea_id, formats: list[dict]}


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _get_cb_db():
    conn = libsql.connect(
        str(_DB_DIR / "cb_bot_replica.db"),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


def _fetch_idea_and_diary(idea_id: int):
    """Вернуть (title, thesis, raw_messages) для идеи."""
    from handlers.post_writer import _get_raw_diary_messages
    conn = _get_cb_db()
    cur = conn.execute(
        "SELECT title, thesis, source_entries, created_at FROM cb_ideas WHERE id = ?", (idea_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, None, []
    title, thesis = row[0], row[1]
    source_entries = json.loads(row[2] or "[]")
    raw = _get_raw_diary_messages(source_entries)
    if not raw and row[3]:
        try:
            from datetime import timedelta
            d = datetime.fromisoformat(row[3][:10])
            fallback = [(d - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(8)]
            raw = _get_raw_diary_messages(fallback)
        except Exception:
            pass
    return title, thesis, raw


# ─── Reels format generation ──────────────────────────────────────────────────

_REELS_FORMAT_SYSTEM = """Предложи 2-3 формата для раскрытия идеи в коротком видео (Reels/Shorts, до 60 сек).

Формат — это НЕ как снимать. Формат — это КАК именно мысль раскрывается структурно.
Не пиши про кадры, камеру, b-roll, руки в кадре — только нарративная логика.

Примеры форматов:
- До/после — начинаешь с одного убеждения/состояния, заканчиваешь противоположным
- Парадокс-разворот — заявляешь что-то контринтуитивное, объясняешь почему это правда
- Один момент под лупой — берёшь один конкретный момент и разворачиваешь изнутри
- Признание + переворот — "я думал X, оказалось Y"
- Вопрос без ответа — задаёшь вопрос, который зритель унесёт с собой
- Сравнение двух подходов — показываешь как выглядит "так" и "иначе"
- Юмор через узнавание — доводишь знакомый паттерн до абсурда

Ответ строго JSON без markdown:
[{"format": "Название (2-4 слова)", "logic": "Как мысль раскрывается структурно (1-2 предложения)", "duration": "~XX сек", "hook": "Первая фраза конкретно под эту идею"}]"""


def _parse_formats(text: str, tag: str) -> list[dict]:
    try:
        if "```" in text:
            text = text[text.index("["):text.rindex("]") + 1]
        return json.loads(text)
    except Exception as e:
        print(f"[{tag}] parse error: {e}, raw: {text[:200]}")
        return []


def _gen_reels_formats_sync(title: str, thesis: str, selected_approach: str) -> list[dict]:
    from handlers.post_writer import CLAUDE_MODEL, _WORK_DIR
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    approach_block = f"\nВыбранный угол: {selected_approach}" if selected_approach else ""
    inp = f"Идея: {title}\nСуть: {thesis}{approach_block}"
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _REELS_FORMAT_SYSTEM, "--model", CLAUDE_MODEL],
        input=inp, capture_output=True, text=True, env=env, timeout=60, cwd=_WORK_DIR,
    )
    return _parse_formats(result.stdout.strip(), "reels-formats")


def _gen_reels_formats_agy_sync(title: str, thesis: str, selected_approach: str) -> list[dict]:
    from handlers.post_writer import _call_agy_sync
    approach_block = f"\nВыбранный угол: {selected_approach}" if selected_approach else ""
    prompt = f"{_REELS_FORMAT_SYSTEM}\n\n---\n\nИдея: {title}\nСуть: {thesis}{approach_block}"
    text, _ = _call_agy_sync(prompt)
    return _parse_formats(text, "reels-formats-agy")


# ─── Prompt builders ──────────────────────────────────────────────────────────

def _reels_prompt(title: str, thesis: str, raw: list[str], selected_approach: str = "", reels_format: dict | None = None) -> tuple[str, str]:
    from prompts_reels import get_reels_system
    diary = "\n\n---\n".join(raw[:5]) if raw else "[Сырых записей нет — пиши строго по тезису]"
    approach_block = f"\n\nВЫБРАННЫЙ УГОЛ ПОДАЧИ:\n{selected_approach}" if selected_approach else ""
    format_block = ""
    if reels_format:
        logic = reels_format.get("logic") or reels_format.get("mechanic", "")
        format_block = (
            f"\n\nФОРМАТ РОЛИКА (строго следуй этому формату — это главное ограничение):\n"
            f"Тип: {reels_format['format']}\n"
            f"Логика раскрытия: {logic}\n"
            f"Хронометраж: {reels_format['duration']}\n"
            f"Первая фраза: {reels_format['hook']}"
        )
    user = (
        f"Идея: {title}\n\nТезис (используй из записей только то, что развивает эту мысль):\n{thesis}"
        f"{approach_block}{format_block}"
        f"\n\nСЫРЫЕ ЗАПИСИ АВТОРА:\n---\n{diary}\n---\n\nСценарий Reels:"
    )
    return get_reels_system(), user


def _youtube_prompt(title: str, thesis: str, raw: list[str], selected_approach: str = "") -> tuple[str, str]:
    from prompts_youtube import get_youtube_system
    diary = "\n\n---\n".join(raw[:5]) if raw else "[Сырых записей нет — пиши строго по тезису]"
    approach_block = f"\n\nВЫБРАННЫЙ УГОЛ ПОДАЧИ (строго следуй):\n{selected_approach}" if selected_approach else ""
    user = (
        f"Идея: {title}\n\nТезис:\n{thesis}"
        f"{approach_block}"
        f"\n\nСЫРЫЕ ЗАПИСИ АВТОРА:\n---\n{diary}\n---\n\nСтруктура ролика:"
    )
    return get_youtube_system(), user


# ─── LLM calls ────────────────────────────────────────────────────────────────

def _claude(system: str, user: str, session_id: str = "") -> tuple[str, str]:
    from handlers.post_writer import _call_claude_sync
    return _call_claude_sync(system, user, session_id)


def _gemini(prompt: str, conv_id: str = "", prev_text: str = "") -> tuple[str, str]:
    from handlers.post_writer import _call_agy_sync
    return _call_agy_sync(prompt, conv_id, prev_text)


# ─── Draft keyboards ──────────────────────────────────────────────────────────

def _reels_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Улучшить", callback_data=f"improve_reels:{idea_id}"),
         InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"regen_reels:{idea_id}")],
        [InlineKeyboardButton(text="🫀 Гуманизировать", callback_data=f"humanize_reels:{idea_id}"),
         InlineKeyboardButton(text="⬛ С нуля", callback_data=f"write_reels:{idea_id}")],
        [InlineKeyboardButton(text="♻️ Переделать в TG пост", callback_data=f"repurpose_tg_reels:{idea_id}")],
        [InlineKeyboardButton(text="❌ Отклонить идею", callback_data=f"dismiss:{idea_id}")],
    ])


def _youtube_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Улучшить", callback_data=f"improve_youtube:{idea_id}"),
         InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"regen_youtube:{idea_id}")],
        [InlineKeyboardButton(text="🫀 Гуманизировать", callback_data=f"humanize_youtube:{idea_id}"),
         InlineKeyboardButton(text="⬛ С нуля", callback_data=f"write_youtube:{idea_id}")],
        [InlineKeyboardButton(text="♻️ Переделать в TG пост", callback_data=f"repurpose_tg_youtube:{idea_id}")],
        [InlineKeyboardButton(text="❌ Отклонить идею", callback_data=f"dismiss:{idea_id}")],
    ])


async def _send_reels(message, text: str, idea_id: int):
    keyboard = _reels_keyboard(idea_id)
    preview = f"<b>📹 Сценарий Reels:</b>\n\n{text}\n\n<i>({len(text)} символов)</i>"
    if len(preview) > 4096:
        await message.answer(text[:4000] + "...")
        await message.answer(f"<i>{len(text)} символов</i>", reply_markup=keyboard)
    else:
        await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")


async def _send_youtube(message, text: str, idea_id: int):
    keyboard = _youtube_keyboard(idea_id)
    preview = f"<b>🎬 Структура YouTube:</b>\n\n{text}\n\n<i>({len(text)} символов)</i>"
    if len(preview) > 4096:
        await message.answer(text[:4000] + "...")
        await message.answer(f"<i>{len(text)} символов</i>", reply_markup=keyboard)
    else:
        await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")


# ─── Core generation ──────────────────────────────────────────────────────────

async def _gen_reels(callback: CallbackQuery, idea_id: int, engine: str):
    title, thesis, raw = _fetch_idea_and_diary(idea_id)
    if not title:
        await callback.message.answer("Идея не найдена.")
        return
    tag = "🤖 [Gemini]" if engine == "gemini" else "📓 [Claude]"
    await callback.message.answer(f"{tag} Пишу сценарий Reels...")

    from handlers.ideas import _idea_context, _ctx_thesis
    user_id = callback.from_user.id
    thesis = _ctx_thesis(user_id, idea_id, thesis)
    ctx = _idea_context.get(user_id, {})
    if ctx.get("idea_id") == idea_id:
        selected_approach = ctx.get("selected_approach", "")
        reels_format = ctx.get("reels_format", None)
    else:
        selected_approach = ""
        reels_format = None

    system, user = _reels_prompt(title, thesis, raw, selected_approach, reels_format)
    loop = asyncio.get_event_loop()
    if engine == "gemini":
        text, conv_id = await loop.run_in_executor(None, _gemini, f"{system}\n\n---\n\n{user}")
        _reels_drafts[callback.from_user.id] = {"idea_id": idea_id, "draft": text, "engine": "gemini", "conv_id": conv_id, "session_id": ""}
    else:
        text, session_id = await loop.run_in_executor(None, _claude, system, user)
        _reels_drafts[callback.from_user.id] = {"idea_id": idea_id, "draft": text, "engine": "claude", "session_id": session_id, "conv_id": ""}
    if not text:
        await callback.message.answer("Не удалось сгенерировать — попробуй снова.")
        return
    await _send_reels(callback.message, text, idea_id)


async def _gen_youtube(callback: CallbackQuery, idea_id: int, engine: str):
    title, thesis, raw = _fetch_idea_and_diary(idea_id)
    if not title:
        await callback.message.answer("Идея не найдена.")
        return
    tag = "🤖 [Gemini]" if engine == "gemini" else "📓 [Claude]"
    await callback.message.answer(f"{tag} Пишу структуру YouTube ролика...")

    from handlers.ideas import _idea_context, _ctx_thesis
    user_id = callback.from_user.id
    thesis = _ctx_thesis(user_id, idea_id, thesis)
    ctx = _idea_context.get(user_id, {})
    selected_approach = ctx.get("selected_approach", "") if ctx.get("idea_id") == idea_id else ""

    system, user = _youtube_prompt(title, thesis, raw, selected_approach)
    loop = asyncio.get_event_loop()
    if engine == "gemini":
        text, conv_id = await loop.run_in_executor(None, _gemini, f"{system}\n\n---\n\n{user}")
        _youtube_drafts[callback.from_user.id] = {"idea_id": idea_id, "draft": text, "engine": "gemini", "conv_id": conv_id, "session_id": ""}
    else:
        text, session_id = await loop.run_in_executor(None, _claude, system, user)
        _youtube_drafts[callback.from_user.id] = {"idea_id": idea_id, "draft": text, "engine": "claude", "session_id": session_id, "conv_id": ""}
    if not text:
        await callback.message.answer("Не удалось сгенерировать — попробуй снова.")
        return
    await _send_youtube(callback.message, text, idea_id)


# ─── Platform selection screens ───────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("platform:reels:"))
async def cb_platform_reels(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[2])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📓 Форматы через Claude", callback_data=f"gen_reels_formats:claude:{idea_id}"),
         InlineKeyboardButton(text="🤖 Форматы через Gemini", callback_data=f"gen_reels_formats:agy:{idea_id}")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"idea:{idea_id}")],
    ])
    await callback.message.edit_text(
        "<i>📹 Reels — выбери движок для подбора форматов:</i>",
        reply_markup=keyboard,
    )
    await callback.answer()


async def _do_gen_reels_formats(callback: CallbackQuery, idea_id: int, engine: str):
    """Общая логика генерации форматов Reels."""
    user_id = callback.from_user.id
    conn = _get_cb_db()
    row = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        return
    title, original_thesis = row[0], row[1]

    from handlers.ideas import _idea_context, _ctx_thesis
    thesis = _ctx_thesis(user_id, idea_id, original_thesis)
    ctx = _idea_context.get(user_id, {})
    selected_approach = ctx.get("selected_approach", "") if ctx.get("idea_id") == idea_id else ""

    tag = "🤖 [Gemini]" if engine == "agy" else "📓 [Claude]"
    msg = await callback.message.answer(f"{tag} Подбираю форматы...")

    loop = asyncio.get_event_loop()
    if engine == "agy":
        formats = await loop.run_in_executor(None, _gen_reels_formats_agy_sync, title, thesis, selected_approach)
    else:
        formats = await loop.run_in_executor(None, _gen_reels_formats_sync, title, thesis, selected_approach)

    if not formats:
        await msg.edit_text("Не удалось подобрать форматы — попробуй снова.")
        return

    _pending_reels_formats[user_id] = {"idea_id": idea_id, "formats": formats}

    lines = ["<b>В каком формате снимем?</b>\n"]
    buttons = []
    for i, f in enumerate(formats):
        logic = f.get("logic") or f.get("mechanic", "")
        lines.append(f"<b>{i + 1}. {f['format']}</b> <i>({f['duration']})</i>\n{logic}\n→ <i>{f['hook']}</i>\n")
        buttons.append([InlineKeyboardButton(
            text=f"{i + 1}. {f['format']} ({f['duration']})",
            callback_data=f"select_reels_format:{idea_id}:{i}",
        )])
    buttons.append([
        InlineKeyboardButton(text="🔄 Ещё (Claude)", callback_data=f"gen_reels_formats:claude:{idea_id}"),
        InlineKeyboardButton(text="🤖 Ещё (Gemini)", callback_data=f"gen_reels_formats:agy:{idea_id}"),
    ])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"idea:{idea_id}")])

    await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("gen_reels_formats:"))
async def cb_gen_reels_formats(callback: CallbackQuery):
    parts = callback.data.split(":")
    engine, idea_id = parts[1], int(parts[2])
    await callback.answer()
    await _do_gen_reels_formats(callback, idea_id, engine)


@router.callback_query(lambda c: c.data and c.data.startswith("select_reels_format:"))
async def cb_select_reels_format(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    idea_id, idx = int(parts[1]), int(parts[2])

    pending = _pending_reels_formats.get(user_id, {})
    formats = pending.get("formats", []) if pending.get("idea_id") == idea_id else []

    if not formats or idx >= len(formats):
        await callback.answer("Формат не найден — перегенерируй.")
        return

    selected = formats[idx]

    from handlers.ideas import _idea_context
    ctx = _idea_context.setdefault(user_id, {"idea_id": idea_id, "refined_thesis": "", "selected_approach": "", "approaches": [], "lens_thesis": ""})
    ctx["reels_format"] = selected
    ctx.setdefault("idea_id", idea_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📓 Claude", callback_data=f"write_reels:{idea_id}"),
         InlineKeyboardButton(text="🤖 Gemini", callback_data=f"write_reels_agy:{idea_id}")],
        [InlineKeyboardButton(text="⚡ Оба сразу", callback_data=f"write_reels_both:{idea_id}")],
        [InlineKeyboardButton(text="🔄 Другой формат", callback_data=f"platform:reels:{idea_id}"),
         InlineKeyboardButton(text="↩️ Назад", callback_data=f"idea:{idea_id}")],
    ])
    logic = selected.get("logic") or selected.get("mechanic", "")
    await callback.message.edit_text(
        f"<b>Формат:</b> {selected['format']} ({selected['duration']})\n"
        f"<i>{logic}</i>\n\n"
        f"<b>Первая фраза:</b> <i>{selected['hook']}</i>\n\n"
        f"Выбери движок для сценария:",
        reply_markup=keyboard,
    )
    await callback.answer("Формат выбран ✓")


@router.callback_query(lambda c: c.data and c.data.startswith("platform:youtube:"))
async def cb_platform_youtube(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[2])
    conn = _get_cb_db()
    row = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📓 Claude", callback_data=f"write_youtube:{idea_id}"),
         InlineKeyboardButton(text="🤖 Gemini", callback_data=f"write_youtube_agy:{idea_id}")],
        [InlineKeyboardButton(text="⚡ Оба сразу", callback_data=f"write_youtube_both:{idea_id}")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"idea:{idea_id}")],
    ])
    await callback.message.edit_text(
        f"<b>{row[0]}</b>\n\n<i>🎬 YouTube ролик — выбери движок:</i>",
        reply_markup=keyboard,
    )
    await callback.answer()


# ─── Generation callbacks ─────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("write_reels:"))
async def cb_write_reels(callback: CallbackQuery):
    await callback.answer("Генерирую сценарий...")
    await _gen_reels(callback, int(callback.data.split(":")[1]), "claude")


@router.callback_query(lambda c: c.data and c.data.startswith("write_reels_agy:"))
async def cb_write_reels_agy(callback: CallbackQuery):
    await callback.answer("Генерирую через Gemini...")
    await _gen_reels(callback, int(callback.data.split(":")[1]), "gemini")


@router.callback_query(lambda c: c.data and c.data.startswith("write_reels_both:"))
async def cb_write_reels_both(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[1])
    await callback.answer("Запускаю оба движка...")
    await callback.message.answer("⚡ Claude и Gemini пишут сценарий параллельно...")
    await asyncio.gather(
        _gen_reels(callback, idea_id, "claude"),
        _gen_reels(callback, idea_id, "gemini"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("write_youtube:"))
async def cb_write_youtube(callback: CallbackQuery):
    await callback.answer("Генерирую структуру ролика...")
    await _gen_youtube(callback, int(callback.data.split(":")[1]), "claude")


@router.callback_query(lambda c: c.data and c.data.startswith("write_youtube_agy:"))
async def cb_write_youtube_agy(callback: CallbackQuery):
    await callback.answer("Генерирую через Gemini...")
    await _gen_youtube(callback, int(callback.data.split(":")[1]), "gemini")


@router.callback_query(lambda c: c.data and c.data.startswith("write_youtube_both:"))
async def cb_write_youtube_both(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[1])
    await callback.answer("Запускаю оба движка...")
    await callback.message.answer("⚡ Claude и Gemini пишут структуру параллельно...")
    await asyncio.gather(
        _gen_youtube(callback, idea_id, "claude"),
        _gen_youtube(callback, idea_id, "gemini"),
    )


# ─── Regen ────────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("regen_reels:"))
async def cb_regen_reels(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _reels_drafts.get(user_id)
    if not stored or stored.get("idea_id") != idea_id:
        await callback.answer("Перегенерирую с нуля...")
        await _gen_reels(callback, idea_id, "claude")
        return
    await callback.answer()
    await callback.message.answer("🔄 Перегенерирую сценарий...")
    loop = asyncio.get_event_loop()
    engine = stored.get("engine", "claude")
    prompt = "Напиши другой вариант сценария. Тот же тезис, другой угол подачи."
    if engine == "gemini" and stored.get("conv_id"):
        text, new_id = await loop.run_in_executor(None, _gemini, prompt, stored["conv_id"], stored["draft"])
        if text:
            _reels_drafts[user_id] = {**stored, "draft": text, "conv_id": new_id}
            await _send_reels(callback.message, text, idea_id)
    elif engine == "claude" and stored.get("session_id"):
        text, new_id = await loop.run_in_executor(None, _claude, "", prompt, stored["session_id"])
        if text:
            _reels_drafts[user_id] = {**stored, "draft": text, "session_id": new_id}
            await _send_reels(callback.message, text, idea_id)
    else:
        await _gen_reels(callback, idea_id, engine)


@router.callback_query(lambda c: c.data and c.data.startswith("regen_youtube:"))
async def cb_regen_youtube(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _youtube_drafts.get(user_id)
    if not stored or stored.get("idea_id") != idea_id:
        await callback.answer("Перегенерирую с нуля...")
        await _gen_youtube(callback, idea_id, "claude")
        return
    await callback.answer()
    await callback.message.answer("🔄 Перегенерирую структуру ролика...")
    loop = asyncio.get_event_loop()
    engine = stored.get("engine", "claude")
    prompt = "Предложи другой вариант структуры. Тот же тезис, другой подход."
    if engine == "gemini" and stored.get("conv_id"):
        text, new_id = await loop.run_in_executor(None, _gemini, prompt, stored["conv_id"], stored["draft"])
        if text:
            _youtube_drafts[user_id] = {**stored, "draft": text, "conv_id": new_id}
            await _send_youtube(callback.message, text, idea_id)
    elif engine == "claude" and stored.get("session_id"):
        text, new_id = await loop.run_in_executor(None, _claude, "", prompt, stored["session_id"])
        if text:
            _youtube_drafts[user_id] = {**stored, "draft": text, "session_id": new_id}
            await _send_youtube(callback.message, text, idea_id)
    else:
        await _gen_youtube(callback, idea_id, engine)


# ─── Improve ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("improve_reels:"))
async def cb_improve_reels(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    if not _reels_drafts.get(user_id) or _reels_drafts[user_id].get("idea_id") != idea_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return
    _awaiting_reels_feedback[user_id] = True
    await callback.answer()
    await callback.message.answer("✏️ Напиши или надиктуй что изменить в сценарии Reels:")


@router.callback_query(lambda c: c.data and c.data.startswith("improve_youtube:"))
async def cb_improve_youtube(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    if not _youtube_drafts.get(user_id) or _youtube_drafts[user_id].get("idea_id") != idea_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return
    _awaiting_youtube_feedback[user_id] = True
    await callback.answer()
    await callback.message.answer("✏️ Напиши или надиктуй что изменить в структуре YouTube:")


# ─── Humanize ─────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("humanize_reels:"))
async def cb_humanize_reels(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _reels_drafts.get(user_id)
    if not stored or stored.get("idea_id") != idea_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return
    await callback.answer()
    await callback.message.answer("🫀 Гуманизирую сценарий...")
    from handlers.post_writer import _build_humanizer_system
    loop = asyncio.get_event_loop()
    hum = _build_humanizer_system()
    if stored.get("session_id"):
        text, new_id = await loop.run_in_executor(
            None, _claude, "", "Гуманизируй этот сценарий Reels. Убери AI-паттерны, сохрани структуру.", stored["session_id"]
        )
    else:
        text, new_id = await loop.run_in_executor(
            None, _claude, hum, f"Гуманизируй:\n\n{stored['draft']}"
        )
    if text:
        _reels_drafts[user_id] = {**stored, "draft": text, "session_id": new_id}
        await _send_reels(callback.message, text, idea_id)


@router.callback_query(lambda c: c.data and c.data.startswith("humanize_youtube:"))
async def cb_humanize_youtube(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _youtube_drafts.get(user_id)
    if not stored or stored.get("idea_id") != idea_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return
    await callback.answer()
    await callback.message.answer("🫀 Гуманизирую структуру ролика...")
    from handlers.post_writer import _build_humanizer_system
    loop = asyncio.get_event_loop()
    hum = _build_humanizer_system()
    if stored.get("session_id"):
        text, new_id = await loop.run_in_executor(
            None, _claude, "", "Гуманизируй структуру ролика. Сделай более живой и разговорной.", stored["session_id"]
        )
    else:
        text, new_id = await loop.run_in_executor(
            None, _claude, hum, f"Гуманизируй:\n\n{stored['draft']}"
        )
    if text:
        _youtube_drafts[user_id] = {**stored, "draft": text, "session_id": new_id}
        await _send_youtube(callback.message, text, idea_id)


# ─── Repurpose to TG post ─────────────────────────────────────────────────────

async def _repurpose_to_tg(callback: CallbackQuery, draft: str, idea_id: int):
    await callback.answer()
    await callback.message.answer("♻️ Переделываю в TG пост...")
    from handlers.post_writer import _call_claude_sync, _drafts, _send_draft
    from prompts import get_post_writer_system
    system = get_post_writer_system()
    user = f"Перепиши это как пост для Telegram-канала в стиле автора:\n\n{draft}"
    loop = asyncio.get_event_loop()
    text, session_id = await loop.run_in_executor(None, _call_claude_sync, system, user)
    if text:
        user_id = callback.from_user.id
        _drafts[user_id] = {"idea_id": idea_id, "draft": text, "system": system, "mode": "diary", "session_id": session_id}
        await _send_draft(callback.message, text, idea_id, mode="diary")


@router.callback_query(lambda c: c.data and c.data.startswith("repurpose_tg_reels:"))
async def cb_repurpose_reels(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _reels_drafts.get(user_id)
    if not stored:
        await callback.answer("Черновик не найден.")
        return
    await _repurpose_to_tg(callback, stored["draft"], idea_id)


@router.callback_query(lambda c: c.data and c.data.startswith("repurpose_tg_youtube:"))
async def cb_repurpose_youtube(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _youtube_drafts.get(user_id)
    if not stored:
        await callback.answer("Черновик не найден.")
        return
    await _repurpose_to_tg(callback, stored["draft"], idea_id)


# ─── Feedback processors (вызываются из post_writer.py) ──────────────────────

async def process_reels_feedback(message: Message, user_id: int, feedback: str):
    stored = _reels_drafts.get(user_id)
    if not stored:
        _awaiting_reels_feedback.pop(user_id, None)
        return
    _awaiting_reels_feedback.pop(user_id)
    idea_id = stored["idea_id"]
    await message.answer("✏️ Улучшаю сценарий Reels...")
    loop = asyncio.get_event_loop()
    engine = stored.get("engine", "claude")
    if engine == "gemini" and stored.get("conv_id"):
        text, new_id = await loop.run_in_executor(None, _gemini, feedback, stored["conv_id"], stored["draft"])
    elif engine == "claude" and stored.get("session_id"):
        text, new_id = await loop.run_in_executor(None, _claude, "", feedback, stored["session_id"])
    else:
        from prompts_reels import get_reels_system
        text, new_id = await loop.run_in_executor(
            None, _claude, get_reels_system(),
            f"Черновик:\n{stored['draft']}\n\nКомментарий: {feedback}\n\nУлучши:",
        )
    if text:
        _reels_drafts[user_id] = {**stored, "draft": text,
                                   "session_id": new_id if engine == "claude" else stored.get("session_id", ""),
                                   "conv_id": new_id if engine == "gemini" else stored.get("conv_id", "")}
        await _send_reels(message, text, idea_id)


async def process_youtube_feedback(message: Message, user_id: int, feedback: str):
    stored = _youtube_drafts.get(user_id)
    if not stored:
        _awaiting_youtube_feedback.pop(user_id, None)
        return
    _awaiting_youtube_feedback.pop(user_id)
    idea_id = stored["idea_id"]
    await message.answer("✏️ Улучшаю структуру YouTube...")
    loop = asyncio.get_event_loop()
    engine = stored.get("engine", "claude")
    if engine == "gemini" and stored.get("conv_id"):
        text, new_id = await loop.run_in_executor(None, _gemini, feedback, stored["conv_id"], stored["draft"])
    elif engine == "claude" and stored.get("session_id"):
        text, new_id = await loop.run_in_executor(None, _claude, "", feedback, stored["session_id"])
    else:
        from prompts_youtube import get_youtube_system
        text, new_id = await loop.run_in_executor(
            None, _claude, get_youtube_system(),
            f"Структура:\n{stored['draft']}\n\nКомментарий: {feedback}\n\nУлучши:",
        )
    if text:
        _youtube_drafts[user_id] = {**stored, "draft": text,
                                    "session_id": new_id if engine == "claude" else stored.get("session_id", ""),
                                    "conv_id": new_id if engine == "gemini" else stored.get("conv_id", "")}
        await _send_youtube(message, text, idea_id)
