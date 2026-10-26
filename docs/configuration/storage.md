# Storage & Retention

Everything fetchly persists lives under one directory: `DATA_DIR` (`/app/data` in the
container). Mount it, back it up, and the instance is fully reproducible.

## Layout

```text
$DATA_DIR/
├── jobs.db               # SQLite (WAL): jobs, settings, share links, analysis cache
├── jobs.db-wal
├── jobs.db-shm
├── jobs/                 # Parent of all per-job directories
│   └── <job-uuid>/       # One directory per job
│       ├── <title>.source.<ext>
│       ├── <title>.mp3
│       ├── thumbnail.jpg
│       ├── trim_<start>_<end>.wav
│       └── trim_<...>_vocals.mp3
├── cookies/              # Per-platform Netscape jars, mode 0600
├── logo/                 # Your uploaded watermark logo (watermark-logo.png), if any
├── thumb-cache/          # Cached remote thumbnails
├── watermark-cache/      # Rendered video watermark badges
├── geolite2/             # GeoLite2-City.mmdb and GeoLite2-ASN.mmdb for requester lookups
├── update_check.json     # Cached upstream release check
└── .cache/torch/         # beat_this model checkpoint (~81 MB)
```

Job media lives in the per-job UUID directories under `DATA_DIR/jobs/`. The sibling
caches and credential directories (`cookies/`, `thumb-cache/`, `watermark-cache/`,
`geolite2/`, `.cache/`) stay directly under `DATA_DIR`.

!!! warning "Breaking change in 1.3.2"
    Per-job directories moved from `DATA_DIR/<uuid>/` to `DATA_DIR/jobs/<uuid>/`, and the
    unused `downloads/` directory is gone. In line with the 1.3.2 no-migration policy, an
    old `DATA_DIR` is not carried over: job directories written by an earlier release stay
    at the old top-level location and are invisible to this version. Start 1.3.2 with a
    fresh `DATA_DIR`.

Several entries appear only after first use: `thumb-cache/` with the first thumbnail,
`watermark-cache/` with the first rendered badge, `geolite2/` after the first successful
database download, `logo/` after you upload a logo, and `update_check.json` after the
first update check. `jobs.db`, `jobs/` and `cookies/` exist from startup.

| Entry | Safe to delete? |
|---|---|
| `thumb-cache/`, `watermark-cache/`, `update_check.json` | Yes. Recreated on demand. |
| `geolite2/` | Yes. Downloaded again by the next refresh. Until then requester country and ASN are not shown. |
| `.cache/torch/` | Yes, while no BPM analysis runs. The ~81 MB checkpoint is downloaded again on the next analysis. |
| `jobs/<job-uuid>/` | Not by hand while the job exists. Retention and the orphan sweep manage these (see below); a directory removed by hand leaves a job row without its files. |
| `jobs.db*`, `cookies/`, `logo/` | No. Settings, history, credentials and your logo. |

!!! danger "This directory is a credentials store"
    `cookies/` holds live, signed-in sessions for your own platform accounts, and
    `jobs.db` holds the admin password hash and your Lalal.ai activation key. Restrict
    its permissions and encrypt your backups.

## Retention

**Settings → General → Retention** (`0`–`365` days, default `0`)

`0` means unlimited: jobs are kept until you remove them explicitly. Any other value is
the number of days a finished job survives — and it is the whole job that goes, not just
its files: the row leaves the database too, so the job disappears from the job history
and its numbers leave the statistics.

## The housekeeping sweep

A background task runs **every hour** and, in one pass:

1. Reads the current retention setting. `0` means unlimited, and the sweep stops here
2. Selects the jobs past their retention age
3. Deletes those jobs' **directories** on disk
4. Deletes the **job rows** and their **share links** — but only for jobs whose
   directory was actually removed. Their targets are gone, so the links could only 404
   from here on, and the table would otherwise grow without bound
5. Removes thumbnail cache entries and rendered watermark badges older than **7 days**
6. Removes **orphaned directories** — directories on disk with no matching job row

!!! info "Why the files go first"
    A job whose directory could not be deleted keeps its row and its share links, so
    the next pass retries it. Deleting the row first would forget the job while its
    files stay on disk, leaving them to the orphan sweep in step 6 without their history
    entry or share links.

!!! info "The orphan sweep has a grace period"
    Step 6 only removes directories with no corresponding job record, and it skips
    anything younger than 15 minutes, so a directory being written by a running job is
    never mistaken for an orphan.

## Manual cleanup

| Action          | Where             | Effect                                 |
| --------------- | ----------------- | -------------------------------------- |
| Remove all jobs | Settings → System | Removes every row and every directory  |
| Delete a trim   | Job page          | Removes trim outputs, keeps the source |

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

The schema is created at startup if it is missing. There is no migration: a database
written by an older release is not upgraded. At startup an existing database whose tables lack expected columns stops the app with a `DatabaseSchemaError` that names the missing column and the database path. Remove `jobs.db` (including `jobs.db-wal` and `jobs.db-shm`) and restart to recreate it; jobs and settings are lost, so back them up first if needed.

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
