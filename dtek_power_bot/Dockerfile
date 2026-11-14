FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt /app/
# libatomic1: новіші Playwright запускають свій Node, якому потрібна ця бібліотека, а в slim-образі її немає
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app/ /app/
CMD ["python", "-u", "/app/bot.py"]
