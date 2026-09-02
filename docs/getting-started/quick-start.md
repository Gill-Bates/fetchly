# Quick Start

Get fetchly running in under five minutes with Docker.

## Prerequisites

- A Linux host with Docker (or Docker Desktop on macOS/Windows)
- A free TCP port — `8000` in the examples below
- Roughly 2 GB of free disk space for the image, plus whatever your downloads need

!!! info "Architecture support"
    Official images are published for `linux/amd64` and `linux/arm64`.

## 1. Generate a secret key

`FETCHLY_SECRET_KEY` signs the session cookie and the invisible anti-bot token.
fetchly refuses to start without it.

```bash
export FETCHLY_SECRET_KEY="$(openssl rand -base64 32)"
```

!!! danger "Keep the key stable"
    Changing the key invalidates every existing session and every pending login form.
    Store it somewhere durable — an `.env` file next to your Compose file, or your
    secret manager.

## 2. Start the container

=== "docker run"

    ```bash
    docker run -d \
      --name fetchly \
      --restart unless-stopped \
      -p 8000:8000 \
      -e FETCHLY_SECRET_KEY \
      -v "$PWD/data:/app/data" \
      giiibates/fetchly:latest
    ```

=== "Docker Compose"

    ```yaml title="compose.yaml"
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
    docker compose up -d
    ```

## 3. Open the UI

Navigate to <http://127.0.0.1:8000>.

!!! warning "Authentication is off on a fresh install"
    There is no built-in account and no default password. Anyone who can reach the
    port has full access until you create an admin account.

## 4. Enable authentication

1. Open **Settings → Security**
2. Enter an **admin username** and **password** (twice)
3. Save — storing credentials switches the login on

Credentials live in the database only; no environment variable can set or override
them. See [Authentication](../security/authentication.md) for the session model.

## 5. Download something

1. Paste a media URL into the input on the dashboard
2. fetchly resolves the title, thumbnail, and duration
3. Pick **Video** or **Audio** and the quality you want
4. Submit — the job appears in the list and streams progress live

[:material-arrow-right: First Steps in detail](first-steps.md){ .md-button .md-button--primary }

## Verify the deployment

```bash
curl -fsS http://127.0.0.1:8000/health
```

```json
{"status":"ok"}
```

The container also ships a `HEALTHCHECK` that probes the same endpoint every 30
seconds, so `docker ps` reports `healthy` once the app is up.

## Where to next

<div class="grid cards" markdown>

-   :material-cog:{ .lg .middle } **Configure it**

    ---

    Environment variables, in-app settings, retention, and worker limits.

    [:octicons-arrow-right-24: Configuration](../configuration/environment.md)

-   :material-shield-lock:{ .lg .middle } **Secure it**

    ---

    Put it behind TLS, turn on authentication, and understand the rate limits.

    [:octicons-arrow-right-24: Security](../security/overview.md)

-   :material-cookie:{ .lg .middle } **Reach gated content**

    ---

    Import a signed-in browser session per platform.

    [:octicons-arrow-right-24: Platform Cookies](../features/cookies.md)

</div>
