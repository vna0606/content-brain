"""
analyzer.py — двухэтапный анализ дневника → идеи для постов.

Этап 1: Claude извлекает смыслы из записей последних 7 дней
         (с проверкой по бренд-стратегии)
Этап 2: Для смыслов с предысторией → запрос в NotebookLM
         Claude генерирует идеи постов
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "01-knowledge-base"))

from db import get_mood_db, get_cb_db, commit
from models import IdeaDraft
from prompts import (
    load_strategy,
    FRESH_MEANINGS_QUERY,
    build_meanings_prompt,
    build_gemini_query,
    build_ideas_prompt,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

CLAUDE_MODEL = "claude-sonnet-4-6"
NOTEBOOK_ID = os.environ.get("NLM_NOTEBOOK_ID", "1eb35d64-3e12-4dc9-8043-f65d703d6281")
FRESH_NOTEBOOK_ID = os.environ.get("NLM_FRESH_NOTEBOOK_ID", "6ed2bc91-8fb5-4eca-a881-324f6db90db1")
NLM_BIN = os.path.expanduser("~/.local/bin/nlm")


# ─── CLI вызовы ───────────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> str:
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", CLAUDE_MODEL],
        capture_output=True, text=True, env=env, timeout=180,
    )
    if result.returncode != 0 and result.stderr:
        print(f"  [claude] stderr: {result.stderr[:200]}")
    return result.stdout.strip()


def _call_notebooklm(question: str, notebook_id: str = None) -> str:
    nid = notebook_id or NOTEBOOK_ID
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    result = subprocess.run(
        [NLM_BIN, "notebook", "query", nid, question],
        capture_output=True, text=True, env=env, timeout=120,
    )
    if result.returncode != 0:
        print(f"  [nlm] ошибка: {result.stderr[:150]}")
        return ""
    try:
        data = json.loads(result.stdout)
        return data.get("value", {}).get("answer", "")
    except (json.JSONDecodeError, AttributeError):
        return result.stdout.strip()


def _parse_json(raw: str) -> list | None:
    """Распарсить JSON-массив из ответа Claude."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
    print(f"  [parse] не удалось распарсить JSON:\n{raw[:300]}")
    return None


# ─── Данные из БД ─────────────────────────────────────────────────────────────

def get_recent_diary_context(mood_db, days: int = 7) -> dict:
    """Получить резюме дней + сырые сообщения за последние N дней."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    cur = mood_db.execute(
        "SELECT date, ai_summary, tags, mood_score, energy_level FROM entries "
        "WHERE status = 'completed' AND ai_summary IS NOT NULL AND date >= ? "
        "ORDER BY date DESC",
        (cutoff,),
    )
    summaries = [
        {
            "date": r[0],
            "ai_summary": r[1],
            "tags": json.loads(r[2] or "[]"),
            "mood_score": r[3],
            "energy_level": r[4],
        }
        for r in cur.fetchall()
    ]

    cur = mood_db.execute(
        "SELECT id, entry_date, content FROM messages "
        "WHERE role = 'user' AND entry_date >= ? "
        "ORDER BY created_at DESC LIMIT 80",
        (cutoff,),
    )
    raw_messages = [
        {"id": r[0], "date": r[1], "content": r[2]}
        for r in cur.fetchall()
        if r[2] and r[2].strip()
    ]

    return {"summaries": summaries, "raw_messages": raw_messages}


def save_ideas(cb_db, ideas: list[IdeaDraft]):
    for idea in ideas:
        row = idea.to_db_row()
        cb_db.execute(
            "INSERT INTO cb_ideas "
            "(title, thesis, relevant_history, relevant_social, source_entries, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["title"], row["thesis"], row["relevant_history"],
             row.get("relevant_social", "[]"), row["source_entries"],
             row["status"], row["created_at"]),
        )
    commit(cb_db)
    print(f"[analyzer] Сохранено идей: {len(ideas)}")


# ─── Основная логика ──────────────────────────────────────────────────────────

def run(no_nlm: bool = False):
    print("[analyzer] Загрузка стратегии...")
    strategy = load_strategy()
    if strategy:
        print(f"[analyzer] Стратегия загружена ({len(strategy)} символов)")
    else:
        print("[analyzer] Стратегия не заполнена — работаем без неё")

    if no_nlm:
        print("[analyzer] Режим --no-nlm: NotebookLM отключён")

    print("[analyzer] Получение записей за 7 дней...")
    mood_db = get_mood_db()
    try:
        context = get_recent_diary_context(mood_db, days=7)
    finally:
        mood_db.close()

    if not context["summaries"] and not context["raw_messages"]:
        print("[analyzer] Нет записей за последние 7 дней. Завершение.")
        return

    print(f"[analyzer] Резюме: {len(context['summaries'])} дней, "
          f"сообщений: {len(context['raw_messages'])}")

    # ── Этап 0: смыслы из свежих Gemini-переписок (NLM fresh) ────────────────
    fresh_meanings = ""
    if not no_nlm:
        print("\n[этап 0] Извлечение смыслов из свежих Gemini-переписок...")
        fresh_answer = _call_notebooklm(FRESH_MEANINGS_QUERY, notebook_id=FRESH_NOTEBOOK_ID)
        if fresh_answer and "нет источников" not in fresh_answer.lower() and len(fresh_answer) > 200:
            fresh_meanings = fresh_answer
            print(f"[этап 0] Получено смыслов из Gemini ({len(fresh_meanings)} символов)")
        else:
            print("[этап 0] Свежих Gemini-переписок нет — пропускаем")
    else:
        print("\n[этап 0] Пропуск (--no-nlm)")

    # ── Этап 1: извлечение смыслов ────────────────────────────────────────────
    print("\n[этап 1] Извлечение смыслов через Claude...")
    meanings_prompt = build_meanings_prompt(context, strategy)
    meanings_raw = _call_claude(meanings_prompt)
    meanings = _parse_json(meanings_raw)

    if not meanings:
        print("[этап 1] Не удалось извлечь смыслы. Завершение.")
        return

    print(f"[этап 1] Извлечено смыслов: {len(meanings)}")
    for i, m in enumerate(meanings, 1):
        needs = "→ нужна предыстория" if m.get("needs_history") else ""
        print(f"  {i}. [{m.get('type', '?')}] {m['meaning'][:80]} {needs}")

    # ── Этап 1.5: инсайты из переписок с Gemini ──────────────────────────────
    gemini_insights = ""
    if not no_nlm:
        gemini_query = build_gemini_query(context, meanings)
        print("\n[этап 1.5] Запрос инсайтов из переписок с Gemini...")
        gemini_insights = _call_notebooklm(gemini_query)
        if gemini_insights:
            print(f"[этап 1.5] Получено ({len(gemini_insights)} символов)")
        else:
            print("[этап 1.5] Нет переписок с Gemini в NotebookLM или ответ пустой")
    else:
        print("\n[этап 1.5] Пропуск (--no-nlm)")

    # ── Этап 2: исторический контекст из NotebookLM ───────────────────────────
    historical_contexts: dict[str, str] = {}
    if not no_nlm:
        needs_history = [m for m in meanings if m.get("needs_history") and m.get("history_query")]
        if needs_history:
            print(f"\n[этап 2] Запрос истории в NotebookLM для {len(needs_history)} смыслов...")
            for m in needs_history:
                query = m["history_query"]
                print(f"  → «{query[:70]}»")
                answer = _call_notebooklm(query)
                if answer:
                    historical_contexts[query] = answer
                    print(f"    ✓ ответ получен ({len(answer)} символов)")
                else:
                    print(f"    ✗ ответ не получен")
        else:
            print("\n[этап 2] Ни один смысл не требует поиска предыстории")
    else:
        print("\n[этап 2] Пропуск (--no-nlm)")

    # ── Этап 3: генерация идей постов ─────────────────────────────────────────
    print("\n[этап 3] Генерация идей постов через Claude...")
    ideas_prompt = build_ideas_prompt(context, meanings, historical_contexts, strategy, gemini_insights, fresh_meanings)
    ideas_raw = _call_claude(ideas_prompt)
    ideas_data = _parse_json(ideas_raw)

    if not ideas_data:
        print("[этап 3] Не удалось сгенерировать идеи.")
        return

    ideas = [
        IdeaDraft(
            title=item.get("title", "Без названия"),
            thesis=item.get("thesis", ""),
            source_entries=item.get("source_dates", []),
            relevant_history=[],
            relevant_social=[],
        )
        for item in ideas_data
        if isinstance(item, dict)
    ]

    print(f"[этап 3] Сгенерировано идей: {len(ideas)}")

    if ideas:
        cb_db = get_cb_db()
        try:
            save_ideas(cb_db, ideas)
        finally:
            cb_db.close()

    print("\n[analyzer] Готово.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-nlm", action="store_true", help="Пропустить все запросы к NotebookLM")
    args = parser.parse_args()
    run(no_nlm=args.no_nlm)
