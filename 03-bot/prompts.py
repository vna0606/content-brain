"""
prompts.py — шаблоны для генерации финального поста через Claude.
Тон голоса берётся из tone_analysis.md (автоматический анализ постов).
Стратегия — из strategy.md (заполняется вручную, опционально).
"""

import os
from pathlib import Path

_DIR = Path(__file__).parent
_tone_cache: str | None = None
_strategy_cache: str | None = None


def load_tone_guide() -> str:
    global _tone_cache
    if _tone_cache is None:
        p = _DIR / "tone_analysis.md"
        _tone_cache = p.read_text(encoding="utf-8") if p.exists() else ""
    return _tone_cache


def load_strategy() -> str:
    global _strategy_cache
    if _strategy_cache is None:
        p = _DIR / "strategy.md"
        try:
            text = p.read_text(encoding="utf-8")
            # Считаем незаполненным если только шаблон (нет реального текста)
            _strategy_cache = text if "[Заполнить]" not in text else ""
        except FileNotFoundError:
            _strategy_cache = ""
    return _strategy_cache


def get_post_writer_system() -> str:
    """Системный промпт: тон голоса + строгий запрет на фантазию."""
    tone = load_tone_guide()
    strategy = load_strategy()

    system = """Ты помогаешь автору превратить его сырые мысли из дневника в пост для Telegram-канала.

ГЛАВНОЕ ПРАВИЛО — самое важное:
Используй ТОЛЬКО то, что автор реально написал или сказал в своих записях.
НЕ придумывай сцены, детали, эмоции, диалоги или ситуации которых нет в исходных текстах.
НЕ дописывай "как было бы логично". Если чего-то нет в записях — этого нет в посте.
Твоя работа: переформатировать и переструктурировать слова автора, а не сочинить что-то новое.
"""

    if tone.strip():
        system += f"\n\nСТИЛЬ И ТОН ГОЛОСА АВТОРА (строго соблюдай):\n{tone}"

    if strategy.strip():
        system += f"\n\nСТРАТЕГИЯ КАНАЛА:\n{strategy}"

    return system


def build_post_prompt(
    title: str,
    thesis: str,
    raw_diary_messages: list[str],
    social_chunks: list[str],
    gemini_messages: list[tuple[str, str]] | None = None,
    nlm_quotes: str = "",
) -> str:
    """
    Промпт для генерации поста.

    raw_diary_messages — реальные слова автора из дневника (не AI-резюме).
    Это первичный источник. Тезис — лишь подсказка о чём пост.
    """
    prompt = f"Идея для поста: {title}\n\nСуть идеи (используй как ориентир, не как источник деталей):\n{thesis}\n"

    if nlm_quotes and "ничего конкретного нет" not in nlm_quotes.lower() and len(nlm_quotes) > 100:
        prompt += f"""
ДОСЛОВНЫЕ СЛОВА АВТОРА ИЗ ПЕРЕПИСОК (главный источник — используй именно их):

{nlm_quotes}

Это прямые цитаты. Строй пост вокруг них.
"""

    if raw_diary_messages:
        raw_text = "\n\n---\n".join(raw_diary_messages[:6])
        prompt += f"""
СЫРЫЕ ЗАПИСИ АВТОРА (именно из этих слов строй пост):

---
{raw_text}
---

Эти записи — единственный источник деталей и эмоций для поста.
"""
    else:
        prompt += "\n[Сырых записей нет — пиши строго по тезису, без домысливания деталей]\n"

    if gemini_messages:
        # Группируем по названию переписки
        by_conv: dict[str, list[str]] = {}
        for title_conv, content in gemini_messages:
            by_conv.setdefault(title_conv, []).append(content)

        gemini_text = ""
        for conv_title, msgs in by_conv.items():
            gemini_text += f"\n[Переписка: «{conv_title}»]\n"
            gemini_text += "\n".join(f"— {m}" for m in msgs)
            gemini_text += "\n"

        prompt += f"""
МОИ СЫРЫЕ МЫСЛИ ИЗ ПЕРЕПИСОК С GEMINI (используй если есть что-то про тему поста):
{gemini_text}
Важно: используй только то, что реально относится к теме. Не тяни всё подряд.
"""

    if social_chunks:
        social_text = "\n\n---\n".join(social_chunks[:2])
        prompt += f"\nСвязанные старые посты автора (для понимания контекста, не для копирования):\n---\n{social_text}\n---\n"

    prompt += "\nНапиши готовый пост. Только текст поста, без предисловий и объяснений."

    return prompt
