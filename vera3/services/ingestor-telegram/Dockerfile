FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared
COPY services/ingestor-telegram/pyproject.toml ./
COPY services/ingestor-telegram/src/ ./src/
RUN pip install --no-cache-dir -e .
# Под root работал до 12.11.2026: том `vera3_tg_sessions` (StringSession на
# /sessions) был создан под root, а Docker переносит владельца из образа только
# на пустой том. Перед этим деплоем на хосте один раз выполнено
#   docker run --rm -v vera3_vera3_tg_sessions:/s alpine chown -R 10001 /s
# Новый том получит владельца из образа сам: /sessions создаётся здесь под vera.
RUN useradd --system --no-create-home --uid 10001 vera     && mkdir -p /sessions && chown -R vera /app /sessions
USER vera
CMD ["python", "-m", "ingestor_telegram.userbot"]
