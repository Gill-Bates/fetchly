<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/img/fetchly_white.svg">
    <img src=".github/img/fetchly_black.svg" alt="fetchly" width="400">
  </picture>
</p>

<p align="center">
  <b>Self-hosted media downloader for YouTube, TikTok, Instagram &amp; Facebook.</b><br>
  Paste a link, preview it, pick a format — with a live job dashboard, waveform audio trimming, BPM analysis, and Lalal.ai stem separation.
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
  <img src=".github/img/screen_2.jpeg" alt="fetchly dashboard: link input, video preview, format picker, and recent downloads" width="800"><br>
  <em>Dashboard — paste a link, preview the media, pick a format, and track every job live.</em>
</p>

<p align="center">
  <img src=".github/img/screen_1.jpeg" alt="fetchly login screen" width="800"><br>
  <em>Authenticated login with invisible, server-verified anti-bot protection.</em>
</p>

<p align="center">
  <img src=".github/img/screen_3.jpeg" alt="fetchly settings: Lalal.ai integration and per-platform cookie tiles" width="800"><br>
  <em>Integrations — connect Lalal.ai for stem separation and paste browser cookies for sign-in-only downloads.</em>
</p>

<p align="center">
  <img src=".github/img/screen_4.jpeg" alt="fetchly settings: host resources, component versions, update check, and changelog" width="800"><br>
  <em>System — host resources, component versions, one-click update checks, and the in-app changelog.</em>
</p>

## Download, shape, share

fetchly turns `yt-dlp` into a self-hosted media workspace. Pick formats in a web UI,
follow every job live, trim audio visually, find its tempo, create stems, and share
finished downloads without handing your media to another service.

## Features

| Feature | What you get |
| --- | --- |
| **Download everywhere** | Save video or audio from YouTube, TikTok, Instagram, and Facebook in one place. |
| **Stay in control** | Follow every queued, active, completed, or failed job from a live dashboard. |
| **Choose your quality** | Pick the format and quality you want, then let fetchly handle the conversion. |
| **Brand every video** | Burn the fetchly logo — or your own uploaded SVG or PNG — and your hostname, once set, into the corner of every downloaded video, or switch it off. |
| **Trim with precision** | Cut audio visually with an interactive waveform before you download or process it further. |
| **Find the tempo** | Analyze BPM and beat confidence, then use the result to guide audio trimming. |
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

> **Note:** Public URLs work without account cookies. Age- or login-gated content may work after importing a current signed-in session under **Settings → Integrations**; platforms can still reject stale or revoked sessions.

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

Open <http://127.0.0.1:8000>, then create an admin account in **Settings → Security**
before exposing fetchly beyond your local machine.

## Learn more

The [fetchly documentation](https://gill-bates.github.io/fetchly/) covers installation,
configuration, reverse proxies, security, platform cookies, the API, and troubleshooting.

---

<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>
