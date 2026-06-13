# 01-knowledge-base — контекст этапа

## Цель

Индексация контента из двух источников (mood-diary + Telegram-канал)
в векторную базу данных cb_vectors для последующего семантического поиска.

## Зона ответственности

ЧИТАЕТ:
- Turso mood-diary: таблица `entries` (TURSO_URL / TURSO_TOKEN)
- Telegram-канал: посты через Telethon MTProto (TG_API_ID / TG_API_HASH)

ПИШЕТ:
- Turso content-brain: таблицы `cb_vectors`, `cb_published_posts`

## Входящий контракт

Нет входящего контракта от других этапов проекта.
Источники — внешние: mood-diary Turso и Telegram MTProto.

## Исходящий контракт

Таблица `cb_vectors` в CONTENT_BRAIN_TURSO:
```
id          INTEGER PK AUTOINCREMENT
source_type TEXT  -- 'diary' | 'telegram_post'
source_id   TEXT  -- уникальный ID в источнике (entry.id или message.id)
content_chunk TEXT -- текстовый фрагмент
embedding   BLOB  -- float32[] сериализованный через numpy.tobytes()
created_at  TEXT  -- ISO 8601
```

Таблица `cb_published_posts` в CONTENT_BRAIN_TURSO:
```
id            INTEGER PK AUTOINCREMENT
post_text     TEXT
published_at  TEXT  -- ISO 8601
tg_message_id INTEGER
topics        TEXT  -- JSON-список тем
```

## Технический стек

- Python 3.11+
- `anthropic` — Claude API для генерации embeddings
- `libsql-client` — Turso/libSQL клиент
- `telethon` — MTProto для чтения Telegram-канала
- `numpy` — работа с векторами
- `python-dotenv` — переменные окружения

## Как запустить

```bash
cd 01-knowledge-base/
cp .env.example .env  # заполнить переменные
pip install -r requirements.txt
python main.py --full    # полная индексация (все записи и посты)
python main.py --update  # только новое за последние 7 дней
```

## Что НЕ делает этот этап

- НЕ анализирует контент и НЕ генерирует идеи (это 02-analyzer)
- НЕ работает с Telegram-ботом (это 03-bot)
- НЕ читает таблицы cb_ideas или cb_published_posts как источник
- НЕ удаляет записи из mood-diary — только читает
- НЕ публикует ничего в Telegram-канал

## Важные замечания

- Telethon сессия: можно скопировать из `outreach-system/01-parser/telegram_session.session`
  (номер +79111068325, уже авторизован — повторный вход не нужен)
- Дедупликация по source_id: перед вставкой проверять `SELECT id FROM cb_vectors WHERE source_id = ?`
- Embeddings: если Claude API не поддерживает embed напрямую — использовать text-embedding через
  простой cosine similarity на стороне 02-analyzer
