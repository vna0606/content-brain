"""
handlers/ideas.py — показ списка идей, экран смысла, варианты подачи.

Флоу:
  /ideas → список → выбрал идею → экран смысла (суть + угол)
  → [Уточнить суть | Через линзу | Варианты подачи | Платформа]
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

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()
_DB_DIR = Path(__file__).parent.parent

# user_id → {idea_id, refined_thesis, lens_thesis, selected_approach, approaches}
_idea_context: dict[int, dict] = {}

# user_id → "claude"|"gemini"  (ожидаем текст/голос с уточнением сути)
_awaiting_thesis: dict[int, str] = {}

# user_id → {idea_id, lens_thesis}  (черновик линзы — ждёт подтверждения)
_pending_lens: dict[int, dict] = {}


# ─── DB ───────────────────────────────────────────────────────────────────────

def _get_db():
    conn = libsql.connect(
        str(_DB_DIR / "cb_bot_replica.db"),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


# ─── /ideas ───────────────────────────────────────────────────────────────────

@router.message(Command("ideas"))
async def cmd_ideas(message: Message):
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, title, thesis, status FROM cb_ideas "
        "WHERE status IN ('new', 'shown') ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer(
            "Нет новых идей.\n\n"
            "Запусти анализатор:\n<code>python 02-analyzer/analyzer.py</code>"
        )
        return

    buttons = []
    lines = ["<b>Идеи для контента:</b>\n"]
    for i, row in enumerate(rows, 1):
        idea_id, title, thesis, status = row[0], row[1], row[2], row[3]
        short = thesis[:100] + "..." if len(thesis) > 100 else thesis
        tag = " ↩️" if status == "shown" else ""
        lines.append(f"<b>{i}. {title}{tag}</b>\n{short}\n")
        buttons.append([InlineKeyboardButton(text=f"{i}. {title[:38]}{tag}", callback_data=f"idea:{idea_id}")])

    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ─── Смысл screen ─────────────────────────────────────────────────────────────

def _ctx_thesis(user_id: int, idea_id: int, original: str) -> str:
    ctx = _idea_context.get(user_id, {})
    if ctx.get("idea_id") == idea_id:
        return ctx.get("lens_thesis") or ctx.get("refined_thesis") or original
    return original


def _ctx_via_lens(user_id: int, idea_id: int) -> bool:
    ctx = _idea_context.get(user_id, {})
    return bool(ctx.get("idea_id") == idea_id and ctx.get("lens_thesis"))


def _ctx_approach(user_id: int, idea_id: int) -> str:
    ctx = _idea_context.get(user_id, {})
    if ctx.get("idea_id") == idea_id:
        return ctx.get("selected_approach", "")
    return ""


def _thesis_text(title: str, thesis: str, approach: str, via_lens: bool = False) -> str:
    label = "Суть (через линзу)" if via_lens else "Суть (что хочу донести)"
    text = f"<b>{title}</b>\n\n<b>{label}:</b>\n{thesis}"
    if approach:
        text += f"\n\n<b>Угол подачи:</b>\n<i>{approach}</i>"
    return text


def _thesis_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📓 Уточнить суть", callback_data=f"refine_thesis:{idea_id}"),
         InlineKeyboardButton(text="🤖 Уточнить суть", callback_data=f"refine_thesis_agy:{idea_id}")],
        [InlineKeyboardButton(text="🔭 Через линзу", callback_data=f"apply_lens:{idea_id}"),
         InlineKeyboardButton(text="🤖 Через линзу", callback_data=f"apply_lens_agy:{idea_id}")],
        [InlineKeyboardButton(text="🎯 Варианты (Claude)", callback_data=f"gen_approaches:{idea_id}"),
         InlineKeyboardButton(text="🤖 Варианты (Gemini)", callback_data=f"gen_approaches_agy:{idea_id}")],
        [InlineKeyboardButton(text="📊 Стратегия (Claude)", callback_data=f"check_strategy:{idea_id}"),
         InlineKeyboardButton(text="🤖 Стратегия (Gemini)", callback_data=f"check_strategy_agy:{idea_id}")],
        [InlineKeyboardButton(text="📱 Telegram", callback_data=f"platform:telegram:{idea_id}"),
         InlineKeyboardButton(text="📹 Reels", callback_data=f"platform:reels:{idea_id}"),
         InlineKeyboardButton(text="🎬 YouTube", callback_data=f"platform:youtube:{idea_id}")],
        [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"dismiss:{idea_id}"),
         InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_ideas")],
    ])


@router.callback_query(lambda c: c.data and c.data.startswith("idea:"))
async def cb_select_idea(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[1])
    conn = _get_db()
    row = conn.execute("SELECT id, title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    if not row:
        await callback.answer("Идея не найдена.")
        conn.close()
        return
    title, thesis = row[1], row[2]
    conn.execute("UPDATE cb_ideas SET status = 'shown' WHERE id = ? AND status = 'new'", (idea_id,))
    conn.commit()
    conn.sync()
    conn.close()

    user_id = callback.from_user.id
    if _idea_context.get(user_id, {}).get("idea_id") != idea_id:
        _idea_context[user_id] = {"idea_id": idea_id, "refined_thesis": "", "selected_approach": "", "approaches": [], "lens_thesis": ""}

    current_thesis = _ctx_thesis(user_id, idea_id, thesis)
    approach = _ctx_approach(user_id, idea_id)
    via_lens = _ctx_via_lens(user_id, idea_id)

    await callback.message.edit_text(
        _thesis_text(title, current_thesis, approach, via_lens=via_lens),
        reply_markup=_thesis_keyboard(idea_id),
    )
    await callback.answer()


# ─── Уточнить суть ────────────────────────────────────────────────────────────

_THESIS_REFINE_SYSTEM = """Ты помогаешь автору чётко сформулировать суть идеи для контента.

Задача: возьми сырые мысли и сформулируй тезис — что именно автор хочет донести читателю/зрителю.

Правила:
- 2-3 предложения максимум
- Только идеи из текста, не добавляй своё
- Конкретно и ёмко, без воды
- Сохрани интонацию и слова автора

Выведи только готовый тезис без вводных фраз."""


def _refine_thesis_sync(raw: str) -> str:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _THESIS_REFINE_SYSTEM, "--model", CLAUDE_MODEL],
        input=raw, capture_output=True, text=True, env=env, timeout=60, cwd=str(_DB_DIR),
    )
    return result.stdout.strip()


def _refine_thesis_agy_sync(raw: str) -> str:
    from handlers.post_writer import _call_agy_sync
    text, _ = _call_agy_sync(f"{_THESIS_REFINE_SYSTEM}\n\n---\n\n{raw}")
    return text


@router.callback_query(lambda c: c.data and c.data.startswith("refine_thesis:") and not c.data.startswith("refine_thesis_agy:"))
async def cb_refine_thesis(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    ctx = _idea_context.setdefault(user_id, {"idea_id": idea_id, "refined_thesis": "", "selected_approach": "", "approaches": [], "lens_thesis": ""})
    ctx["idea_id"] = idea_id
    _awaiting_thesis[user_id] = "claude"
    await callback.answer()
    await callback.message.answer("📓 Напиши или надиктуй свои мысли — Claude сформулирует суть:")


@router.callback_query(lambda c: c.data and c.data.startswith("refine_thesis_agy:"))
async def cb_refine_thesis_agy(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    ctx = _idea_context.setdefault(user_id, {"idea_id": idea_id, "refined_thesis": "", "selected_approach": "", "approaches": [], "lens_thesis": ""})
    ctx["idea_id"] = idea_id
    _awaiting_thesis[user_id] = "gemini"
    await callback.answer()
    await callback.message.answer("🤖 Напиши или надиктуй свои мысли — Gemini сформулирует суть:")


async def process_thesis_input(message: Message, user_id: int, text: str):
    """Вызывается из post_writer.py при получении текста/голоса."""
    engine = _awaiting_thesis.pop(user_id, None)
    ctx = _idea_context.get(user_id, {})
    idea_id = ctx.get("idea_id")
    if not idea_id:
        return

    thesis = text
    loop = asyncio.get_event_loop()
    if engine == "claude":
        await message.answer("📓 Формулирую суть через Claude...")
        thesis = await loop.run_in_executor(None, _refine_thesis_sync, text) or text
    elif engine == "gemini":
        await message.answer("🤖 Формулирую суть через Gemini...")
        thesis = await loop.run_in_executor(None, _refine_thesis_agy_sync, text) or text

    ctx["refined_thesis"] = thesis
    ctx["lens_thesis"] = ""
    conn = _get_db()
    row = conn.execute("SELECT title FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    title = row[0] if row else "Идея"
    approach = ctx.get("selected_approach", "")

    await message.answer(
        _thesis_text(title, thesis, approach),
        reply_markup=_thesis_keyboard(idea_id),
        parse_mode="HTML",
    )


# ─── Через линзу ──────────────────────────────────────────────────────────────

_LENS_SYSTEM_TEMPLATE = """Ты помогаешь автору пересмотреть суть идеи через его личную линзу восприятия.

ЛИНЗА АВТОРА:
{lens}

Два шага:
1. Вычлени из тезиса чистую суть — что автор хочет сказать, без добавлений.
2. Проверь: есть ли в этой сути скрытая последовательность или выбор ("сначала А, потом Б" / "либо А, либо Б"). Если да и А с Б по сути одно — переформулируй так, чтобы это было видно через саму структуру мысли.

Правила:
- Не добавляй фразы вида "люди думают", "мир считает" — если этого нет в исходном тезисе.
- Если тезис не предполагает ложного выбора — верни его почти без изменений.
- Сохраняй слова и интонацию автора, не добавляй свои идеи.

Выведи только готовый тезис, без вводных фраз и объяснений."""


def _apply_lens_sync(thesis: str) -> str:
    from handlers.post_writer import CLAUDE_MODEL
    from prompts import load_lens
    system = _LENS_SYSTEM_TEMPLATE.format(lens=load_lens())
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", system, "--model", CLAUDE_MODEL],
        input=thesis, capture_output=True, text=True, env=env, timeout=60, cwd=str(_DB_DIR),
    )
    return result.stdout.strip()


def _apply_lens_agy_sync(thesis: str) -> str:
    from handlers.post_writer import _call_agy_sync
    from prompts import load_lens
    system = _LENS_SYSTEM_TEMPLATE.format(lens=load_lens())
    text, _ = _call_agy_sync(f"{system}\n\n---\n\n{thesis}")
    return text


def _lens_preview_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"confirm_lens:{idea_id}")],
        [InlineKeyboardButton(text="🔄 Ещё (Claude)", callback_data=f"apply_lens:{idea_id}"),
         InlineKeyboardButton(text="🤖 Ещё (Gemini)", callback_data=f"apply_lens_agy:{idea_id}")],
        [InlineKeyboardButton(text="↩️ Отклонить, вернуть прежнюю суть", callback_data=f"idea:{idea_id}")],
    ])


async def _show_lens(callback: CallbackQuery, idea_id: int, engine: str):
    """Общая логика: прогнать текущую суть через линзу — показать предпросмотр без сохранения."""
    user_id = callback.from_user.id
    conn = _get_db()
    row = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    title, original_thesis = row[0], row[1]

    thesis = _ctx_thesis(user_id, idea_id, original_thesis)

    from prompts import load_lens
    if not load_lens().strip():
        await callback.answer()
        await callback.message.answer(
            "⚠️ Файл <code>lens.md</code> не заполнен.\n\nЗаполни его, чтобы смотреть идеи через линзу.",
            parse_mode="HTML",
        )
        return

    await callback.answer()
    tag = "🤖 [Gemini]" if engine == "gemini" else "🔭 [Claude]"
    msg = await callback.message.answer(f"{tag} Смотрю через линзу...")

    loop = asyncio.get_event_loop()
    if engine == "gemini":
        lens_thesis = await loop.run_in_executor(None, _apply_lens_agy_sync, thesis)
    else:
        lens_thesis = await loop.run_in_executor(None, _apply_lens_sync, thesis)

    if not lens_thesis:
        await msg.edit_text("Не удалось применить линзу — попробуй снова.")
        return

    _pending_lens[user_id] = {"idea_id": idea_id, "lens_thesis": lens_thesis}

    ctx = _idea_context.get(user_id, {})
    approach = ctx.get("selected_approach", "") if ctx.get("idea_id") == idea_id else ""
    await msg.edit_text(
        _thesis_text(title, lens_thesis, approach, via_lens=True),
        reply_markup=_lens_preview_keyboard(idea_id),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("apply_lens:") and not c.data.startswith("apply_lens_agy:"))
async def cb_apply_lens(callback: CallbackQuery):
    await _show_lens(callback, int(callback.data.split(":")[1]), "claude")


@router.callback_query(lambda c: c.data and c.data.startswith("apply_lens_agy:"))
async def cb_apply_lens_agy(callback: CallbackQuery):
    await _show_lens(callback, int(callback.data.split(":")[1]), "gemini")


@router.callback_query(lambda c: c.data and c.data.startswith("confirm_lens:"))
async def cb_confirm_lens(callback: CallbackQuery):
    """Подтвердить линзу — теперь она подхватывается везде ниже по флоу."""
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])

    pending = _pending_lens.get(user_id)
    if not pending or pending.get("idea_id") != idea_id:
        await callback.answer("Черновик линзы не найден — прогони снова.")
        return
    lens_thesis = pending["lens_thesis"]
    _pending_lens.pop(user_id, None)

    ctx = _idea_context.setdefault(user_id, {"idea_id": idea_id, "refined_thesis": "", "selected_approach": "", "approaches": [], "lens_thesis": ""})
    ctx["idea_id"] = idea_id
    ctx["lens_thesis"] = lens_thesis

    conn = _get_db()
    row = conn.execute("SELECT title FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    title = row[0] if row else "Идея"
    approach = ctx.get("selected_approach", "")

    await callback.message.edit_text(
        _thesis_text(title, lens_thesis, approach, via_lens=True),
        reply_markup=_thesis_keyboard(idea_id),
    )
    await callback.answer("Линза применена ✓")


# ─── Варианты подачи ──────────────────────────────────────────────────────────

_APPROACH_SYSTEM = """Предложи 2-3 разных способа раскрыть одну идею.

Каждый способ = другой угол подачи, другая точка входа для читателя/зрителя.
Не пересказ — именно другой способ рассказать о том же.

Примеры углов: "через личный перелом", "через ошибочное убеждение", "через конкретный момент",
"через сравнение тогда/сейчас", "через практический инструмент", "через контрпример".

Ответ строго JSON без markdown:
[{"name": "Название угла (3-5 слов)", "angle": "Как именно раскрыть — 1-2 предложения конкретно"}]"""


def _gen_approaches_sync(thesis: str) -> list[dict]:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _APPROACH_SYSTEM, "--model", CLAUDE_MODEL],
        input=f"Идея: {thesis}",
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(_DB_DIR),
    )
    text = result.stdout.strip()
    try:
        if "```" in text:
            text = text[text.index("["):text.rindex("]") + 1]
        return json.loads(text)
    except Exception as e:
        print(f"[approaches] parse error: {e}, raw: {text[:200]}")
        return []


def _gen_approaches_agy_sync(thesis: str) -> list[dict]:
    from handlers.post_writer import _call_agy_sync
    full_prompt = f"{_APPROACH_SYSTEM}\n\n---\n\nИдея: {thesis}"
    text, _ = _call_agy_sync(full_prompt)
    try:
        if "```" in text:
            text = text[text.index("["):text.rindex("]") + 1]
        return json.loads(text)
    except Exception as e:
        print(f"[approaches-agy] parse error: {e}, raw: {text[:200]}")
        return []


def _approaches_keyboard(approaches: list[dict], idea_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Общая логика показа списка вариантов подачи."""
    lines = ["<b>Как раскрыть эту идею?</b>\n"]
    buttons = []
    for i, a in enumerate(approaches):
        lines.append(f"<b>{i + 1}. {a['name']}</b>\n<i>{a['angle']}</i>\n")
        buttons.append([InlineKeyboardButton(text=f"{i + 1}. {a['name']}", callback_data=f"select_approach:{idea_id}:{i}")])
    buttons.append([
        InlineKeyboardButton(text="🔄 Ещё (Claude)", callback_data=f"gen_approaches:{idea_id}"),
        InlineKeyboardButton(text="🤖 Ещё (Gemini)", callback_data=f"gen_approaches_agy:{idea_id}"),
    ])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"idea:{idea_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_approaches(callback: CallbackQuery, idea_id: int, engine: str):
    """Общая логика генерации и показа вариантов подачи."""
    user_id = callback.from_user.id
    conn = _get_db()
    row = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    title, original_thesis = row[0], row[1]

    thesis = _ctx_thesis(user_id, idea_id, original_thesis)

    await callback.answer()
    tag = "🤖 [Gemini]" if engine == "gemini" else "📓 [Claude]"
    msg = await callback.message.answer(f"{tag} Генерирую варианты подачи...")

    loop = asyncio.get_event_loop()
    if engine == "gemini":
        approaches = await loop.run_in_executor(None, _gen_approaches_agy_sync, thesis)
    else:
        approaches = await loop.run_in_executor(None, _gen_approaches_sync, thesis)

    if not approaches:
        await msg.edit_text("Не удалось сгенерировать варианты — попробуй снова.")
        return

    ctx = _idea_context.setdefault(user_id, {"idea_id": idea_id, "refined_thesis": "", "selected_approach": "", "approaches": [], "lens_thesis": ""})
    ctx["approaches"] = approaches
    ctx["idea_id"] = idea_id

    text, keyboard = _approaches_keyboard(approaches, idea_id)
    await msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("gen_approaches:") and not c.data.startswith("gen_approaches_agy:"))
async def cb_gen_approaches(callback: CallbackQuery):
    await _show_approaches(callback, int(callback.data.split(":")[1]), "claude")


@router.callback_query(lambda c: c.data and c.data.startswith("gen_approaches_agy:"))
async def cb_gen_approaches_agy(callback: CallbackQuery):
    await _show_approaches(callback, int(callback.data.split(":")[1]), "gemini")


@router.callback_query(lambda c: c.data and c.data.startswith("select_approach:"))
async def cb_select_approach(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    idea_id, idx = int(parts[1]), int(parts[2])

    ctx = _idea_context.get(user_id, {})
    approaches = ctx.get("approaches", []) if ctx.get("idea_id") == idea_id else []

    if not approaches or idx >= len(approaches):
        await callback.answer("Вариант не найден — перегенерируй.")
        return

    selected = approaches[idx]
    approach_text = f"{selected['name']} — {selected['angle']}"
    ctx["selected_approach"] = approach_text

    conn = _get_db()
    row = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    title, original_thesis = row[0], row[1]
    thesis = _ctx_thesis(user_id, idea_id, original_thesis)

    await callback.message.edit_text(
        _thesis_text(title, thesis, approach_text, via_lens=_ctx_via_lens(user_id, idea_id)),
        reply_markup=_thesis_keyboard(idea_id),
    )
    await callback.answer("Угол подачи выбран ✓")


# ─── Стратегическая проверка ─────────────────────────────────────────────────

_STRATEGY_CHECK_SYSTEM = """Ты — стратег личного бренда. Оцени насколько идея для контента соответствует стратегии автора.

Дай оценку по шкале:
✅ Полностью соответствует — усиливает позиционирование
⚖️ Частично соответствует — работает, но есть нюансы
❌ Противоречит стратегии — риск размытия позиционирования

Формат ответа (строго):
[эмодзи] [суть оценки в одну строку]

Почему: [1-2 предложения конкретно]

Что делать: [если ✅ — что особенно сильно работает; если ⚖️/❌ — как скорректировать подачу чтобы попасть в стратегию]

Отвечай коротко и конкретно. Не пересказывай стратегию обратно."""


def _check_strategy_sync(title: str, thesis: str, strategy: str) -> str:
    from handlers.post_writer import CLAUDE_MODEL
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    inp = f"СТРАТЕГИЯ АВТОРА:\n{strategy}\n\n---\n\nИДЕЯ:\nНазвание: {title}\nСуть: {thesis}"
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", _STRATEGY_CHECK_SYSTEM, "--model", CLAUDE_MODEL],
        input=inp, capture_output=True, text=True, env=env, timeout=60,
        cwd=str(_DB_DIR),
    )
    return result.stdout.strip()


def _check_strategy_agy_sync(title: str, thesis: str, strategy: str) -> str:
    from handlers.post_writer import _call_agy_sync
    full_prompt = (
        f"{_STRATEGY_CHECK_SYSTEM}\n\n---\n\n"
        f"СТРАТЕГИЯ АВТОРА:\n{strategy}\n\n---\n\n"
        f"ИДЕЯ:\nНазвание: {title}\nСуть: {thesis}"
    )
    text, _ = _call_agy_sync(full_prompt)
    return text


@router.callback_query(lambda c: c.data and c.data.startswith("check_strategy:") and not c.data.startswith("check_strategy_agy:"))
async def cb_check_strategy(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])

    conn = _get_db()
    row = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    title, original_thesis = row[0], row[1]

    thesis = _ctx_thesis(user_id, idea_id, original_thesis)

    from prompts import load_strategy
    strategy = load_strategy()
    if not strategy.strip():
        await callback.answer()
        await callback.message.answer(
            "⚠️ Файл <code>strategy.md</code> не заполнен.\n\n"
            "Заполни его, чтобы получать стратегическую оценку идей.",
            parse_mode="HTML",
        )
        return

    await callback.answer()
    msg = await callback.message.answer("📊 Сверяю с твоей стратегией...")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _check_strategy_sync, title, thesis, strategy)

    if not result:
        await msg.edit_text("Не удалось получить оценку — попробуй снова.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Мнение Gemini", callback_data=f"check_strategy_agy:{idea_id}")],
        [InlineKeyboardButton(text="↩️ К идее", callback_data=f"idea:{idea_id}")],
    ])
    await msg.edit_text(
        f"<b>📊 Стратегическая оценка</b>\n\n{result}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("check_strategy_agy:"))
async def cb_check_strategy_agy(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])

    conn = _get_db()
    row = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    title, original_thesis = row[0], row[1]

    thesis = _ctx_thesis(user_id, idea_id, original_thesis)

    from prompts import load_strategy
    strategy = load_strategy()
    if not strategy.strip():
        await callback.answer()
        await callback.message.answer("⚠️ <code>strategy.md</code> не заполнен.", parse_mode="HTML")
        return

    await callback.answer()
    msg = await callback.message.answer("🤖 Запрашиваю мнение Gemini...")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _check_strategy_agy_sync, title, thesis, strategy)

    if not result:
        await msg.edit_text("Gemini не ответил — попробуй снова.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📓 Мнение Claude", callback_data=f"check_strategy:{idea_id}")],
        [InlineKeyboardButton(text="↩️ К идее", callback_data=f"idea:{idea_id}")],
    ])
    await msg.edit_text(
        f"<b>🤖 Стратегическая оценка (Gemini)</b>\n\n{result}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ─── Platform → engine screens ────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("platform:telegram:"))
async def cb_platform_telegram(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[2])
    conn = _get_db()
    row = conn.execute("SELECT title FROM cb_ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📓 Claude — из дневника", callback_data=f"write_diary:{idea_id}"),
         InlineKeyboardButton(text="🤖 Gemini — из дневника", callback_data=f"write_agy_diary:{idea_id}")],
        [InlineKeyboardButton(text="⚡ Оба сразу — из дневника", callback_data=f"write_both_diary:{idea_id}")],
        [InlineKeyboardButton(text="🔍 Claude — дневник + архив", callback_data=f"write_archive:{idea_id}"),
         InlineKeyboardButton(text="🤖 Gemini — архив", callback_data=f"write_agy_archive:{idea_id}")],
        [InlineKeyboardButton(text="✍️ Claude — полный режим (NLM)", callback_data=f"write:{idea_id}")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"idea:{idea_id}")],
    ])
    await callback.message.edit_text(
        f"<b>{row[0]}</b>\n\n<i>📱 Telegram пост — выбери движок:</i>",
        reply_markup=keyboard,
    )
    await callback.answer()


# ─── dismiss / back ───────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("dismiss:"))
async def cb_dismiss_idea(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[1])
    conn = _get_db()
    conn.execute("UPDATE cb_ideas SET status = 'dismissed' WHERE id = ?", (idea_id,))
    conn.commit()
    conn.sync()
    conn.close()
    await callback.answer("Идея отклонена.")
    await cmd_ideas(callback.message)


@router.callback_query(lambda c: c.data == "back_to_ideas")
async def cb_back_to_ideas(callback: CallbackQuery):
    await callback.answer()
    await cmd_ideas(callback.message)
