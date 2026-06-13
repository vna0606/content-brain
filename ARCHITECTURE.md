# content-brain — Архитектура

## Назначение

AI-система для создания контента Telegram-канала из личного дневника.
Читает записи mood-diary за последние 7 дней → извлекает смыслы → ищет предысторию через NotebookLM → предлагает идеи постов → пишет готовые посты в тон голоса автора.

## Стек

| Компонент | Технология |
|-----------|-----------|
| Язык | Python 3.11+ |
| БД-клиент | `libsql_experimental` (sync, local replica) |
| Telegram-бот | `aiogram 3.x` |
| Telegram MTProto | `telethon` (кампейн-аккаунт +79177386362) |
| AI (анализ/генерация) | Claude CLI (`claude -p ...`) — без API-ключа |
| AI (семантический поиск) | NotebookLM через `~/.local/bin/nlm` |
| Транскрипция голоса | Groq API whisper-large-v3 |
| Embeddings | SHA-256 хэш + numpy (для хранения; поиск — через NotebookLM) |

## Этапы

```
content-brain/
├── 01-knowledge-base/  — индексация дневника и канала в Turso
├── 02-analyzer/        — двухэтапный анализ → идеи постов
├── 03-bot/             — Telegram-бот: выбор идеи, генерация поста
└── scripts/            — утилиты (youtube_import.py для MacBook)
```

## Поток данных

```
mood-diary Turso (entries + messages)
         │
         ▼
┌─────────────────────┐     Telethon (кампейн +79177086362)
│  01-knowledge-base  │◄────────────────────────────────────
│  python main.py     │     читает @nikbase, транскрибирует
│  --full / --update  │     голосовые через Groq
└──────────┬──────────┘
           │ cb_diary_vectors, cb_social_posts,
           │ cb_social_vectors (в Turso content-brain)
           │
           ▼
┌─────────────────────┐     NotebookLM (nlm notebook query)
│  02-analyzer/       │◄────────────────────────────────────
│  analyzer.py        │     5 источников: дневник (3 части)
│                     │     + посты канала + резюме дней
│  Этап 1: Claude     │
│  → смыслы из 7 дней │
│  Этап 2: NLM        │
│  → предыстория      │
│  Этап 3: Claude     │
│  → идеи постов      │
└──────────┬──────────┘
           │ cb_ideas (status='new')
           │
           ▼
┌─────────────────────┐
│  03-bot/            │
│  aiogram polling    │
│                     │
│  /ideas → список    │
│  выбор → пост       │
│  /gextract → импорт │
│  Gemini-переписок   │
│  Claude CLI пишет   │
│  по strategy.md     │
└──────────┬──────────┘
           │ cb_published_posts + cb_ideas status='used'
           │
           ▼
    Telegram-канал @nikbase (пользователь публикует вручную)
```

## Turso БД

### mood-diary (только чтение)
`TURSO_MOOD_URL=libsql://mood-diary-nikves.aws-eu-west-1.turso.io`

| Таблица | Что содержит |
|---------|-------------|
| `entries` | Резюме дней: date, ai_summary, tags, mood_score, energy_level |
| `messages` | Сырые записи: entry_date, role ('user'/'gemini_import'), content |

### content-brain (чтение + запись)
`TURSO_CONTENT_BRAIN_URL=libsql://content-brain-nikves.aws-eu-west-1.turso.io`

| Таблица | Что содержит |
|---------|-------------|
| `cb_diary_vectors` | Векторы дневника: source_id ('entry_{date}' или 'msg_{id}'), embedding BLOB |
| `cb_social_posts` | Сырые посты канала и YouTube: source_type, content, url, published_at |
| `cb_social_vectors` | Векторы постов: social_post_id → embedding |
| `cb_ideas` | Идеи постов: title, thesis, status ('new'→'shown'→'used'/'dismissed') |
| `cb_published_posts` | Опубликованные посты: post_text, published_at, tg_message_id |

## NotebookLM

Ноутбук `content-brain` (ID: `1eb35d64-3e12-4dc9-8043-f65d703d6281`)

| Источник | Что содержит | Размер |
|----------|-------------|--------|
| Дневник — резюме дней | ai_summary всех дней | ~10 KB |
| Дневник — сырые записи (1/3) | сообщения пользователя | ~1.5 MB |
| Дневник — сырые записи (2/3) | сообщения пользователя | ~1.5 MB |
| Дневник — сырые записи (3/3) | сообщения пользователя | ~600 KB |
| Telegram-канал @nikbase | 298 постов (текст + транскрипты голосовых) | ~570 KB |

**Авторизация:** `~/.local/bin/nlm login --provider openclaw --cdp-url http://127.0.0.1:18800`
Требует SSH-туннель с MacBook (Chrome с `--remote-debugging-port=9222`).
Cookies хранятся в `~/.notebooklm-mcp-cli/profiles/default/`.

## NotebookLM — два ноутбука

| Ноутбук | ID | Назначение |
|---------|----|-----------|
| `content-brain` (архив) | `1eb35d64-3e12-4dc9-8043-f65d703d6281` | Весь исторический контекст — автообновляется |
| `content-brain-fresh` | `6ed2bc91-8fb5-4eca-a881-324f6db90db1` | Свежие Gemini-переписки — управляется вручную |

**`content-brain-fresh` — как использовать:**
Добавляй туда актуальные переписки с Gemini. Анализатор запрашивает этот ноутбук первым
и извлекает из него смыслы как первичный источник контента для постов.
Когда переписка "устарела" — удали из fresh (она останется в архиве через nlm_updater).

## NotebookLM — автообновление (nlm_updater.py)

Обновляется **автоматически по крону**:

| Расписание | Режим | Что делает |
|-----------|-------|-----------|
| Воскресенье 03:00 MSK | `nlm_updater.py` | Новые Telegram/YouTube батчи + замена резюме дней |
| 1-е число 03:00 MSK | `nlm_updater.py --monthly` | Всё выше + сырые записи дневника за прошлый месяц |

**Стратегия — инкрементальные батчи (не перезапись старого):**
- Telegram/YouTube: добавляются как `"Telegram-канал @nikbase 2026-07"` — по месяцам
- Резюме дней: заменяется полностью каждую неделю (мал, всегда актуален)
- Сырые записи дневника: `"Дневник — сырые записи 2026-06"` — 12 источников в год

```bash
# Запустить вручную:
python3 01-knowledge-base/nlm_updater.py           # еженедельный режим
python3 01-knowledge-base/nlm_updater.py --monthly # ежемесячный режим
```

**Состояние:** `01-knowledge-base/.nlm_update_state.json`
**Лог:** `/home/ubuntu/content-brain-nlm-update.log`

## Индексация (01-knowledge-base)

```bash
python3 main.py --full    # полная: весь дневник + весь канал
python3 main.py --update  # инкрементальная: последние 30 дней
```

**Что индексируется:**
- `entries.ai_summary` → `cb_diary_vectors` (source_id: `entry_{date}`)
- `messages` где role='user' → `cb_diary_vectors` (source_id: `msg_{id}`)
- Посты @nikbase (текст) → `cb_social_posts` + `cb_social_vectors`
- Голосовые сообщения → транскрипция Groq → `cb_social_posts` (пометка `[голосовое]`)

**Важно:** Telegram-индексация делает два прохода: сначала собирает всё в память
(включая транскрипты), потом открывает свежее DB-соединение и пишет. Это избегает
таймаута Turso-стрима при долгой транскрипции.

**Telethon сессия:** `outreach-system/04-sheets-sender/campaign.session` (скопирована)
Аккаунт +79177386362 — подписан на @nikbase.

## Анализатор (02-analyzer)

```bash
python3 analyzer.py   # запустить анализ (без аргументов)
```

**Двухэтапный процесс:**

1. **Извлечение смыслов** — Claude получает записи 7 дней + стратегию и выдаёт структурированный список смыслов с типами (ценность/позиция/трансформация/...) и флагом `needs_history`
2. **Поиск предыстории** — для смыслов с `needs_history=true` делается запрос к NotebookLM с конкретным вопросом
3. **Генерация идей** — Claude на основе смыслов + исторического контекста генерирует 3-5 идей постов

**Стратегия:** читается из `03-bot/strategy.md`. Если не заполнена — работает без неё.

## Бот (03-bot)

```bash
python3 main.py   # запуск polling
```

**Структура handlers:**

| Файл | Назначение |
|------|-----------|
| `handlers/ideas.py` | Команда `/ideas` — список идей со статусом 'new' |
| `handlers/post_writer.py` | Генерация поста по выбранной идее через Claude CLI |
| `handlers/channel_monitor.py` | Ловит `channel_post` → сохраняет в `cb_social_posts` + `cb_social_vectors` |
| `handlers/gemini_import.py` | Команда `/gextract` — импорт Gemini-переписок в `content-brain-fresh` |

**Тон голоса:**
- `strategy.md` — основной файл стратегии (заполнить вручную)
- `extract_tone.py` — скрипт извлечения тона из постов канала
- `tone_analysis.md` — результат анализа тона (генерируется `extract_tone.py`)

**Авто-сбор постов канала:**
Бот должен быть добавлен как администратор в @nikbase.
Handler `channel_monitor.py` ловит `channel_post` → сохраняет в `cb_social_posts` + `cb_social_vectors`.

**Генерация постов:** Claude CLI через `subprocess`, тон голоса из `strategy.md`.

## Переменные окружения

Все хранятся в `content-brain/.env` (и в глобальном `workspace/.env`).

| Переменная | Назначение |
|-----------|-----------|
| `TURSO_MOOD_URL` / `TURSO_MOOD_TOKEN` | mood-diary Turso (read-only) |
| `TURSO_CONTENT_BRAIN_URL` / `TURSO_CONTENT_BRAIN_TOKEN` | content-brain Turso |
| `NLM_NOTEBOOK_ID` | ID ноутбука NotebookLM |
| `CONTENT_BRAIN_BOT_TOKEN` | Токен Telegram-бота |
| `TG_API_ID` / `TG_API_HASH` | Кампейн-аккаунт (33361321 / 67a7d...) |
| `TG_CHANNEL_USERNAME` | `nikbase` |
| `GROQ_API_KEY` | Транскрипция голосовых |
| `TELEGRAM_USER_ID` | ID владельца для уведомлений (5950805456) |

## YouTube-импорт (MacBook)

```bash
cd scripts/
pip install yt-dlp youtube-transcript-api requests
python youtube_import.py --channel https://www.youtube.com/@НазваниеКанала
```

Записывает напрямую в Turso content-brain через HTTP API. Интернет с сервера для YouTube не нужен.

## Связи с экосистемой

| Проект | Связь |
|--------|-------|
| `mood-diary` | Источник данных (read-only Turso) |
| `outreach-system` | Telethon-сессия campaign.session |
| `workspace/.env` | Глобальные credentials |

## Известные особенности

- NotebookLM cookies истекают — при ошибке авторизации нужен SSH-туннель с Mac и `nlm login --provider openclaw`
- Embeddings в Turso — хэш-based (не семантические), используются для хранения, поиск идёт через NLM
- Публикация в канал — пользователь делает вручную, бот только помечает идею как `used`
- strategy.md — заполнить вручную, это критично для качества идей и постов
- gemini_import.py (`/gextract`) — импортирует Gemini-переписки напрямую в ноутбук `content-brain-fresh`; после устаревания переписки удалить из fresh вручную

## История изменений

- 2026-06-01 — создание проекта, базовая структура
- 2026-06-01 — полная индексация: 13 резюме + 670 сообщений + 298 постов (50 голосовых транскрибировано)
- 2026-06-01 — интеграция NotebookLM (nlm v0.6.3, ноутбук content-brain, 5 источников)
- 2026-06-02 — двухэтапный анализатор: извлечение смыслов → NLM предыстория → идеи
- 2026-06-02 — Claude CLI вместо Anthropic SDK; libsql_experimental вместо libsql_client
- 2026-06-02 — channel_monitor.py: автосбор новых постов @nikbase в БД
- 2026-06-02 — добавлен YouTube в NLM (63 ролика); ноутбук content-brain-fresh для Gemini-переписок
- 2026-06-02 — nlm_updater.py: автообновление NLM по крону (еженедельно + ежемесячно)
- 2026-06-07 — handlers/gemini_import.py: команда /gextract для импорта Gemini-переписок в content-brain-fresh; extract_tone.py + tone_analysis.md: анализ и хранение тона голоса автора
