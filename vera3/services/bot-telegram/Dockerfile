FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared
COPY services/bot-telegram/pyproject.toml ./
COPY services/bot-telegram/src/ ./src/
RUN pip install --no-cache-dir -e .
CMD ["python", "-m", "bot_telegram.bot"]
