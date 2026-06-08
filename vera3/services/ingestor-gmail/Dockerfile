FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared
COPY services/ingestor-gmail/pyproject.toml ./
COPY services/ingestor-gmail/src/ ./src/
RUN pip install --no-cache-dir -e .
CMD ["python", "-m", "ingestor_gmail.poller"]
