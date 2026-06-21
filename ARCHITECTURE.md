# content-brain — Архитектура

## Назначение

AI-система для создания контента личного бренда из дневника.
Источник: записи mood-diary за последние 7 дней + переписки с Gemini.
Платформы: Telegram-пост, Reels/Shorts, YouTube.
Движки генерации: Claude CLI + Antigravity (Gemini) — доступны на каждом шаге.

## Стек

| Компонент | Технология |
|-----------|-----------|
| Язык | Python 3.11+ |
| БД-клиент | `libsql_experimental` (sync, local replica) |
| Telegram-бот | `aiogram 3.x` |
| Telegram MTProto | `telethon` (кампейн-аккаунт +79177386362) |
| AI (анализ/генерация) | Claude CLI (`claude -p ...`) — без API-ключа, через подписку |
| AI (альтернатива) | Antigravity CLI (`/snap/bin/antigravity-cli -p ...`) — Gemini |
| AI (семантический поиск) | NotebookLM через `~/.local/bin/nlm` |
| Транскрипция голоса | Groq API whisper-large-v3 (+ Finland VPS fallback + faster-whisper tiny) |
| Embeddings | SHA-256 хэш + numpy (для хранения; семантический поиск — через NLM) |

## Структура

```
content-brain/
├── 01-knowledge-base/  — индексация дневника и канала в Turso
├── 02-analyzer/        — двухэтапный анализ → идеи постов + анализ событий
├── 03-bot/             — Telegram-бот: выбор идеи, генерация контента
└── scripts/            — утилиты (youtube_import.py для MacBook)
```

## Поток данных

```
mood-diary Turso (entries + messages)
         │
         ▼
┌─────────────────────┐     Telethon (кампейн +79177386362)
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
│  analyzer.py        │     content-brain-fresh (Gemini-переписки)
│                     │     + content-brain (архив: дневник + посты)
│  Этап 1: Claude     │
│  → смыслы из 7 дней │
│  Этап 2: NLM        │
│  → предыстория      │
│  Этап 3: Claude     │
│  → 3-5 идей постов  │
└──────────┬──────────┘
           │ cb_ideas (status='new')
           │
           ▼
┌───────────────────────────────────────────────────┐
│  03-bot/ (aiogram polling)                        │
│                                                   │
│  /analyze → запуск analyzer.py в фоне             │
│  /analyze_fast → без NLM (быстрый режим)          │
│  /ideas → список → экран смысла → генерация       │
│  /gextract → импорт Gemini-переписок в NLM-fresh  │
│                                                   │
│  голос/текст вне фидбека → capture.py → идеи      │
└──────────┬────────────────────────────────────────┘
           │ cb_published_posts + cb_ideas status='used'
           │
           ▼
    пользователь публикует вручную в @nikbase
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
| `cb_social_posts` | Посты канала и YouTube: source_type, content, url, published_at |
| `cb_social_vectors` | Векторы постов: social_post_id → embedding |
| `cb_ideas` | Идеи: title, thesis, source_type ('analyzer'/'voice'), status ('new'→'shown'→'used'/'dismissed') |
| `cb_voice_captures` | Сырые голосовые/текстовые заметки: raw_text, created_at |
| `cb_published_posts` | Опубликованные посты: post_text, published_at, tg_message_id |

## NotebookLM

| Ноутбук | ID | Назначение |
|---------|----|-----------|
| `content-brain` (архив) | `1eb35d64-3e12-4dc9-8043-f65d703d6281` | Весь исторический контекст — автообновляется по крону |
| `content-brain-fresh` | `6ed2bc91-8fb5-4eca-a881-324f6db90db1` | Свежие Gemini-переписки — управляется вручную |

**Авторизация:** `~/.local/bin/nlm login --provider openclaw --cdp-url http://127.0.0.1:18800`
Требует SSH-туннель с MacBook (Chrome с `--remote-debugging-port=9222`). Cookies истекают.

## NotebookLM — автообновление (nlm_updater.py)

| Расписание | Режим | Что делает |
|-----------|-------|-----------|
| Воскресенье 03:00 MSK | `nlm_updater.py` | Новые Telegram/YouTube батчи + замена резюме дней |
| 1-е число 03:00 MSK | `nlm_updater.py --monthly` | Всё выше + сырые записи дневника за прошлый месяц |

**Состояние:** `01-knowledge-base/.nlm_update_state.json`
**Лог:** `/home/ubuntu/content-brain-nlm-update.log`

## Индексация (01-knowledge-base)

```bash
python3 main.py --full    # полная: весь дневник + весь канал
python3 main.py --update  # инкрементальная: последние 30 дней
```

Что индексируется:
- `entries.ai_summary` → `cb_diary_vectors` (source_id: `entry_{date}`)
- `messages` role='user' → `cb_diary_vectors` (source_id: `msg_{id}`)
- Посты @nikbase → `cb_social_posts` + `cb_social_vectors`
- Голосовые → транскрипция Groq → `cb_social_posts` (пометка `[голосовое]`)

Два прохода Telethon: сначала собираем всё в память (с транскрипцией), потом пишем в свежее
соединение — иначе Turso-стрим падает по таймауту.

## Анализатор (02-analyzer)

```bash
python3 analyzer.py          # полный анализ с NotebookLM
python3 analyzer.py --no-nlm # только дневник + Claude, без NLM
```

### Основной анализатор (analyzer.py)

1. **Извлечение смыслов** — Claude: записи 7 дней + стратегия → структурированный список смыслов (тип, стратегическое соответствие, нужна ли предыстория)
2. **Поиск предыстории** — для смыслов с `needs_history=true` → запрос к NLM-fresh, потом NLM-архив
3. **Генерация идей** — Claude → 3-5 идей с `title`, `thesis` (суть + угол + поворот)

Идеи платформо-нейтральны (не привязаны к TG/Reels/YouTube). Системный промпт `IDEAS_SYSTEM` в `prompts.py` явно запрещает указывать платформу. Стратегия читается из `03-bot/strategy.md`.

### Анализатор событий (events_analyzer.py, events_strategy_analyzer.py)

Отдельный поток анализа для событий/мероприятий:
- `events_analyzer.py` — извлекает события из дневника и внешних источников
- `events_strategy_analyzer.py` — сверяет события со стратегией канала
- `events_prompts.py` — промпты для извлечения событий
- `events_strategy_prompts.py` — промпты для стратегического анализа событий

## Бот (03-bot)

```bash
python3 main.py   # запуск polling
```

### Структура файлов

| Файл | Назначение |
|------|-----------|
| `bot.py` | Создание `Bot` и `Dispatcher` (aiogram 3.x, parse_mode=HTML) |
| `main.py` | Точка входа, polling, регистрация роутеров |
| `transcribe.py` | Трёхуровневая транскрипция: Groq → Finland VPS → faster-whisper tiny |
| `strategy.md` | Стратегия @nikbase (222 строки, заполнен) — тон голоса, позиционирование |
| `lens.md` | Творческая линза — угол взгляда и фильтр для генерации контента |
| `prompts.py` | Системный промпт Telegram-поста + `build_post_prompt()` + `load_strategy()` |
| `prompts_reels.py` | Системный промпт сценария Reels/Shorts (`get_reels_system()`) |
| `prompts_youtube.py` | Системный промпт структуры YouTube-ролика (`get_youtube_system()`) |
| `humanizer.md` | Правила гуманизации текста (убираем AI-паттерны) |
| `extract_tone.py` | Утилита извлечения авторского тона из постов канала |
| `handlers/analyze.py` | `/analyze`, `/analyze_fast` — запуск analyzer.py в фоне |
| `handlers/ideas.py` | `/ideas`, экран смысла, уточнение сути, варианты подачи, стратегия |
| `handlers/post_writer.py` | Генерация TG-поста (Claude + Gemini), гуманизатор, фидбек, роутинг голоса/текста |
| `handlers/format_writer.py` | Генерация Reels и YouTube (Claude + Gemini), форматы Reels |
| `handlers/capture.py` | Захват голоса/текста вне фидбека → идеи через Claude |
| `handlers/channel_monitor.py` | `channel_post` → сохраняет в `cb_social_posts` + `cb_social_vectors` |
| `handlers/gemini_import.py` | `/gextract` — импорт Gemini-переписок в ноутбук `content-brain-fresh` |
| `handlers/events.py` | Обработка команд для работы с событиями/мероприятиями |
| `handlers/events_strategy.py` | Стратегический анализ событий — сверка с `strategy.md` |

### Команды бота

| Команда | Что делает |
|---------|-----------|
| `/analyze` | Запускает `analyzer.py` в фоне (с NLM), уведомляет по завершении |
| `/analyze_fast` | Запускает `analyzer.py --no-nlm` (быстрый, только Claude) |
| `/ideas` | Показывает список идей со статусом 'new'/'shown' |
| `/gextract` | Импортирует Gemini-переписку в ноутбук `content-brain-fresh` |

### Флоу идея → контент

#### Экран смысла (ideas.py)

После выбора идеи показывает:
- Название + thesis (оригинальный или уточнённый)
- Угол подачи (если выбран)
- Кнопки: уточнить суть (C/G) / варианты подачи (C/G) / стратегия (C/G) / платформа

**Уточнить суть** (Claude/Gemini): пользователь диктует/пишет сырые мысли → модель кристаллизует в тезис.

**Варианты подачи** (Claude/Gemini): 2-3 смысловых угла к той же идее (НЕ форматы видео).
JSON: `[{"name": "...", "angle": "..."}]`. Хранятся в `_idea_context[user_id]["approaches"]`.

**Проверить стратегию** (Claude/Gemini): сверяет thesis со `strategy.md` → оценка ✅/⚖️/❌ + объяснение.

#### Telegram-пост (post_writer.py)

Три режима:
- `diary` — только сырые записи дневника
- `archive` — дневник + исторический контекст из NLM-архива
- `current` — NLM-fresh (приоритет) + дневник

Оба движка: Claude CLI (`--system-prompt` + stdin) и Antigravity (`-p` full_prompt).

Сессионность Claude: `--resume SESSION_ID` для фидбека и перегенерации без перезагрузки контекста.

После черновика: `[✏️ Улучшить] [🫀 Гуманизировать] [🔄 Перегенерировать] [♻️ → Reels/YouTube]`

#### Reels / Shorts (format_writer.py)

```
[📹 Reels]
  → [📓 Форматы через Claude] [🤖 Форматы через Gemini]
  → 2-3 нарративных формата (До/после / Парадокс-разворот / Один момент / ...)
    каждый: format + logic (как мысль раскрывается) + duration + hook (первая фраза)
  → выбрал формат
  → [📓 Claude] [🤖 Gemini] [⚡ Оба сразу]
  → сценарий под конкретный нарративный формат
```

⚠️ Форматы — это НЕ инструкции по съёмке, а нарративная структура (как идея раскрывается).

#### YouTube (format_writer.py)

```
[🎬 YouTube]
  → [📓 Claude] [🤖 Gemini] [⚡ Оба сразу]
  → структура ролика: НАЗВАНИЕ + ХУК + ОБЕЩАНИЕ + 3-5 блоков + ФИНАЛ
```

#### Голосовой/текстовый захват (capture.py)

Любое голосовое или текстовое сообщение вне режима фидбека:
```
голос → Groq транскрипция → Claude → 1-3 идеи → cb_ideas (source_type='voice')
текст → Claude → 1-3 идеи → cb_ideas (source_type='voice')
сохраняется в cb_voice_captures (raw_text)
→ показывает кнопки с идеями → те же экраны смысла и платформы
```

Системный промпт `_EXTRACT_SYSTEM` в `capture.py`: title до 60 символов, thesis 2-3 предложения.

### Состояние в памяти (in-memory state dicts)

```python
# ideas.py
_idea_context: dict[int, dict]   # user_id → {idea_id, refined_thesis, selected_approach, approaches, reels_format}
_awaiting_thesis: dict[int, str] # user_id → "claude"|"gemini"

# post_writer.py
_drafts: dict[int, dict]              # user_id → {idea_id, draft, system, mode, session_id}
_agy_drafts: dict[int, dict]          # user_id → {idea_id, draft, mode, conv_id}
_awaiting_feedback: dict[int, bool]   # Claude фидбек
_agy_awaiting_feedback: dict[int, bool]

# format_writer.py
_reels_drafts: dict[int, dict]        # user_id → {idea_id, draft, engine, session_id, conv_id}
_youtube_drafts: dict[int, dict]
_awaiting_reels_feedback: dict[int, bool]
_awaiting_youtube_feedback: dict[int, bool]
_pending_reels_formats: dict[int, dict] # user_id → {idea_id, formats: list}
```

Роутинг входящих сообщений (голос/текст) в `post_writer.py`:
```
_awaiting_thesis → process_thesis_input (ideas.py)
_agy_awaiting_feedback → _process_agy_feedback
_awaiting_feedback → _process_feedback
_awaiting_reels_feedback → process_reels_feedback (format_writer)
_awaiting_youtube_feedback → process_youtube_feedback (format_writer)
иначе → handle_voice_capture / handle_text_capture (capture.py)
```

### Antigravity (Gemini) — важные особенности

Вызов:
```python
subprocess.run(["/snap/bin/antigravity-cli", "-p", full_prompt,
                "--dangerously-skip-permissions"], cwd=WORK_DIR)
# С продолжением беседы:
subprocess.run([..., "--conversation", conv_id], ...)
```

**Критический баг**: с `--conversation` stdout содержит ВЕСЬ предыдущий разговор + новый ответ.
Фикс в `_call_agy_sync(prev_text=...)`:
```python
anchor = prev_text[-200:].strip()
idx = output.rfind(anchor)
if idx != -1:
    output = output[idx + len(anchor):].strip()
```
Всегда передавай `prev_text=stored["draft"]` при регенерации, гуманизации, фидбеке.

Проекты Antigravity хранятся в `03-bot/.antigravitycli/` как симлинки на
`/home/ubuntu/snap/antigravity-cli/2/.gemini/config/projects/{conv_id}.json`.

## Переменные окружения

Все в `content-brain/.env` (и в глобальном `workspace/.env`).

| Переменная | Назначение |
|-----------|-----------|
| `TURSO_MOOD_URL` / `TURSO_MOOD_TOKEN` | mood-diary Turso (read-only) |
| `TURSO_CONTENT_BRAIN_URL` / `TURSO_CONTENT_BRAIN_TOKEN` | content-brain Turso |
| `NLM_NOTEBOOK_ID` | ID архивного ноутбука NLM |
| `NLM_FRESH_NOTEBOOK_ID` | ID fresh-ноутбука NLM |
| `CONTENT_BRAIN_BOT_TOKEN` | Токен Telegram-бота |
| `TG_API_ID` / `TG_API_HASH` | Кампейн-аккаунт (33361321 / 67a7d...) |
| `TG_CHANNEL_USERNAME` | `nikbase` |
| `GROQ_API_KEY` | Транскрипция голосовых |
| `TELEGRAM_USER_ID` | ID владельца для уведомлений (5950805456) |
| `FINLAND_WHISPER_URL` | URL Finland VPS whisper (по умолчанию http://2.26.85.234:5000/transcribe) |

## YouTube-импорт (MacBook)

```bash
cd scripts/
pip install yt-dlp youtube-transcript-api requests
python youtube_import.py --channel https://www.youtube.com/@НазваниеКанала
```

Записывает напрямую в Turso content-brain через HTTP API.

## Связи с экосистемой

| Проект | Связь |
|--------|-------|
| `mood-diary` | Источник данных (read-only Turso) |
| `outreach-system` | Telethon-сессия campaign.session |
| `workspace/.env` | Глобальные credentials |

## Известные особенности

- **NLM авторизация** — cookies истекают периодически; нужен SSH-туннель с Mac и `nlm login --provider openclaw`
- **Antigravity history** — с `--conversation` возвращает полный разговор в stdout; фикс через `prev_text` anchor (см. выше)
- **Embeddings** — хэш-based (не семантические), хранятся в Turso; семантический поиск идёт только через NLM
- **Публикация** — пользователь публикует вручную; бот помечает идею как `used` только если пользователь явно подтверждает
- **strategy.md** — заполнен (222 строки); читается через `load_strategy()` в `prompts.py` (кешируется в памяти)
- **lens.md** — творческая линза; читается в `03-bot/`; дополняет strategy.md углом подачи материала
- **Конфликт инстансов** — только один инстанс бота; при рестарте проверять `ps aux | grep main.py`
- **Claude сессии** — `_get_latest_session_id()` ищет последний `.jsonl` в `~/.claude/projects/`; после рестарта бота сессии теряются
- **`/analyze_fast`** — режим без NLM; таймаут фоновой задачи 15 минут; полезен когда SSH-туннель недоступен
- **Платформо-нейтральные идеи** — `IDEAS_SYSTEM` в `02-analyzer/prompts.py` явно запрещает привязку идей к платформе; платформа выбирается в боте на этапе генерации контента

## История изменений

- 2026-06-01 — создание проекта, базовая структура
- 2026-06-01 — полная индексация: 13 резюме + 670 сообщений + 298 постов
- 2026-06-01 — интеграция NotebookLM (ноутбук content-brain, 5 источников)
- 2026-06-02 — двухэтапный анализатор, Claude CLI, libsql_experimental, channel_monitor
- 2026-06-02 — YouTube в NLM, ноутбук content-brain-fresh, nlm_updater.py
- 2026-06-07 — gemini_import.py (/gextract), extract_tone.py, analyze.py, transcribe.py, --no-nlm
- 2026-06-14 — Antigravity (Gemini) интеграция; три режима генерации (diary/archive/current); гуманизатор
- 2026-06-15 — мультиплатформа: Reels/Shorts + YouTube; голосовой и текстовый захват (capture.py)
- 2026-06-15 — экран смысла: уточнение сути (C/G), варианты подачи (C/G), стратегия (C/G)
- 2026-06-15 — Reels: нарративные форматы (C/G) + движок сценария (C/G); format_writer.py
- 2026-06-16 — все функции переведены на двойной движок C/G; нарративные форматы вместо продакшн-инструкций
- 2026-06-21 — events-ветка: events_analyzer.py, events_strategy_analyzer.py, events_prompts.py, events_strategy_prompts.py в 02-analyzer; handlers/events.py, handlers/events_strategy.py в 03-bot; lens.md; IDEAS_SYSTEM переписан на платформо-нейтральный язык («контент-редактор личного бренда»)
