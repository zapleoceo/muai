FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared aiosqlite asyncpg && rm -rf /tmp/shared
COPY services/sync-bridge/src/ ./src/
ENV PYTHONPATH=/app/src
CMD ["python", "-m", "sync_bridge.main"]
