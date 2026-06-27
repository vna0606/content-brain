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

## Параллельный поток: события/инфоповоды (events_analyzer.py)

Отдельный скрипт `events_analyzer.py` — не смыслы, а конкретные факты из жизни
(увольнение, решение, начало/завершение чего-то). Источник тот же дневник,
но критерий обратный: то, что `MEANINGS_SYSTEM` явно исключает ("факт без позиции").

Пишет в отдельную таблицу `cb_events` (не `cb_ideas`):
```
id             INTEGER PK
title          TEXT   -- короткое название события
description    TEXT   -- что произошло, конкретно, без выводов
source_entries TEXT   -- JSON: список дат-источников
status         TEXT   -- 'new' | 'shown' | 'used' | 'dismissed'
created_at     TEXT   -- ISO 8601
```

Читается ботом (03-bot) через команду `/events` — отдельный флоу без суть/линзы,
сразу к подбору форматов подачи. Запуск: `python events_analyzer.py`.

### Тестовый вариант: события со стратегией (events_strategy_analyzer.py)

Та же задача, но с подключённой `strategy.md` (через `load_strategy()` из `prompts.py`) —
вычленяются только инфоповоды, которые подходят автору по теме/тону канала.
Пишет в отдельную таблицу `cb_events_strategy` (та же схема, что у `cb_events`,
не пересекается с лёгкой версией). Читается ботом через `/events_strategy`.
Запуск: `python events_strategy_analyzer.py`.

Цель — сравнить качество с лёгкой версией (`events_analyzer.py`) на практике;
обе версии временно живут параллельно, пока не станет ясно, какая остаётся основной.

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
python analyzer.py                   # смыслы → идеи (cb_ideas)
python events_analyzer.py            # события/инфоповоды, лёгкий (cb_events)
python events_strategy_analyzer.py   # события/инфоповоды, со стратегией (cb_events_strategy)
```

## Что НЕ делает этот этап

- НЕ индексирует контент (это 01-knowledge-base)
- НЕ работает с Telegram-ботом (это 03-bot)
- НЕ генерирует финальные посты — только идеи (тезисы)
- НЕ публикует ничего в Telegram
- НЕ меняет статус идей — только создаёт новые со статусом 'new'
