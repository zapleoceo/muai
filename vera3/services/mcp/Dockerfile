FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared
COPY services/mcp/pyproject.toml ./
COPY services/mcp/src/ ./src/
RUN pip install --no-cache-dir -e .
EXPOSE 8000
# Непривилегированный пользователь — см. комментарий в services/gateway/Dockerfile.
RUN useradd --system --no-create-home --uid 10001 vera && chown -R vera /app
USER vera

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["python", "-m", "vera_mcp"]
