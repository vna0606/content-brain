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

import io

import libsql_experimental as libsql
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

CLAUDE_MODEL = "claude-sonnet-4-6"
_DB_DIR = Path(__file__).parent.parent
NLM_BIN = os.path.expanduser("~/.local/bin/nlm")
FRESH_NOTEBOOK_ID = os.environ.get("NLM_FRESH_NOTEBOOK_ID", "6ed2bc91-8fb5-4eca-a881-324f6db90db1")
ARCHIVE_NOTEBOOK_ID = os.environ.get("NLM_NOTEBOOK_ID", "1eb35d64-3e12-4dc9-8043-f65d703d6281")

# Хранилище черновиков и состояний: keyed by user_id
_drafts: dict[int, dict] = {}          # user_id → {idea_id, draft, system, base_prompt}
_awaiting_feedback: dict[int, bool] = {}  # user_id → True если ждём комментарий


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


def _get_archive_context_from_nlm(thesis: str) -> str:
    """Запросить у архивного NLM исторический контекст по теме — предыстория, как думал раньше."""
    query = (
        f"По теме: {thesis[:300]}\n\n"
        f"Найди в архиве: как автор думал об этом раньше? Какие инсайты или осознания были зафиксированы? "
        f"Приводи конкретные цитаты из записей — слова автора, не твои интерпретации. "
        f"Если по теме ничего нет — так и скажи."
    )
    try:
        env = {**os.environ, "HOME": os.path.expanduser("~")}
        result = subprocess.run(
            [NLM_BIN, "notebook", "query", ARCHIVE_NOTEBOOK_ID, query],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if result.returncode != 0:
            return ""
        import json as _json
        try:
            data = _json.loads(result.stdout)
            return data.get("value", {}).get("answer", "")
        except (_json.JSONDecodeError, AttributeError):
            return result.stdout.strip()
    except Exception as e:
        print(f"[nlm-archive] исключение: {e}")
        return ""


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


def _call_claude_sync(system: str, user_prompt: str) -> str:
    """Вызов Claude: system через --system-prompt, user через stdin (не @file).
    @file заставляет Claude 'анализировать' файл и нарратировать — stdin этого не делает."""
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", system, "--model", CLAUDE_MODEL],
        input=user_prompt,
        capture_output=True, text=True, env=env, timeout=180,
    )
    if result.returncode != 0 and result.stderr:
        print(f"[claude] stderr: {result.stderr[:300]}")
    return result.stdout.strip()


@router.callback_query(lambda c: c.data and c.data.startswith("write:"))
async def cb_write_post(callback: CallbackQuery):
    """Сгенерировать пост: текущий режим (NLM fresh приоритет + diary если NLM пустой)."""
    idea_id = int(callback.data.split(":")[1])
    await _generate_and_send(callback, idea_id, mode="current")


@router.callback_query(lambda c: c.data and c.data.startswith("write_diary:"))
async def cb_write_diary(callback: CallbackQuery):
    """Сгенерировать пост только из записей дневника — без NLM и Gemini."""
    idea_id = int(callback.data.split(":")[1])
    await _generate_and_send(callback, idea_id, mode="diary")


@router.callback_query(lambda c: c.data and c.data.startswith("write_archive:"))
async def cb_write_archive(callback: CallbackQuery):
    """Сгенерировать пост из дневника + исторический контекст из архивного NLM."""
    idea_id = int(callback.data.split(":")[1])
    await _generate_and_send(callback, idea_id, mode="archive")


async def _generate_and_send(callback: CallbackQuery, idea_id: int, mode: str):
    """Общая логика генерации поста для всех трёх режимов.

    mode:
      'current'  — NLM fresh (приоритет) + diary только если NLM пустой
      'diary'    — только сырые записи дневника, без NLM и Gemini
      'archive'  — записи дневника + исторический контекст из архивного NLM
    """
    conn = _get_cb_db()
    cur = conn.execute(
        "SELECT id, title, thesis, relevant_social, source_entries, created_at FROM cb_ideas WHERE id = ?",
        (idea_id,),
    )
    row = cur.fetchone()
    if not row:
        await callback.answer("Идея не найдена.")
        conn.close()
        return

    title, thesis = row[1], row[2]
    relevant_social = json.loads(row[3] or "[]")
    source_entries = json.loads(row[4] or "[]")
    idea_created_at = row[5] or ""

    await callback.answer("Генерирую пост...")

    mode_labels = {
        "current": "Пишу пост (текущий режим)...",
        "diary":   "Пишу пост только из записей дневника...",
        "archive": "Пишу пост из дневника + ищу исторический контекст...",
    }
    await callback.message.answer(mode_labels.get(mode, "Пишу пост..."))

    # Загружаем дневниковые записи всегда
    raw_messages = _get_raw_diary_messages(source_entries)
    if not raw_messages and idea_created_at:
        try:
            from datetime import datetime, timedelta
            idea_date = datetime.fromisoformat(idea_created_at[:10])
            fallback_dates = [(idea_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(8)]
            raw_messages = _get_raw_diary_messages(fallback_dates)
        except Exception:
            pass

    # Источники зависят от режима
    social_chunks = []
    gemini_messages = []
    nlm_quotes = ""

    if mode == "current":
        social_chunks = _get_social_chunks(conn, relevant_social)
        gemini_messages = _get_all_gemini_messages(conn)
        conn.close()
        await callback.message.answer("🔍 Ищу твои слова в переписках с Gemini...")
        loop = asyncio.get_event_loop()
        nlm_quotes = await loop.run_in_executor(None, _get_raw_quotes_from_nlm, thesis)

    elif mode == "diary":
        conn.close()
        # Только дневник — ни NLM, ни Gemini

    elif mode == "archive":
        conn.close()
        await callback.message.answer("🔍 Ищу исторический контекст в архиве...")
        loop = asyncio.get_event_loop()
        nlm_quotes = await loop.run_in_executor(None, _get_archive_context_from_nlm, thesis)
    else:
        conn.close()

    print(f"[post_writer][{mode}] diary={len(raw_messages)}, gemini={len(gemini_messages)}, nlm={len(nlm_quotes)}")

    from prompts import get_post_writer_system, build_post_prompt
    system = get_post_writer_system()
    user_prompt = build_post_prompt(title, thesis, raw_messages, social_chunks, gemini_messages, nlm_quotes)

    loop = asyncio.get_event_loop()
    post_text = await loop.run_in_executor(None, _call_claude_sync, system, user_prompt)

    if not post_text:
        await callback.message.answer("Не удалось сгенерировать пост. Попробуй снова.")
        return

    conn2 = _get_cb_db()
    conn2.execute("UPDATE cb_ideas SET status = 'shown' WHERE id = ?", (idea_id,))
    conn2.commit()
    conn2.sync()
    conn2.close()

    user_id = callback.from_user.id
    _drafts[user_id] = {"idea_id": idea_id, "draft": post_text, "system": system}
    _awaiting_feedback.pop(user_id, None)

    await _send_draft(callback.message, post_text, idea_id)


async def _send_draft(message, post_text: str, idea_id: int):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Улучшить (дать комментарий)", callback_data=f"improve:{idea_id}")],
        [InlineKeyboardButton(text="🔄 Перегенерировать с нуля", callback_data=f"write:{idea_id}")],
        [InlineKeyboardButton(text="❌ Отклонить идею", callback_data=f"dismiss:{idea_id}")],
    ])

    preview = f"<b>Черновик поста:</b>\n\n{post_text}\n\n<i>({len(post_text)} символов)</i>"

    if len(preview) > 4096:
        await message.answer(post_text[:4000] + "...")
        await message.answer(f"<i>{len(post_text)} символов</i>", reply_markup=keyboard)
    else:
        await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")


def _extract_draft_from_message(text: str) -> str:
    """Извлечь текст черновика из сообщения бота.
    Формат: 'Черновик поста:\\n\\n{draft}\\n\\n(N символов)'"""
    import re
    prefix = "Черновик поста:\n\n"
    if prefix not in text:
        return ""
    draft = text[text.index(prefix) + len(prefix):]
    draft = re.sub(r"\n\n\(\d+ символов\)\s*$", "", draft).strip()
    return draft


@router.callback_query(lambda c: c.data and c.data.startswith("improve:"))
async def cb_improve_post(callback: CallbackQuery):
    """Запросить комментарий для улучшения черновика."""
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])

    stored = _drafts.get(user_id)

    # Если память пустая (рестарт бота) — восстанавливаем черновик из текста сообщения
    if not stored or stored.get("idea_id") != idea_id:
        msg_text = callback.message.text or ""
        draft = _extract_draft_from_message(msg_text)
        if not draft:
            await callback.answer("Черновик не найден — перегенерируй пост.")
            return
        from prompts import get_post_writer_system
        stored = {"idea_id": idea_id, "draft": draft, "system": get_post_writer_system()}
        _drafts[user_id] = stored

    _awaiting_feedback[user_id] = True
    await callback.answer()
    await callback.message.answer(
        "Напиши или надиктуй что нужно изменить — я передам это Claude вместе с черновиком.\n\n"
        "Например: «убери метафору про гендиректора», «разверни мысль подробнее», «финал слабый»"
    )


async def _process_feedback(message: Message, user_id: int, feedback: str):
    """Общая логика улучшения черновика по фидбэку (текст или голос)."""
    stored = _drafts.get(user_id)
    if not stored:
        _awaiting_feedback.pop(user_id, None)
        return

    _awaiting_feedback.pop(user_id)
    idea_id = stored["idea_id"]
    draft = stored["draft"]
    system = stored["system"]

    await message.answer("✏️ Улучшаю пост с учётом твоего комментария...")

    from prompts import build_improve_prompt
    improve_prompt = build_improve_prompt(draft, feedback)

    loop = asyncio.get_event_loop()
    improved = await loop.run_in_executor(None, _call_claude_sync, system, improve_prompt)

    if not improved:
        await message.answer("Не удалось улучшить пост. Попробуй ещё раз.")
        _awaiting_feedback[user_id] = True
        return

    _drafts[user_id] = {**stored, "draft": improved}
    await _send_draft(message, improved, idea_id)


@router.message(F.voice)
async def on_feedback_voice(message: Message, bot: Bot):
    """Принять голосовой комментарий, транскрибировать и улучшить черновик."""
    user_id = message.from_user.id

    if not _awaiting_feedback.get(user_id):
        return

    await message.answer("🎤 Транскрибирую голосовое...")

    buf = io.BytesIO()
    await bot.download(message.voice, destination=buf)
    audio_bytes = buf.getvalue()

    from transcribe import transcribe_sync
    loop = asyncio.get_event_loop()
    feedback = await loop.run_in_executor(None, transcribe_sync, audio_bytes, ".ogg")

    if not feedback:
        await message.answer("Не удалось распознать голосовое — попробуй написать текстом.")
        return

    await message.answer(f"📝 Распознал: {feedback}")

    # Дальше — та же логика что и текстовый фидбэк
    await _process_feedback(message, user_id, feedback)


@router.message(F.text)
async def on_feedback_text(message: Message):
    """Принять текстовый комментарий и улучшить черновик."""
    user_id = message.from_user.id

    if not _awaiting_feedback.get(user_id):
        return  # не наш — пропускаем

    await _process_feedback(message, user_id, message.text.strip())


