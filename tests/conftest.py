"""
conftest.py — test-wide environment isolation.

Redirects all file-backed components (kill switch, audit trail, store state)
into a temp dir so tests never touch /app/data or the real .env.
Must be imported before any project module (pytest handles this automatically).
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="polymarket_tests_"))
os.environ["KILL_SWITCH_PATH"] = str(_TMP / "kill_switch")
os.environ["KILL_SWITCH_REASON_PATH"] = str(_TMP / "kill_switch.reason")
os.environ["AUDIT_DB_PATH"] = str(_TMP / "audit_trail.db")
os.environ["STORE_STATE_PATH"] = str(_TMP / "store_state.json")
os.environ["MARKET_DB_PATH"] = str(_TMP / "market_intelligence.db")
os.environ["MODEL_PATH"] = str(_TMP / "model.pkl")
os.environ["TRADING_MODE"] = "paper"
# API security env must be present before Settings() is first constructed.
os.environ["API_TOKEN"] = "test-token-123"
os.environ["CORS_ORIGINS"] = "http://allowed.example"