# content-brain — корневой контекст

## Цель проекта

Превращать записи личного дневника (mood-diary) в идеи и готовые посты для Telegram-канала @nikbase.
Двухэтапный анализ: сначала Claude извлекает смыслы из записей последних 7 дней,
потом NotebookLM находит предысторию, потом Claude пишет пост в тон голоса автора.

## Архитектура — поток данных

```
mood-diary Turso                 Telegram @nikbase (Telethon)
(entries + messages)             посты + голосовые → Groq транскрипция
         │                                    │
         └──────────────┬─────────────────────┘
                        ▼
              ┌─────────────────────┐
              │  01-knowledge-base  │
              │  python main.py     │
              │  --full / --update  │
              └──────────┬──────────┘
                         │ cb_diary_vectors
                         │ cb_social_posts + cb_social_vectors
                         ▼
              ┌─────────────────────┐        ┌──────────────────────┐
              │    02-analyzer      │◄───────│  NotebookLM (nlm)    │
              │  analyzer.py        │        │  ноутбук content-    │
              │                     │        │  brain: 5 источников │
              │  1. Claude → смыслы │        └──────────────────────┘
              │  2. NLM → история   │
              │  3. Claude → идеи   │
              └──────────┬──────────┘
                         │ cb_ideas (status='new')
                         ▼
              ┌─────────────────────┐
              │      03-bot         │
              │  aiogram 3.x        │
              │  /ideas → выбор     │
              │  → Claude пост      │
              │  по strategy.md     │
              └─────────────────────┘
```

## Контракты между этапами

| От | К | Формат | Детали |
|----|---|--------|--------|
| 01-knowledge-base | 02-analyzer | Turso `cb_diary_vectors` | source_id, content_chunk, embedding BLOB |
| 01-knowledge-base | 02-analyzer | Turso `cb_social_posts/vectors` | source_type, content, url |
| 02-analyzer | 03-bot | Turso `cb_ideas` | title, thesis, status='new' |
| 03-bot | экосистема | Turso `cb_published_posts` | post_text, published_at |
| 03-bot | @nikbase | `channel_post` handler | автосбор новых постов в cb_social_posts |

## Схемы таблиц Turso (content-brain DB)

`TURSO_CONTENT_BRAIN_URL=libsql://content-brain-nikves.aws-eu-west-1.turso.io`

### cb_diary_vectors
```sql
id INTEGER PK, source_id TEXT UNIQUE,  -- 'entry_{date}' | 'msg_{id}'
content_chunk TEXT, embedding BLOB,    -- float32[] numpy SHA-256-seeded
entry_date TEXT, created_at TEXT
```

### cb_social_posts
```sql
id INTEGER PK, source_type TEXT,       -- 'telegram' | 'youtube'
source_id TEXT UNIQUE,                 -- 'tg_{id}' | 'yt_{id}'
content TEXT, url TEXT, published_at TEXT
```

### cb_social_vectors
```sql
id INTEGER PK, social_post_id INTEGER, source_id TEXT UNIQUE,
content_chunk TEXT, embedding BLOB, created_at TEXT
```

### cb_ideas (контракт 02 → 03)
```sql
id INTEGER PK, title TEXT, thesis TEXT,
relevant_history TEXT,  -- JSON source_ids
relevant_social TEXT,   -- JSON source_ids
source_entries TEXT,    -- JSON ['2026-05-30', ...]
status TEXT DEFAULT 'new',  -- 'new'|'shown'|'used'|'dismissed'
created_at TEXT
```

### cb_published_posts
```sql
id INTEGER PK, post_text TEXT,
published_at TEXT, tg_message_id INTEGER, topics TEXT
```

## Mood-diary схема (read-only)

`TURSO_MOOD_URL=libsql://mood-diary-nikves.aws-eu-west-1.turso.io`

```
entries  — date, status ('completed'/NULL), mood_score, energy_level,
           tags (JSON), ai_summary, day_vibe (JSON), manual_mood, manual_energy
messages — id, entry_date, role ('user'|'gemini_import'), content, created_at
```

## Переменные окружения

| Переменная | Этап | Значение |
|-----------|------|---------|
| `TURSO_MOOD_URL` / `TURSO_MOOD_TOKEN` | 01, 02 | mood-diary (read-only) |
| `TURSO_CONTENT_BRAIN_URL` / `TURSO_CONTENT_BRAIN_TOKEN` | 01, 02, 03 | content-brain DB |
| `NLM_NOTEBOOK_ID` | 02 | `1eb35d64-3e12-4dc9-8043-f65d703d6281` |
| `CONTENT_BRAIN_BOT_TOKEN` | 03 | токен бота |
| `TG_API_ID` / `TG_API_HASH` | 01 | кампейн-аккаунт: `33361321` / `67a7d...` |
| `TG_CHANNEL_USERNAME` | 01, 03 | `nikbase` |
| `GROQ_API_KEY` | 01 | транскрипция голосовых |
| `TELEGRAM_USER_ID` | 03 | `5950805456` |

## Ключевые технические решения

**libsql_experimental** (не libsql_client) — sync API с local replica:
```python
conn = libsql.connect("replica.db", sync_url=URL, auth_token=TOKEN)
conn.sync()   # pull
conn.commit(); conn.sync()  # push после записи
```

**Claude CLI** (не Anthropic SDK) — без API-ключа, через подписку:
```python
subprocess.run(["claude", "-p", prompt, "--model", "claude-sonnet-4-6"],
               capture_output=True, text=True, env={**os.environ, "HOME": ...})
```

**NotebookLM** — семантический поиск через nlm CLI:
```python
subprocess.run([NLM_BIN, "notebook", "query", NOTEBOOK_ID, question], ...)
# Ответ: JSON {"value": {"answer": "..."}}
```

**Telethon два прохода** — сначала транскрибируем голосовые (без DB), потом пишем в свежее соединение. Иначе Turso-стрим падает по таймауту при длинной транскрипции.

**Анализатор двухэтапный:**
1. Claude → смыслы (тип, стратегическое соответствие, нужна ли предыстория)
2. Для `needs_history=true` → `nlm notebook query` с конкретным вопросом
3. Claude → 3-5 идей постов на основе смыслов + исторического контекста

## NotebookLM — авторизация

Cookies истекают, нужно периодически переавторизовываться:

1. На MacBook запустить Chrome: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-nlm`
2. SSH-туннель: `ssh -R 18800:localhost:9222 ubuntu@<SERVER_IP>`
3. На сервере: `~/.local/bin/nlm login --provider openclaw --cdp-url http://127.0.0.1:18800`

## NotebookLM — два ноутбука

| Ноутбук | ID | Назначение |
|---------|----|-----------|
| `content-brain` (архив) | `1eb35d64-3e12-4dc9-8043-f65d703d6281` | Весь исторический контекст |
| `content-brain-fresh` | `6ed2bc91-8fb5-4eca-a881-324f6db90db1` | Свежие Gemini-переписки (вручную) |

**Как работает анализатор с ноутбуками:**
1. Запрашивает `content-brain-fresh` → извлекает смыслы из свежих Gemini-переписок (первичный источник)
2. Запрашивает `content-brain` (архив) → ищет историческую глубину по темам дневника

**Управление `content-brain-fresh`:**
- Добавляй свежие переписки с Gemini вручную через notebooklm.google.com
- Когда переписка устарела — удали из fresh (исторический контекст подхватит архив)

## NotebookLM — автообновление

Архивный ноутбук обновляется **автоматически**:

```
Воскресенье 03:00 MSK → nlm_updater.py
  - Новые Telegram-посты (батч "Telegram @nikbase YYYY-MM" если ≥5 постов)
  - Новые YouTube-ролики (батч "YouTube-ролики YYYY-MM")
  - Резюме дней → заменяется полностью

1-е число 03:00 MSK → nlm_updater.py --monthly
  - Всё выше + сырые записи дневника за прошлый месяц
    ("Дневник — сырые записи YYYY-MM") → 12 источников в год
```

Лог: `/home/ubuntu/content-brain-nlm-update.log`
Состояние: `01-knowledge-base/.nlm_update_state.json`

## Субагенты проекта

| Агент | Файл | Зона ответственности |
|-------|------|---------------------|
| content-brain-kb-expert | `.claude/agents/content-brain-kb-expert.md` | `01-knowledge-base/` |
| content-brain-analyzer-expert | `.claude/agents/content-brain-analyzer-expert.md` | `02-analyzer/` |
| content-brain-bot-expert | `.claude/agents/content-brain-bot-expert.md` | `03-bot/` |

## Что нужно сделать вручную

- [ ] Заполнить `03-bot/strategy.md` — позиционирование, тон голоса, примеры постов
- [ ] Добавить бота в @nikbase как администратора (для channel_monitor)
- [ ] Запустить YouTube-импорт с MacBook (`scripts/youtube_import.py`)
- [ ] Настроить cron или systemd для периодического запуска analyzer.py
