"""
main.py — CLI entry point for the Polymarket Trading Bot.

Commands:
  python main.py serve         Start FastAPI server + all strategies (for Docker/web UI)
  python main.py run           Start all enabled strategies + Rich terminal dashboard
  python main.py paper         Force paper-trade mode and start
  python main.py markets       List active Polymarket markets (no auth needed)
  python main.py cancel-all    Emergency: cancel all open orders on the exchange
  python main.py status        Print current risk status and positions
"""
from __future__ import annotations

import asyncio
import logging
import os

import typer
from rich import box
from rich.console import Console
from rich.table import Table

# ── Bootstrap logging before importing project modules ────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="polymarket-bot",
    help="Polymarket automated trading bot — market making, arb scanning, signal trading.",
    add_completion=False,
)
console = Console()


# ── Helper: validate .env loaded ─────────────────────────────────────────────

def _check_env() -> None:
    """Warn if .env is missing or has placeholder values."""
    if not os.path.exists(".env"):
        console.print(
            "[yellow]⚠  No .env file found. Copy .env.example to .env and configure it.[/yellow]"
        )
    from config import settings
    if not settings.has_credentials:
        console.print(
            "[yellow]⚠  POLY_PRIVATE_KEY is not set. Paper-trade mode will be used.[/yellow]"
        )
        os.environ["PAPER_TRADE"] = "true"


# ── Shared bot startup sequence ───────────────────────────────────────────────

async def _startup(force_paper: bool = False) -> None:
    """Initialise all bot components."""
    from config import settings

    if force_paper:
        settings.paper_trade = True   # type: ignore[attr-defined]
        settings.trading_mode = "paper"

    # Live-mode guard (P0-GOV-01): live requires explicit double authorization.
    if not settings.paper_trade:
        if not settings.live_trading_enabled:
            console.print("[red]❌ LIVE trading is not enabled — set LIVE_TRADING_ENABLED=true explicitly to authorize real funds.[/red]")
            raise typer.Exit(1)
        if not settings.has_credentials:
            console.print("[red]❌ No wallet credentials configured (POLY_PRIVATE_KEY). Refusing to run live.[/red]")
            raise typer.Exit(1)

    # Set log level from config
    logging.getLogger().setLevel(settings.log_level)

    from core.clob_client import clob_client
    from core.ws_client import ws_client
    from paper.simulator import paper_sim

    # Derive or load API credentials
    if settings.has_credentials and not settings.paper_trade:
        console.print("[cyan]🔑 Deriving API credentials…[/cyan]")
        try:
            await clob_client.derive_api_key()
            console.print(f"[green]✅ Authenticated as {clob_client.address}[/green]")
        except Exception as e:
            console.print(f"[red]❌ Auth failed: {e}[/red]")
            raise typer.Exit(1)
    else:
        # Paper mode: still derive keys (for data access) or skip
        try:
            await clob_client.derive_api_key()
        except Exception:
            pass

    # Start paper simulator if needed
    if settings.paper_trade:
        await paper_sim.start()

    # Start WebSocket listener
    await ws_client.start()

    console.print(
        f"[bold green]🚀 Bot started  "
        f"({'[yellow]PAPER[/yellow]' if settings.paper_trade else '[red]LIVE[/red]'})[/bold green]"
    )


async def _shutdown(strategies: list) -> None:
    """Gracefully stop all components."""
    from config import settings
    from core.ws_client import ws_client
    from paper.simulator import paper_sim

    for s in strategies:
        await s.stop()
    await ws_client.stop()
    if settings.paper_trade:
        await paper_sim.stop()
    console.print("\n[bold]Bot stopped.[/bold]")


# ── run command ───────────────────────────────────────────────────────────────

@app.command()
def run(
    paper: bool = typer.Option(False, "--paper", "-p", help="Force paper-trade mode"),
) -> None:
    """Start all enabled strategies and the live dashboard."""
    _check_env()
    asyncio.run(_run_async(force_paper=paper))


async def _run_async(force_paper: bool = False) -> None:
    from config import settings
    from dashboard.app import Dashboard
    from strategies.arb_scanner import ArbScannerStrategy
    from strategies.market_maker import MarketMakerStrategy
    from strategies.signal_trader import SignalTraderStrategy

    await _startup(force_paper)

    strategies = []
    if settings.mm_enabled:
        strategies.append(MarketMakerStrategy())
    if settings.arb_enabled:
        strategies.append(ArbScannerStrategy())
    if settings.signal_enabled:
        strategies.append(SignalTraderStrategy())

    if not strategies:
        console.print("[red]No strategies are enabled. Set MM_ENABLED=true, ARB_ENABLED=true, or SIGNAL_ENABLED=true in .env[/red]")
        raise typer.Exit(1)

    for s in strategies:
        await s.start()

    dash = Dashboard()

    try:
        await dash.start()   # blocks until Ctrl+C
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        dash.stop()
        await _shutdown(strategies)


# ── paper command ─────────────────────────────────────────────────────────────

@app.command()
def paper() -> None:
    """Force paper-trade mode (safe simulation with live market data)."""
    os.environ["PAPER_TRADE"] = "true"
    asyncio.run(_run_async(force_paper=True))


# ── markets command ───────────────────────────────────────────────────────────

@app.command()
def markets(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of markets to list"),
    search: str | None = typer.Option(None, "--search", "-s", help="Search query"),
) -> None:
    """List active Polymarket markets. Does not require credentials."""
    asyncio.run(_list_markets(limit=limit, search=search))


async def _list_markets(limit: int, search: str | None) -> None:
    from core.gamma_client import gamma_client

    console.print("[cyan]Fetching markets from Gamma API…[/cyan]")
    try:
        if search:
            items = await gamma_client.search_markets(search, limit=limit)
        else:
            items = await gamma_client.get_markets(active=True, limit=limit)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)
    finally:
        await gamma_client.close()

    table = Table(
        "#", "Slug", "Question", "Volume 24h", "End Date", "YES Token ID",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )

    for i, mkt in enumerate(items, 1):
        slug = mkt.get("slug", "—")
        question = (mkt.get("question", "—") or "")[:60]
        vol = f"${float(mkt.get('volume24hr', 0) or 0):,.0f}"
        end = (mkt.get("endDate") or mkt.get("end_date_iso", "—") or "—")[:10]
        tokens = mkt.get("tokens", [])
        yes_tid = next((t["token_id"] for t in tokens if t.get("outcome", "").upper() == "YES"), "—")
        table.add_row(str(i), slug[:30], question, vol, end, yes_tid[:20] + "…" if len(yes_tid) > 20 else yes_tid)

    console.print(table)
    console.print(f"\n[dim]{len(items)} market(s) shown[/dim]")


# ── cancel-all command ────────────────────────────────────────────────────────

@app.command(name="cancel-all")
def cancel_all() -> None:
    """Emergency: cancel ALL open orders on the exchange."""
    _check_env()
    asyncio.run(_cancel_all_async())


async def _cancel_all_async() -> None:
    from config import settings
    from core.clob_client import clob_client

    if settings.paper_trade:
        from core.data_store import store
        cancelled = await store.cancel_all_orders()
        console.print(f"[yellow]Paper mode — cancelled {len(cancelled)} virtual orders[/yellow]")
        return

    if not settings.has_credentials:
        console.print("[red]No credentials configured. Cannot cancel live orders.[/red]")
        raise typer.Exit(1)

    await clob_client.derive_api_key()
    ok = await clob_client.cancel_all_orders()
    if ok:
        console.print("[green]✅ All open orders cancelled on the exchange.[/green]")
    else:
        console.print("[red]❌ Cancel-all failed — check logs.[/red]")
    await clob_client.close()


# ── status command ────────────────────────────────────────────────────────────

@app.command()
def status() -> None:
    """Print current risk manager status and open positions."""
    asyncio.run(_status_async())


async def _status_async() -> None:
    from core.data_store import store
    from risk.manager import risk_manager

    report = await risk_manager.status_report()

    console.print("\n[bold]Risk Status[/bold]")
    for k, v in report.items():
        color = "red" if (k == "kill_switch" and v) else "white"
        console.print(f"  [dim]{k}:[/dim] [{color}]{v}[/{color}]")

    console.print(f"\n[bold]Open Orders:[/bold] {len(store.open_orders)}")
    console.print(f"[bold]Positions:[/bold] {len(store.positions)}")
    console.print("[bold]Daily P&L:[/bold] ", _fmt_pnl(store.daily_pnl))


def _fmt_pnl(v: float) -> str:
    return f"[{'green' if v >= 0 else 'red'}]${v:+,.2f}[/{'green' if v >= 0 else 'red'}]"


# ── serve command ─────────────────────────────────────────────────────────────

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port"),
    paper: bool = typer.Option(False, "--paper", help="Force paper-trade mode"),
    reload: bool = typer.Option(False, "--reload", help="Enable hot-reload (dev only)"),
) -> None:
    """Start the FastAPI server (REST + WebSocket) for the Web UI."""
    _check_env()
    if paper:
        os.environ["PAPER_TRADE"] = "true"

    import uvicorn

    # Apply configured log level (serve() doesn't go through _startup()).
    from config import settings
    log_level = settings.log_level.lower()
    logging.getLogger().setLevel(settings.log_level)
    console.print(
        f"[bold green]🚀 Starting API server on http://{host}:{port}[/bold green]  "
        f"({'[yellow]PAPER[/yellow]' if (paper or os.environ.get('PAPER_TRADE','').lower()=='true') else '[red]LIVE[/red]'})"
    )
    uvicorn.run(
        "api.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
