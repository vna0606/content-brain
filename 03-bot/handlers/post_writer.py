"""
handlers/post_writer.py — генерация полного поста по выбранной идее.

Источник контента: реальные сырые записи из mood_diary за даты идеи.
Claude только переформатирует — не придумывает.
"""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

CLAUDE_MODEL = "claude-sonnet-4-6"
_DB_DIR = Path(__file__).parent.parent
NLM_BIN = os.path.expanduser("~/.local/bin/nlm")
FRESH_NOTEBOOK_ID = os.environ.get("NLM_FRESH_NOTEBOOK_ID", "6ed2bc91-8fb5-4eca-a881-324f6db90db1")


def _get_cb_db():
    conn = libsql.connect(
        str(_DB_DIR / "cb_bot_replica.db"),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


def _get_mood_db():
    replica = str(_DB_DIR / "mood_replica_bot.db")
    conn = libsql.connect(
        replica,
        sync_url=os.environ["TURSO_MOOD_URL"],
        auth_token=os.environ["TURSO_MOOD_TOKEN"],
    )
    conn.sync()
    return conn


def _get_raw_quotes_from_nlm(thesis: str) -> str:
    """Запросить у NLM дословные цитаты автора по теме идеи.
    Используем fresh-ноутбук — там актуальные переписки с Gemini."""
    query = (
        f"Найди и процитируй дословно что АВТОР (человек, не ты) говорил "
        f"по этой теме: {thesis[:300]}\n\n"
        f"Правила: только его слова, без твоих интерпретаций и пересказа. "
        f"Если есть несколько высказываний — приведи каждое отдельно. "
        f"Если ничего конкретного нет — так и напиши."
    )
    try:
        env = {**os.environ, "HOME": os.path.expanduser("~")}
        result = subprocess.run(
            [NLM_BIN, "notebook", "query", FRESH_NOTEBOOK_ID, query],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if result.returncode != 0:
            print(f"[nlm] ошибка: {result.stderr[:100]}")
            return ""
        import json as _json
        try:
            data = _json.loads(result.stdout)
            return data.get("value", {}).get("answer", "")
        except (_json.JSONDecodeError, AttributeError):
            return result.stdout.strip()
    except Exception as e:
        print(f"[nlm] исключение: {e}")
        return ""


def _get_raw_diary_messages(source_entries: list[str]) -> list[str]:
    """Загрузить реальные сырые сообщения автора из mood_diary за даты идеи."""
    if not source_entries:
        return []
    try:
        mood = _get_mood_db()
        placeholders = ",".join("?" * len(source_entries))
        # Берём user-сообщения за эти даты + день до (анализатор мог сдвинуть дату)
        from datetime import datetime, timedelta
        extended_dates = set(source_entries)
        for d in source_entries:
            try:
                prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                extended_dates.add(prev)
            except ValueError:
                pass
        all_dates = list(extended_dates)
        ph2 = ",".join("?" * len(all_dates))
        cur = mood.execute(
            f"SELECT content FROM messages WHERE role='user' AND entry_date IN ({ph2}) "
            f"ORDER BY created_at LIMIT 10",
            all_dates,
        )
        rows = cur.fetchall()
        mood.close()
        return [r[0] for r in rows if r[0] and r[0].strip() and len(r[0].strip()) > 20]
    except Exception as e:
        print(f"[post_writer] Ошибка загрузки дневника: {e}")
        return []


def _get_all_gemini_messages(conn) -> list[tuple[str, str]]:
    """Загрузить все Gemini-сообщения: (conversation_title, content).
    Передаём всё в промпт — Claude сам определит что релевантно.
    Псевдослучайные эмбеддинги не дают семантического поиска, поэтому
    фильтровать по косинусному сходству бессмысленно."""
    try:
        cur = conn.execute(
            "SELECT conversation_title, content FROM cb_gemini_messages ORDER BY conversation_id, message_index"
        )
        return [(r[0], r[1]) for r in cur.fetchall() if r[1]]
    except Exception as e:
        print(f"[post_writer] Ошибка загрузки Gemini-сообщений: {e}")
        return []


def _get_social_chunks(conn, relevant_social: list[str]) -> list[str]:
    chunks = []
    for source_id in relevant_social[:3]:
        cur = conn.execute(
            "SELECT content_chunk FROM cb_social_vectors WHERE source_id = ?",
            (source_id,),
        )
        row = cur.fetchone()
        if row:
            chunks.append(row[0])
    return chunks


def _call_claude_sync(full_prompt: str) -> str:
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(full_prompt)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["claude", "-p", f"@{tmp_path}", "--model", CLAUDE_MODEL],
            capture_output=True, text=True, env=env, timeout=180,
        )
        if result.returncode != 0 and result.stderr:
            print(f"[claude] stderr: {result.stderr[:300]}")
        return result.stdout.strip()
    finally:
        os.unlink(tmp_path)


@router.callback_query(lambda c: c.data and c.data.startswith("write:"))
async def cb_write_post(callback: CallbackQuery):
    """Сгенерировать пост по выбранной идее."""
    idea_id = int(callback.data.split(":")[1])

    conn = _get_cb_db()
    cur = conn.execute(
        "SELECT id, title, thesis, relevant_social, source_entries FROM cb_ideas WHERE id = ?",
        (idea_id,),
    )
    row = cur.fetchone()

    if not row:
        await callback.answer("Идея не найдена.")
        conn.close()
        return

    title = row[1]
    thesis = row[2]
    relevant_social = json.loads(row[3] or "[]")
    source_entries = json.loads(row[4] or "[]")

    await callback.answer("Генерирую пост...")
    await callback.message.answer("Пишу пост, подожди немного...")

    social_chunks = _get_social_chunks(conn, relevant_social)
    gemini_messages = _get_all_gemini_messages(conn)
    conn.close()

    raw_messages = _get_raw_diary_messages(source_entries)

    # NLM: дословные цитаты автора из Gemini-переписок по теме идеи
    await callback.message.answer("🔍 Ищу твои слова в переписках с Gemini...")
    loop = asyncio.get_event_loop()
    nlm_quotes = await loop.run_in_executor(None, _get_raw_quotes_from_nlm, thesis)

    print(f"[post_writer] diary={len(raw_messages)}, gemini_db={len(gemini_messages)}, nlm={len(nlm_quotes)} символов")

    from prompts import get_post_writer_system, build_post_prompt
    system = get_post_writer_system()
    user_prompt = build_post_prompt(title, thesis, raw_messages, social_chunks, gemini_messages, nlm_quotes)
    full_prompt = f"{system}\n\n{user_prompt}"

    loop = asyncio.get_event_loop()
    post_text = await loop.run_in_executor(None, _call_claude_sync, full_prompt)

    if not post_text:
        await callback.message.answer("Не удалось сгенерировать пост. Попробуй снова.")
        return

    conn2 = _get_cb_db()
    conn2.execute("UPDATE cb_ideas SET status = 'shown' WHERE id = ?", (idea_id,))
    conn2.commit()
    conn2.sync()
    conn2.close()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Перегенерировать", callback_data=f"write:{idea_id}")],
        [InlineKeyboardButton(text="Отклонить идею", callback_data=f"dismiss:{idea_id}")],
    ])

    preview = f"<b>Черновик поста:</b>\n\n{post_text}\n\n<i>({len(post_text)} символов)</i>"

    if len(preview) > 4096:
        await callback.message.answer(post_text[:4000] + "...")
        await callback.message.answer(
            f"<i>{len(post_text)} символов</i>",
            reply_markup=keyboard,
        )
    else:
        await callback.message.answer(preview, reply_markup=keyboard, parse_mode="HTML")
