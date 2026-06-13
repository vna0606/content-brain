"""
indexer.py — читает mood-diary и Telegram-канал, пишет векторы в cb_db.

mood-diary схема (read-only):
  entries  — date, status, mood_score, energy_level, tags, ai_summary, day_vibe
  messages — id, entry_date, role ('user'|'assistant'), content, created_at

Что индексируем:
  Дневник → cb_diary_vectors:
    - ai_summary из entries (source_id='entry_{date}')
    - сырые сообщения пользователя из messages (source_id='msg_{id}')
  Telegram → cb_social_posts + cb_social_vectors
"""

import asyncio
import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import requests as _requests
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, MessageMediaWebPage
from dotenv import load_dotenv

from db import get_mood_db, get_cb_db, init_schema, commit, already_indexed
from embedder import embed_text, vec_to_blob

load_dotenv()

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_CHANNEL = os.environ.get("TG_CHANNEL_USERNAME", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
SESSION_FILE = os.path.join(os.path.dirname(__file__), "telegram_session")


async def _transcribe_voice(tg_client, message) -> str:
    """Скачать голосовое/аудио сообщение и транскрибировать через Groq."""
    if not GROQ_API_KEY:
        return ""
    try:
        audio_bytes = await tg_client.download_media(message, bytes)
        if not audio_bytes:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        try:
            with open(tmp_path, "rb") as af:
                resp = _requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": ("audio.ogg", af, "audio/ogg")},
                    data={"model": "whisper-large-v3", "language": "ru", "response_format": "text"},
                    timeout=60,
                )
            if resp.status_code == 200:
                return resp.text.strip()
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        print(f"  [transcribe] ошибка: {e}")
    return ""


def index_diary_summaries(cb_db, mood_db, days: int | None = None) -> int:
    """Индексировать ai_summary из entries. Один вектор на день."""
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cur = mood_db.execute(
            "SELECT date, ai_summary, tags FROM entries "
            "WHERE status = 'completed' AND ai_summary IS NOT NULL AND date >= ? "
            "ORDER BY date DESC",
            (cutoff,),
        )
    else:
        cur = mood_db.execute(
            "SELECT date, ai_summary, tags FROM entries "
            "WHERE status = 'completed' AND ai_summary IS NOT NULL "
            "ORDER BY date DESC"
        )

    rows = cur.fetchall()
    indexed = 0
    for row in rows:
        date, ai_summary, tags_json = row[0], row[1], row[2]
        source_id = f"entry_{date}"
        if already_indexed(cb_db, "cb_diary_vectors", source_id):
            continue

        tags = json.loads(tags_json or "[]")
        chunk = f"[{date}] {ai_summary}"
        if tags:
            chunk += f"\nТеги: {', '.join(tags)}"

        vec = embed_text(chunk)
        cb_db.execute(
            "INSERT INTO cb_diary_vectors (source_id, content_chunk, embedding, entry_date, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, chunk, vec_to_blob(vec), date, datetime.now(timezone.utc).isoformat()),
        )
        indexed += 1

    if indexed:
        commit(cb_db)
    print(f"[diary/summaries] Проиндексировано: {indexed}")
    return indexed


def index_diary_messages(cb_db, mood_db, days: int | None = None) -> int:
    """Индексировать сырые сообщения пользователя из messages."""
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cur = mood_db.execute(
            "SELECT id, entry_date, content, created_at FROM messages "
            "WHERE role = 'user' AND entry_date >= ? ORDER BY created_at DESC",
            (cutoff,),
        )
    else:
        cur = mood_db.execute(
            "SELECT id, entry_date, content, created_at FROM messages "
            "WHERE role = 'user' ORDER BY created_at DESC"
        )

    rows = cur.fetchall()
    indexed = 0
    for row in rows:
        msg_id, entry_date, content, created_at = row[0], row[1], row[2], row[3]
        if not content or not content.strip():
            continue

        source_id = f"msg_{msg_id}"
        if already_indexed(cb_db, "cb_diary_vectors", source_id):
            continue

        chunk = f"[{entry_date}] {content.strip()}"
        vec = embed_text(chunk)
        cb_db.execute(
            "INSERT INTO cb_diary_vectors (source_id, content_chunk, embedding, entry_date, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, chunk, vec_to_blob(vec), entry_date,
             created_at or datetime.now(timezone.utc).isoformat()),
        )
        indexed += 1

    if indexed:
        commit(cb_db)
    print(f"[diary/messages] Проиндексировано: {indexed}")
    return indexed


async def index_telegram_posts(days: int | None = None) -> int:
    """
    Читать посты Telegram-канала, сохранять в cb_social_posts + cb_social_vectors.

    Логика в два прохода:
      1. Собрать все записи (+ транскрибировать голосовые) в памяти — без DB
      2. Открыть свежее DB-соединение и быстро записать всё — без таймаутов
    """
    if not TG_CHANNEL:
        print("[telegram] TG_CHANNEL_USERNAME не задан, пропуск")
        return 0

    cutoff_dt = None
    if days:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)

    # --- Проход 1: собрать уже существующие source_id чтобы не дублировать ---
    check_db = get_cb_db()
    cur = check_db.execute("SELECT source_id FROM cb_social_posts WHERE source_type = 'telegram'")
    existing_ids = {row[0] for row in cur.fetchall()}
    check_db.close()

    # --- Проход 2: Telethon — читаем, транскрибируем, складываем в список ---
    collected: list[dict] = []
    skipped_voice = 0

    async with TelegramClient(SESSION_FILE, TG_API_ID, TG_API_HASH) as tg:
        channel = await tg.get_entity(TG_CHANNEL)
        async for message in tg.iter_messages(channel, limit=1000):
            if cutoff_dt and message.date.replace(tzinfo=timezone.utc) < cutoff_dt:
                break

            source_id = f"tg_{message.id}"
            if source_id in existing_ids:
                continue

            content = ""
            if message.text and message.text.strip():
                content = message.text.strip()
            elif message.voice or (message.audio and getattr(message.audio, 'voice', False)):
                print(f"  [voice] id={message.id}, транскрибирую...")
                text = await _transcribe_voice(tg, message)
                if text:
                    content = f"[голосовое] {text}"
                else:
                    skipped_voice += 1
                    print(f"  [voice] id={message.id} — транскрипция не удалась, пропуск")
                    continue
            else:
                continue

            collected.append({
                "source_id": source_id,
                "content": content,
                "published_at": message.date.isoformat(),
                "url": f"https://t.me/{TG_CHANNEL}/{message.id}",
            })

    print(f"[telegram] Собрано для записи: {len(collected)} (голосовых пропущено: {skipped_voice})")

    if not collected:
        return 0

    # --- Проход 3: свежее DB-соединение → быстрая запись ---
    cb_db = get_cb_db()
    indexed = 0
    try:
        for item in collected:
            cur = cb_db.execute(
                "INSERT INTO cb_social_posts (source_type, source_id, content, url, published_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("telegram", item["source_id"], item["content"], item["url"], item["published_at"]),
            )
            social_post_id = cur.lastrowid

            vec = embed_text(item["content"])
            cb_db.execute(
                "INSERT INTO cb_social_vectors (social_post_id, source_id, content_chunk, embedding, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (social_post_id, item["source_id"], item["content"][:1000], vec_to_blob(vec),
                 datetime.now(timezone.utc).isoformat()),
            )
            indexed += 1

            if indexed % 20 == 0:
                commit(cb_db)
                print(f"  [telegram] записано: {indexed}...")

        if indexed % 20 != 0:
            commit(cb_db)
    finally:
        cb_db.close()

    print(f"[telegram] Проиндексировано: {indexed} (голосовых без транскрипта: {skipped_voice})")
    return indexed


async def run_indexing(full: bool = False):
    """full=True — всё; full=False — последние 30 дней дневника, 90 Telegram."""
    diary_days = None if full else 30
    social_days = None if full else 90

    print("[indexer] Подключение к БД...")
    mood_db = get_mood_db()
    cb_db = get_cb_db()

    try:
        print("[indexer] Инициализация схемы...")
        init_schema(cb_db)

        print("[indexer] Индексация резюме дней...")
        index_diary_summaries(cb_db, mood_db, days=diary_days)

        print("[indexer] Индексация сырых записей...")
        index_diary_messages(cb_db, mood_db, days=diary_days)
    finally:
        mood_db.close()
        cb_db.close()

    # Telegram — отдельно: открывает и закрывает собственное соединение
    # чтобы транскрипция голосовых не роняла Turso-стрим таймаутом
    print("[indexer] Индексация Telegram-канала...")
    await index_telegram_posts(days=social_days)

    print("[indexer] Готово.")
