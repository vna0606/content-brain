# 02-analyzer — контекст этапа

## Цель

Анализировать записи дневника за последние 7 дней, находить релевантный
исторический контент через векторный поиск в cb_vectors и генерировать
3-5 идей для постов в формате IdeaDraft. Сохранять идеи в cb_ideas.

## Зона ответственности

ЧИТАЕТ:
- Turso content-brain: таблица `cb_vectors` (все векторы)
- Turso mood-diary: таблица `entries` за последние 7 дней

ПИШЕТ:
- Turso content-brain: таблица `cb_ideas`

## Входящий контракт

Таблица `cb_vectors` (заполненная этапом 01-knowledge-base):
```
source_type TEXT  -- 'diary' | 'telegram_post'
source_id   TEXT
content_chunk TEXT
embedding   BLOB  -- float32[] через numpy
created_at  TEXT
```

## Исходящий контракт

Таблица `cb_ideas` (читается этапом 03-bot):
```
id               INTEGER PK
title            TEXT   -- короткий заголовок идеи
thesis           TEXT   -- развёрнутый тезис/суть идеи
relevant_history TEXT   -- JSON: список source_id релевантного контента
source_entries   TEXT   -- JSON: список ID записей дневника-источников
status           TEXT   -- 'new' | 'shown' | 'used' | 'dismissed'
created_at       TEXT   -- ISO 8601
```

## Технический стек

- Python 3.11+
- `anthropic` — Claude API для генерации идей
- `libsql-client` — Turso
- `numpy` — косинусное сходство для векторного поиска
- `python-dotenv`

## Как запустить

```bash
cd 02-analyzer/
pip install -r requirements.txt
# .env берётся из ../  или создаётся локальный
python analyzer.py
```

## Что НЕ делает этот этап

- НЕ индексирует контент (это 01-knowledge-base)
- НЕ работает с Telegram-ботом (это 03-bot)
- НЕ генерирует финальные посты — только идеи (тезисы)
- НЕ публикует ничего в Telegram
- НЕ меняет статус идей — только создаёт новые со статусом 'new'
