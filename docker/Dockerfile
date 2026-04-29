FROM python:3.13-slim

ARG WAVESURFER_VERSION=7

# Fail fast + better curl behavior
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install dependencies without recommends to keep image slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ffmpeg \
    curl \
    gosu \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install latest yt-dlp (no pinning, but hardened download)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    --fail --retry 5 --retry-delay 2 --connect-timeout 5 \
    -o /usr/local/bin/yt-dlp \
    && chmod 0755 /usr/local/bin/yt-dlp \
    && /usr/local/bin/yt-dlp --version >/dev/null

WORKDIR /app

# Create venv and install Python dependencies
COPY requirements.txt ./requirements.txt
RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/venv/bin:$PATH"

# Refresh WaveSurfer assets during image build.
RUN mkdir -p /app/static/vendor/wavesurfer/dist/plugins && \
    curl -L "https://unpkg.com/wavesurfer.js@${WAVESURFER_VERSION}/dist/wavesurfer.esm.js" \
        --fail --retry 5 --retry-delay 2 --connect-timeout 5 \
        -o /app/static/vendor/wavesurfer/dist/wavesurfer.esm.js && \
    curl -L "https://unpkg.com/wavesurfer.js@${WAVESURFER_VERSION}/dist/plugins/regions.esm.js" \
        --fail --retry 5 --retry-delay 2 --connect-timeout 5 \
        -o /app/static/vendor/wavesurfer/dist/plugins/regions.esm.js

# Create non-root user
RUN useradd -u 1000 -m --no-log-init appuser

# Copy application files from the local build context
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser middleware ./middleware
COPY --chown=appuser:appuser VERSION BUILD_INFO ./
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh

# Python runtime hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Prepare data directory with correct permissions
RUN mkdir -p /app/data/downloads && \
    chown -R appuser:appuser /app/data

ENV DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKERS=auto \
    TIMEOUT=60

# Health check for orchestration
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=2 \
    CMD curl --fail --silent --max-time 2 http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
