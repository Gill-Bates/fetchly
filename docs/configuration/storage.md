# Storage & Retention

Everything fetchly persists lives under one directory: `DATA_DIR` (`/app/data` in the
container). Mount it, back it up, and the instance is fully reproducible.

## Layout

```text
$DATA_DIR/
├── jobs.db               # SQLite (WAL): jobs, settings, share links, analysis cache
├── jobs.db-wal
├── jobs.db-shm
├── <job-uuid>/           # One directory per job
│   ├── <title>.source.<ext>
│   ├── <title>.mp3
│   ├── thumbnail.jpg
│   ├── trim_<start>_<end>.wav
│   └── trim_<...>_vocals.mp3
├── cookies/              # Per-platform Netscape jars, mode 0600
├── logo/                 # Your uploaded watermark logo, if any
├── thumb-cache/          # Cached remote thumbnails
├── watermark-cache/      # Rendered video watermark badges
├── update_check.json     # Cached upstream release check
└── .cache/torch/         # beat_this model checkpoint (~81 MB)
```

!!! danger "This directory is a credentials store"
    `cookies/` holds live, signed-in sessions for your own platform accounts, and
    `jobs.db` holds the admin password hash and your Lalal.ai activation key. Restrict
    its permissions and encrypt your backups.

## Retention

**Settings → General → Retention** (`0`–`365` days, default `0`)

`0` means unlimited: job files are kept until you remove them explicitly. Any other
value is the number of days a finished job's artifacts survive.

## The housekeeping sweep

A background task runs **every hour** and, in one pass:

1. Reads the current retention setting
2. Deletes the artifacts of jobs past their retention age
3. Deletes the **share links** for those jobs — their targets are gone, so the links
   could only 404 from here on, and the table would otherwise grow without bound
4. Removes thumbnail cache entries older than **7 days**
5. Removes **orphaned directories** — directories on disk with no matching job row

!!! info "Retained rows protect their directories"
    The orphan sweep only removes directories with no corresponding job record, and it
    skips anything younger than a 15-minute grace period so a directory being written
    by a running job is never mistaken for an orphan.

## Manual cleanup

| Action | Where | Effect |
|---|---|---|
| Remove all jobs | Settings → System | Removes every row and every directory |
| Delete a trim | Job page | Removes trim outputs, keeps the source |

There is no per-job delete action in the UI or API — only the bulk
**Remove all jobs** action and the automatic retention sweep remove job rows and their
directories.

!!! warning "Removing jobs breaks their share links"
    Links to a removed job return the standard "link unavailable" page immediately.

## Disk usage

**Settings → System → Host resources** shows storage alongside CPU, RAM, and uptime.

```bash
du -sh data/
du -sh data/*/ | sort -rh | head
```

Video jobs at `max` quality dominate. If space is tight:

- Set a retention period instead of leaving it unlimited
- Prefer `720p` or audio-only where the source resolution does not matter
- Cap the input in **Settings → General → Runtime limits**

## The database

SQLite in WAL mode. On a clean shutdown the WAL is checkpointed and the database
closed, which is why the container's `stop_grace_period` must stay above
`GRACEFUL_TIMEOUT` — a kill mid-checkpoint leaves the WAL to be replayed on next start.

The schema migrates forward automatically at startup; there is no manual migration
step.

## Backup and restore

=== "Backup"

    ```bash
    docker compose stop
    tar czf fetchly-backup-$(date +%F).tar.gz data/
    docker compose start
    ```

=== "Restore"

    ```bash
    docker compose down
    rm -rf data/
    tar xzf fetchly-backup-2026-09-01.tar.gz
    docker compose up -d
    ```

Stopping first is what makes the copy consistent. A hot copy can catch the database
mid-write.

!!! tip "A smaller backup"
    To back up only the configuration and job history, take `jobs.db*` and `cookies/`
    and skip the job directories. You lose the downloaded media but keep every setting,
    credential, and record.

## Moving to another host

The data directory is portable across hosts of the same architecture family — it is
SQLite plus plain files. Copy it, point the new instance's `DATA_DIR` at it, and set the
**same `FETCHLY_SECRET_KEY`** if you want existing sessions to survive.
