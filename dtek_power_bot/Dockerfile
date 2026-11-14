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

# версія — після важких шарів, інакше кожне оновлення заново ставить браузер
ARG BUILD_VERSION
ENV ADDON_VERSION=${BUILD_VERSION}
COPY app/ /app/
COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
