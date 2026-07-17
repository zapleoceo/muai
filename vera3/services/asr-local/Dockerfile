FROM python:3.12-slim
WORKDIR /app
ENV HF_HOME=/app/hf-cache
COPY services/asr-local/pyproject.toml ./
COPY services/asr-local/src/ ./src/
RUN pip install --no-cache-dir -e .
# Модель запечена в образ: старт не зависит от сети, рестарт быстрый.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
CMD ["uvicorn", "asr_local.app:app", "--host", "0.0.0.0", "--port", "8000"]
