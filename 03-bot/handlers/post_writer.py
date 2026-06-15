"""
handlers/post_writer.py — генерация полного поста по выбранной идее.

Источник контента: реальные сырые записи из mood_diary за даты идеи.
Claude только переформатирует — не придумывает.
"""

import asyncio
import glob
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
_WORK_DIR = str(Path(__file__).parent.parent)  # content-brain/03-bot
NLM_BIN = os.path.expanduser("~/.local/bin/nlm")
FRESH_NOTEBOOK_ID = os.environ.get("NLM_FRESH_NOTEBOOK_ID", "6ed2bc91-8fb5-4eca-a881-324f6db90db1")
ARCHIVE_NOTEBOOK_ID = os.environ.get("NLM_NOTEBOOK_ID", "1eb35d64-3e12-4dc9-8043-f65d703d6281")

# Antigravity
AGY_BIN = "/snap/bin/antigravity-cli"
AGY_SETTINGS = os.path.expanduser("~/snap/antigravity-cli/2/.gemini/antigravity-cli/settings.json")
AGY_CONV_CACHE = os.path.expanduser("~/snap/antigravity-cli/2/.gemini/antigravity-cli/cache/last_conversations.json")
AGY_MODEL = "Gemini 3.5 Flash (Medium)"  # label из settings.json
_agy_settings_lock = __import__("threading").Lock()

# Хранилище черновиков и состояний: keyed by user_id
_drafts: dict[int, dict] = {}          # Claude: {idea_id, draft, system, mode, session_id}
_agy_drafts: dict[int, dict] = {}      # Antigravity: {idea_id, draft, mode, conv_id}
_awaiting_feedback: dict[int, bool] = {}   # user_id → True если ждём комментарий для Claude
_agy_awaiting_feedback: dict[int, bool] = {}  # user_id → True если ждём комментарий для Antigravity


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


def _get_voice_samples(conn, limit: int = 5) -> list[str]:
    """Загрузить примеры старых постов для voice calibration в гуманизаторе."""
    try:
        cur = conn.execute(
            "SELECT content FROM cb_social_posts WHERE source_type = 'telegram' "
            "ORDER BY published_at DESC LIMIT ?", (limit,)
        )
        return [r[0] for r in cur.fetchall() if r[0] and len(r[0]) > 100]
    except Exception as e:
        print(f"[post_writer] ошибка загрузки voice samples: {e}")
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


def _get_latest_session_id() -> str:
    """Найти session_id последней Claude-сессии по work_dir (как в claude-bot)."""
    project_dir = os.path.abspath(_WORK_DIR).replace("/", "-")
    pattern = os.path.expanduser(f"~/.claude/projects/{project_dir}/*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return ""
    latest = max(files, key=os.path.getmtime)
    return os.path.splitext(os.path.basename(latest))[0]


def _agy_get_conv_id() -> str:
    """Читает ID последней беседы Antigravity для нашей рабочей директории."""
    try:
        with open(AGY_CONV_CACHE) as f:
            cache = json.load(f)
        return cache.get(_WORK_DIR, "") or ""
    except Exception:
        return ""


def _agy_set_model():
    """Выставляет нужную модель в settings.json Antigravity перед вызовом."""
    if not os.path.exists(AGY_SETTINGS):
        return
    with _agy_settings_lock:
        try:
            with open(AGY_SETTINGS) as f:
                cfg = json.load(f)
            cfg["model"] = AGY_MODEL
            with open(AGY_SETTINGS, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            print(f"[agy] set model failed: {e}")


def _call_agy_sync(full_prompt: str, conv_id: str = "", prev_text: str = "") -> tuple[str, str]:
    """Вызов Antigravity (Gemini). Возвращает (text, new_conv_id).

    Antigravity принимает весь промпт одним аргументом (-p),
    не через stdin. При наличии conv_id — продолжает беседу.

    prev_text: предыдущий черновик. Нужен потому что antigravity-cli
    с --conversation возвращает в stdout ВЕСЬ разговор (все предыдущие
    ответы + новый). Стрипаем по anchor из конца prev_text.
    """
    _agy_set_model()
    cmd = [AGY_BIN, "-p", full_prompt, "--dangerously-skip-permissions"]
    if conv_id:
        cmd += ["--conversation", conv_id]
    result = subprocess.run(
        cmd,
        capture_output=True, text=True,
        cwd=_WORK_DIR,
        env={**os.environ, "HOME": os.path.expanduser("~")},
        timeout=180,
    )
    if result.returncode != 0 and result.stderr:
        print(f"[agy] stderr: {result.stderr[:300]}")
    output = result.stdout.strip()
    if prev_text and conv_id and output:
        anchor = prev_text[-200:].strip() if len(prev_text) > 200 else prev_text.strip()
        idx = output.rfind(anchor)
        if idx != -1:
            output = output[idx + len(anchor):].strip()
            print(f"[agy] stripped prev_text, extracted {len(output)} chars")
        else:
            print(f"[agy] anchor not found in output, returning full stdout")
    new_conv_id = _agy_get_conv_id()
    return output, new_conv_id


def _call_claude_sync(system: str, user_prompt: str, session_id: str = "") -> tuple[str, str]:
    """Вызов Claude. Возвращает (text, new_session_id).

    Если session_id передан — продолжает существующую сессию (--resume).
    System-prompt при resume не нужен: он уже в истории сессии.
    """
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    if session_id:
        cmd = ["claude", "-p", "--resume", session_id, "--model", CLAUDE_MODEL]
    else:
        cmd = ["claude", "-p", "--system-prompt", system, "--model", CLAUDE_MODEL]
    result = subprocess.run(
        cmd,
        input=user_prompt,
        capture_output=True, text=True, env=env, timeout=180,
        cwd=_WORK_DIR,
    )
    if result.returncode != 0 and result.stderr:
        print(f"[claude] stderr: {result.stderr[:300]}")
    new_session_id = _get_latest_session_id()
    return result.stdout.strip(), new_session_id


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
    print(f"[post_writer] callback.data={callback.data!r} → mode={mode}")

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
    post_text, session_id = await loop.run_in_executor(None, _call_claude_sync, system, user_prompt)

    if not post_text:
        await callback.message.answer("Не удалось сгенерировать пост. Попробуй снова.")
        return

    conn2 = _get_cb_db()
    conn2.execute("UPDATE cb_ideas SET status = 'shown' WHERE id = ?", (idea_id,))
    conn2.commit()
    conn2.sync()
    conn2.close()

    user_id = callback.from_user.id
    _drafts[user_id] = {
        "idea_id": idea_id, "draft": post_text,
        "system": system, "mode": mode, "session_id": session_id,
    }
    _awaiting_feedback.pop(user_id, None)
    print(f"[session] новая сессия: {session_id}")

    await _send_draft(callback.message, post_text, idea_id, mode)


_MODE_TO_PREFIX = {"current": "write", "diary": "write_diary", "archive": "write_archive"}


async def _send_draft(message, post_text: str, idea_id: int, mode: str = "current"):
    regen_prefix = _MODE_TO_PREFIX.get(mode, "write")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Улучшить (дать комментарий)", callback_data=f"improve:{idea_id}")],
        [InlineKeyboardButton(text="🫀 Гуманизировать", callback_data=f"humanize:{idea_id}")],
        [
            InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"regen:{idea_id}"),
            InlineKeyboardButton(text="⬛ С нуля", callback_data=f"{regen_prefix}:{idea_id}"),
        ],
        [InlineKeyboardButton(text="❌ Отклонить идею", callback_data=f"dismiss:{idea_id}")],
    ])

    preview = f"<b>Черновик поста:</b>\n\n{post_text}\n\n<i>({len(post_text)} символов)</i>"

    if len(preview) > 4096:
        await message.answer(post_text[:4000] + "...")
        await message.answer(f"<i>{len(post_text)} символов</i>", reply_markup=keyboard)
    else:
        await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")


def _build_humanizer_system() -> str:
    """Читает humanizer.md и возвращает системный промпт (без YAML frontmatter)."""
    p = Path(__file__).parent.parent / "humanizer.md"
    text = p.read_text(encoding="utf-8")
    # Убираем YAML frontmatter (--- ... ---)
    if text.startswith("---"):
        end = text.index("---", 3)
        text = text[end + 3:].lstrip()
    return text + "\n\nВажно: пост написан на русском языке — применяй все правила к русскому тексту."


def _humanize_sync(draft: str, voice_samples: list[str]) -> str:
    """Прогнать черновик через гуманизатор с voice calibration."""
    system = _build_humanizer_system()

    voice_block = ""
    if voice_samples:
        samples_text = "\n\n---\n".join(voice_samples[:3])
        voice_block = f"Voice calibration — примеры постов автора (его реальный стиль письма):\n\n{samples_text}\n\n"

    user_prompt = (
        f"{voice_block}"
        f"Гуманизируй этот текст. Убери AI-паттерны, сохрани смысл и голос автора:\n\n{draft}"
    )

    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", system, "--model", CLAUDE_MODEL],
        input=user_prompt,
        capture_output=True, text=True, env=env, timeout=180,
    )
    if result.returncode != 0 and result.stderr:
        print(f"[humanizer] stderr: {result.stderr[:200]}")
    return result.stdout.strip()


@router.callback_query(lambda c: c.data and c.data.startswith("humanize:"))
async def cb_humanize(callback: CallbackQuery):
    """Прогнать текущий черновик через гуманизатор."""
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])

    stored = _drafts.get(user_id)
    if not stored or stored.get("idea_id") != idea_id:
        msg_text = callback.message.text or ""
        draft = _extract_draft_from_message(msg_text)
        if not draft:
            await callback.answer("Черновик не найден — перегенерируй пост.")
            return
        from prompts import get_post_writer_system
        stored = {
            "idea_id": idea_id, "draft": draft,
            "system": get_post_writer_system(), "mode": "current",
            "session_id": "",  # сессия потеряна после рестарта — fallback на полный промпт
        }
        _drafts[user_id] = stored

    await callback.answer()
    await callback.message.answer("🫀 Гуманизирую текст — убираю AI-паттерны...")

    draft = stored["draft"]
    session_id = stored.get("session_id", "")
    mode = stored.get("mode", "current")

    loop = asyncio.get_event_loop()
    if session_id:
        # Продолжаем сессию — передаём правила гуманизатора как user-сообщение
        humanizer_rules = _build_humanizer_system()
        prompt = (
            f"Теперь гуманизируй последний пост который ты написал.\n\n"
            f"Правила:\n{humanizer_rules}\n\n"
            f"Выведи только готовый текст поста."
        )
        humanized, new_session_id = await loop.run_in_executor(
            None, _call_claude_sync, "", prompt, session_id
        )
    else:
        # Fallback: новый вызов с voice calibration
        conn = _get_cb_db()
        voice_samples = _get_voice_samples(conn)
        conn.close()
        humanized, new_session_id = await loop.run_in_executor(
            None, _humanize_sync, draft, voice_samples
        )
        # _humanize_sync возвращает str, session_id берём отдельно
        new_session_id = _get_latest_session_id()

    if not humanized:
        await callback.message.answer("Не удалось гуманизировать — попробуй ещё раз.")
        return

    _drafts[user_id] = {**stored, "draft": humanized, "session_id": new_session_id}
    await _send_draft(callback.message, humanized, idea_id, mode)


async def _generate_agy_and_send(callback: CallbackQuery, idea_id: int, mode: str = "diary"):
    """Генерация поста через Antigravity (Gemini)."""
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
    source_entries = json.loads(row[4] or "[]")
    idea_created_at = row[5] or ""
    conn.close()

    raw_messages = _get_raw_diary_messages(source_entries)
    if not raw_messages and idea_created_at:
        try:
            from datetime import datetime, timedelta
            idea_date = datetime.fromisoformat(idea_created_at[:10])
            fallback = [(idea_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(8)]
            raw_messages = _get_raw_diary_messages(fallback)
        except Exception:
            pass

    nlm_quotes = ""
    if mode == "archive":
        loop = asyncio.get_event_loop()
        nlm_quotes = await loop.run_in_executor(None, _get_archive_context_from_nlm, thesis)

    from prompts import get_post_writer_system, build_post_prompt
    system = get_post_writer_system()
    user_prompt = build_post_prompt(title, thesis, raw_messages, [], [], nlm_quotes)
    full_prompt = f"{system}\n\n---\n\n{user_prompt}"

    print(f"[agy][{mode}] diary={len(raw_messages)}, nlm={len(nlm_quotes)}")
    loop = asyncio.get_event_loop()
    post_text, conv_id = await loop.run_in_executor(None, _call_agy_sync, full_prompt)

    if not post_text:
        await callback.message.answer("🤖 Antigravity не смог сгенерировать пост. Попробуй ещё раз.")
        return

    user_id = callback.from_user.id
    _agy_drafts[user_id] = {"idea_id": idea_id, "draft": post_text, "mode": mode, "conv_id": conv_id}
    _agy_awaiting_feedback.pop(user_id, None)
    print(f"[agy-session] conv_id={conv_id}")

    await _send_agy_draft(callback.message, post_text, idea_id, mode)


async def _send_agy_draft(message, post_text: str, idea_id: int, mode: str = "diary"):
    regen_prefix = {"diary": "write_agy_diary", "archive": "write_agy_archive"}.get(mode, "write_agy_diary")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Улучшить [Gemini]", callback_data=f"improve_agy:{idea_id}")],
        [InlineKeyboardButton(text="🫀 Гуманизировать [Gemini]", callback_data=f"humanize_agy:{idea_id}")],
        [
            InlineKeyboardButton(text="🔄 Перегенерировать [Gemini]", callback_data=f"regen_agy:{idea_id}"),
            InlineKeyboardButton(text="⬛ С нуля", callback_data=f"{regen_prefix}:{idea_id}"),
        ],
        [InlineKeyboardButton(text="❌ Отклонить идею", callback_data=f"dismiss:{idea_id}")],
    ])
    preview = f"<b>🤖 Черновик [Gemini]:</b>\n\n{post_text}\n\n<i>({len(post_text)} символов)</i>"
    if len(preview) > 4096:
        await message.answer(post_text[:4000] + "...")
        await message.answer(f"<i>{len(post_text)} символов</i>", reply_markup=keyboard)
    else:
        await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("write_agy_diary:"))
async def cb_write_agy_diary(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[1])
    await callback.answer("Генерирую через Gemini...")
    await callback.message.answer("🤖 Пишу пост через Antigravity (Gemini) только из дневника...")
    await _generate_agy_and_send(callback, idea_id, mode="diary")


@router.callback_query(lambda c: c.data and c.data.startswith("write_agy_archive:"))
async def cb_write_agy_archive(callback: CallbackQuery):
    idea_id = int(callback.data.split(":")[1])
    await callback.answer("Генерирую через Gemini...")
    await callback.message.answer("🤖 Пишу пост через Antigravity (Gemini) из дневника + архив...")
    await _generate_agy_and_send(callback, idea_id, mode="archive")


@router.callback_query(lambda c: c.data and c.data.startswith("write_both_diary:"))
async def cb_write_both_diary(callback: CallbackQuery):
    """Запустить оба движка параллельно."""
    idea_id = int(callback.data.split(":")[1])
    await callback.answer("Генерирую оба варианта...")
    await callback.message.answer("⚡ Запускаю Claude и Gemini параллельно...")
    # Запускаем оба параллельно
    await asyncio.gather(
        _generate_and_send(callback, idea_id, mode="diary"),
        _generate_agy_and_send(callback, idea_id, mode="diary"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("regen_agy:"))
async def cb_regen_agy(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _agy_drafts.get(user_id)

    if not stored or stored.get("idea_id") != idea_id or not stored.get("conv_id"):
        await callback.answer("Сессия не найдена — перегенерирую с нуля...")
        await _generate_agy_and_send(callback, idea_id, "diary")
        return

    await callback.answer()
    await callback.message.answer("🔄 Прошу Gemini написать другой вариант...")

    conv_id = stored["conv_id"]
    loop = asyncio.get_event_loop()
    new_text, new_conv_id = await loop.run_in_executor(
        None, _call_agy_sync, "Напиши другой вариант этого поста. Тот же тезис, другой угол подачи.", conv_id, stored["draft"]
    )

    if not new_text:
        await callback.message.answer("Не получилось — попробуй ещё раз.")
        return

    _agy_drafts[user_id] = {**stored, "draft": new_text, "conv_id": new_conv_id}
    await _send_agy_draft(callback.message, new_text, idea_id, stored.get("mode", "diary"))


@router.callback_query(lambda c: c.data and c.data.startswith("improve_agy:"))
async def cb_improve_agy(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _agy_drafts.get(user_id)
    if not stored or stored.get("idea_id") != idea_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return
    _agy_awaiting_feedback[user_id] = True
    await callback.answer()
    await callback.message.answer(
        "🤖 Напиши или надиктуй что изменить в посте от Gemini:"
    )


@router.callback_query(lambda c: c.data and c.data.startswith("humanize_agy:"))
async def cb_humanize_agy(callback: CallbackQuery):
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _agy_drafts.get(user_id)
    if not stored or stored.get("idea_id") != idea_id:
        await callback.answer("Черновик не найден — перегенерируй.")
        return

    await callback.answer()
    await callback.message.answer("🫀 Гуманизирую текст от Gemini...")

    conv_id = stored.get("conv_id", "")
    if conv_id:
        humanizer_rules = _build_humanizer_system()
        prompt = f"Гуманизируй последний пост. Правила:\n{humanizer_rules}\n\nВыведи только готовый текст."
        loop = asyncio.get_event_loop()
        humanized, new_conv_id = await loop.run_in_executor(None, _call_agy_sync, prompt, conv_id, stored["draft"])
    else:
        from prompts import get_post_writer_system
        humanizer_sys = _build_humanizer_system()
        draft = stored["draft"]
        full_p = f"{humanizer_sys}\n\n---\n\nГуманизируй этот текст:\n\n{draft}"
        loop = asyncio.get_event_loop()
        humanized, new_conv_id = await loop.run_in_executor(None, _call_agy_sync, full_p)

    if not humanized:
        await callback.message.answer("Не удалось гуманизировать — попробуй ещё раз.")
        return

    _agy_drafts[user_id] = {**stored, "draft": humanized, "conv_id": new_conv_id}
    await _send_agy_draft(callback.message, humanized, idea_id, stored.get("mode", "diary"))


async def _process_agy_feedback(message: Message, user_id: int, feedback: str):
    """Улучшить черновик Antigravity по фидбэку."""
    stored = _agy_drafts.get(user_id)
    if not stored:
        _agy_awaiting_feedback.pop(user_id, None)
        return
    _agy_awaiting_feedback.pop(user_id)
    idea_id = stored["idea_id"]
    conv_id = stored.get("conv_id", "")
    await message.answer("✏️ Улучшаю пост от Gemini...")
    loop = asyncio.get_event_loop()
    if conv_id:
        improved, new_conv_id = await loop.run_in_executor(None, _call_agy_sync, feedback, conv_id, stored["draft"])
    else:
        from prompts import get_post_writer_system, build_improve_prompt
        full_p = f"{get_post_writer_system()}\n\n---\n\n{build_improve_prompt(stored['draft'], feedback)}"
        improved, new_conv_id = await loop.run_in_executor(None, _call_agy_sync, full_p)
    if improved:
        _agy_drafts[user_id] = {**stored, "draft": improved, "conv_id": new_conv_id}
        await _send_agy_draft(message, improved, idea_id, stored.get("mode", "diary"))


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


@router.callback_query(lambda c: c.data and c.data.startswith("regen:"))
async def cb_regen_quick(callback: CallbackQuery):
    """Быстрая регенерация — продолжает текущую сессию, не перезагружает данные."""
    user_id = callback.from_user.id
    idea_id = int(callback.data.split(":")[1])
    stored = _drafts.get(user_id)

    if not stored or stored.get("idea_id") != idea_id or not stored.get("session_id"):
        # Нет сессии — падаем в полную регенерацию
        await callback.answer("Сессия не найдена — перегенерирую с нуля...")
        mode = stored.get("mode", "diary") if stored else "diary"
        await _generate_and_send(callback, idea_id, mode)
        return

    await callback.answer()
    await callback.message.answer("🔄 Пишу другой вариант...")

    session_id = stored["session_id"]
    loop = asyncio.get_event_loop()
    new_text, new_session_id = await loop.run_in_executor(
        None, _call_claude_sync, "", "Напиши другой вариант этого поста. Тот же тезис, другой угол подачи.", session_id
    )

    if not new_text:
        await callback.message.answer("Не получилось — попробуй ещё раз.")
        return

    _drafts[user_id] = {**stored, "draft": new_text, "session_id": new_session_id}
    await _send_draft(callback.message, new_text, idea_id, stored.get("mode", "current"))


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
    session_id = stored.get("session_id", "")

    await message.answer("✏️ Улучшаю пост с учётом твоего комментария...")

    loop = asyncio.get_event_loop()
    if session_id:
        # Продолжаем сессию — передаём только фидбэк, контекст уже там
        improved, new_session_id = await loop.run_in_executor(
            None, _call_claude_sync, "", feedback, session_id
        )
    else:
        # Fallback без сессии — передаём черновик + фидбэк
        from prompts import build_improve_prompt
        improve_prompt = build_improve_prompt(draft, feedback)
        improved, new_session_id = await loop.run_in_executor(
            None, _call_claude_sync, system, improve_prompt
        )

    if not improved:
        await message.answer("Не удалось улучшить пост. Попробуй ещё раз.")
        _awaiting_feedback[user_id] = True
        return

    _drafts[user_id] = {**stored, "draft": improved, "session_id": new_session_id}
    await _send_draft(message, improved, idea_id, stored.get("mode", "current"))


@router.message(F.voice)
async def on_feedback_voice(message: Message, bot: Bot):
    """Голосовой фидбэк — роутит к Claude, Antigravity, Reels/YouTube или capture."""
    user_id = message.from_user.id
    is_claude = _awaiting_feedback.get(user_id)
    is_agy = _agy_awaiting_feedback.get(user_id)

    from handlers.format_writer import _awaiting_reels_feedback, _awaiting_youtube_feedback, process_reels_feedback, process_youtube_feedback
    is_reels = _awaiting_reels_feedback.get(user_id)
    is_youtube = _awaiting_youtube_feedback.get(user_id)

    if not any([is_claude, is_agy, is_reels, is_youtube]):
        from handlers.capture import handle_voice_capture
        await handle_voice_capture(message, bot)
        return

    await message.answer("🎤 Транскрибирую голосовое...")
    buf = io.BytesIO()
    await bot.download(message.voice, destination=buf)
    from transcribe import transcribe_sync
    loop = asyncio.get_event_loop()
    feedback = await loop.run_in_executor(None, transcribe_sync, buf.getvalue(), ".ogg")
    if not feedback:
        await message.answer("Не удалось распознать — попробуй написать текстом.")
        return
    await message.answer(f"📝 Распознал: {feedback}")

    if is_agy:
        await _process_agy_feedback(message, user_id, feedback)
    elif is_claude:
        await _process_feedback(message, user_id, feedback)
    elif is_reels:
        await process_reels_feedback(message, user_id, feedback)
    elif is_youtube:
        await process_youtube_feedback(message, user_id, feedback)


@router.message(F.text)
async def on_feedback_text(message: Message):
    """Текстовый фидбэк — роутит к Claude, Antigravity или Reels/YouTube."""
    user_id = message.from_user.id
    if _agy_awaiting_feedback.get(user_id):
        await _process_agy_feedback(message, user_id, message.text.strip())
    elif _awaiting_feedback.get(user_id):
        await _process_feedback(message, user_id, message.text.strip())
    else:
        from handlers.format_writer import _awaiting_reels_feedback, _awaiting_youtube_feedback, process_reels_feedback, process_youtube_feedback
        if _awaiting_reels_feedback.get(user_id):
            await process_reels_feedback(message, user_id, message.text.strip())
        elif _awaiting_youtube_feedback.get(user_id):
            await process_youtube_feedback(message, user_id, message.text.strip())


