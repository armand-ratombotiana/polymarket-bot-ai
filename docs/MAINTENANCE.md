# Maintenance & Backup Operations

Operational runbook for the Polymarket bot's backup, restore, DB maintenance,
and health-check scripts. All scripts live in `/home/z/my-project/scripts/`
and are designed to be cron-friendly (no TTY required, no interactive input
unless explicitly destructive).

## TL;DR

```bash
# Install the cron schedule (idempotent, safe to re-run)
./scripts/setup-cron.sh

# Manual one-off backup (auto-verifies the result)
./scripts/backup.sh

# Verify a specific backup manually
python3 scripts/verify_backup.py /home/z/my-project/backups/<timestamp>

# Apply GFS rotation (dry-run first, then for real)
python3 scripts/backup_rotation.py --dry-run
python3 scripts/backup_rotation.py

# Deep integrity check on live DBs (integrity_check + orphans + bloat + indices)
python3 scripts/check_integrity.py

# Round-trip test: backup → restore → compare for every live DB
python3 scripts/test_restore.py

# Restore from a specific timestamp
./scripts/restore.sh 20260903_140000

# Run DB maintenance immediately (VACUUM + ANALYZE + integrity_check)
./scripts/db-maintenance.sh

# Health check (used by cron + alerting)
./scripts/health-check.sh && echo "all good" || echo "investigate"
```

---

## 1. Backup strategy

### What's backed up

| Asset class                | Source location                                | Notes |
|----------------------------|------------------------------------------------|-------|
| SQLite databases (`.db`)   | `mini-services/polymarket-bot/data/*.db`       | Uses SQLite Online Backup API (safe while bot is live) |
| Config JSONs               | `mini-services/polymarket-bot/data/*.json`     | `model_registry.json`, `store_state.json`, `vector_index.json` |
| Vector store               | `mini-services/polymarket-bot/data/*.npz`      | `vector_store.npz` (ML embeddings) |

**Not** backed up:
- Python source code (managed by git — use git, not these scripts)
- Node modules / virtualenvs (regenerable from `package.json` / `requirements.txt`)
- Logs (rotated in place by the logger, intentionally ephemeral)

### When

- **Cadence:** every 6 hours at minute 15 (00:15, 06:15, 12:15, 18:15 UTC).
  This produces ≤4 snapshots per day, balancing RPO (≤6h of trade data loss
  in the worst case) against disk usage.
- **Retention:** 14 days (≥56 snapshots retained). Older backups are pruned
  at the end of every backup run via `find -mtime +14`.
- **Trigger:** cron job installed by `setup-cron.sh`. Manual runs welcome.

### Where

- **Default root:** `/home/z/my-project/backups/`
- **Layout:**
  ```
  backups/
    20260903_141500/         # one dir per backup
      audit_trail.db.gz
      closed_positions.db.gz
      decision_ledger.db.gz
      ...
      model_registry.json
      store_state.json
      vector_index.json
      vector_store.npz
      MANIFEST.txt           # host, timestamp, file list, SHA256 of each .db.gz
    20260903_201500/
    ...
    pre_restore_20260904_101530/   # safety snapshot taken by restore.sh
      ...
  ```

### How (technique)

- The script uses `sqlite3 .backup` (or its Python equivalent
  `sqlite3.Connection.backup()` when the `sqlite3` CLI is unavailable).
  Both call the same underlying [SQLite Online Backup API](https://sqlite.org/backup.html):
  - **Read source DB without holding a long-lived lock.** Other writers can
    proceed during the copy.
  - **Atomic page-by-page copy** — no torn writes even if the bot is mid-trade.
  - **The destination is a fresh file**, not a copy of the source path —
    WAL/SHM sidecar files don't follow the backup, so the backup is a
    self-contained snapshot.
- Each `.db` is gzipped after backup (typical 60-80% compression on these
  schemas — SQLite leaves a lot of slack in `BLOB` and `TEXT` pages).
- A `MANIFEST.txt` records host, timestamp, file list, byte sizes, and the
  SHA256 of each compressed DB so restore-time integrity is end-to-end
  verifiable.

### Env vars

| Var              | Default                                                  | Purpose |
|------------------|----------------------------------------------------------|---------|
| `BOT_DATA_DIR`   | `/home/z/my-project/mini-services/polymarket-bot/data`   | Source data dir |
| `BACKUP_DIR`     | `/home/z/my-project/backups`                             | Destination root |
| `RETENTION_DAYS` | `14`                                                     | Days to keep |
| `LOG_FILE`       | (empty → stderr only)                                    | Optional log file |

### Manual backup (one-off)

```bash
# Default everything
./scripts/backup.sh

# Custom retention / location
RETENTION_DAYS=30 BACKUP_DIR=/mnt/nas/polymarket-backups ./scripts/backup.sh
```

---

## 2. Restore procedure

> **DANGER ZONE.** Restore OVERWRITES live databases. The script forces you
> to type `RESTORE` to confirm unless `--force` is passed.

### Pre-restore checklist

1. **Identify the backup timestamp** to restore from:
   ```bash
   ls -1 /home/z/my-project/backups/
   # → 20260903_141500
   # → 20260903_201500
   # → ...
   ```
2. **Notify users** that the bot will be down for ~2-5 minutes.
3. **Verify the backup is intact** (optional but recommended):
   ```bash
   cat /home/z/my-project/backups/<timestamp>/MANIFEST.txt
   cd /home/z/my-project/backups/<timestamp>/ && sha256sum -c <(sha256sum *.db.gz)
   ```

### Restore workflow (what the script does)

The script automates the entire workflow below. Manual steps are listed
only for understanding:

1. **Snapshot current live DBs** to `backups/pre_restore_<ts>/`.
   This is a safety net — even after a successful restore, the pre-restore
   snapshot is NEVER deleted, so you can roll back manually if needed.
2. **Stop the bot:**
   - `systemctl stop polymarket-bot` (if systemd unit exists)
   - `pkill -f "mini-services/polymarket-bot/main.py"`
   - `pkill -f "uvicorn.*api.server"`
   - `pkill -f "supervisord.*polymarket"`
   - 2-second sleep to let in-flight writes flush.
3. **Gunzip each `.db.gz`** into `$BOT_DATA_DIR`, overwriting the live DB.
   WAL/SHM sidecar files are removed first to prevent stale WAL data from
   "resurrecting" pre-restore rows.
4. **Restore config JSONs** (model_registry, store_state, vector_index).
5. **Run `PRAGMA integrity_check`** on every restored DB.
   - All-OK → success, log shows the safety-snapshot path, exits 0.
   - Any FAIL → exits 2, prints roll-forward instructions.

### Restore command

```bash
# Interactive (recommended)
./scripts/restore.sh 20260903_141500

# Non-interactive (for scripted / off-hours restore)
./scripts/restore.sh 20260903_141500 --force
```

### Post-restore

1. Restart the bot:
   ```bash
   sudo systemctl start polymarket-bot   # or whatever your deployment uses
   ```
2. Verify health:
   ```bash
   ./scripts/health-check.sh | jq .
   ```
3. **Keep the pre-restore snapshot for ≥7 days** in case a downstream
   consumer surfaces a data incompatibility you didn't catch in testing.
   Only manually `rm -rf backups/pre_restore_<ts>/` once you're confident.

### Rolling back a restore

If the restored backup is bad:

```bash
# Stop the bot
sudo systemctl stop polymarket-bot

# Copy the pre-restore snapshot BACK over the live DBs
cp /home/z/my-project/backups/pre_restore_<ts>/*.db \
   /home/z/my-project/mini-services/polymarket-bot/data/

# Restart
sudo systemctl start polymarket-bot
```

---

## 3. DB maintenance schedule

`db-maintenance.sh` runs three SQLite maintenance commands on every `.db`
in the data dir:

| Command                  | Effect                                                       | Locking impact |
|--------------------------|--------------------------------------------------------------|----------------|
| `PRAGMA integrity_check` | Verifies all pages are readable + indices match data.       | Read-only, no lock. |
| `VACUUM`                 | Reclaims free pages, compacts the file, rebuilds row order. | Brief write lock. Bot may see `SQLITE_BUSY` for a few hundred ms — already retried by the bot's persistence layer. Skipped (not fatal) if DB is locked mid-trade. |
| `ANALYZE`                | Updates `sqlite_stat1` so the query planner picks good indices. | Read-only, no lock. |

### Schedule

- **Cadence:** daily at 03:00 local time (lowest-traffic hour).
- **Trigger:** cron job installed by `setup-cron.sh`.

### When to run manually

- After a large bulk import (e.g. backfilling historical market data) —
  VACUUM will reclaim the bloat.
- After an unclean shutdown / crash — integrity_check confirms no
  page corruption.
- After upgrading SQLite version — ANALYZE refreshes planner stats
  for the new planner.

### Manual run

```bash
./scripts/db-maintenance.sh
# View the log
tail -50 /home/z/my-project/logs/db-maintenance.log
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0    | All DBs processed cleanly. |
| 3    | One or more DBs failed integrity_check (VACUUM/ANALYZE were skipped on those DBs to avoid worsening corruption). |

### Sample output

```
[2026-09-03 03:00:00] === DB maintenance start ===
[2026-09-03 03:00:00] data_dir: /home/z/my-project/mini-services/polymarket-bot/data
[2026-09-03 03:00:00] [audit_trail.db] size before: 84K (86016 bytes)
[2026-09-03 03:00:00]   [audit_trail.db] integrity_check: OK
[2026-09-03 03:00:00]   [audit_trail.db] VACUUM: OK
[2026-09-03 03:00:00]   [audit_trail.db] ANALYZE: OK
[2026-09-03 03:00:00]   [audit_trail.db] size after:  84K (86016 bytes) — reclaimed 0 bytes
...
[2026-09-03 03:00:09] === DB maintenance complete ===
[2026-09-03 03:00:09]   processed: 8 DB(s)
[2026-09-03 03:00:09]   failed:    0 DB(s)
[2026-09-03 03:00:09]   reclaimed: 4 MB total
```

---

## 4. Health check interpretation

`health-check.sh` produces a JSON report and exits 0 (healthy) / 1 (degraded
or unhealthy). Designed for cron + a wrapper alerting tool (e.g. run on a
5-minute cron and pipe to a Slack/PagerDuty webhook if exit code is non-zero).

### JSON schema

```jsonc
{
  "timestamp": "2026-09-03T14:20:00Z",        // ISO 8601 UTC
  "status": "healthy|degraded|unhealthy",       // overall rollup
  "checks": [
    {
      "name": "frontend",                       // or "backend" / "disk" / "memory" / "db:<name>"
      "status": "ok|warn|fail",
      "detail": "HTTP 200 from http://localhost:3000"   // human-readable context
    },
    ...
  ],
  "databases": [
    { "name": "decision_ledger.db", "bytes": 97542144, "mb": "93.02", "integrity": "ok" },
    ...
  ]
}
```

### Status thresholds

| Status     | Meaning                                                | Exit code |
|------------|--------------------------------------------------------|-----------|
| `healthy`  | All checks `ok`.                                       | 0         |
| `degraded` | ≥1 check `warn` (e.g. disk > 85% full) but no `fail`. | 1         |
| `unhealthy`| ≥1 check `fail` (frontend down, DB corrupt, etc.).     | 1         |

> **Cron-friendly:** the script never crashes — a missing `free` / `curl`
> failure / `df` returning weird output all degrade to a `warn` status
> rather than a stack trace.

### Checks performed

| Check                       | What it does                                                          | Failure thresholds |
|-----------------------------|-----------------------------------------------------------------------|--------------------|
| `frontend`                  | `curl -s -o /dev/null -w '%{http_code}' http://localhost:3000`        | HTTP ≠ 200 or connection refused |
| `backend`                   | `curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/health` | HTTP ≠ 200 or connection refused |
| `disk`                      | `df -P /home/z/my-project`                                             | ≥85% → warn, ≥95% → fail |
| `memory`                    | `free -m` (Linux only — `warn` if `free` missing)                     | ≥85% → warn, ≥95% → fail |
| `db:<name>` (per-DB)        | `stat` file size + `PRAGMA integrity_check`                           | 0 bytes → fail, integrity ≠ "ok" → fail |

### Interpreting common failures

- **`frontend` fail, `backend` ok** → Next.js dev server crashed.
  `cd /home/z/my-project && bun run dev` to restart.
- **`backend` fail, `frontend` ok** → FastAPI/uvicorn down.
  `sudo systemctl status polymarket-bot` to investigate.
- **Both `frontend` and `backend` fail** → host itself unreachable or
  networking broken — check `systemctl status sshd` and `ip addr`.
- **`disk` ≥95%** → urgent. Run `./scripts/db-maintenance.sh` to VACUUM
  (can reclaim 5-20% on long-lived DBs), prune old backups
  (`find /home/z/my-project/backups -mtime +7 -exec rm -rf {} \;`),
  investigate `/tmp` and `/var/log` for runaway logs.
- **`db:<name>` integrity ≠ "ok"** → **Stop the bot immediately** and
  consult the disaster recovery plan below.

### One-shot health check from the CLI

```bash
# Pretty-printed
./scripts/health-check.sh | jq .

# Just the exit code (for shell scripts)
if ./scripts/health-check.sh >/dev/null; then
  echo "healthy"
else
  echo "unhealthy — see logs/health-check.cron.log"
fi

# Filter to just failures
./scripts/health-check.sh | jq '.checks[] | select(.status != "ok")'
```

---

## 5. Cron setup

`setup-cron.sh` installs three cron jobs into the **current user's** crontab.
It is idempotent: re-running replaces only the polymarket-bot block
(demarcated by `# >>> polymarket-bot maintenance (W12-2) >>>` /
`# <<< polymarket-bot maintenance (W12-2) <<<` marker lines), preserving
any other cron entries.

### Install

```bash
./scripts/setup-cron.sh
```

### Installed schedule

| Schedule         | Job                       | Log                                |
|------------------|---------------------------|------------------------------------|
| `15 */6 * * *`   | `backup.sh`               | `logs/backup.cron.log`             |
| `0 3 * * *`      | `db-maintenance.sh`       | `logs/db-maintenance.cron.log`     |
| `*/5 * * * *`    | `health-check.sh`         | `logs/health-check.cron.log`       |

### Verify cron is running

```bash
systemctl status cron   # Debian/Ubuntu
systemctl status crond  # RHEL/Fedora
```

If the daemon isn't running, install + enable it:
```bash
sudo apt-get install -y cron && sudo systemctl enable --now cron   # Debian
sudo dnf install -y cronie && sudo systemctl enable --now crond     # RHEL
```

### Remove all polymarket-bot cron entries

```bash
crontab -l | sed '/# >>> polymarket-bot maintenance (W12-2) >>>/,/# <<< polymarket-bot maintenance (W12-2) <<</d' | crontab -
```

### Override defaults via env vars

Cron jobs inherit a minimal env. If your paths differ from defaults, edit
the cron entries to export vars before invoking the script:

```cron
15 */6 * * * BOT_DATA_DIR=/opt/polymarket/data BACKUP_DIR=/mnt/backups /home/z/my-project/scripts/backup.sh >> /home/z/my-project/logs/backup.cron.log 2>&1
```

---

## 6. Backup verification, rotation & integrity

The `backup.sh` script creates snapshots, but creating a snapshot doesn't
prove it can be restored — SQLite can write a structurally broken DB if the
underlying filesystem has issues, if the bot crashes mid-write, or if the gzip
pass corrupts the tail. The four Python scripts in this section close that
gap by **verifying** backups, **rotating** them on a GFS schedule, **checking**
integrity of live DBs, and **round-trip testing** the backup→restore pipeline.

### 6.1 Verify a single backup

`scripts/verify_backup.py` opens each `.db` (or `.db.gz`) in a backup
directory, runs `PRAGMA integrity_check`, and confirms the expected tables
are present with row counts reported. Run it manually against any backup
timestamp:

```bash
python3 scripts/verify_backup.py /home/z/my-project/backups/20260903_141500
```

Output:
- A human-readable table of `file / size / integrity / row counts per table`.
- A `verification_report.json` written inside the backup dir for offline
  inspection.
- Exit code 0 if all DBs pass, 1 if any fail. A DB that is "skipped"
  (never existed in the deployment, e.g. `ab_tests.db` on hosts without the
  AB-testing module) does **not** count as a failure.

### 6.2 Automated verification (post-backup)

`backup.sh` calls `verify_backup.py` automatically at the end of every run.
Verification failure is logged as a **WARNING, not fatal** — the rest of the
cron schedule proceeds (other backups of unrelated DBs aren't penalised),
and the operator sees the warning in `logs/backup.cron.log`:

```
[2026-09-03 14:15:09] Verifying backup integrity...
[2026-09-03 14:15:10]   Verification: PASS
```

To run verification independently on a schedule (e.g. hourly verification of
the latest backup without doing a fresh backup):

```cron
30 */2 * * * python3 /home/z/my-project/scripts/verify_backup.py $(ls -1dt /home/z/my-project/backups/2[0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9] | head -1) >> /home/z/my-project/logs/verify-backup.cron.log 2>&1
```

### 6.3 What to do if verification fails

1. **Don't restore from the bad backup.** The verification failure means the
   backup is not safe to use as a restore source. Restore from the most
   recent *passing* backup instead:

   ```bash
   # List recent backup dirs + whether they verify.
   for d in /home/z/my-project/backups/2*/; do
     [ -d "$d" ] || continue
     python3 /home/z/my-project/scripts/verify_backup.py "$d" >/dev/null 2>&1 \
       && echo "OK    $d" \
       || echo "FAIL  $d"
   done
   ```

2. **Investigate the cause.** Common causes:
   - **Disk full at backup time** → SQLite wrote a partial page. Check
     `df -h` and `./scripts/health-check.sh | jq '.checks[] | select(.name=="disk")'`.
   - **Gzip interrupted** → the `.db.gz` file is truncated. Confirm with
     `gunzip -t /path/to/backup/file.db.gz`.
   - **Schema drift** → a migration added a new table or column since the
     verifier was last updated. Compare `EXPECTED_TABLES` in `verify_backup.py`
     with `SELECT name FROM sqlite_master WHERE type='table'` on the live DB.
   - **Backup is missing a DB entirely** → `backup.sh` couldn't read a DB
     (e.g. it was locked mid-trade). Check the backup cron log for the
     corresponding `sqlite_backup` failure message.

3. **If the LIVE DB is also corrupt** → that's a much bigger problem. See
   §7 (Disaster recovery, Scenario B). Run `./scripts/check_integrity.py`
   to triage.

### 6.4 Backup rotation (GFS policy)

`scripts/backup_rotation.py` applies a Grandfather-Father-Son retention
policy on top of `backup.sh`'s simple `find -mtime +N` pruning:

| Tier         | Bucket   | Retain                                            |
|--------------|----------|---------------------------------------------------|
| Son          | Daily    | Most recent backup per calendar day, last 7 days   |
| Father       | Weekly   | Most recent backup per ISO week, last 4 weeks      |
| Grandfather  | Monthly  | Most recent backup per calendar month, last 12 months |
| Ceiling      | Max age  | Hard prune after 90 days (override `--max-age-days`) |

The newest backup is ALWAYS kept even if it falls outside all buckets — we
never leave the operator with zero restore points. This means a 30-day-old
backup may be pruned even though it's inside the monthly window, but only if
the same calendar month already has a newer representative.

```bash
# Preview what would be pruned (no deletion):
python3 scripts/backup_rotation.py --dry-run

# Apply the rotation:
python3 scripts/backup_rotation.py

# Custom max age + JSON output (for monitoring ingestion):
python3 scripts/backup_rotation.py --max-age-days 180 --json > /tmp/rotation.json
```

Recommended cadence: weekly (e.g. Sunday 02:00), chained after
`db-maintenance.sh`. Add to cron alongside the existing entries:

```cron
0 2 * * 0 python3 /home/z/my-project/scripts/backup_rotation.py >> /home/z/my-project/logs/backup-rotation.cron.log 2>&1
```

`pre_restore_*` directories (created by `restore.sh` as safety snapshots)
are NEVER pruned by this script — they must be removed manually once the
operator is confident the restore succeeded.

### 6.5 Live data integrity checker

`scripts/check_integrity.py` runs deeper checks than `db-maintenance.sh`'s
`PRAGMA integrity_check`:

- **Structural**: `PRAGMA integrity_check` + `PRAGMA quick_check` on every
  live DB (opened read-only via `file:...?mode=ro`).
- **Foreign keys**: `PRAGMA foreign_key_check` (only finds declared FKs; most
  of our tables use logical / undeclared FKs).
- **Logical orphans**: cross-DB relationships — e.g. every non-NULL
  `execution_quality.decision_id` should exist in
  `decision_ledger.decision_events`. Implemented via `ATTACH DATABASE` so the
  check works across DB files. The `decision_rejections.decision_id`
  relationship is "soft" (the bot can log a rejection before allocating a
  decision_id) and reported as info only.
- **Table bloat**: row counts vs configurable soft ceilings. Exceeding a
  ceiling is a WARNING, not an error. Default ceilings are derived from the
  documented retention policy with a 2x safety margin; override per-table via
  `--bloat-ceiling db=table=N`.
- **Index health**: every index listed via `PRAGMA index_list`; whether
  `sqlite_stat1` (the ANALYZE output) has stats for it. A missing stat is
  a hint that `db-maintenance.sh` hasn't been run recently.

```bash
# Run all checks, human-readable output:
python3 scripts/check_integrity.py

# JSON output for monitoring:
python3 scripts/check_integrity.py --json

# Override a bloat ceiling:
python3 scripts/check_integrity.py --bloat-ceiling audit_trail.db=audit_events=2000000

# Run against a test data dir:
BOT_DATA_DIR=/tmp/test-data python3 scripts/check_integrity.py
```

Exit codes: `0` (healthy), `1` (usage error), `2` (integrity failure or
orphaned records), `3` (data dir missing).

### 6.6 Restore round-trip test

`scripts/test_restore.py` proves the backup+restore pipeline end-to-end.
For each live DB it:

1. **Backs up** the live DB via the Online Backup API into a temp dir
   (read-only — live DBs are never opened write-mode).
2. **Gzips** the backup (mimics `backup.sh`).
3. **Restores** it into a fresh temp dir (mimics `restore.sh`'s gunzip step).
4. **Compares** the original vs restored DB on:
   - `PRAGMA integrity_check` on the restored copy
   - Schema (sha256 of `sqlite_master` rows)
   - Per-table row counts
   - Per-table row-content hashes (sha256 of every row tuple, sorted by
     primary key — catches row-level divergence even when counts match)
5. **Cleans up** temp files (unless `--keep-temp`).

```bash
# Default: test every live DB once.
python3 scripts/test_restore.py

# JSON output for CI / monitoring ingestion:
python3 scripts/test_restore.py --json

# Keep temp dirs for debugging a failure:
python3 scripts/test_restore.py --keep-temp
# (then inspect /tmp/restore_test_XXXX/{backup,restore}/)
```

Exit codes: `0` (all DBs round-tripped cleanly), `1` (usage error), `2`
(one or more DBs failed to round-trip), `3` (data dir missing).
Recommended cadence: daily, in a low-traffic window (e.g. 02:30 local).

---

## 7. Disaster recovery plan

### Scenario A: Host crash / filesystem corruption

1. **Provision a fresh host** with the same OS + Python/Node versions.
2. **Clone the repo** (`git clone <repo>`).
3. **Install deps** (`bun install`, `pip install -r requirements.txt`).
4. **Restore the most recent good backup:**
   ```bash
   ./scripts/restore.sh <latest_timestamp> --force
   ```
5. **Run maintenance + health check:**
   ```bash
   ./scripts/db-maintenance.sh
   ./scripts/health-check.sh | jq .
   ```
6. **Start the bot.**

**RTO target:** ~30 minutes (mostly provisioning + dep install).
**RPO target:** ≤6 hours (the worst-case gap between backup runs).

### Scenario B: Single DB corrupt (integrity_check fails)

1. **Stop the bot** (`systemctl stop polymarket-bot`).
2. **Identify the corrupt DB** from the health check report:
   ```bash
   ./scripts/health-check.sh | jq '.databases[] | select(.integrity != "ok")'
   ```
3. **Restore just that DB** from the most recent backup:
   ```bash
   # Find latest backup containing the corrupt DB
   ls /home/z/my-project/backups/

   # Gunzip JUST that one file over the live DB
   gunzip -c /home/z/my-project/backups/<ts>/<db>.db.gz \
     > /home/z/my-project/mini-services/polymarket-bot/data/<db>.db

   # Verify integrity
   python3 -c "import sqlite3; print(sqlite3.connect('/path/to/db').execute('PRAGMA integrity_check;').fetchone())"
   ```
4. **Restart the bot**, monitor for 5 minutes.

   If the corrupt DB was `decision_ledger.db`, you may lose up to 6h of
   decision-history rows — audit_trail (90-day retention) will still
   contain the audit events for that window, so the data isn't truly lost.

### Scenario C: Accidental data deletion (e.g. `DELETE FROM trades`)

1. **DON'T restart the bot** — keep the current live DB as-is.
2. **Restore from backup** using `restore.sh`. This snapshots live DBs
   first (so the post-incident state is preserved for forensics) and
   then overlays the backup.
3. **Re-apply any trades that happened between backup and incident**
   from the audit_trail (90-day retention) — the audit log records every
   decision / order event with full context.

### Scenario D: Ransomware / mass file deletion

1. **Isolate the host** (network disconnect).
2. **Pull off-site backups** (if configured — the current scripts back
   up to local disk only; for production, mount a remote NAS or S3
   bucket and override `BACKUP_DIR`).
3. **Rebuild from scratch** using Scenario A.
4. **Rotate all secrets** (API keys, JWT signing keys, etc.) — assume
   compromise.

### Off-site backup (recommended for production)

The default `BACKUP_DIR=/home/z/my-project/backups` is local-only — fine
for dev/staging but NOT a real disaster-recovery solution. For production,
mount a remote filesystem or sync to object storage:

```bash
# After local backup.sh runs, rsync to a remote host:
rsync -avz --delete /home/z/my-project/backups/ \
  backup-user@nas.internal:/backups/polymarket-bot/

# Or sync to S3 (requires awscli configured):
aws s3 sync /home/z/my-project/backups/ \
  s3://my-backup-bucket/polymarket-bot/ \
  --delete --storage-class STANDARD_IA
```

Add either of these as a 4th cron entry (post-backup) or chain to the
end of `backup.sh` via a wrapper script.

---

## 8. File inventory

| File                                | Purpose                              | Default cron schedule |
|-------------------------------------|--------------------------------------|----------------------|
| `scripts/backup.sh`                 | Create timestamped DB backups (auto-verifies) | `15 */6 * * *` |
| `scripts/restore.sh`                | Restore DBs from a backup timestamp  | (manual) |
| `scripts/db-maintenance.sh`         | VACUUM + ANALYZE + integrity_check    | `0 3 * * *` |
| `scripts/health-check.sh`           | JSON health report, exit 0/1         | `*/5 * * * *` |
| `scripts/setup-cron.sh`             | Install (or refresh) cron entries     | (one-time, manual) |
| `scripts/verify_backup.py`          | Verify a backup dir (integrity + tables + row counts) | invoked by `backup.sh` |
| `scripts/backup_rotation.py`       | GFS retention: daily×7d / weekly×4w / monthly×12m / max-age×90d | weekly (manual) |
| `scripts/check_integrity.py`        | Deep live-DB integrity check (orphans + bloat + indices) | daily (manual) |
| `scripts/test_restore.py`           | Round-trip test: backup → restore → compare | daily (manual) |
| `docs/MAINTENANCE.md`               | This document                        | — |
| `logs/backup.cron.log`              | Backup script output                 | appended by cron |
| `logs/db-maintenance.cron.log`      | Maintenance script output            | appended by cron |
| `logs/health-check.cron.log`        | Health check JSON reports            | appended by cron |
| `logs/backup-rotation.cron.log`     | Rotation script output               | appended by cron |
| `logs/verify-backup.cron.log`       | (Optional) hourly verifier output    | appended by cron |

---

## 9. Testing the scripts (without affecting production)

```bash
# Syntax check (no execution)
bash -n scripts/*.sh
python3 -m py_compile scripts/*.py

# Backup into a tmp dir (no impact on production backups)
BOT_DATA_DIR=/tmp/test-data BACKUP_DIR=/tmp/test-backups ./scripts/backup.sh

# Verify the test backup
python3 scripts/verify_backup.py /tmp/test-backups/$(ls /tmp/test-backups | tail -1)

# Run health check (read-only — always safe)
./scripts/health-check.sh | jq .

# Run db-maintenance on a COPY of your DBs (don't touch live data):
cp -r /home/z/my-project/mini-services/polymarket-bot/data /tmp/test-data
BOT_DATA_DIR=/tmp/test-data ./scripts/db-maintenance.sh

# Deep integrity check on live DBs (read-only — always safe)
python3 scripts/check_integrity.py

# Round-trip test (read-only on live DBs, writes only to /tmp)
python3 scripts/test_restore.py

# Rotation dry-run (no deletion)
python3 scripts/backup_rotation.py --dry-run
```
