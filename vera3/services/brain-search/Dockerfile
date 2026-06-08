FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared
COPY services/brain-search/pyproject.toml ./
COPY services/brain-search/src/ ./src/
RUN pip install --no-cache-dir -e .
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["uvicorn", "brain_search.app:app", "--host", "0.0.0.0", "--port", "8000"]
