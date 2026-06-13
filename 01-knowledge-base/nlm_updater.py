#!/usr/bin/env python3
"""
nlm_updater.py — автоматическое обновление NLM archive-ноутбука из Turso.

Запуски:
  Еженедельно (воскресенье 03:00 MSK): python3 nlm_updater.py
    - Новые Telegram-посты (батч по месяцам, если ≥5 постов)
    - Новые YouTube-ролики (батч по месяцам)
    - Резюме дней дневника (замена: всегда актуальное)

  Ежемесячно (1-е число 03:00 MSK): python3 nlm_updater.py --monthly
    - Всё что выше +
    - Сырые записи дневника за прошлый месяц (новый источник)

Лог: /home/ubuntu/content-brain-nlm-update.log
Состояние: .nlm_update_state.json (хранит даты последних синков и ID источников)
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, date
from calendar import monthrange

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from db import get_cb_db, get_mood_db

NOTEBOOK_ID = os.environ.get("NLM_NOTEBOOK_ID", "1eb35d64-3e12-4dc9-8043-f65d703d6281")
NLM_BIN = os.path.expanduser("~/.local/bin/nlm")
STATE_FILE = os.path.join(os.path.dirname(__file__), ".nlm_update_state.json")
MIN_NEW_POSTS = 5


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "last_telegram_date": "2026-06-02",
        "last_youtube_date": "2026-06-02",
        "diary_source_id": "a85e8b42-2b65-47bc-bdf4-66652fc90c15",
        "diary_raw_months_uploaded": [],
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def nlm_add(filepath: str, title: str) -> str | None:
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        [NLM_BIN, "source", "add", NOTEBOOK_ID,
         "--file", filepath, "--title", title, "--wait"],
        capture_output=True, text=True, env=env, timeout=300,
    )
    if result.returncode != 0:
        log(f"  [NLM] ошибка: {result.stderr[:200]}")
        return None
    for line in result.stdout.splitlines():
        if "Source ID:" in line:
            return line.split("Source ID:")[-1].strip()
    return "ok"


def nlm_delete(source_id: str) -> bool:
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        [NLM_BIN, "source", "delete", source_id, "--confirm"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return result.returncode == 0


# ─── Еженедельные задачи ──────────────────────────────────────────────────────

def update_telegram(state: dict) -> dict:
    since = state.get("last_telegram_date", "2026-06-02")
    log(f"[weekly] Telegram: новые посты после {since}...")

    cb = get_cb_db()
    cur = cb.execute(
        "SELECT content, published_at, url FROM cb_social_posts "
        "WHERE source_type='telegram' AND published_at > ? "
        "ORDER BY published_at ASC",
        (since,)
    )
    rows = cur.fetchall()
    cb.close()

    if len(rows) < MIN_NEW_POSTS:
        log(f"  Постов: {len(rows)} — меньше {MIN_NEW_POSTS}, пропускаем")
        return state

    by_month: dict[str, list] = {}
    for content, pub_at, url in rows:
        month = pub_at[:7] if pub_at else "unknown"
        by_month.setdefault(month, []).append((content, pub_at, url))

    for month, posts in by_month.items():
        lines = [f"TELEGRAM-КАНАЛ @nikbase — {month}\n{'=' * 40}\n"]
        for content, pub_at, url in posts:
            lines.append(f"=== {pub_at[:10]} ===\n{content}\n[{url}]\n")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False, encoding="utf-8") as f:
            f.write("\n".join(lines))
            tmp = f.name

        title = f"Telegram-канал @nikbase {month}"
        log(f"  → «{title}» ({len(posts)} постов)")
        sid = nlm_add(tmp, title)
        os.unlink(tmp)
        log(f"  {'✓' if sid else '✗'} source_id: {sid}")

    state["last_telegram_date"] = rows[-1][1][:10]
    return state


def update_youtube(state: dict) -> dict:
    since = state.get("last_youtube_date", "2026-06-02")
    log(f"[weekly] YouTube: новые ролики после {since}...")

    cb = get_cb_db()
    cur = cb.execute(
        "SELECT content, published_at, url FROM cb_social_posts "
        "WHERE source_type='youtube' AND published_at > ? "
        "ORDER BY published_at ASC",
        (since,)
    )
    rows = cur.fetchall()
    cb.close()

    if not rows:
        log("  Новых роликов нет")
        return state

    by_month: dict[str, list] = {}
    for content, pub_at, url in rows:
        month = pub_at[:7] if pub_at else "unknown"
        by_month.setdefault(month, []).append((content, pub_at, url))

    for month, videos in by_month.items():
        lines = [f"YOUTUBE-РОЛИКИ — {month}\n{'=' * 40}\n"]
        for content, pub_at, url in videos:
            lines.append(f"=== {pub_at[:10]} ===\n[{url}]\n{content.strip()}\n")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False, encoding="utf-8") as f:
            f.write("\n".join(lines))
            tmp = f.name

        title = f"YouTube-ролики {month}"
        log(f"  → «{title}» ({len(videos)} роликов)")
        sid = nlm_add(tmp, title)
        os.unlink(tmp)
        log(f"  {'✓' if sid else '✗'} source_id: {sid}")

    state["last_youtube_date"] = rows[-1][1][:10]
    return state


def update_diary_summaries(state: dict) -> dict:
    log("[weekly] Дневник — резюме дней: заменяем...")

    mood = get_mood_db()
    cur = mood.execute(
        "SELECT date, ai_summary, tags, mood_score, energy_level FROM entries "
        "WHERE status='completed' AND ai_summary IS NOT NULL ORDER BY date DESC"
    )
    rows = cur.fetchall()
    mood.close()

    if not rows:
        log("  Нет резюме")
        return state

    lines = [f"ДНЕВНИК НИКИТЫ — РЕЗЮМЕ ДНЕЙ\n{'=' * 50}\n"]
    for date_, summary, tags, mood_score, energy in rows:
        lines.append(
            f"=== {date_} (настроение {mood_score}/10, энергия {energy}/10) ===\n{summary}"
        )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, encoding="utf-8") as f:
        f.write("\n\n".join(lines))
        tmp = f.name

    old_id = state.get("diary_source_id")
    if old_id:
        log(f"  Удаляю старый источник ({old_id})...")
        log(f"  {'✓' if nlm_delete(old_id) else '✗'}")

    log(f"  Добавляю ({len(rows)} резюме)...")
    new_id = nlm_add(tmp, "Дневник — резюме дней")
    os.unlink(tmp)

    if new_id:
        state["diary_source_id"] = new_id
        log(f"  ✓ source_id: {new_id}")

    return state


# ─── Ежемесячная задача: сырые записи дневника ───────────────────────────────

def update_diary_raw_monthly(state: dict) -> dict:
    """Добавляет все сырые записи дневника за прошлый месяц."""
    today = date.today()
    if today.month == 1:
        year, month_num = today.year - 1, 12
    else:
        year, month_num = today.year, today.month - 1

    month_str = f"{year}-{month_num:02d}"
    uploaded = state.get("diary_raw_months_uploaded", [])

    if month_str in uploaded:
        log(f"[monthly] Дневник сырые записи {month_str}: уже загружено, пропускаем")
        return state

    log(f"[monthly] Дневник — сырые записи за {month_str}...")

    start = f"{month_str}-01"
    last_day = monthrange(year, month_num)[1]
    end = f"{month_str}-{last_day:02d}"

    mood = get_mood_db()
    cur = mood.execute(
        "SELECT entry_date, content FROM messages "
        "WHERE role='user' AND entry_date >= ? AND entry_date <= ? "
        "ORDER BY entry_date ASC, rowid ASC",
        (start, end)
    )
    rows = cur.fetchall()
    mood.close()

    if not rows:
        log(f"  За {month_str} нет записей")
        uploaded.append(month_str)
        state["diary_raw_months_uploaded"] = uploaded
        return state

    # Группируем по дате
    from collections import defaultdict
    by_date = defaultdict(list)
    for entry_date, content in rows:
        if content and content.strip():
            by_date[entry_date].append(content.strip())

    lines = [f"ДНЕВНИК НИКИТЫ — СЫРЫЕ ЗАПИСИ {month_str}\n{'=' * 50}\n"]
    for d in sorted(by_date.keys()):
        lines.append(f"=== {d} ===\n" + "\n---\n".join(by_date[d]))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, encoding="utf-8") as f:
        f.write("\n\n".join(lines))
        tmp = f.name

    file_kb = os.path.getsize(tmp) // 1024
    log(f"  Файл: {file_kb} KB, {len(rows)} записей за {len(by_date)} дней")

    title = f"Дневник — сырые записи {month_str}"
    log(f"  Добавляю в NLM: «{title}»...")
    sid = nlm_add(tmp, title)
    os.unlink(tmp)

    if sid:
        log(f"  ✓ source_id: {sid}")
        uploaded.append(month_str)
        state["diary_raw_months_uploaded"] = uploaded
    else:
        log("  ✗ ошибка загрузки")

    return state


# ─── Точка входа ─────────────────────────────────────────────────────────────

def run(monthly: bool = False):
    mode = "monthly" if monthly else "weekly"
    log(f"=== NLM updater запущен ({mode}) ===")

    state = load_state()

    # Еженедельные задачи
    state = update_telegram(state)
    state = update_youtube(state)
    state = update_diary_summaries(state)

    # Ежемесячная задача
    if monthly:
        state = update_diary_raw_monthly(state)

    save_state(state)
    log(f"=== NLM updater завершён ({mode}) ===")


if __name__ == "__main__":
    monthly_mode = "--monthly" in sys.argv
    run(monthly=monthly_mode)
