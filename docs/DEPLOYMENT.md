# Deployment Guide

This document covers production deployment of the Polymarket trading bot:
a Next.js 16 frontend served by `bun`, a Python 3.12 FastAPI backend, an
ML ensemble, and a Caddy reverse proxy terminating TLS.

For local development, see `CONTRIBUTING.md` instead.

## 1. Overview

The production stack consists of:

| Layer        | Technology                  | Port  | Notes                          |
| ------------ | --------------------------- | ----- | ------------------------------ |
| Edge gateway | Caddy 2                     | 80/443| TLS termination, reverse proxy |
| Frontend     | Next.js 16 standalone       | 3000  | `bun .next/standalone/server.js` |
| Backend API  | FastAPI on uvicorn          | 8080  | Trading engine + ML            |
| Persistence  | SQLite (file-based)         | -     | `data/*.db` under the bot dir  |

The Next.js `/api/bot` route proxies authenticated dashboard requests to
the FastAPI backend using a shared `API_TOKEN`. In production the
frontend and backend may run on the same host or split across hosts.

## 2. Prerequisites

### 2.1 Server requirements

- **OS:** Ubuntu 22.04 LTS or Debian 12 (other modern Linux works).
- **Node.js:** 20+ (only needed for the build step).
- **bun:** 1.1+ runtime, installed system-wide.
- **Python:** 3.12+.
- **RAM:** 4 GB minimum, 8 GB recommended for production (the ML
  ensemble + LightGBM can spike memory during retraining).
- **Disk:** 20 GB minimum (SQLite databases, logs, model artifacts).
- **CPU:** 2 vCPU minimum; 4 vCPU recommended when running the full ML
  ensemble and shadow inference.

### 2.2 Network and TLS

- A public domain name pointing at the server (A/AAAA record).
- Ports 80 and 443 open inbound for Caddy.
- Port 22 open for SSH (lock down to your IP if possible).
- Caddy obtains and renews Let's Encrypt certificates automatically.

### 2.3 Local tooling

- `git` for cloning and upgrades.
- `sqlite3` CLI for backups and ad-hoc inspection.
- `curl` for the post-deploy smoke checks.

## 3. Environment Configuration

All configuration is read from environment variables via `pydantic-settings`
in `mini-services/polymarket-bot/config.py`. In production, place these in
`mini-services/polymarket-bot/.env` (root-owned, mode 0600).

### 3.1 Core environment

| Variable                | Production value                            | Notes                                   |
| ----------------------- | ------------------------------------------- | --------------------------------------- |
| `TRADING_MODE`          | `paper` or `live`                           | Flip to `live` only after safety gate. |
| `PAPER_TRADE`           | `true`                                      | Mirrors `TRADING_MODE=paper`.           |
| `LIVE_TRADING_ENABLED`  | `false`                                     | Flip to `true` after the 10-check gate. |
| `API_TOKEN`             | A strong random string, 64+ chars           | Generate with `openssl rand -base64 48`. |
| `CORS_ORIGINS`          | `https://yourdomain.com`                   | Comma-separated list; never `*` in prod.|
| `LOG_LEVEL`             | `INFO`                                      | Use `WARNING` on a noisy prod host.     |
| `DASHBOARD_REFRESH_MS`  | `1000`                                      | Frontend polling interval.              |

### 3.2 Risk parameters

Set these conservatively in production. All amounts are in USDC.

| Variable                       | Default | Recommended prod start | Notes                          |
| ------------------------------ | ------- | ---------------------- | ------------------------------ |
| `MAX_OPEN_ORDERS`              | `8`     | `4`                    | Lower in prod until stable.   |
| `MAX_POSITION_PER_MARKET_USDC` | `3.0`   | `2.0`                  | Per-market cap.                |
| `MAX_TOTAL_EXPOSURE_USDC`      | `25.0`  | `10.0`                 | Total open exposure across all markets. |
| `DAILY_LOSS_LIMIT_USDC`        | `2.0`   | `1.0`                  | Trip the kill switch at this daily loss. |

### 3.3 Strategy toggles

| Variable                | Default | Notes                                              |
| ----------------------- | ------- | -------------------------------------------------- |
| `SIGNAL_ENABLED`        | `false` | Enable signal-trader strategy.                     |
| `SIGNAL_MIN_CONFIDENCE` | `0.50`  | Minimum model confidence to act on a signal.       |
| `MM_ENABLED`            | `true`  | Market-maker strategy.                             |
| `MM_SPREAD_BPS`         | `200`   | Quote spread in basis points.                      |
| `MM_QUOTE_SIZE_USDC`    | `1.5`   | Quote size per side.                                |
| `MM_MAX_INVENTORY_USDC` | `15.0`  | Inventory cap before the MM stops quoting.          |
| `ARB_ENABLED`           | `true`  | Arbitrage scanner strategy.                        |
| `ARB_MIN_PROFIT_BPS`   | `50`    | Minimum profit threshold for an arb trade.         |
| `ARB_SCAN_INTERVAL_SECONDS` | `15` | How often the arb scanner polls.                   |
| `ARB_ORDER_SIZE_USDC`  | `1.5`   | Order size for arb trades.                          |

### 3.4 Storage paths

| Variable                 | Default path (under `mini-services/polymarket-bot/`) |
| ------------------------ | --------------------------------------------------- |
| `MARKET_DB_PATH`         | `data/market_intelligence.db`                       |
| `AUDIT_DB_PATH`          | `data/audit_trail.db`                               |
| `STORE_STATE_PATH`      | `data/store_state.json`                             |
| `KILL_SWITCH_PATH`      | `data/kill_switch`                                  |
| `KILL_SWITCH_REASON_PATH` | `data/kill_switch.reason`                          |
| `MODEL_REGISTRY_PATH`   | `data/model_registry.json`                          |
| `VECTOR_STORE_PATH`     | `data/vector_index.json`                            |
| `MODEL_PATH`            | `data/model.pkl`                                    |
| `DECISION_LEDGER_DB_PATH` | `data/decision_ledger.db`                         |

If `DATABASE_URL` is set (PostgreSQL/TimescaleDB connection string), the
TimescaleDB backend in `core/timescale_db.py` will be used for time-series
storage. Otherwise, SQLite is used. For an initial deployment, leave
`DATABASE_URL` unset and rely on SQLite.

## 4. Build Steps

### 4.1 Clone and prepare

```bash
sudo mkdir -p /opt/polymarket-bot
sudo chown deploy:deploy /opt/polymarket-bot
git clone https://github.com/armand-ratombotiana/polymarket-bot-ai.git \
  /opt/polymarket-bot
cd /opt/polymarket-bot
```

### 4.2 Build the frontend

Next.js is configured to emit a standalone server (see `next.config.ts`).
The `build` script copies `.next/static` and `public/` into the standalone
output directory.

```bash
cd /opt/polymarket-bot
bun install
bun run build
```

The standalone server ends up at `.next/standalone/server.js`.

### 4.3 Install backend dependencies

```bash
cd /opt/polymarket-bot/mini-services/polymarket-bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Use a virtualenv so system Python packages are untouched and upgrades are
isolated.

### 4.4 Place production environment files

```bash
# Frontend .env (used by Next.js /api/bot proxy)
sudo cp /opt/polymarket-bot/.env /opt/polymarket-bot/.env.production
sudo chown deploy:deploy /opt/polymarket-bot/.env.production
sudo chmod 0600 /opt/polymarket-bot/.env.production
# Edit: set API_TOKEN and any production-only vars.

# Backend .env
sudo cp /opt/polymarket-bot/mini-services/polymarket-bot/.env \
        /opt/polymarket-bot/mini-services/polymarket-bot/.env
sudo chown deploy:deploy /opt/polymarket-bot/mini-services/polymarket-bot/.env
sudo chmod 0600 /opt/polymarket-bot/mini-services/polymarket-bot/.env
# Edit: set production values per section 3.
```

## 5. Start Services

### 5.1 Frontend (Next.js production server)

```bash
cd /opt/polymarket-bot
NODE_ENV=production bun .next/standalone/server.js
```

This listens on port 3000 by default. To bind a different port or host,
export `PORT` and `HOSTNAME` before launching (the standalone server
reads these).

### 5.2 Backend (FastAPI)

The Next.js dashboard will auto-start the backend on the first
`/api/bot?action=start` request from the UI. For production deployments
where the backend must always be running, start it directly:

```bash
cd /opt/polymarket-bot/mini-services/polymarket-bot
source .venv/bin/activate
python -m uvicorn api.server:app --host 0.0.0.0 --port 8080 \
  --workers 1 --log-level info
```

Use `--workers 1` because the trading engine and strategies hold
in-memory state (positions, order books, model ensembles). Multi-worker
deployments require a shared state backend (Redis or Postgres) which is
out of scope for this guide.

Alternatively, use the CLI entry point:

```bash
python main.py serve --host 0.0.0.0 --port 8080
```

## 6. Caddy Gateway Configuration

Caddy terminates TLS and reverse-proxies to the frontend (port 3000) by
default, and to a dynamic backend port when an `XTransformPort` query
parameter is present (used for ad-hoc port forwarding in development).

Place the following at `/etc/caddy/Caddyfile`:

```caddyfile
yourdomain.com {
    @transform_port_query {
        query XTransformPort=*
    }

    handle @transform_port_query {
        reverse_proxy localhost:{query.XTransformPort} {
            header_up Host {host}
            header_up X-Forwarded-For {remote_host}
            header_up X-Forwarded-Proto {scheme}
            header_up X-Real-IP {remote_host}
        }
    }

    handle {
        reverse_proxy localhost:3000 {
            header_up Host {host}
            header_up X-Forwarded-For {remote_host}
            header_up X-Forwarded-Proto {scheme}
            header_up X-Real-IP {remote_host}
        }
    }
}
```

Then reload Caddy:

```bash
sudo systemctl reload caddy
```

Caddy obtains a Let's Encrypt certificate automatically on first request
to `https://yourdomain.com`. Verify with:

```bash
curl -I https://yourdomain.com
```

## 7. Process Management

Run both services under `systemd` so they survive reboots and restart on
crash.

### 7.1 Next.js frontend service

Create `/etc/systemd/system/polymarket-web.service`:

```ini
[Unit]
Description=Polymarket Bot — Next.js frontend
After=network.target

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/opt/polymarket-bot
EnvironmentFile=/opt/polymarket-bot/.env.production
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=HOSTNAME=0.0.0.0
ExecStart=/usr/local/bin/bun /opt/polymarket-bot/.next/standalone/server.js
Restart=on-failure
RestartSec=5
StandardOutput=append:/opt/polymarket-bot/server.log
StandardError=append:/opt/polymarket-bot/server.log

[Install]
WantedBy=multi-user.target
```

### 7.2 FastAPI backend service

Create `/etc/systemd/system/polymarket-bot.service`:

```ini
[Unit]
Description=Polymarket Bot — FastAPI backend
After=network.target

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/opt/polymarket-bot/mini-services/polymarket-bot
EnvironmentFile=/opt/polymarket-bot/mini-services/polymarket-bot/.env
ExecStart=/opt/polymarket-bot/mini-services/polymarket-bot/.venv/bin/python \
    -m uvicorn api.server:app --host 0.0.0.0 --port 8080 --workers 1 \
    --log-level info
Restart=on-failure
RestartSec=5
StandardOutput=append:/opt/polymarket-bot/mini-services/polymarket-bot/server.log
StandardError=append:/opt/polymarket-bot/mini-services/polymarket-bot/server.log

[Install]
WantedBy=multi-user.target
```

### 7.3 Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-web.service
sudo systemctl enable --now polymarket-bot.service
sudo systemctl status polymarket-web.service polymarket-bot.service
```

If you prefer `pm2`, the equivalent commands are:

```bash
pm2 start "/usr/local/bin/bun /opt/polymarket-bot/.next/standalone/server.js" \
  --name polymarket-web
pm2 start "/opt/polymarket-bot/mini-services/polymarket-bot/.venv/bin/python \
  -m uvicorn api.server:app --host 0.0.0.0 --port 8080" \
  --name polymarket-bot --cwd /opt/polymarket-bot/mini-services/polymarket-bot
pm2 save
pm2 startup
```

## 8. Database Management

### 8.1 Locations

All SQLite databases live under
`/opt/polymarket-bot/mini-services/polymarket-bot/data/`. The key ones:

| File                       | Purpose                                  |
| -------------------------- | ---------------------------------------- |
| `market_intelligence.db`   | Market metadata + screener cache.        |
| `audit_trail.db`           | Append-only audit log of all actions.    |
| `decision_ledger.db`       | Decision log (accepted + rejected).      |
| `shadow_trades.db`         | Shadow-trade outcomes (ML evaluation).   |
| `execution_quality.db`     | Fill quality metrics.                    |
| `closed_positions.db`      | Historical closed positions.             |
| `observability.db`         | Observability metrics time-series.       |
| `model.pkl`                | Pickled ML ensemble.                     |
| `model_registry.json`      | Active model version metadata.           |

### 8.2 Backup strategy

Use SQLite's online backup API via the `sqlite3` CLI. This is safe to run
while the bot is writing to the database.

Create `/opt/polymarket-bot/scripts/backup-dbs.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR=/opt/polymarket-bot/backups/$(date -u +%Y%m%dT%H%M%SZ)
DATA_DIR=/opt/polymarket-bot/mini-services/polymarket-bot/data
mkdir -p "$BACKUP_DIR"

for db in "$DATA_DIR"/*.db; do
  name=$(basename "$db")
  sqlite3 "$db" ".backup '$BACKUP_DIR/$name'"
done

# Keep the last 14 days of backups.
find /opt/polymarket-bot/backups -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
```

Schedule it via cron (run as the `deploy` user):

```bash
sudo -u deploy crontab -e
# Add:
0 */6 * * * /opt/polymarket-bot/scripts/backup-dbs.sh >> /opt/polymarket-bot/backups/backup.log 2>&1
```

This backs up every 6 hours and prunes backups older than 14 days. Adjust
the retention window based on your data volume and storage budget.

### 8.3 Retention pruning

The app's retention policy (`core/retention.py`) prunes stale rows on a
schedule. Check the current policy via the Retention panel in the UI, or
query the API:

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  https://yourdomain.com/api/system/prune
```

Manual pruning is also supported:

```bash
curl -X POST -H "Authorization: Bearer $API_TOKEN" \
  https://yourdomain.com/api/system/prune
```

## 9. Monitoring

### 9.1 Health endpoints

- **Frontend** (Next.js): not instrumented; rely on the gateway's
  `/` response.
- **Backend**: `GET /api/system/health` returns server status, uptime,
  and key sub-system status.
- **Observability**: `GET /api/observability` returns 31+ metrics across
  6 categories (risk, capital, execution, ML, retention, system).

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  https://yourdomain.com/api/system/health | jq

curl -H "Authorization: Bearer $API_TOKEN" \
  https://yourdomain.com/api/observability | jq
```

The `/api/health` alias also exists for simpler probes.

### 9.2 Log files

| Log                                  | Purpose                          |
| ------------------------------------ | -------------------------------- |
| `/opt/polymarket-bot/dev.log`        | `bun run dev` output (dev only). |
| `/opt/polymarket-bot/server.log`     | Next.js production server output. |
| `/opt/polymarket-bot/mini-services/polymarket-bot/server.log` | FastAPI + uvicorn output. |

Tail all three in production:

```bash
sudo journalctl -u polymarket-web.service -u polymarket-bot.service -f
```

### 9.3 Alerts to set up

- **5xx error spike** on Caddy access logs (or via an uptime monitor
  hitting `/api/health`).
- **OOM kills** on the `polymarket-bot` service. Monitor
  `journalctl -u polymarket-bot.service` for `Killed` messages and
  `dmesg | grep -i 'out of memory'`.
- **ML drift detection** triggers. The `/api/ml/drift` endpoint reports
  PSI per feature; alert if any PSI exceeds the configured threshold.
- **Kill switch activation**. Check `data/kill_switch` exists; if it does,
  the bot has halted itself. Alert immediately.
- **Daily loss approaching limit**. Alert when realised daily loss
  exceeds 80% of `DAILY_LOSS_LIMIT_USDC`.

### 9.4 Health check + monitoring scripts (W12-5)

The repo ships three standalone monitoring scripts under `scripts/`:

| Script | Runs as | What it does |
| --- | --- | --- |
| `scripts/health_check.py` | one-shot | 10 checks (frontend, backend, DBs, disk, memory, ML, kill switch, alerts, observability). Prints JSON to stdout; exits 0 if all pass, 1 otherwise. |
| `scripts/monitor.py` | daemon | Calls `health_check.py` every 60 s, appends JSONL samples to `data/health_monitor.jsonl`, prints a state-transition alert to stderr (visible via `journalctl`) whenever the overall state changes (OK → DEGRADED → DOWN). |
| `scripts/status_report.py` | one-shot | Aggregates six endpoints (`/api/health`, `/api/status`, `/api/snapshot`, `/api/ml/metrics`, `/api/alerts/stats`, `/api/cache/stats`) into a single JSON report with a human-readable summary on stderr. |

Reference systemd unit/timer files live under `docs/systemd/` (NOT installed
by default):

| File | Purpose |
| --- | --- |
| `docs/systemd/polymarket-health.service` | One-shot unit that runs `health_check.py`. |
| `docs/systemd/polymarket-health.timer` | Fires the one-shot unit every 5 minutes. |
| `docs/systemd/polymarket-monitor.service` | Long-running daemon that runs `monitor.py` continuously. |

To install them on a production host:

```bash
sudo cp docs/systemd/polymarket-*.service /etc/systemd/system/
sudo cp docs/systemd/polymarket-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-health.timer
sudo systemctl enable --now polymarket-monitor.service
```

The health-check timer's `SuccessExitStatus=0 1` directive means
`systemctl status polymarket-health.service` shows green even when the
bot is unhealthy — the actual signal is in the JSON stdout and in the
journal. Inspect with:

```bash
# Latest health-check result (JSON)
sudo journalctl -u polymarket-health.service -n 80 --no-pager

# Live monitor daemon stream (state-transition alerts on stderr)
sudo journalctl -u polymarket-monitor.service -f

# The JSONL log file (one record per 60 s sample)
tail -f /opt/polymarket-bot/mini-services/polymarket-bot/data/health_monitor.jsonl | jq .
```

See `docs/systemd/README.md` for the full operator runbook (environment
overrides, drop-ins, log rotation).

## 10. Security Checklist

Run through this checklist before flipping `TRADING_MODE` to `live`.

- [ ] Strong `API_TOKEN` generated (64+ chars, base64-encoded randomness).
- [ ] SSL/TLS configured via Caddy (Let's Encrypt cert auto-renewing).
- [ ] `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` set as defaults.
- [ ] The 10-check live safety gate (`/api/live/readiness`) passes.
- [ ] Kill switch tested (create `data/kill_switch` file, verify the bot
      halts new orders; remove file to resume).
- [ ] `MAX_TOTAL_EXPOSURE_USDC` set conservatively (start at 10).
- [ ] `DAILY_LOSS_LIMIT_USDC` set conservatively (start at 1).
- [ ] `CORS_ORIGINS` restricted to your exact domain (no `*`).
- [ ] Database backups scheduled and verified restorable.
- [ ] Firewall rules limit inbound traffic to ports 22, 80, 443 only.
- [ ] `.env` files are owned by `deploy` with mode `0600`.
- [ ] `node_modules`, `data/`, and log files are not committed to git
      (verify against `.gitignore`).
- [ ] Unprivileged `deploy` user runs the services (not root).
- [ ] SSH key-only auth enabled; root SSH login disabled.

## 11. Upgrading

### 11.1 Pull and rebuild

```bash
cd /opt/polymarket-bot
git fetch --tags
git checkout v0.X.Y   # or `git pull` for latest main

# Frontend
bun install
bun run build

# Backend (only if requirements changed)
cd mini-services/polymarket-bot
source .venv/bin/activate
pip install -r requirements.txt
```

### 11.2 Verify, then restart

```bash
# Run the test suite before flipping traffic.
cd /opt/polymarket-bot/mini-services/polymarket-bot
source .venv/bin/activate
python -m pytest tests/ -v

# Restart services in order: backend first, then frontend.
sudo systemctl restart polymarket-bot.service
sleep 3
sudo systemctl restart polymarket-web.service
```

### 11.3 Post-deploy smoke checks

```bash
# Backend health
curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  https://yourdomain.com/api/system/health | jq .

# Observability
curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  https://yourdomain.com/api/observability | jq '.system'

# Frontend serves
curl -fsS https://yourdomain.com/ -o /dev/null -w "%{http_code}\n"
```

Expect `200` from all three.

## 12. Rollback

If a deployment misbehaves, roll back to the previous known-good commit.

```bash
cd /opt/polymarket-bot

# Identify the previous good tag/commit
git tag --list 'v*' --sort=-v:refname | head -5

# Check out the previous version
git checkout v0.X.Y-1

# Rebuild and restart
bun install
bun run build
cd mini-services/polymarket-bot
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart polymarket-bot.service
sudo systemctl restart polymarket-web.service
```

If you need to roll back the SQLite databases, restore from the most
recent backup (see §8.2):

```bash
sudo systemctl stop polymarket-bot.service
DATA_DIR=/opt/polymarket-bot/mini-services/polymarket-bot/data
BACKUP_DIR=/opt/polymarket-bot/backups/<most-recent-timestamp>
for db in "$BACKUP_DIR"/*.db; do
  cp "$db" "$DATA_DIR/$(basename "$db")"
done
sudo systemctl start polymarket-bot.service
```

## 13. Troubleshooting

### 13.1 Backend not starting

- Check `mini-services/polymarket-bot/server.log` for the traceback.
- Verify Python version: `python3.12 --version` (must be 3.12+).
- Verify the venv is active and `requirements.txt` is installed:
  `pip list | grep fastapi`.
- Verify port 8080 is free: `ss -ltnp | grep :8080`. Kill any stale
  uvicorn process if present.
- Verify `.env` is readable by the `deploy` user and contains
  `API_TOKEN`.

### 13.2 Frontend blank or 500

- Check `/opt/polymarket-bot/server.log` for Next.js errors.
- Verify `.env.production` (or `.env`) contains `API_TOKEN` matching
  the backend's value.
- Verify the standalone build exists: `ls .next/standalone/server.js`.
  If missing, re-run `bun run build`.
- Verify port 3000 is listening: `ss -ltnp | grep :3000`.

### 13.3 OOM kills

The ML ensemble (especially LightGBM) and shadow inference can spike
memory. Symptoms: `journalctl` shows `Killed`, `dmesg` reports
`Out of memory: Killed process ...`.

Mitigations, in order of preference:

1. Increase server RAM to 8 GB+.
2. Add swap (4 GB):
   ```bash
   sudo fallocate -l 4G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
3. Reduce `MAX_OPEN_ORDERS` and `MM_QUOTE_SIZE_USDC` to lower the
   number of in-memory orders.
4. Disable LightGBM in the ensemble (remove from `requirements.txt`
   or set the relevant flag in `ml/ensemble_meta_learner.py`).
5. Reduce the retraining cadence so the model reload happens less often.

### 13.4 502 Bad Gateway from Caddy

- Verify the backend is running: `systemctl status polymarket-bot.service`.
- Verify the backend is listening on the expected port:
  `curl -fsS http://localhost:8080/api/health`.
- Verify Caddy can reach the backend: check Caddy logs with
  `sudo journalctl -u caddy -f`.
- Check that the `XTransformPort` query parameter is not being set
  incorrectly (which would route traffic to a non-listening port).
- If running split-host, verify the firewall allows Caddy's host to
  reach the backend's port.

### 13.5 Kill switch stuck on

If `data/kill_switch` exists and the bot refuses to place orders:

- Read `data/kill_switch.reason` to understand why it tripped.
- Address the underlying cause (e.g. daily loss exceeded, position
  limit hit).
- Remove the kill-switch file to resume trading:
  ```bash
  rm /opt/polymarket-bot/mini-services/polymarket-bot/data/kill_switch
  rm /opt/polymarket-bot/mini-services/polymarket-bot/data/kill_switch.reason
  ```
- Verify via `/api/system/health` that the bot reports `kill_switch: false`.
