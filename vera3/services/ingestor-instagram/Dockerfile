FROM python:3.12-slim

WORKDIR /app
COPY shared /app/shared
COPY services/ingestor-instagram /app/svc

RUN pip install --no-cache-dir -e /app/shared && \
    pip install --no-cache-dir -e /app/svc

# Непривилегированный пользователь. Раньше все контейнеры шли под root — на
# хосте с общим демоном и смонтированным docker.sock (контейнер prune) это
# лишняя ступень для эскалации. Ставится ПОСЛЕ pip install: установка пишет в
# системный site-packages.
RUN useradd --system --no-create-home --uid 10001 vera && chown -R vera /app
USER vera

CMD ["python", "-m", "ingestor_instagram"]
