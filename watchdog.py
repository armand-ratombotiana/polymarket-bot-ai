"""
watchdog.py — Health watchdog for 24/7 operation.
Runs inside the bot container alongside the API server.
Pings /api/health every 30s and restarts the bot program via supervisorctl
if there are 3 consecutive failures.
"""
import logging
import subprocess
import time
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchdog")

HEALTH_URL = "http://127.0.0.1:8000/api/health"
CHECK_INTERVAL = 30
MAX_FAILURES = 5
INITIAL_GRACE_PERIOD = 60

consecutive_fails = 0

# Wait for bot startup before beginning checks
time.sleep(INITIAL_GRACE_PERIOD)

while True:
    time.sleep(CHECK_INTERVAL)
    try:
        urllib.request.urlopen(HEALTH_URL, timeout=5)
        if consecutive_fails > 0:
            log.info("health recovered after %d failure(s)", consecutive_fails)
        consecutive_fails = 0
        log.info("health OK")
    except Exception as e:
        consecutive_fails += 1
        log.warning("health FAIL #%d: %s", consecutive_fails, e)
        if consecutive_fails >= MAX_FAILURES:
            log.error("Requesting supervisorctl restart of bot process")
            subprocess.run(
                ["supervisorctl", "-c", "/app/supervisord.conf", "restart", "bot"],
                capture_output=True,
            )
            consecutive_fails = 0
