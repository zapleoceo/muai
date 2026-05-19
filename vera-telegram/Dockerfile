FROM python:3.12-slim
WORKDIR /app
COPY ../shared /shared
RUN pip install /shared
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app/ ./app/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
