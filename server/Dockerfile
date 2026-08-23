# amd64 only — see .github/workflows/docker.yml. Server pulls yt-dlp/ffmpeg
# binaries and behaviour that isn't worth verifying on a second architecture.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# ffmpeg is a hard runtime dependency of pipeline.py, not a dev tool.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies in their own layer so an edit to main.py doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

RUN useradd -m -u 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# /data is the volume mount point for the SQLite file (D-004: one file, one
# volume). Scratch stays under /app so the compose tmpfs mount lands on an
# already-owned, already-empty directory.
ENV PWA_YT_DB=/data/pwa-yt.db \
    PWA_YT_SCRATCH=/app/scratch \
    PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
