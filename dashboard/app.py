"""
dashboard/app.py — Rich-powered live terminal dashboard.

Four-panel layout:
┌─────────────────────────────────────────────────────┐
│  HEADER  — mode, wallet, kill-switch status          │
├─────────────────┬───────────────────────────────────┤
│  MARKETS        │  POSITIONS & P&L                  │
├─────────────────┴───────────────────────────────────┤
│  OPEN ORDERS                                        │
├─────────────────────────────────────────────────────┤
│  EVENT LOG                                          │
└─────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import time
from typing import List

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from config import settings
from core.data_store import Order, OrderStatus, Position, Side, store
from paper.simulator import paper_sim
from risk.manager import risk_manager


def _fmt_pnl(val: float) -> Text:
    color = "green" if val >= 0 else "red"
    return Text(f"${val:+,.2f}", style=f"bold {color}")


def _fmt_price(p: float) -> str:
    return f"{p:.4f}"


class Dashboard:
    """Renders and refreshes the terminal UI using Rich Live."""

    def __init__(self) -> None:
        self._console = Console()
        self._refresh_s = settings.dashboard_refresh_ms / 1000
        self._running = False
        self._start_time = time.time()

    async def start(self) -> None:
        self._running = True
        await asyncio.to_thread(self._render_loop)

    def stop(self) -> None:
        self._running = False

    # ── Render loop ───────────────────────────────────────────────────────

    def _render_loop(self) -> None:
        with Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=int(1000 / settings.dashboard_refresh_ms),
            screen=True,
        ) as live:
            import time as _time
            while self._running:
                live.update(self._build_layout())
                _time.sleep(self._refresh_s)

    # ── Layout builder ────────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="orders", size=10),
            Layout(name="log", size=12),
        )
        layout["body"].split_row(
            Layout(name="markets"),
            Layout(name="positions"),
        )

        layout["header"].update(self._header_panel())
        layout["markets"].update(self._markets_panel())
        layout["positions"].update(self._positions_panel())
        layout["orders"].update(self._orders_panel())
        layout["log"].update(self._log_panel())

        return layout

    # ── Panel builders ────────────────────────────────────────────────────

    def _header_panel(self) -> Panel:
        mode = "[bold red]LIVE[/bold red]" if not settings.paper_trade else "[bold yellow]PAPER[/bold yellow]"
        kill = "[bold red]🛑 KILL SWITCH ON[/bold red]" if store.kill_switch_active else "[green]✅ Running[/green]"
        uptime_s = int(time.time() - self._start_time)
        h, m, s = uptime_s // 3600, (uptime_s % 3600) // 60, uptime_s % 60

        paper_bal = ""
        if settings.paper_trade:
            paper_bal = f"  │  Paper Balance: [cyan]${paper_sim.virtual_balance:,.2f}[/cyan]"

        pnl = _fmt_pnl(store.daily_pnl)
        text = (
            f"[bold]Polymarket Bot[/bold]  │  Mode: {mode}  │  Status: {kill}"
            f"  │  Daily P&L: {pnl.markup}  │  Uptime: {h:02d}:{m:02d}:{s:02d}{paper_bal}"
        )
        return Panel(text, style="bold blue", box=box.HORIZONTALS)

    def _markets_panel(self) -> Panel:
        table = Table(
            "Market", "Bid", "Ask", "Mid", "Spread", "Updated",
            box=box.SIMPLE_HEAVY, expand=True, show_header=True,
            header_style="bold cyan",
        )

        books = dict(store.order_books)  # snapshot
        if not books:
            table.add_row("[dim]No market data yet — waiting for WebSocket…[/dim]", "", "", "", "", "")
        else:
            for token_id, book in list(books.items())[:15]:
                slug = store.market_slugs.get(token_id, token_id[:14] + "…")
                bid = _fmt_price(book.best_bid) if book.best_bid else "—"
                ask = _fmt_price(book.best_ask) if book.best_ask else "—"
                mid = _fmt_price(book.mid) if book.mid else "—"
                spread = f"{book.spread:.4f}" if book.spread else "—"
                age = int(time.time() - book.updated_at)
                updated = f"{age}s ago" if age < 60 else f"{age//60}m ago"
                table.add_row(slug[:30], bid, ask, mid, spread, updated)

        return Panel(table, title="[bold]📈 Markets[/bold]", border_style="blue")

    def _positions_panel(self) -> Panel:
        table = Table(
            "Market", "YES Shares", "Avg Entry", "R-P&L", "Exposure",
            box=box.SIMPLE_HEAVY, expand=True, show_header=True,
            header_style="bold magenta",
        )

        positions = dict(store.positions)
        if not positions:
            table.add_row("[dim]No open positions[/dim]", "", "", "", "")
        else:
            for token_id, pos in positions.items():
                if pos.yes_shares < 0.01 and pos.total_invested < 0.01:
                    continue
                slug = store.market_slugs.get(token_id, token_id[:14] + "…")
                pnl_text = _fmt_pnl(pos.realised_pnl)
                table.add_row(
                    slug[:28],
                    f"{pos.yes_shares:.2f}",
                    _fmt_price(pos.avg_entry_price),
                    pnl_text.markup,
                    f"${pos.current_exposure:.2f}",
                )

        daily_row = _fmt_pnl(store.daily_pnl)
        return Panel(
            table,
            title=f"[bold]💰 Positions  (Daily P&L: {daily_row.markup})[/bold]",
            border_style="magenta",
        )

    def _orders_panel(self) -> Panel:
        table = Table(
            "ID", "Market", "Side", "Price", "Size", "Filled", "Strategy", "Age",
            box=box.SIMPLE_HEAVY, expand=True, show_header=True,
            header_style="bold yellow",
        )

        orders = dict(store.open_orders)
        if not orders:
            table.add_row("[dim]No open orders[/dim]", "", "", "", "", "", "", "")
        else:
            for oid, order in list(orders.items())[:12]:
                slug = store.market_slugs.get(order.token_id, order.token_id[:12] + "…")
                side_style = "green" if order.side == Side.BUY else "red"
                age = int(time.time() - order.created_at)
                paper_tag = " [dim](P)[/dim]" if order.paper else ""
                table.add_row(
                    oid[-10:],
                    slug[:20],
                    f"[{side_style}]{order.side.value}[/{side_style}]",
                    _fmt_price(order.price),
                    f"{order.size:.2f}",
                    f"{order.size_matched:.2f}",
                    order.strategy + paper_tag,
                    f"{age}s",
                )

        return Panel(table, title="[bold]📋 Open Orders[/bold]", border_style="yellow")

    def _log_panel(self) -> Panel:
        events = list(store.event_log[-18:])
        lines = []
        for ev in reversed(events):
            lines.append(ev)
        text = "\n".join(lines) if lines else "[dim]No events yet…[/dim]"
        return Panel(text, title="[bold]📜 Event Log[/bold]", border_style="dim")
