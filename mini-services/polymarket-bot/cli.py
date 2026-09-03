"""Polymarket Bot CLI — manage the trading bot from the command line.

Usage:
    python cli.py status          # Show bot status
    python cli.py balance         # Show account balance
    python cli.py positions       # List open positions
    python cli.py orders          # List open orders
    python cli.py trades [N]      # Show last N trades
    python cli.py health          # Health check
    python cli.py retrain         # Trigger ML retrain
    python cli.py kill-switch     # Activate kill switch
    python cli.py flags           # List feature flags
    python cli.py flag <key>      # Get/set a feature flag
    python cli.py alerts          # Show recent alerts
    python cli.py metrics         # Show ML metrics
    python cli.py circuit-breakers # Show circuit breaker status
    python cli.py cache           # Show cache stats
    python cli.py backup          # Create a backup
"""
import os
import sys
import json
import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.json import JSON

app = typer.Typer(help="Polymarket Bot CLI")
console = Console()

# Configuration
API_URL = os.environ.get("BOT_API_URL", "http://localhost:8080")
API_TOKEN = os.environ.get("API_TOKEN", os.environ.get("BOT_API_TOKEN", ""))

def _headers():
    if not API_TOKEN:
        console.print("[red]Error: API_TOKEN not set[/red]")
        raise typer.Exit(1)
    return {"Authorization": f"Bearer {API_TOKEN}"}

def _get(path: str, params: dict = None) -> dict:
    """Make a GET request to the API."""
    try:
        r = httpx.get(f"{API_URL}{path}", headers=_headers(), params=params, timeout=30)
        if r.status_code == 401:
            console.print("[red]Unauthorized — check API_TOKEN[/red]")
            raise typer.Exit(1)
        if r.status_code == 429:
            console.print("[yellow]Rate limited — try again in a minute[/yellow]")
            raise typer.Exit(1)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {API_URL}[/red]")
        raise typer.Exit(1)

def _post(path: str, json_body: dict = None) -> dict:
    """Make a POST request."""
    try:
        r = httpx.post(f"{API_URL}{path}", headers={**_headers(), "Content-Type": "application/json"}, json=json_body or {}, timeout=60)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {API_URL}[/red]")
        raise typer.Exit(1)

@app.command()
def status():
    """Show bot status."""
    data = _get("/api/status")
    table = Table(title="Bot Status")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    for key, val in data.items():
        if isinstance(val, (dict, list)):
            val = json.dumps(val, indent=2)[:200]
        table.add_row(str(key), str(val))
    console.print(table)

@app.command()
def balance():
    """Show account balance."""
    data = _get("/api/status")
    balance = data.get("paper_balance") or data.get("balance") or "N/A"
    console.print(Panel(f"[bold green]Balance: ${balance}[/bold green]", title="Account Balance"))

@app.command()
def positions():
    """List open positions."""
    data = _get("/api/positions")
    positions = data.get("positions", data) if isinstance(data, dict) else data
    if not positions:
        console.print("[yellow]No open positions[/yellow]")
        return
    table = Table(title="Open Positions")
    table.add_column("Token", style="cyan")
    table.add_column("Side", style="magenta")
    table.add_column("Size", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("P&L", justify="right")
    for p in positions:
        pnl = p.get("unrealized_pnl") or p.get("pnl") or 0
        pnl_color = "green" if pnl >= 0 else "red"
        table.add_row(
            str(p.get("token_id", ""))[:16],
            str(p.get("side", "")),
            f"{p.get('size', 0):.2f}",
            f"{p.get('avg_price', 0):.4f}",
            f"{p.get('current_price', 0):.4f}",
            f"[{pnl_color}]{pnl:+.2f}[/{pnl_color}]",
        )
    console.print(table)

@app.command()
def orders():
    """List open orders."""
    data = _get("/api/orders")
    orders = data.get("orders", data) if isinstance(data, dict) else data
    if not orders:
        console.print("[yellow]No open orders[/yellow]")
        return
    table = Table(title="Open Orders")
    table.add_column("ID", style="cyan")
    table.add_column("Token", style="cyan")
    table.add_column("Side")
    table.add_column("Price", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Status")
    for o in orders:
        table.add_row(
            str(o.get("order_id", ""))[:8],
            str(o.get("token_id", ""))[:16],
            str(o.get("side", "")),
            f"{o.get('price', 0):.4f}",
            f"{o.get('size', 0):.2f}",
            str(o.get("status", "")),
        )
    console.print(table)

@app.command()
def trades(limit: int = typer.Option(20, "-n", "--limit", help="Number of trades to show")):
    """Show recent trades."""
    data = _get("/api/trades", {"limit": limit})
    trades = data.get("trades", data) if isinstance(data, dict) else data
    if not trades:
        console.print("[yellow]No trades found[/yellow]")
        return
    table = Table(title=f"Last {len(trades)} Trades")
    table.add_column("Token", style="cyan")
    table.add_column("Side")
    table.add_column("Price", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Time")
    for t in trades:
        ts = t.get("timestamp") or t.get("created_at", "")
        table.add_row(
            str(t.get("token_id", ""))[:16],
            str(t.get("side", "")),
            f"{t.get('price', 0):.4f}",
            f"{t.get('size', 0):.2f}",
            str(ts)[:19],
        )
    console.print(table)

@app.command()
def health():
    """Health check."""
    data = _get("/api/health")
    console.print(Panel(JSON(json.dumps(data)), title="Health Check"))

@app.command()
def retrain():
    """Trigger ML model retrain."""
    console.print("[yellow]Triggering ML retrain...[/yellow]")
    data = _post("/api/ml/retrain")
    console.print(Panel(JSON(json.dumps(data)), title="Retrain Result"))

@app.command()
def kill_switch():
    """Activate kill switch."""
    confirm = typer.confirm("Are you sure you want to activate the kill switch?")
    if not confirm:
        console.print("[yellow]Cancelled[/yellow]")
        raise typer.Exit()
    data = _post("/api/kill-switch/activate")
    console.print(Panel(JSON(json.dumps(data)), title="Kill Switch Activated", style="red"))

@app.command()
def flags():
    """List feature flags."""
    data = _get("/api/flags")
    table = Table(title="Feature Flags")
    table.add_column("Key", style="cyan")
    table.add_column("Enabled")
    table.add_column("Description")
    for f in data:
        status = "[green]ON[/green]" if f.get("enabled") else "[red]OFF[/red]"
        table.add_row(f.get("key", ""), status, f.get("description", ""))
    console.print(table)

@app.command()
def flag(key: str, enabled: bool = None):
    """Get or set a feature flag."""
    if enabled is None:
        data = _get(f"/api/flags/{key}")
        console.print(Panel(JSON(json.dumps(data)), title=f"Flag: {key}"))
    else:
        data = _post(f"/api/flags/{key}", {"enabled": enabled})
        console.print(f"[green]Flag '{key}' set to {enabled}[/green]")

@app.command()
def alerts():
    """Show recent alerts."""
    data = _get("/api/alerts", {"limit": 20})
    alerts = data.get("alerts", []) if isinstance(data, dict) else data
    if not alerts:
        console.print("[green]No alerts[/green]")
        return
    table = Table(title="Recent Alerts")
    table.add_column("Severity")
    table.add_column("Name", style="cyan")
    table.add_column("Message")
    table.add_column("Acked")
    for a in alerts:
        sev = a.get("severity", "info")
        sev_color = {"critical": "red", "error": "red", "warning": "yellow", "info": "blue"}.get(sev, "white")
        acked = "[green]Yes[/green]" if a.get("acknowledged") else "[red]No[/red]"
        table.add_row(f"[{sev_color}]{sev}[/{sev_color}]", a.get("name", ""), a.get("message", "")[:60], acked)
    console.print(table)

@app.command()
def metrics():
    """Show ML metrics."""
    data = _get("/api/ml/metrics")
    console.print(Panel(JSON(json.dumps(data, default=str)), title="ML Metrics"))

@app.command()
def circuit_breakers():
    """Show circuit breaker status."""
    data = _get("/api/circuit-breakers")
    table = Table(title="Circuit Breakers")
    table.add_column("Name", style="cyan")
    table.add_column("State")
    table.add_column("Failures", justify="right")
    table.add_column("Threshold", justify="right")
    for b in data.get("breakers", []):
        state = b.get("state", "closed")
        state_color = {"closed": "green", "open": "red", "half_open": "yellow"}.get(state, "white")
        table.add_row(b.get("name", ""), f"[{state_color}]{state}[/{state_color}]", str(b.get("failure_count", 0)), str(b.get("failure_threshold", 0)))
    console.print(table)

@app.command()
def cache():
    """Show cache statistics."""
    data = _get("/api/cache/stats")
    table = Table(title="Cache Statistics")
    table.add_column("Name", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Hits", justify="right")
    table.add_column("Misses", justify="right")
    table.add_column("Hit Rate", justify="right")
    for c in data.get("caches", []):
        hr = c.get("hit_rate", 0) * 100
        hr_color = "green" if hr > 50 else "yellow"
        table.add_row(c.get("name", ""), str(c.get("size", 0)), str(c.get("hits", 0)), str(c.get("misses", 0)), f"[{hr_color}]{hr:.1f}%[/{hr_color}]")
    console.print(table)

if __name__ == "__main__":
    app()
