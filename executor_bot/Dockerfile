FROM python:3.12-slim
WORKDIR /app
COPY executor_bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY executor_bot/ ./executor_bot/
CMD ["python", "-m", "executor_bot.main"]
