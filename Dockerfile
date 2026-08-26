# syntax=docker/dockerfile:1

# ---- Builder ----
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Runtime ----
FROM python:3.12-slim

WORKDIR /app

# tesseract + rus/eng: pytesseract (services/extractor.py, services/ai_grader.py)
# вызывает бинарник tesseract с lang="rus+eng" — без пакетов OCR падает в проде.
# curl — для HEALTHCHECK. libreoffice — конвертация docx/pptx/xlsx -> pdf
# (routers/uploads.py ищет soffice через shutil.which).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        curl \
        libreoffice \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY . .

RUN mkdir -p uploads
#
# Контейнер намеренно работает под root: /app/uploads — bind-mount с хоста,
# и после git pull на VPS он принадлежит хостовому пользователю. Non-root
# пользователь внутри контейнера получает иной UID и не может писать в
# uploads -> падение загрузки файлов с EACCES. Изоляции это почти не мешает
# (процесс один, секреты приходят через env), а деплой остаётся «git pull &&
# docker compose up» без ручного chown.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]