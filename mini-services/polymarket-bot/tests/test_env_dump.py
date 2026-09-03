import os
import sys

def test_dump_env():
    print("=== Env vars at test entry ===")
    for k in ['JOB_QUEUE_DB', 'AUDIT_DB_PATH', 'STORE_STATE_PATH', 'TRADING_MODE', 'API_TOKEN']:
        print(f"  {k} = {os.environ.get(k, '<NOT SET>')}")
    from api.server import app
    print(f"App routes: {len(app.routes)}")
    assert True
