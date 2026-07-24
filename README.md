# Chatra Backend

FastAPI + SQLAlchemy бэкенд для образовательной платформы Chatra.

Стек: **Python 3.14**, FastAPI, SQLAlchemy 2.0, PostgreSQL (в проде) / SQLite (для локальной разработки), OpenAI API.

---

## 1. Системные зависимости (до установки Python-пакетов)

Часть функций требует внешних утилит:

| Утилита | Для чего | Установка (macOS) |
|---|---|---|
| **PostgreSQL** | основная БД в проде | `brew install postgresql@16` |
| **poppler** (`pdftoppm`) | рендер картинок слайдов лекций аватара | `brew install poppler` |
| **LibreOffice** (`soffice`) | конвертация `.pptx` → PDF → картинки слайдов | `brew install --cask libreoffice` |
| **Tesseract** (опц.) | OCR в сканах/картинках заданий | `brew install tesseract` |

> Без `soffice` + `pdftoppm` лекции аватара получат аудио, но **без картинок слайдов** (пайплайн молча помечает их `ready`). Для базовой работы (классы, задания, чат, ИИ-оценивание) эти утилиты не нужны.

Linux (Debian/Ubuntu): `sudo apt install postgresql poppler-utils libreoffice tesseract-ocr`.

---

## 2. Установка

```bash
python3 -m venv venv
source venv/bin/activate          # macOS / Linux
# venv\Scripts\activate           # Windows

pip install -r requirements.txt
```

> `requirements.txt` тянет torch/torchvision/easyocr — установка тяжёлая и небыстрая. Это нормально.

---

## 3. Файл `.env`

Создать `.env` в корне бэкенда. Секреты сгенерировать так (каждый — своей командой):

```bash
openssl rand -hex 32   # для SECRET_KEY
openssl rand -hex 32   # для REFRESH_SECRET_KEY (другое значение)
```

Пример `.env`:

```env
# --- Обязательное ---
OPENAI_API_KEY=sk-proj-...            # platform.openai.com/api-keys
SECRET_KEY=минимум-32-случайных-символа          # без него сервер не стартует
REFRESH_SECRET_KEY=другие-32-случайных-символа   # тоже обязателен, ДРУГОЙ секрет

# --- База данных ---
# SQLite (быстрый старт, без установки Postgres):
DATABASE_URL=sqlite:///./chatra.db
# ...или PostgreSQL (прод):
# DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/test_jwt

# --- Ссылки на файлы / сеть ---
APP_BASE_URL=http://localhost:8000    # база для URL загруженных файлов

# --- Опциональное (AI-аватар) ---
# ELEVENLABS_API_KEY=...              # клон голоса + озвучка лекций
# DID_API_KEY=...                     # видео-интро аватара
```

Полный список переменных — в таблице [ниже](#переменные-окружения).

---

## 4. Запуск

```bash
source venv/bin/activate
uvicorn main:app --reload --reload-exclude venv --host 0.0.0.0 --port 8000
```

- Swagger UI: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/health>

`--host 0.0.0.0` обязателен, если к бэкенду будут ходить с телефона/другого устройства (см. [раздел 6](#6-запуск-для-другого-устройства-телефон--вторая-машина)). Для прода убрать `--reload`:

```bash
nohup ./venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > uvicorn.log 2>&1 &
```

---

## 5. База данных и миграции

### Новая / пустая база — миграции НЕ нужны

При старте приложение само создаёт таблицы:
- **SQLite** — `Base.metadata.create_all` создаёт всё в `chatra.db`.
- **PostgreSQL** — создаются схемы `university` и `school` и все таблицы в каждой (`main.py`).

То есть на чистой БД достаточно запустить сервер — схема поднимется сама.

### Существующая база — накатить миграции

Проект **не использует Alembic**. Миграции — это пронумерованные файлы в `migrations/`:
- `NNN_*.sql` — DDL для ручного применения (`psql`);
- `NNN_*.py` / `NNN_backfill_*.py` — DDL **+ перенос данных**, применяются одним скриптом и сразу по обеим схемам (`university`, `school`).

Порядок (применять по возрастанию номера, только то, чего ещё нет в вашей БД):

| # | Что добавляет | Как накатить |
|---|---|---|
| 002 | pgvector для RAG | `psql "$DATABASE_URL" -f migrations/002_rag_pgvector.sql` |
| 003 | `classes.org_type` | `psql … -f migrations/003_class_org_type.sql` |
| 004 | `ai_usage.org_type` | `psql … -f migrations/004_ai_usage_org_type.sql` |
| 005 | инвайт-коды классов | `python migrations/005_backfill_invite_codes.py` |
| 006 | перенос постов-классов в `classes` | `python migrations/006_backfill_posts_to_classes.py` |
| 007 | `messages.file_url` / `is_read` | `psql … -f migrations/007_messages_file_url_is_read.sql` |
| 008 | язык лекций аватара | `psql … -f migrations/008_avatar_lecture_language.sql` |
| **009** | **учебные потоки (когорты)** | `python migrations/009_backfill_cohorts.py` |

### ⚠️ Ловушка: миграционные скрипты не читают `.env`

`load_dotenv()` вызывается только в `main.py`. Скрипты в `migrations/` импортируют `db` напрямую, поэтому **без явного `DATABASE_URL` в окружении они уйдут в дефолтный SQLite** и «мигрируют не ту базу». Всегда задавайте URL инлайн:

```bash
DATABASE_URL="postgresql://postgres:PASSWORD@localhost:5432/test_jwt" \
  ./venv/bin/python migrations/009_backfill_cohorts.py
```

Python-миграции идемпотентны — повторный запуск безопасен. Откат: `… 009_backfill_cohorts.py --downgrade`.

### Миграция 009 — учебные потоки (последняя)

Добавляет `classes.rotation_mode`, таблицы `cohorts` / `cohort_students` / `deadlines`, колонку `submissions.deadline_id` и бэкфилл (каждому классу — активный поток `2025/2026`, перенос участников и дедлайнов). Без неё эндпоинты классов падают с `column classes.rotation_mode does not exist`.

```bash
DATABASE_URL="postgresql://postgres:PASSWORD@localhost:5432/test_jwt" \
  ./venv/bin/python migrations/009_backfill_cohorts.py
# [university] cohorts +N, cohort_students +N, deadlines +N, submissions.deadline_id +N
# [school]     ...
```

---

## 6. Запуск для другого устройства (телефон / вторая машина)

Приложение Flutter по умолчанию ходит на `http://localhost:8000`
(`Chatra2/lib/services/api_service.dart` → `defaultBaseUrl`). `localhost`
работает только на той же машине, поэтому для телефона/эмулятора адрес нужно
поменять на один из вариантов ниже.

### Вариант A — по локальной сети (телефон и ПК в одном Wi-Fi)

1. Узнать IP машины с бэкендом:
   ```bash
   ipconfig getifaddr en0     # macOS, обычно вида 192.168.x.x
   # Linux: hostname -I
   ```
2. Запустить бэкенд с `--host 0.0.0.0` (см. раздел 4).
3. В приложении поменять базовый URL на этот IP:
   ```dart
   // Chatra2/lib/services/api_service.dart
   static const String defaultBaseUrl = 'http://192.168.1.50:8000';
   ```
4. В `.env` бэкенда выставить тот же адрес, чтобы ссылки на файлы были рабочими:
   ```env
   APP_BASE_URL=http://192.168.1.50:8000
   ```

- **Android-эмулятор**: вместо IP используйте `http://10.0.2.2:8000` (алиас хоста).
- **iOS-симулятор**: можно оставить `http://localhost:8000`.

### Вариант B — через ngrok (работает откуда угодно, без общей сети)

Так поднят прод-туннель проекта. Бэкенд слушает `0.0.0.0:8000`, а в отдельном терминале:

```bash
ngrok http 8000
# или на закреплённый домен:
ngrok http --domain=glacier-radiated-wipe.ngrok-free.dev 8000
```

Затем в приложении:

```dart
static const String defaultBaseUrl = 'https://glacier-radiated-wipe.ngrok-free.dev';
```

> Приложение само переписывает `localhost`-ссылки на файлы (`/uploads/...`) на текущий `baseUrl`, так что при смене адреса картинки/файлы продолжат грузиться. Но `APP_BASE_URL` в `.env` всё равно стоит держать актуальным.

После изменения `defaultBaseUrl` пересобрать приложение (`flutter run`).

---

## Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `OPENAI_API_KEY` | **Обязательно для AI функций** | — |
| `SECRET_KEY` | **Обязателен** — секрет для подписи access-JWT | — (сервер не стартует) |
| `REFRESH_SECRET_KEY` | **Обязателен** — секрет для refresh-JWT (другой) | — (сервер не стартует) |
| `DATABASE_URL` | SQLAlchemy строка подключения | `sqlite:///./chatra.db` |
| `APP_BASE_URL` | Базовый URL для ссылок на файлы | `http://localhost:8000` |
| `UPLOAD_DIR` | Папка для файлов, загруженных **до** подключения R2 (legacy) | `uploads` |
| `R2_ACCOUNT_ID` | ID аккаунта Cloudflare (для хранения **новых** файлов) | — |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | S3-совместимые ключи R2 | — |
| `R2_BUCKET_NAME` | Бакет R2 (разный для dev/prod) | — |
| `R2_ENDPOINT_URL` | Свой endpoint R2 (если не строится из `R2_ACCOUNT_ID`) | не задан |
| `R2_PUBLIC_BASE_URL` | Домен CDN перед бакетом (заготовка на будущее) | не задан → раздача через `/uploads/...` с подписью |
| `CORS_ORIGINS` | Разрешённые origins через запятую, или `*` | `*` |
| `ELEVENLABS_API_KEY` | Клон голоса учителя + озвучка лекций аватара | не задан → озвучка недоступна |
| `DID_API_KEY` | Видео-интро аватара (говорящее фото, 15-30 сек) | не задан → видео-интро пропускается |

---

## Хранение файлов (Cloudflare R2)

Все **новые** файлы (загрузки в классах, обложки, TTS/видео аватара, слайды
лекций) сохраняются в Cloudflare R2 через `services/storage/`
(S3-совместимый API, см. `services/storage/r2_storage.py`). Файлы,
загруженные до подключения R2, остаются в локальной папке `uploads/` — они не
мигрируются и продолжают отдаваться как раньше.

И локальные, и R2-файлы отдаются клиенту через один и тот же защищённый
эндпоинт `/uploads/...` с HMAC-подписью (SEC-1, `services/file_urls.py`) —
прямого публичного доступа к бакету нет, ссылка на файл выдаётся только
вместе с ресурсом, на который у пользователя есть права. Настройка R2 — в
секции `R2_*` переменных выше и в `.env.example`.

Чтобы подключить собственный CDN-домен перед бакетом (например
`cdn.chatra.aican.cloud`), задайте `R2_PUBLIC_BASE_URL` — тогда новые файлы
начнут отдаваться прямой публичной ссылкой в обход подписанного прокси.

---

## AI-аватар преподавателя

Учитель один раз создаёт своего аватара (фото + образец голоса), затем по
материалам, закреплённым за классом, может создавать лекции — аватар
озвучивает презентацию слайд за слайдом голосом учителя и в конце
формирует конспект.

**Создание аватара и каждая новая лекция требуют одобрения администратора**
в разделах `/admin/avatars` и `/admin/avatar-lectures` — это сознательное
ограничение, так как голос и видео генерируются через платные сторонние API
(ElevenLabs и D-ID).

Без ключей `ELEVENLABS_API_KEY` / `DID_API_KEY` система продолжает работать:
заявки на аватар можно одобрять, лекции — создавать, но озвучка/видео не
генерируются, пока администратор не добавит ключи. Текст лекции и конспект
всегда генерируются через `OPENAI_API_KEY`.

Видео-аватар генерируется **только для первого слайда лекции** (короткое
говорящее интро ~15-30 сек). Остальная часть — слайд + статичное фото
учителя + аудио, синхронно листающиеся в плеере на фронтенде.

---

## AI-оценивание

Задания оцениваются через OpenAI GPT-4o-mini по умолчанию.
Для GPT-4o измените `OPENAI_MODEL` в `services/ai_grader.py` и `routers/ai.py`.

---

## Структура проекта

```
main.py          — точка входа, FastAPI app, создание схем/таблиц
models.py        — SQLAlchemy модели
schemas.py       — Pydantic схемы
db.py            — подключение к БД, get_engine(org) для схем university/school
deps.py          — зависимости (get_db, get_current_user, get_current_teacher)
security.py      — JWT утилиты
routers/         — эндпоинты (auth, users, classes, cohorts, ai, avatars, ...)
crud/            — операции с БД
services/        — бизнес-логика (ai_grader, deadline_checker, ...)
services/storage/ — Cloudflare R2 (хранилище новых файлов), см. раздел выше
parsers/         — парсинг файлов (PDF, DOCX, OCR)
websocket.py     — WebSocket чат
migrations/      — SQL + Python миграции (НЕ Alembic, см. раздел 5)
uploads/         — файлы, загруженные до подключения R2 (legacy, не пополняется)
```
