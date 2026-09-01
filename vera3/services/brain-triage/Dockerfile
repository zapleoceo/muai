FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared

COPY services/brain-triage/pyproject.toml ./
COPY services/brain-triage/src/ ./src/
RUN pip install --no-cache-dir -e .

# Непривилегированный пользователь. Раньше все контейнеры шли под root — на
# хосте с общим демоном и смонтированным docker.sock (контейнер prune) это
# лишняя ступень для эскалации. Ставится ПОСЛЕ pip install: установка пишет в
# системный site-packages.
RUN useradd --system --no-create-home --uid 10001 vera && chown -R vera /app
USER vera

# Единственный сервис с репликами и с реальным сценарием «процесс жив, работы
# не делает» (исчерпание пула фоновыми задачами). HTTP-порта у него нет,
# поэтому признак живости — файл-отметка, которую main_loop обновляет в начале
# КАЖДОЙ итерации. Через БД делать нельзя: недоступность Postgres уронила бы
# разом все пять реплик, хотя чинить надо не их.
# start-period 60s: первая отметка ставится только после старта цикла.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python -m brain_triage.heartbeat || exit 1

CMD ["python", "-m", "brain_triage.worker"]
