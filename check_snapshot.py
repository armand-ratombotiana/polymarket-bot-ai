import json
import os
import sys
import urllib.error
import urllib.request

try:
    from config import settings
    _DEFAULT_TOKEN = settings.api_token
except ImportError:
    _DEFAULT_TOKEN = ""

BASE = os.environ.get("BOT_BASE_URL", "http://127.0.0.1:8080")
API_TOKEN = os.environ.get("API_TOKEN", _DEFAULT_TOKEN or "change_me_generate_a_strong_token")


def get(path: str) -> dict:
    explicit_base = os.environ.get("BOT_BASE_URL")
    candidate_urls = [explicit_base] if explicit_base else [
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8087",
    ]

    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    last_err = None

    for base_url in filter(None, candidate_urls):
        url = base_url.rstrip("/") + path
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} ({url})"
            if e.code == 401 or e.code == 403:
                body = e.read().decode("utf-8", errors="replace")
                print(f"❌ Auth Error querying {url}: {body}", file=sys.stderr)
                sys.exit(1)
            continue
        except Exception as e:
            last_err = str(e)
            continue

    print(f"❌ Could not reach Polymarket Bot snapshot on candidate ports ({candidate_urls}). Last error: {last_err}", file=sys.stderr)
    print("👉 Ensure the bot is running (`python main.py serve` or `docker compose --profile paper up -d`)", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    snap = get("/api/snapshot")
    print(f"mode: {snap.get('mode')} | kill_switch: {snap.get('kill_switch')} (durable: {snap.get('kill_switch_durable')}) "
          f"| observation: {snap.get('observation_only')}")
    print(f"strategies ({len(snap.get('strategies', []))}):", snap.get("strategies"))
    print(f"order_books: {len(snap.get('order_books', []))} "
          f"| open_orders: {len(snap.get('open_orders', []))} "
          f"| positions: {len(snap.get('positions', []))} "
          f"| recent_trades: {len(snap.get('recent_trades', []))}")
    print(f"daily_pnl: ${snap.get('daily_pnl', 0.0):+.2f} | paper_balance: {snap.get('paper_balance')}")

    books = snap.get("order_books", [])
    if books:
        print("\n--- books (first 6) ---")
        for b in books[:6]:
            print(f"  {(b.get('slug') or '')[:45]:<45} mid={b.get('mid')} "
                  f"bid={b.get('best_bid')} ask={b.get('best_ask')}")

    orders = snap.get("open_orders", [])
    if orders:
        print("\n--- open orders ---")
        for o in orders[:6]:
            print(f"  {o.get('side'):<4} {(o.get('slug') or '')[:40]:<40} "
                  f"price={o.get('price')} size={o.get('size')} strat={o.get('strategy')} paper={o.get('paper')}")

    trades = snap.get("recent_trades", [])
    if trades:
        print("\n--- recent trades ---")
        for t in trades[:8]:
            print(f"  {t.get('side'):<4} {(t.get('slug') or '')[:45]:<45} "
                  f"price={t.get('price')} size={t.get('size')} pnl={t.get('pnl')} strat={t.get('strategy')}")

    positions = snap.get("positions", [])
    if positions:
        print("\n--- positions ---")
        for p in positions[:6]:
            print(f"  {(p.get('slug') or '')[:45]:<45} yes_shares={p.get('yes_shares')} "
                  f"avg={p.get('avg_entry_price')} invested={p.get('total_invested')} pnl={p.get('realised_pnl')}")

    events = snap.get("events", [])
    if events:
        print("\n--- recent events (first 10) ---")
        for e in events[:10]:
            print(f"  {e}")


if __name__ == "__main__":
    main()
