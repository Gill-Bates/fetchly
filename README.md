<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/img/fetchly_white.svg">
    <img src=".github/img/fetchly_black.svg" alt="fetchly" width="400">
  </picture>
</p>

<p align="center">
  <b>Self-hosted media downloader for YouTube, TikTok, Instagram &amp; Facebook.</b><br>
  Paste a link, preview it, pick a format — with a live job dashboard, waveform audio trimming, and Lalal.ai stem separation.
</p>

<p align="center">
  <a href="https://github.com/Gill-Bates/fetchly/releases"><img src="https://img.shields.io/github/v/release/Gill-Bates/fetchly?logo=github&logoColor=white" alt="GitHub Release"></a>
  <a href="https://hub.docker.com/r/giiibates/fetchly"><img src="https://img.shields.io/docker/pulls/giiibates/fetchly?logo=docker&logoColor=white" alt="Docker Pulls"></a>
  <a href="https://hub.docker.com/r/giiibates/fetchly"><img src="https://img.shields.io/docker/image-size/giiibates/fetchly?logo=docker&logoColor=white" alt="Docker Image Size"></a>
  <br>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Platform-linux%2Famd64%20|%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform">
</p>

---

<p align="center">
  <img src=".github/img/screen_1.jpeg" alt="fetchly dashboard: link input, video preview, format picker, and recent downloads" width="800"><br>
  <em>Dashboard — paste a link, preview the media, pick a format, and track every job live.</em>
</p>

<p align="center">
  <img src=".github/img/screen_2.jpeg" alt="fetchly login screen" width="800"><br>
  <em>Authenticated login with invisible, server-verified anti-bot protection.</em>
</p>

## Why fetchly

- **A web UI, not the CLI** — no `yt-dlp` flags to remember; format and quality are picked from menus.
- **It stays running** — a persistent dashboard for queued, active, done, and failed jobs, not a one-shot command.
- **More than downloading** — trim audio on a waveform and split vocals from instrumentals without leaving the app.
- **Share with friends and family** — hand out an unguessable link to a finished download; recipients need no account, and you can cap how often each link works.
- **Self-hosted** — a single container, SQLite on a mounted volume, authenticated access, and a hardened default setup.

## Features

| Feature | What you get |
| --- | --- |
| **Download everywhere** | Save video or audio from YouTube, TikTok, Instagram, and Facebook in one place. |
| **Stay in control** | Follow every queued, active, completed, or failed job from a live dashboard. |
| **Choose your quality** | Pick the format and quality you want, then let fetchly handle the conversion. |
| **Trim with precision** | Cut audio visually with an interactive waveform before you download or process it further. |
| **Create clean stems** | Connect Lalal.ai to separate vocals and instrumentals when you need production-ready tracks. |
| **Share what you made** | Send friends and family a share link to a finished download — no account needed on their side, with an optional limit on how many times it can be used. |
| **Keep it private** | Run everything yourself with persistent storage, authentication, CSRF protection, anti-bot checks, and login rate limiting. |
| **Ready to run** | Get the complete application and its required dependencies in one Docker image. |

## Supported platforms

| | Platform | Video | Audio |
| --- | --- | --- | --- |
| <picture><source media="(prefers-color-scheme: dark)" srcset=".github/img/social/youtube_white.svg"><img src=".github/img/social/youtube_black.svg" alt="YouTube" width="24"></picture> | **YouTube** | ✅ | ✅ |
| <picture><source media="(prefers-color-scheme: dark)" srcset=".github/img/social/tiktok_white.svg"><img src=".github/img/social/tiktok_black.svg" alt="TikTok" width="24"></picture> | **TikTok** | ✅ | ✅ |
| <picture><source media="(prefers-color-scheme: dark)" srcset=".github/img/social/instagram_white.svg"><img src=".github/img/social/instagram_black.svg" alt="Instagram" width="24"></picture> | **Instagram** | ✅ | ✅ |
| <picture><source media="(prefers-color-scheme: dark)" srcset=".github/img/social/facebook_white.svg"><img src=".github/img/social/facebook_black.svg" alt="Facebook" width="24"></picture> | **Facebook** | ✅ | ✅ |

> **Note:** Only publicly accessible URLs work — private videos will not download.

## Quickstart

Docker is the recommended way to run fetchly. The image ([Docker Hub](https://hub.docker.com/r/giiibates/fetchly)) bundles `ffmpeg`, `yt-dlp`, `yt-dlp-ejs`, and `deno` — nothing else to install.

### Docker Compose

```yaml
# compose.yaml
services:
  fetchly:
    image: giiibates/fetchly:latest
    container_name: fetchly
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      FETCHLY_SECRET_KEY: ${FETCHLY_SECRET_KEY:?generate with: openssl rand -base64 32}
    volumes:
      - ./data:/app/data
```

```bash
export FETCHLY_SECRET_KEY="$(openssl rand -base64 32)"
docker compose up -d
```

A fuller compose file with logging, timezone, and reverse-proxy notes lives in [`docker/docker-compose.yml`](docker/docker-compose.yml).

### docker run

```bash
docker run --rm \
  -p 8000:8000 \
  -e FETCHLY_SECRET_KEY="$(openssl rand -base64 32)" \
  -v "$PWD/data:/app/data" \
  giiibates/fetchly:latest
```

Then open <http://127.0.0.1:8000>. Authentication is off on a fresh install — create an
admin account under **Settings → Security** to require a login.

To build the image yourself: `docker build -f docker/Dockerfile -t fetchly .`

### Standalone

Requires Linux, Python 3.13, and `ffmpeg`, `yt-dlp`, `deno` on `PATH`.

```bash
pip install -r requirements.txt && pip install yt-dlp yt-dlp-ejs
export FETCHLY_SECRET_KEY="$(openssl rand -base64 32)"
python run.py
```

## Configuration

Everything is set through environment variables (with Compose, put them in an `.env` file next to the compose file).

| Variable | Default | Description |
| --- | --- | --- |
| `FETCHLY_SECRET_KEY` | — | **Required.** Signs session cookies. Generate with `openssl rand -base64 32`; the app refuses to start without it. |
| `FETCHLY_BEHIND_HTTPS` | `0` | Set to `1` when serving over HTTPS (e.g. behind a reverse proxy) so session cookies are marked `Secure`. |
| `TZ` | `Etc/UTC` | Container timezone, used for timestamps in the UI and logs. |
| `LOG_LEVEL` | `info` | One of `debug`, `info`, `warning`, `error`. |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | Bind address for the standalone server (`python run.py`). |

**Authentication** is off by default and there is no built-in account. Create an admin
username and password under **Settings → Security**; saving them switches the login on.
Credentials live in the database only — no environment variable can set or override them.

**Lalal.ai** stem separation is enabled by entering your Lalal.ai **activation key** under **Settings** in the app — no environment variable required.

**Cookies** for age- or login-gated YouTube, TikTok, Instagram and Facebook content are added per platform under **Settings → Integrations**. Press **Paste cookies** and follow the steps in the dialog: open the platform signed in, tick **Disable cache** in the Network tab, reload with `Ctrl+Shift+R`, then right-click the request and choose *Copy as cURL*. The hard reload matters — a page answered from the cache or a service worker shows only *provisional headers*, which carry no cookies. A bare `cookie` header, a *Copy as fetch* command, the JSON export of a cookie extension and the contents of a prepared Netscape file are accepted just as well. Each tile shows how long the stored cookies remain valid. They are stored on the data volume at `data/cookies/`, readable only by the owner. While a platform's cookies are missing or expired, its downloads simply run signed out.

### Reverse proxy

fetchly speaks plain HTTP inside the container. Terminate TLS at a proxy (Caddy, nginx, Traefik), forward to port `8000`, and set `FETCHLY_BEHIND_HTTPS=1`. An example [`Caddyfile`](Caddyfile) is included.

## Built with

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [yt-dlp-ejs](https://github.com/yt-dlp/ejs) and [deno](https://deno.land) — media extraction and YouTube JS-challenge solving
- [ffmpeg](https://ffmpeg.org/) — transcoding, audio trimming, and waveform generation
- [wavesurfer.js](https://wavesurfer.xyz/) — browser-based waveform display and trim UI
- [Gunicorn](https://gunicorn.org/) + [Uvicorn](https://www.uvicorn.org/) — ASGI server used in Docker
- [Lalal.ai](https://www.lalal.ai/) — optional vocals/instrumental stem separation

<p align="center">
  <a href="https://www.lalal.ai/">
    <img src=".github/img/lalal_ai.svg" alt="Lalal.ai" width="130">
  </a>
</p>

---

<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>
