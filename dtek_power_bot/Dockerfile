FROM python:3.12-slim-bookworm
ARG BUILD_VERSION

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    ADDON_VERSION=${BUILD_VERSION}

WORKDIR /app
COPY requirements.txt /app/
# libatomic1: новіші Playwright запускають свій Node, якому потрібна ця бібліотека, а в slim-образі її немає
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app/ /app/
COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
