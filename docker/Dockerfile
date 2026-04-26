FROM debian:trixie-slim

# Pinned yt-dlp version for reproducibility
ARG YTDLP_VERSION=2025.03.31

# Install dependencies without recommends to keep image slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp with pinned version
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/yt-dlp \
    -o /usr/local/bin/yt-dlp \
    && chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

# Create venv and install Python dependencies
COPY requirements.txt .
RUN python3 -m venv /venv && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/venv/bin:$PATH"

# Create non-root user
RUN useradd -m appuser && \
    chown -R appuser:appuser /app

# Copy application files
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Prepare data directory with correct permissions
RUN mkdir -p /app/data/downloads /app/data/logs && \
    chown -R appuser:appuser /app/data

VOLUME ["/app/data"]

ENV DATA_DIR=/app/data \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000 \
    UVICORN_WORKERS=1 \
    MAX_RESTARTS=5 \
    RESTART_DELAY=3

# Health check for orchestration
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=2 \
    CMD curl -f http://localhost:8000/ || exit 1

USER appuser

ENTRYPOINT ["/entrypoint.sh"]
