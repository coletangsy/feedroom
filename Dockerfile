FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FEEDROOM_STATE_DB=/data/threads.sqlite3

WORKDIR /app
COPY pyproject.toml README.md ./
COPY feedroom ./feedroom
RUN pip install --no-cache-dir '.[r2]' \
    && groupadd --gid 10001 feedroom \
    && useradd --uid 10001 --gid feedroom --no-create-home feedroom \
    && install -d -o feedroom -g feedroom /data

USER feedroom
ENTRYPOINT ["feedroom"]
CMD ["preview"]

FROM base AS cards

USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 fonts-noto-cjk \
    && pip install --no-cache-dir '.[cards]' \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
USER feedroom

FROM base AS runtime
