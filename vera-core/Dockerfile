FROM python:3.12-slim
WORKDIR /app
COPY shared/ /shared
RUN pip install --no-cache-dir /shared
COPY vera-core/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vera-core/app/ ./app/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
