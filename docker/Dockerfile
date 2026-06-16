FROM python:3.13-slim AS builder

ARG WAVESURFER_VERSION=7

# Builder commands do not require bash-specific features.
SHELL ["/bin/sh", "-c"]

# Install builder dependencies without recommends.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

ENV VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH"

# Create venv and install Python dependencies.
COPY requirements.txt ./requirements.txt
RUN python -m venv "$VIRTUAL_ENV" && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Refresh WaveSurfer assets during image build.
RUN mkdir -p /build/static/vendor/wavesurfer/dist/plugins && \
    curl -L "https://unpkg.com/wavesurfer.js@${WAVESURFER_VERSION}/dist/wavesurfer.esm.js" \
        --fail --retry 5 --retry-delay 2 --connect-timeout 5 \
        -o /build/static/vendor/wavesurfer/dist/wavesurfer.esm.js && \
    curl -L "https://unpkg.com/wavesurfer.js@${WAVESURFER_VERSION}/dist/plugins/regions.esm.js" \
        --fail --retry 5 --retry-delay 2 --connect-timeout 5 \
        -o /build/static/vendor/wavesurfer/dist/plugins/regions.esm.js


FROM python:3.13-slim AS runtime

ARG WAVESURFER_VERSION=7

# Fail fast + better curl behavior
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install runtime dependencies without recommends to keep image slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ffmpeg \
    curl \
    gosu \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the prebuilt virtualenv from the builder stage.
COPY --from=builder /venv /venv

ENV VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH"

# Install latest yt-dlp into venv via pip (kept fresh at each build).
RUN pip install --no-cache-dir yt-dlp && yt-dlp --version

# Create non-root user
RUN useradd -u 1000 -m --no-log-init appuser

# Copy application files from the local build context
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser middleware ./middleware
COPY --chown=appuser:appuser VERSION BUILD_INFO ./
COPY --from=builder --chown=appuser:appuser /build/static/vendor/wavesurfer ./app/static/vendor/wavesurfer
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

EXPOSE 8000

# Health check for orchestration
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --max-time 2 http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
