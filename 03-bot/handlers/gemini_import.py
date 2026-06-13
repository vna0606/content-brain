"""
handlers/gemini_import.py — импорт переписок с Gemini в cb_gemini_messages.

Flow:
  1. Пользователь кидает .md или .txt файл в бот
  2. Бот парсит формат Gemini Export, показывает превью
  3. Пользователь подтверждает → каждое сообщение → строка в cb_gemini_messages
  4. При генерации постов эти сообщения используются как сырые мысли автора
"""

import hashlib
import io
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

_DB_DIR = Path(__file__).parent.parent
_KB_PATH = str(Path(__file__).parent.parent.parent / "01-knowledge-base")
if _KB_PATH not in sys.path:
    sys.path.insert(0, _KB_PATH)

# Временное хранилище: user_id → parsed данные (до подтверждения)
_pending: dict[int, dict] = {}


# ─── БД ───────────────────────────────────────────────────────────────────────

def _get_db():
    conn = libsql.connect(
        str(_DB_DIR / "cb_bot_replica.db"),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


# ─── Парсинг .md формата Gemini Export ────────────────────────────────────────

def _parse_gemini_md(text: str) -> dict | None:
    """
    Парсит файл в формате Gemini Export (.md).
    Структура:
      # Заголовок переписки
      **Exported:** дата
      ## Prompt:
      текст сообщения
      ## Prompt:
      текст сообщения
    """
    title = ""
    export_date = ""

    for line in text.split('\n')[:15]:
        if line.startswith('# ') and not title:
            title = line[2:].strip()
        if '**Exported:**' in line:
            m = re.search(r'\*\*Exported:\*\*\s*(.+)', line)
            if m:
                export_date = m.group(1).strip()

    if not title:
        return None

    # Режем по разделителям ## Prompt:
    parts = re.split(r'\n##\s*Prompt:\s*\n', text)
    if len(parts) < 2:
        return None

    messages = []
    for part in parts[1:]:
        # Убираем хвост (Powered by / ---)
        msg = re.sub(r'\n---\n.*$', '', part, flags=re.DOTALL).strip()
        if msg and len(msg) > 10:
            messages.append(msg)

    if not messages:
        return None

    # Парсим дату
    conv_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if export_date:
        for fmt in ('%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(export_date[:10], fmt)
                conv_date = dt.strftime('%Y-%m-%d')
                break
            except ValueError:
                pass

    return {'title': title, 'conv_date': conv_date, 'messages': messages}


def _conversation_id(title: str, conv_date: str) -> str:
    return hashlib.md5(f"{conv_date}_{title}".encode()).hexdigest()[:12]


def _already_imported(conn, conv_id: str) -> bool:
    cur = conn.execute(
        "SELECT id FROM cb_gemini_messages WHERE conversation_id = ? LIMIT 1",
        (conv_id,)
    )
    return cur.fetchone() is not None


def _store_messages(conn, parsed: dict) -> int:
    from embedder import embed_text, vec_to_blob

    conv_id = _conversation_id(parsed['title'], parsed['conv_date'])
    now = datetime.now(timezone.utc).isoformat()

    for i, content in enumerate(parsed['messages']):
        vec = embed_text(content)
        conn.execute(
            "INSERT INTO cb_gemini_messages "
            "(conversation_id, conversation_title, conv_date, message_index, content, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conv_id, parsed['title'], parsed['conv_date'],
             i, content, vec_to_blob(vec), now),
        )

    conn.commit()
    conn.sync()
    return len(parsed['messages'])


# ─── Handlers ─────────────────────────────────────────────────────────────────

@router.message(F.document)
async def on_document(message: Message, bot: Bot):
    """Получить .md/.txt файл → проверить формат → показать превью."""
    doc = message.document
    filename = doc.file_name or ""

    if not any(filename.endswith(ext) for ext in ('.md', '.txt')):
        return

    buf = io.BytesIO()
    await bot.download(doc, destination=buf)

    try:
        text = buf.getvalue().decode('utf-8')
    except UnicodeDecodeError:
        try:
            text = buf.getvalue().decode('cp1251')
        except Exception:
            await message.answer("Не удалось прочитать файл — проблема с кодировкой.")
            return

    parsed = _parse_gemini_md(text)
    if not parsed:
        # Не похоже на Gemini export — молча игнорируем
        return

    _pending[message.from_user.id] = parsed

    preview = "\n\n".join(
        f"<i>{m[:150]}{'...' if len(m) > 150 else ''}</i>"
        for m in parsed['messages'][:2]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, добавить в базу", callback_data="gemini_import:yes")],
        [InlineKeyboardButton(text="Нет", callback_data="gemini_import:no")],
    ])

    await message.answer(
        f"<b>Переписка с Gemini:</b>\n"
        f"📌 {parsed['title']}\n"
        f"📅 {parsed['conv_date']}\n"
        f"💬 {len(parsed['messages'])} сообщений\n\n"
        f"Первые сообщения:\n{preview}\n\n"
        f"Добавить как сырые мысли в базу знаний?",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("gemini_import:"))
async def cb_confirm_import(callback: CallbackQuery):
    action = callback.data.split(":")[1]

    if action == "no":
        _pending.pop(callback.from_user.id, None)
        await callback.answer("Отменено.")
        await callback.message.edit_text("Отменено.")
        return

    parsed = _pending.pop(callback.from_user.id, None)
    if not parsed:
        await callback.answer("Данные потерялись — скинь файл ещё раз.")
        await callback.message.edit_text("Данные потерялись — скинь файл ещё раз.")
        return

    await callback.answer("Добавляю...")
    await callback.message.edit_text("⏳ Обрабатываю и сохраняю...")

    conn = _get_db()
    conv_id = _conversation_id(parsed['title'], parsed['conv_date'])

    if _already_imported(conn, conv_id):
        conn.close()
        await callback.message.edit_text(
            f"Эта переписка уже есть в базе:\n<b>{parsed['title']}</b>",
            parse_mode="HTML",
        )
        return

    try:
        count = _store_messages(conn, parsed)
        conn.close()
        await callback.message.edit_text(
            f"✅ Добавлено {count} сообщений\n<b>{parsed['title']}</b>\n{parsed['conv_date']}",
            parse_mode="HTML",
        )
    except Exception as e:
        conn.close()
        await callback.message.edit_text(f"Ошибка: {e}")
