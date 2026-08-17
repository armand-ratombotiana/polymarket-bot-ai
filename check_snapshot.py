import json
import urllib.request

BASE = "http://localhost:8087"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.load(r)

def main():
    snap = get("/api/snapshot")
    print("mode:", snap.get("mode"), "| kill_switch:", snap.get("kill_switch"),
          "| observation:", snap.get("observation_only"))
    print("strategies:", snap.get("strategies"))
    print("order_books:", len(snap.get("order_books", [])),
          "| open_orders:", len(snap.get("open_orders", [])),
          "| positions:", len(snap.get("positions", [])),
          "| recent_trades:", len(snap.get("recent_trades", [])))
    print("daily_pnl:", snap.get("daily_pnl"), "| paper_balance:", snap.get("paper_balance"))
    print("--- books (first 6) ---")
    for b in snap.get("order_books", [])[:6]:
        print("  ", (b.get("slug") or "")[:45], "mid=", b.get("mid"),
              "bid=", b.get("best_bid"), "ask=", b.get("best_ask"),
              "updated", round((b.get("updated_at") or 0) - 0))
    print("--- open orders ---")
    for o in snap.get("open_orders", [])[:6]:
        print("  ", o.get("side"), (o.get("slug") or "")[:40], o.get("price"), o.get("size"),
              o.get("strategy"), "paper=", o.get("paper"))
    print("--- recent trades ---")
    for t in snap.get("recent_trades", [])[:8]:
        print("  ", t.get("side"), (t.get("slug") or "")[:45], t.get("price"),
              t.get("size"), "pnl=", t.get("pnl"), t.get("strategy"))
    print("--- positions ---")
    for p in snap.get("positions", [])[:6]:
        print("  ", (p.get("slug") or "")[:45], "yes_shares=", p.get("yes_shares"),
              "avg=", p.get("avg_entry_price"), "invested=", p.get("total_invested"),
              "pnl=", p.get("realised_pnl"))
    print("--- recent events ---")
    for e in snap.get("events", [])[:10]:
        print("  ", e)

if __name__ == "__main__":
    main()
