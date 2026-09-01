FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared
COPY services/ingestor-trello/pyproject.toml ./
COPY services/ingestor-trello/src/ ./src/
RUN pip install --no-cache-dir -e .
# Непривилегированный пользователь. Раньше все контейнеры шли под root — на
# хосте с общим демоном и смонтированным docker.sock (контейнер prune) это
# лишняя ступень для эскалации. Ставится ПОСЛЕ pip install: установка пишет в
# системный site-packages.
RUN useradd --system --no-create-home --uid 10001 vera && chown -R vera /app
USER vera

CMD ["python", "-m", "ingestor_trello.poller"]
