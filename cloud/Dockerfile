FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    TC_LOCAL_DATA_DIR=/data HOST=0.0.0.0 PORT=8787
COPY audio-scraper /app/audio-scraper
COPY cloud/pyproject.toml cloud/uv.lock cloud/README.md /app/cloud/
COPY cloud/src /app/cloud/src
RUN uv sync --project cloud --frozen --no-dev --extra ingest \
    && useradd --uid 10001 --create-home studio \
    && mkdir -p /data && chown -R studio:studio /data /app
USER studio
EXPOSE 8787
CMD ["/app/cloud/.venv/bin/transcript-cloud-studio"]
