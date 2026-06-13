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

## Файл strategy.md

Пользователь заполняет `strategy.md` вручную.
Бот читает его при запуске и использует как системный промпт
для генерации постов. Без заполненного strategy.md
генерация будет работать с базовым промптом.
