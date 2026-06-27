# 03-bot — контекст этапа

## Цель

Telegram-бот, через который пользователь видит идеи для постов,
выбирает понравившуюся, генерирует готовый пост в своём тоне голоса
и публикует его (или сохраняет черновик).

## Зона ответственности

ЧИТАЕТ:
- Turso content-brain: таблица `cb_ideas` (идеи со статусом 'new')
- Turso content-brain: таблица `cb_vectors` (для контекста при генерации поста)
- Файл `strategy.md` — бренд-стратегия и тон голоса

ПИШЕТ:
- Turso content-brain: таблица `cb_ideas` (обновляет status: shown/used/dismissed)
- Turso content-brain: таблица `cb_published_posts` (после публикации)

## Входящий контракт

Таблица `cb_ideas`:
```
id               INTEGER PK
title            TEXT
thesis           TEXT
relevant_history TEXT  -- JSON
source_entries   TEXT  -- JSON
status           TEXT  -- 'new' | 'shown' | 'used' | 'dismissed'
created_at       TEXT
```

## Исходящий контракт

Таблица `cb_published_posts` (после публикации):
```
id            INTEGER PK
post_text     TEXT
published_at  TEXT
tg_message_id INTEGER  -- если бот публикует в канал
topics        TEXT     -- JSON
```

## Технический стек

- Python 3.11+
- `aiogram==3.x` — Telegram Bot API
- `anthropic` — Claude API для генерации полного поста
- `libsql-client` — Turso
- `python-dotenv`

## Как запустить

```bash
cd 03-bot/
cp .env.example .env  # заполнить CONTENT_BRAIN_BOT_TOKEN и др.
pip install -r requirements.txt
python main.py
```

## Что НЕ делает этот этап

- НЕ индексирует контент (это 01-knowledge-base)
- НЕ генерирует идеи автоматически (это 02-analyzer)
- НЕ читает напрямую из mood-diary Turso (только через cb_ideas)
- НЕ изменяет таблицы cb_vectors или cb_published_posts напрямую
  (только cb_ideas.status и cb_published_posts INSERT)
- НЕ публикует посты без подтверждения пользователя

## Параллельный флоу: события/инфоповоды (handlers/events.py)

Отдельная команда `/events` — лёгкий контент без суть/линзы/стратегии.
Источник: таблица `cb_events` (заполняется `02-analyzer/events_analyzer.py`),
не пересекается с `cb_ideas`.

Входящий контракт `cb_events`:
```
id             INTEGER PK
title          TEXT
description    TEXT   -- что произошло, конкретно
source_entries TEXT   -- JSON
status         TEXT   -- 'new' | 'shown' | 'used' | 'dismissed'
created_at     TEXT
```

Флоу: событие → подбор форматов подачи (5-10 вариантов, по образцу
"один инфоповод → много форматов") → выбор формата → текстовый черновик →
улучшить/гуманизировать/перегенерировать. v1: только Telegram-текст,
без отдельных Reels/YouTube веток и без проверки по стратегии/линзе.

Промпт подбора форматов версионируется в `handlers/event_formats_prompts.py`
(V1, V2, ... + `EVENT_FORMATS_SYSTEM_ACTIVE`) — чтобы откатиться на предыдущую
формулировку, переключи `EVENT_FORMATS_SYSTEM_ACTIVE` на нужную версию.

## Тестовый параллельный флоу: события со стратегией (handlers/events_strategy.py)

Команда `/events_strategy` — тот же флоу, что и `/events`, но источник —
`cb_events_strategy` (заполняется `02-analyzer/events_strategy_analyzer.py`),
где инфоповоды вычленяются с учётом `strategy.md`. Полностью изолирован от `/events`:
своя таблица, свой набор callback-префиксов (`event_s:`, `gen_event_s_formats:` и т.д.),
свои state-словари (`_event_s_drafts`, `_pending_event_s_formats`, `_awaiting_event_s_feedback`).

Форматно-генерационная логика (подбор форматов, системный промпт черновика, построение
промпта) переиспользуется из `handlers/events.py` напрямую импортом — дублируется только
то, что обязано отличаться из-за разных таблиц и callback-неймспейсов.

Это сравнительный тест против лёгкой версии: если результат окажется лучше — останется
основным путём, `/events` — как быстрый запасной без сверки со стратегией.

## Файл strategy.md

Пользователь заполняет `strategy.md` вручную.
Бот читает его при запуске и использует как системный промпт
для генерации постов. Без заполненного strategy.md
генерация будет работать с базовым промптом.
