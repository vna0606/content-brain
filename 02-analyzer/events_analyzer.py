"""
events_analyzer.py — отдельный анализатор: вычленяет события/инфоповоды из дневника.

Параллельный поток к analyzer.py — не смыслы, а конкретные факты из жизни,
из которых можно сделать лёгкий контент (без суть/линзы). Результат — таблица
cb_events, отдельная от cb_ideas.

Запуск: python events_analyzer.py
"""

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "01-knowledge-base"))

from db import get_mood_db, get_cb_db, init_schema, commit
from models import EventDraft
from analyzer import _call_claude, _parse_json, get_recent_diary_context
from events_prompts import build_events_prompt

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def save_events(cb_db, events: list[EventDraft]):
    for event in events:
        row = event.to_db_row()
        cb_db.execute(
            "INSERT INTO cb_events (title, description, source_entries, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["title"], row["description"], row["source_entries"], row["status"], row["created_at"]),
        )
    commit(cb_db)
    print(f"[events_analyzer] Сохранено событий: {len(events)}")


def run():
    print("[events_analyzer] Получение записей за 7 дней...")
    mood_db = get_mood_db()
    try:
        context = get_recent_diary_context(mood_db, days=7)
    finally:
        mood_db.close()

    if not context["summaries"] and not context["raw_messages"]:
        print("[events_analyzer] Нет записей за последние 7 дней. Завершение.")
        return

    print(f"[events_analyzer] Резюме: {len(context['summaries'])} дней, "
          f"сообщений: {len(context['raw_messages'])}")

    print("\n[events_analyzer] Извлечение событий через Claude...")
    prompt = build_events_prompt(context)
    raw = _call_claude(prompt)
    events_data = _parse_json(raw)

    if not events_data:
        print("[events_analyzer] Не удалось извлечь события.")
        return

    events = [
        EventDraft(
            title=item.get("title", "Без названия"),
            description=item.get("description", ""),
            source_entries=item.get("source_dates", []),
        )
        for item in events_data
        if isinstance(item, dict)
    ]

    print(f"[events_analyzer] Извлечено событий: {len(events)}")
    for i, e in enumerate(events, 1):
        print(f"  {i}. {e.title}")

    if events:
        cb_db = get_cb_db()
        try:
            init_schema(cb_db)  # на случай если cb_events ещё не создана
            save_events(cb_db, events)
        finally:
            cb_db.close()

    print("\n[events_analyzer] Готово.")


if __name__ == "__main__":
    run()
