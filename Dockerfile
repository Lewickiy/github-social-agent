# ── Stage 1: build the dashboard frontend ───────────────────────────────
FROM node:22-slim AS frontend-build

WORKDIR /frontend

# Install deps first (layers cached unless package files change)
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime ──────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# ── Системные зависимости для torch (OpenMP и др.) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── Зависимости ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Код ──
COPY *.py .
COPY api/ api/
COPY migrations/ migrations/
COPY ml_service/ ml_service/
COPY workers/ workers/

# ── Собранный фронтенд (раздаётся FastAPI) ──
COPY --from=frontend-build /frontend/dist frontend/dist

# ── Каталог для ML-моделей ──
# Контейнер запускается от UID 1000 (см. docker-compose user), поэтому
# каталог должен быть доступен на запись этому пользователю.
RUN mkdir -p /app/models && chown 1000:1000 /app/models

# ── Volume для БД и логов (монтируются с хоста) ──
VOLUME ["/app/data"]

# ── Переменные окружения: пути внутри volume ──
ENV DATABASE=/app/data/github_social.db
ENV LOG_FILE=/app/logs/github_social.log

# По умолчанию — CLI-бот (--silent через docker-compose).
ENTRYPOINT ["python", "-u", "main.py"]
CMD ["--help"]
