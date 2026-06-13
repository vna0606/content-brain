"""
youtube_import.py — импорт видео YouTube-канала в Turso content-brain.

Запускать на MacBook (YouTube блокирует запросы с серверов).

Установка зависимостей:
    pip install yt-dlp youtube-transcript-api requests

Использование:
    python youtube_import.py --channel https://www.youtube.com/@ВашКанал
    python youtube_import.py --channel UCxxxxxxxx  # по channel ID

Что делает:
    - Получает список всех видео канала
    - Для каждого видео: название, дата, описание, субтитры (авто или ручные)
    - Записывает в Turso: cb_social_posts + cb_social_vectors (hash-embedding)
"""

import argparse
import hashlib
import json
import struct
import sys
import time
from datetime import datetime, timezone

import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

# ─── Turso credentials ────────────────────────────────────────────────────────
TURSO_URL = "https://content-brain-nikves.aws-eu-west-1.turso.io"
TURSO_TOKEN = (
    "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9"
    ".eyJhIjoicnciLCJpYXQiOjE3ODAzMzgxNzksImlkIjoiMDE5ZTg0NmMtYTIwMS03MWM0"
    "LWE3MjgtZDIxMzFkMGY0YjM3IiwicmlkIjoiYzZiNzQyZjAtNzZkOS00NTdhLWJiMDIt"
    "MmI4ZmEwMzc3NzU0In0"
    ".mczlSIT0HMoBC-h6uHhlPnn3-_Vi-QeSPZ1gh1Xu6_B38z5SdTDq_OfLYhwjLQBGxM5p9GQ7iYLJgk_oLoQMBg"
)

EMBEDDING_DIM = 256


# ─── Embedding (детерминированный, как в embedder.py) ─────────────────────────

def _embed_text(text: str) -> bytes:
    """SHA-256-seeded псевдо-вектор → bytes для Turso BLOB."""
    import random as _rnd
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big")
    rng = _rnd.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIM)]
    # Нормализация
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return struct.pack(f"{EMBEDDING_DIM}f", *vec)


# ─── Turso HTTP API ────────────────────────────────────────────────────────────

def _turso_execute(sql: str, args: list) -> dict:
    """Выполнить один SQL-запрос через Turso HTTP API."""
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": [{"type": "text", "value": str(a)} if not isinstance(a, bytes)
                             else {"type": "blob", "base64": __import__("base64").b64encode(a).decode()}
                             for a in args],
                },
            }
        ]
    }
    resp = requests.post(f"{TURSO_URL}/v2/pipeline", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _turso_query(sql: str, args: list = None) -> list[dict]:
    """SELECT запрос через Turso HTTP API, возвращает список строк как dict."""
    result = _turso_execute(sql, args or [])
    rows_data = result["results"][0]["response"]["result"]
    cols = [c["name"] for c in rows_data["cols"]]
    return [dict(zip(cols, [v["value"] for v in row])) for row in rows_data["rows"]]


def _already_indexed(source_id: str) -> bool:
    rows = _turso_query(
        "SELECT id FROM cb_social_posts WHERE source_id = ?", [source_id]
    )
    return len(rows) > 0


def _insert_video(video_id: str, title: str, description: str, transcript: str,
                  published_at: str, url: str) -> int:
    """Вставить видео в cb_social_posts, вернуть id."""
    content = f"{title}\n\n{description}"
    if transcript:
        content += f"\n\n--- Субтитры ---\n{transcript[:8000]}"

    _turso_execute(
        "INSERT INTO cb_social_posts (source_type, source_id, content, url, published_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ["youtube", f"yt_{video_id}", content, url, published_at],
    )
    rows = _turso_query(
        "SELECT id FROM cb_social_posts WHERE source_id = ?", [f"yt_{video_id}"]
    )
    return int(rows[0]["id"])


def _insert_vector(social_post_id: int, video_id: str, content_chunk: str):
    embedding = _embed_text(content_chunk)
    _turso_execute(
        "INSERT INTO cb_social_vectors (social_post_id, source_id, content_chunk, embedding, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [str(social_post_id), f"yt_{video_id}", content_chunk[:500],
         embedding, datetime.now(timezone.utc).isoformat()],
    )


# ─── YouTube ──────────────────────────────────────────────────────────────────

def get_channel_videos(channel_url: str) -> list[dict]:
    """Получить список всех видео канала через yt-dlp."""
    print(f"Получение списка видео: {channel_url}")
    ydl_opts = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "playlist_items": "1-500",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    entries = info.get("entries", [])
    print(f"Найдено видео: {len(entries)}")
    return entries


def get_video_details(video_id: str) -> dict | None:
    """Получить метаданные одного видео."""
    ydl_opts = {"quiet": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        return info
    except Exception as e:
        print(f"  ⚠ Не удалось получить данные видео {video_id}: {e}")
        return None


def get_transcript(video_id: str) -> str:
    """Получить субтитры видео (русские → любые авто → пустая строка)."""
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        # Приоритет: ручные русские → ручные любые → авто русские → авто любые
        for lang in ["ru", "en"]:
            try:
                t = transcript_list.find_transcript([lang])
                entries = t.fetch()
                return " ".join(e["text"] for e in entries)
            except Exception:
                pass

        try:
            t = transcript_list.find_generated_transcript(["ru", "en"])
            entries = t.fetch()
            return " ".join(e["text"] for e in entries)
        except Exception:
            pass

    except (NoTranscriptFound, TranscriptsDisabled):
        pass
    except Exception as e:
        print(f"  ⚠ Субтитры {video_id}: {e}")

    return ""


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Импорт YouTube-канала в Turso content-brain")
    parser.add_argument("--channel", required=True,
                        help="URL или @handle канала, напр: https://www.youtube.com/@nikbase")
    parser.add_argument("--limit", type=int, default=0,
                        help="Лимит видео (0 = все)")
    args = parser.parse_args()

    # Получить список видео
    videos = get_channel_videos(args.channel)
    if args.limit:
        videos = videos[:args.limit]

    total = len(videos)
    indexed = 0
    skipped = 0

    for i, entry in enumerate(videos, 1):
        video_id = entry.get("id") or entry.get("url", "").split("v=")[-1]
        if not video_id:
            continue

        source_id = f"yt_{video_id}"
        title = entry.get("title", f"video_{video_id}")
        print(f"[{i}/{total}] {title[:60]}")

        if _already_indexed(source_id):
            print("  → уже в БД, пропуск")
            skipped += 1
            continue

        # Метаданные
        details = get_video_details(video_id)
        description = ""
        published_at = datetime.now(timezone.utc).isoformat()
        if details:
            description = details.get("description", "") or ""
            upload_date = details.get("upload_date")
            if upload_date:
                published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00+00:00"

        # Субтитры
        print("  → получение субтитров...")
        transcript = get_transcript(video_id)
        if transcript:
            print(f"  → субтитры: {len(transcript)} символов")
        else:
            print("  → субтитры не найдены")

        url = f"https://www.youtube.com/watch?v={video_id}"
        content_chunk = f"{title}\n{description[:300]}"
        if transcript:
            content_chunk += f"\n{transcript[:200]}"

        # Запись в Turso
        try:
            post_id = _insert_video(video_id, title, description, transcript, published_at, url)
            _insert_vector(post_id, video_id, content_chunk)
            indexed += 1
            print(f"  → сохранено (id={post_id})")
        except Exception as e:
            print(f"  ✗ Ошибка записи: {e}")

        # Пауза чтобы не спамить YouTube
        time.sleep(1)

    print(f"\nГотово: {indexed} добавлено, {skipped} пропущено, {total - indexed - skipped} ошибок")


if __name__ == "__main__":
    main()
