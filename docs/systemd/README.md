# systemd unit & timer reference files

This directory holds **reference** systemd unit files for the W12-5 health
check + monitoring scripts. They are NOT installed automatically — they
exist as documentation of the intended production wiring so an operator
can drop them into `/etc/systemd/system/` and `systemctl enable` them.

## Files

| File | Purpose |
| --- | --- |
| `polymarket-health.service` | One-shot: runs `scripts/health_check.py` and exits 0/1. |
| `polymarket-health.timer` | Fires `polymarket-health.service` every 5 minutes. |
| `polymarket-monitor.service` | Long-running daemon: runs `scripts/monitor.py` continuously. |

## Installation (operator runbook)

```bash
# 1. Copy the three files into /etc/systemd/system/ (sudo required).
sudo cp /home/z/my-project/docs/systemd/polymarket-*.service /etc/systemd/system/
sudo cp /home/z/my-project/docs/systemd/polymarket-health.timer /etc/systemd/system/

# 2. Reload systemd so the new units show up.
sudo systemctl daemon-reload

# 3. Enable + start the timer (health check every 5 min).
sudo systemctl enable --now polymarket-health.timer

# 4. Enable + start the long-running monitor daemon.
sudo systemctl enable --now polymarket-monitor.service

# 5. Verify.
systemctl status polymarket-health.timer
systemctl status polymarket-monitor.service
journalctl -u polymarket-health.service -n 50 --no-pager
journalctl -u polymarket-monitor.service -f
```

## Environment

All three unit files expect the bot stack (FastAPI backend + Next.js
frontend) to be reachable at the URLs the scripts default to:

* `FRONTEND_URL` → `http://localhost:3000`
* `BACKEND_URL` → `http://localhost:8080`
* `BOT_DATA_DIR` → `/home/z/my-project/mini-services/polymarket-bot/data`
* `API_TOKEN` → baked-in dev token (override via `Environment=` for prod)
* `MONITOR_LOG` → `…/data/health_monitor.jsonl` (used by `monitor.py`)

If your deployment uses different URLs (e.g. Caddy on 443), drop an
`Environment=` override in a systemd drop-in:

```bash
sudo systemctl edit polymarket-health.service
# in the editor, add:
# [Service]
# Environment=BACKEND_URL=https://api.example.com
# Environment=FRONTEND_URL=https://app.example.com
# Environment=API_TOKEN=<prod-token>
```

The same `Environment=` block can be added to
`polymarket-monitor.service` — both scripts read the same variables.

## Notes

* The timer is `Persistent=true` so a missed firing (host was down) runs
  once on next boot.
* `monitor.py` writes one JSONL record per sample to
  `BOT_DATA_DIR/health_monitor.jsonl`. Rotate it with `logrotate` if you
  keep the daemon running 24/7 — one record is ~2 KB, so at a 60s
  interval that's ~3 MB/day or ~1 GB/year.
* `health.service` exits 0 when all 10 checks pass, 1 otherwise — the
  exit code is visible in `systemctl status` and in the journal, so it
  can be wired into a host-level alerting rule (e.g. Alertmanager's
  `systemd` integration).
