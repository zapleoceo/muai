FROM python:3.12-slim

WORKDIR /app
COPY shared /app/shared
COPY services/ingestor-instagram /app/svc

RUN pip install --no-cache-dir -e /app/shared && \
    pip install --no-cache-dir -e /app/svc

CMD ["python", "-m", "ingestor_instagram"]
