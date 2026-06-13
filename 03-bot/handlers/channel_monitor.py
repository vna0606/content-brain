"""
channel_monitor.py — автосбор новых постов из Telegram-канала в cb_social_posts.

Как подключить:
  1. Добавить бота в канал как администратора (достаточно права "Чтение сообщений")
  2. В main.py добавить 'channel_post' в allowed_updates

Когда в канале появляется новый пост — он автоматически сохраняется в Turso.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import libsql_experimental as libsql
from aiogram import Router
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

router = Router()

_DB_DIR = Path(__file__).parent.parent
TG_CHANNEL = os.environ.get("TG_CHANNEL_USERNAME", "")


def _get_db():
    replica = str(_DB_DIR / "cb_bot_replica.db")
    conn = libsql.connect(
        replica,
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


def _embed_text_sync(text: str):
    """Inline embedding — импортируем из 01-knowledge-base через sys.path."""
    import sys
    kb_path = str(Path(__file__).parent.parent.parent / "01-knowledge-base")
    if kb_path not in sys.path:
        sys.path.insert(0, kb_path)
    from embedder import embed_text, vec_to_blob
    return embed_text(text), vec_to_blob


@router.channel_post()
async def on_channel_post(message: Message):
    """Новый пост в канале → сохранить в cb_social_posts + cb_social_vectors."""
    if not message.text:
        return

    # Проверяем что пост из нашего канала
    if TG_CHANNEL and message.chat.username and message.chat.username.lower() != TG_CHANNEL.lower():
        return

    source_id = f"tg_{message.message_id}"
    post_text = message.text.strip()
    published_at = (message.date or datetime.now(timezone.utc)).isoformat()
    url = f"https://t.me/{TG_CHANNEL}/{message.message_id}" if TG_CHANNEL else ""

    conn = _get_db()

    # Дедупликация
    cur = conn.execute(
        "SELECT id FROM cb_social_posts WHERE source_id = ?", (source_id,)
    )
    if cur.fetchone():
        conn.close()
        return

    # Сохранить пост
    cur = conn.execute(
        "INSERT INTO cb_social_posts (source_type, source_id, content, url, published_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("telegram", source_id, post_text, url, published_at),
    )
    social_post_id = cur.lastrowid

    # Создать вектор
    import sys
    kb_path = str(Path(__file__).parent.parent.parent / "01-knowledge-base")
    if kb_path not in sys.path:
        sys.path.insert(0, kb_path)
    from embedder import embed_text, vec_to_blob

    vec = embed_text(post_text)
    conn.execute(
        "INSERT INTO cb_social_vectors (social_post_id, source_id, content_chunk, embedding, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (social_post_id, source_id, post_text, vec_to_blob(vec),
         datetime.now(timezone.utc).isoformat()),
    )

    conn.commit()
    conn.sync()
    conn.close()
