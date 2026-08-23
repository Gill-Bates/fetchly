# tubeyou

tubeyou is a FastAPI application for authenticated YouTube download jobs with a live dashboard, SQLite persistence, waveform-based audio trimming, and optional Lalal.ai stem separation.

It targets Linux and Python 3.13, and runs locally via `run.py` or in Docker via Gunicorn with Uvicorn workers.

## Table of Contents

- [Quickstart](#quickstart)
  - [Docker](#docker)
- [Bundled/Used Applications](#bundledused-applications)

## Quickstart

Requirements: Linux, Python 3.13, `ffmpeg`, `yt-dlp` and [deno](https://deno.land) in `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install yt-dlp yt-dlp-ejs

export TUBEYOU_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export TUBEYOU_ADMIN_PASSWORD="change-me"

python run.py
```

Open `http://127.0.0.1:8000` and sign in with username `admin` (or `TUBEYOU_ADMIN_USER`) and the password from `TUBEYOU_ADMIN_PASSWORD`.

### Docker

```bash
docker build -f docker/Dockerfile -t tubeyou .
docker run --rm \
	-p 8000:8000 \
	-e TUBEYOU_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
	-e TUBEYOU_ADMIN_PASSWORD="change-me" \
	-v "$PWD/data:/app/data" \
	tubeyou
```

Note: `docker/docker-compose.yml` and the included `Caddyfile` are deployment-specific examples (fixed network, addresses, and domain) — review and adjust before using them as-is.

## Bundled/Used Applications

- [ffmpeg](https://ffmpeg.org/) — media transcoding, audio trimming, and waveform generation
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — downloading video/audio from YouTube
- [yt-dlp-ejs](https://github.com/yt-dlp/ejs) + [deno](https://deno.land) — JavaScript challenge solving, required by yt-dlp for full YouTube support
- [wavesurfer.js](https://wavesurfer.xyz/) — browser-based waveform display and trim UI
- [Gunicorn](https://gunicorn.org/) + [Uvicorn](https://www.uvicorn.org/) — ASGI server used in Docker
- [Lalal.ai](https://www.lalal.ai/) (optional, external API) — vocals/instrumental stem separation

The Docker image already installs `ffmpeg`, `yt-dlp`, `yt-dlp-ejs`, `deno`, and all Python dependencies. Both come from upstream rather than from Debian — `ffmpeg`/`ffprobe` as a static [BtbN build](https://github.com/BtbN/FFmpeg-Builds) of the newest stable ffmpeg release series (not master/nightly), `yt-dlp` as the newest stable PyPI release, both refetched whenever a newer one exists — and the distro packages are pinned out via `docker/apt-no-distro-media.pref` so they cannot shadow them. `deno` is the JavaScript runtime yt-dlp-ejs executes; yt-dlp resolves it from `PATH` by itself, so no configuration is required.

---
<br/>
<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>