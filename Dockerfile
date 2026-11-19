FROM python:3.13-slim

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
