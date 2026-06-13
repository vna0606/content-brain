"""
extract_tone.py — анализ стиля постов из cb_social_posts через Claude.

Что делает:
  - Читает все Telegram-посты из Turso
  - Отправляет Claude для анализа тона голоса и стиля
  - Сохраняет результат в tone_analysis.md

Запуск:
  cd 03-bot/
  python extract_tone.py
"""

import os
import subprocess
import sys
from pathlib import Path

import libsql_experimental as libsql
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

CLAUDE_MODEL = "claude-sonnet-4-6"
OUTPUT_FILE = Path(__file__).parent / "tone_analysis.md"
REPLICA_DB = Path(__file__).parent / "cb_bot_replica.db"

MAX_POSTS = 100
MAX_CHARS = 80_000


def get_posts() -> list[dict]:
    conn = libsql.connect(
        str(REPLICA_DB),
        sync_url=os.environ["TURSO_CONTENT_BRAIN_URL"],
        auth_token=os.environ["TURSO_CONTENT_BRAIN_TOKEN"],
    )
    conn.sync()
    cur = conn.execute(
        "SELECT content, url, published_at FROM cb_social_posts "
        "WHERE source_type = 'telegram' "
        "ORDER BY published_at DESC LIMIT ?",
        (MAX_POSTS,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"content": r[0], "url": r[1], "published_at": r[2]} for r in rows if r[0]]


def build_prompt(posts: list[dict]) -> str:
    # Склеить посты, обрезав по общему лимиту символов
    posts_text = ""
    for i, p in enumerate(posts, 1):
        date = (p["published_at"] or "")[:10]
        block = f"=== Пост {i} ({date}) ===\n{p['content']}\n\n"
        if len(posts_text) + len(block) > MAX_CHARS:
            print(f"  Достигнут лимит символов, взято {i - 1} из {len(posts)} постов")
            break
        posts_text += block

    return f"""Проанализируй стиль автора Telegram-канала и выдай готовый документ.

ВАЖНО: Отвечай только самим документом. Никаких предисловий, никаких "вот что я сделал", никаких объяснений. Начни сразу с первого раздела.

Ниже {len(posts)} постов из канала. Изучи их и напиши документ точно в таком формате:

---

## Общие характеристики стиля

[3-5 предложений: тон, дистанция с читателем, серьёзность, личное vs экспертное]

## Как начинаются посты (хуки)

[Паттерны первых предложений. 3-5 конкретных примеров первых строк из постов — цитатами]

## Как заканчиваются посты

[Паттерны финальных предложений. Конкретные примеры концовок — цитатами]

## Структура и ритм

[Типичная длина (слов, абзацев), длина предложений, как разбиты абзацы, когда используются списки]

## Повторяющиеся речевые паттерны

[Характерные обороты и конструкции — конкретные фразы из постов. Что делает текст узнаваемым]

## Чего нет и не должно быть

[Что автор избегает — с примерами того что было бы "не в стиле"]

## Темы и угол подачи

[О чём пишет и под каким углом — личный опыт, рефлексия, наблюдения, практика]

## Инструкция для AI

При написании постов для этого автора:
- [правило 1]
- [правило 2]
- [правило 3]
...и так далее, конкретные правила из анализа выше

## Три хороших примера постов

[Выбери 3 поста которые лучше всего передают стиль автора. Вставь полные тексты.]

---

ПОСТЫ ДЛЯ АНАЛИЗА:

{posts_text}
"""


def call_claude(prompt: str) -> str:
    import tempfile
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    print("  Вызов Claude (может занять 1-2 минуты)...")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(prompt)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["claude", "-p", f"@{tmp_path}", "--model", CLAUDE_MODEL],
            capture_output=True, text=True, env=env, timeout=300,
        )
        if result.returncode != 0 and result.stderr:
            print(f"  [claude] stderr: {result.stderr[:300]}")
        return result.stdout.strip()
    finally:
        os.unlink(tmp_path)


def main():
    print("[extract_tone] Загрузка постов из Turso...")
    posts = get_posts()
    if not posts:
        print("Нет постов в cb_social_posts. Сначала запусти индексацию (01-knowledge-base/main.py).")
        sys.exit(1)

    print(f"[extract_tone] Найдено постов: {len(posts)}")

    prompt = build_prompt(posts)
    print(f"[extract_tone] Размер промпта: {len(prompt):,} символов")

    analysis = call_claude(prompt)
    if not analysis:
        print("Claude не вернул результат.")
        sys.exit(1)

    header = f"# Анализ тона голоса\n\n_Сгенерировано на основе {len(posts)} постов из @nikbase_\n\n"
    OUTPUT_FILE.write_text(header + analysis, encoding="utf-8")

    print(f"\n[extract_tone] Готово! Результат: {OUTPUT_FILE}")
    print("Следующий шаг: просмотри tone_analysis.md и перенеси ключевые моменты в strategy.md")


if __name__ == "__main__":
    main()
