FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set fund false \
    && npm config set audit false
WORKDIR /app
COPY shared/ /shared
RUN pip install --no-cache-dir /shared
COPY vera-core/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vera-core/app/ ./app/
COPY vera-core/tests/ ./tests/
COPY vera-core/pytest.ini ./
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
