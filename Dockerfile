FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    TRANSCRIPT_DATA_DIR=/data HF_HOME=/models HOST=0.0.0.0 PORT=8000
COPY pyproject.toml uv.lock README.md ./
COPY audio-scraper ./audio-scraper
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home studio \
    && mkdir -p /data /models && chown -R studio:studio /data /models /app
USER studio
EXPOSE 8000
CMD ["/app/.venv/bin/transcript-web"]
