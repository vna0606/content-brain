"""
handlers/ideas.py — показ списка идей и выбор конкретной.

Команды:
  /ideas — показать список новых идей
"""

import json
import os
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

_DB_DIR = Path(__file__).parent.parent


def _get_db():
    replica = str(_DB_DIR / "cb_bot_replica.db")
    conn = libsql.connect(
        replica,
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


@router.message(Command("ideas"))
async def cmd_ideas(message: Message):
    """Показать список новых идей."""
    conn = _get_db()
    cur = conn.execute(
        "SELECT id, title, thesis, status FROM cb_ideas WHERE status IN ('new', 'shown') ORDER BY created_at DESC LIMIT 10"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await message.answer(
            "Нет новых идей.\n\n"
            "Запусти анализатор:\n<code>python 02-analyzer/analyzer.py</code>"
        )
        return

    buttons = []
    text_lines = ["<b>Идеи для постов:</b>\n"]

    for i, row in enumerate(rows, 1):
        idea_id, title, thesis, status = row[0], row[1], row[2], row[3]
        short = thesis[:100] + "..." if len(thesis) > 100 else thesis
        tag = " ↩️" if status == "shown" else ""
        text_lines.append(f"<b>{i}. {title}{tag}</b>\n{short}\n")
        buttons.append([
            InlineKeyboardButton(text=f"{i}. {title[:38]}{tag}", callback_data=f"idea:{idea_id}")
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("\n".join(text_lines), reply_markup=keyboard)


@router.callback_query(lambda c: c.data and c.data.startswith("idea:"))
async def cb_select_idea(callback: CallbackQuery):
    """Показать полную идею с кнопками."""
    idea_id = int(callback.data.split(":")[1])

    conn = _get_db()
    cur = conn.execute(
        "SELECT id, title, thesis FROM cb_ideas WHERE id = ?", (idea_id,)
    )
    row = cur.fetchone()

    if not row:
        await callback.answer("Идея не найдена.")
        conn.close()
        return

    title, thesis = row[1], row[2]

    conn.execute("UPDATE cb_ideas SET status = 'shown' WHERE id = ? AND status = 'new'", (idea_id,))
    conn.commit()
    conn.sync()
    conn.close()

    keyboard = _platform_keyboard(idea_id)
    await callback.message.edit_text(
        f"<b>{title}</b>\n\n{thesis}\n\n<i>Выбери формат:</i>",
        reply_markup=keyboard,
    )
    await callback.answer()


def _platform_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Telegram пост", callback_data=f"platform:telegram:{idea_id}")],
        [InlineKeyboardButton(text="📹 Reels / Shorts", callback_data=f"platform:reels:{idea_id}")],
        [InlineKeyboardButton(text="🎬 YouTube ролик", callback_data=f"platform:youtube:{idea_id}")],
        [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"dismiss:{idea_id}"),
         InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_ideas")],
    ])


@router.callback_query(lambda c: c.data and c.data.startswith("platform:telegram:"))
async def cb_platform_telegram(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[2])
    conn = _get_db()
    cur = conn.execute("SELECT title, thesis FROM cb_ideas WHERE id = ?", (idea_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        await callback.answer("Идея не найдена.")
        return
    title, thesis = row[0], row[1]
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
        f"<b>{title}</b>\n\n<i>📱 Telegram пост — выбери движок:</i>",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("dismiss:"))
async def cb_dismiss_idea(callback: CallbackQuery):
    """Пометить идею как отклонённую."""
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
