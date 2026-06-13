"""
db.py — Turso connections и DDL для content-brain.

Использует libsql_experimental (sync API + local replica).
Две БД:
  - mood_db : только чтение, таблицы `entries` и `messages` из mood-diary
  - cb_db   : чтение/запись, все cb_* таблицы

mood-diary схема (read-only):
  entries  — date, status, mood_score, energy_level, tags, ai_summary, day_vibe
  messages — id, entry_date, role ('user'|'assistant'), content, created_at
"""

import os
from pathlib import Path

import libsql_experimental as libsql
from dotenv import load_dotenv

load_dotenv()

_DB_DIR = Path(__file__).parent


def get_mood_db():
    """Sync connection к mood-diary Turso (только SELECT)."""
    replica = str(_DB_DIR / "mood_replica.db")
    conn = libsql.connect(
        replica,
        sync_url=os.environ["TURSO_MOOD_URL"],
        auth_token=os.environ["TURSO_MOOD_TOKEN"],
    )
    conn.sync()
    return conn


def get_cb_db():
    """Sync connection к content-brain Turso (чтение + запись)."""
    replica = str(_DB_DIR / "cb_replica.db")
    conn = libsql.connect(
        replica,
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    return conn


def commit(conn):
    """Commit + sync изменений в Turso."""
    conn.commit()
    conn.sync()


# --- DDL ---

_DDL = [
    """CREATE TABLE IF NOT EXISTS cb_diary_vectors (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id     TEXT NOT NULL UNIQUE,
        content_chunk TEXT NOT NULL,
        embedding     BLOB,
        entry_date    TEXT,
        created_at    TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cb_social_posts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type  TEXT NOT NULL,
        source_id    TEXT NOT NULL UNIQUE,
        content      TEXT NOT NULL,
        url          TEXT,
        published_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cb_social_vectors (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        social_post_id INTEGER NOT NULL,
        source_id      TEXT NOT NULL UNIQUE,
        content_chunk  TEXT NOT NULL,
        embedding      BLOB,
        created_at     TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cb_published_posts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        post_text     TEXT NOT NULL,
        published_at  TEXT NOT NULL,
        tg_message_id INTEGER,
        topics        TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS cb_ideas (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        title            TEXT NOT NULL,
        thesis           TEXT NOT NULL,
        relevant_history TEXT,
        relevant_social  TEXT,
        source_entries   TEXT,
        status           TEXT DEFAULT 'new',
        created_at       TEXT NOT NULL
    )""",
]


def init_schema(conn):
    """Создать таблицы content-brain если не существуют."""
    for ddl in _DDL:
        conn.execute(ddl)
    commit(conn)


def already_indexed(conn, table: str, source_id: str) -> bool:
    """Проверить дедупликацию: есть ли source_id в таблице."""
    cur = conn.execute(f"SELECT id FROM {table} WHERE source_id = ?", (source_id,))
    return cur.fetchone() is not None
