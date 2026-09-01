FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared
COPY services/ingestor-telegram/pyproject.toml ./
COPY services/ingestor-telegram/src/ ./src/
RUN pip install --no-cache-dir -e .
# ЕДИНСТВЕННЫЙ сервис, оставленный под root, и это не недосмотр.
#
# Он пишет StringSession в том `vera3_tg_sessions`, смонтированный на
# /sessions. Том в проде УЖЕ существует и принадлежит root: Docker переносит
# владельца из образа только на ПУСТОЙ том, при первом монтировании. Добавить
# сюда `USER vera` — значит получить Permission denied на записи сессии, то
# есть уронить основной источник данных сразу после деплоя, который
# запускается автоматически по пушу в master.
#
# Чтобы дожать: один раз на хосте
#   docker compose stop ingestor-telegram
#   docker run --rm -v vera3_tg_sessions:/s alpine chown -R 10001 /s
# и только потом добавлять сюда те же две строки, что у остальных десяти.
CMD ["python", "-m", "ingestor_telegram.userbot"]
