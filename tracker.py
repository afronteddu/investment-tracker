#!/usr/bin/env python3
"""
Investment Tracker — portfolio dashboard + scanner
Run: python tracker.py           # dashboard (refreshes every N seconds)
     python tracker.py --scan    # one-shot watchlist scanner
     python tracker.py --once    # one-shot dashboard snapshot
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date, datetime

import yaml
import yfinance as yf
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# ── Config ──────────────────────────────────────────────────────────────────

def load_config(path="portfolio.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)

# ── Data fetching ────────────────────────────────────────────────────────────

def fetch_quotes(tickers: list[str]) -> dict:
    if not tickers:
        return {}
    data = {}
    batch = yf.Tickers(" ".join(tickers))
    for ticker in tickers:
        try:
            info = batch.tickers[ticker].fast_info
            data[ticker] = {
                "price": getattr(info, "last_price", None),
                "prev_close": getattr(info, "previous_close", None),
                "day_high": getattr(info, "day_high", None),
                "day_low": getattr(info, "day_low", None),
                "currency": getattr(info, "currency", "?"),
            }
        except Exception:
            data[ticker] = {"price": None, "prev_close": None,
                            "day_high": None, "day_low": None, "currency": "?"}
    return data

def day_change_pct(q: dict) -> float:
    p, pc = q.get("price"), q.get("prev_close")
    if p and pc and pc != 0:
        return (p - pc) / pc * 100
    return None

# ── Alerts ───────────────────────────────────────────────────────────────────

_alerted: set[str] = set()

def maybe_alert(ticker: str, pct: float, threshold: float):
    key = f"{ticker}_{pct > 0}"
    if abs(pct) >= threshold and key not in _alerted:
        direction = "UP" if pct > 0 else "DOWN"
        msg = f"ALERT: {ticker} is {direction} {abs(pct):.1f}% today"
        # macOS native notification
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "Investment Tracker"'
        ], capture_output=True)
        _alerted.add(key)

# ── Portfolio table ──────────────────────────────────────────────────────────

BUCKET_COLORS = {
    "retirement": "cyan",
    "growth": "yellow",
    "high_conviction": "magenta",
}

def build_portfolio_table(positions: list, quotes: dict, threshold_pct: float) -> Table:
    table = Table(
        title="Portfolio",
        show_header=True,
        header_style="bold white",
        border_style="grey30",
        show_lines=False,
        expand=True,
    )
    table.add_column("Ticker", style="bold", width=10)
    table.add_column("Bucket", width=14)
    table.add_column("Shares", justify="right", width=8)
    table.add_column("Cost", justify="right", width=9)
    table.add_column("Price", justify="right", width=9)
    table.add_column("Day %", justify="right", width=8)
    table.add_column("P&L", justify="right", width=11)
    table.add_column("P&L %", justify="right", width=8)
    table.add_column("Target", justify="right", width=9)
    table.add_column("To target", justify="right", width=10)
    table.add_column("Stop", justify="right", width=9)

    total_cost = 0.0
    total_value = 0.0

    for pos in positions:
        ticker = pos["ticker"]
        shares = pos.get("shares") or 0
        cost = pos.get("cost_basis") or 0
        target = pos.get("target_price") or 0
        stop = pos.get("stop_loss") or 0
        bucket = pos.get("bucket", "")
        color = BUCKET_COLORS.get(bucket, "white")

        q = quotes.get(ticker, {})
        price = q.get("price")
        pct = day_change_pct(q)

        # P&L
        position_cost = shares * cost
        position_value = shares * price if price else None
        pnl = (position_value - position_cost) if position_value is not None else None
        pnl_pct = (pnl / position_cost * 100) if (pnl is not None and position_cost > 0) else None

        total_cost += position_cost
        if position_value is not None:
            total_value += position_value

        # To target
        to_target = ""
        if target and price:
            to_target_pct = (target - price) / price * 100
            to_target = f"{to_target_pct:+.1f}%"

        # Day change color
        if pct is not None:
            if pct >= threshold_pct:
                day_str = Text(f"{pct:+.2f}%", style="bold green")
            elif pct <= -threshold_pct:
                day_str = Text(f"{pct:+.2f}%", style="bold red")
            elif pct > 0:
                day_str = Text(f"{pct:+.2f}%", style="green")
            else:
                day_str = Text(f"{pct:+.2f}%", style="red")
            maybe_alert(ticker, pct, threshold_pct)
        else:
            day_str = Text("—", style="dim")

        # Stop loss warning
        stop_str = ""
        if stop:
            stop_str = f"{stop:.2f}"
            if price and price <= stop:
                stop_str = f"[bold red]⚠ {stop:.2f}[/bold red]"

        table.add_row(
            f"[{color}]{ticker}[/{color}]",
            f"[{color}]{bucket}[/{color}]",
            f"{shares:.1f}" if shares else "—",
            f"{cost:.2f}" if cost else "—",
            f"{price:.2f}" if price else "—",
            day_str,
            f"{pnl:+.0f}" if pnl is not None else "—",
            f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—",
            f"{target:.2f}" if target else "—",
            to_target,
            stop_str,
        )

    # Totals row
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]", "", "", f"{total_cost:.0f}", f"{total_value:.0f}", "",
        f"[bold {'green' if total_pnl >= 0 else 'red'}]{total_pnl:+.0f}[/bold {'green' if total_pnl >= 0 else 'red'}]",
        f"[bold {'green' if total_pnl_pct >= 0 else 'red'}]{total_pnl_pct:+.1f}%[/bold {'green' if total_pnl_pct >= 0 else 'red'}]",
        "", "", "",
    )

    return table

# ── Scanner table ─────────────────────────────────────────────────────────────

def build_scanner_table(watchlist: list, quotes: dict, gain_thresh: float, loss_thresh: float) -> Table:
    table = Table(
        title="Watchlist Scanner",
        show_header=True,
        header_style="bold white",
        border_style="grey30",
        expand=True,
    )
    table.add_column("Ticker", style="bold", width=12)
    table.add_column("Price", justify="right", width=10)
    table.add_column("Day %", justify="right", width=10)
    table.add_column("Day High", justify="right", width=10)
    table.add_column("Day Low", justify="right", width=10)
    table.add_column("Signal", width=20)

    rows = []
    for ticker in watchlist:
        q = quotes.get(ticker, {})
        price = q.get("price")
        pct = day_change_pct(q)
        rows.append((ticker, q, price, pct))

    # Sort by day change pct descending
    rows.sort(key=lambda x: x[3] if x[3] is not None else 0, reverse=True)

    for ticker, q, price, pct in rows:
        if pct is None:
            table.add_row(ticker, "—", "—", "—", "—", "[dim]no data[/dim]")
            continue

        day_high = q.get("day_high")
        day_low = q.get("day_low")

        if pct >= gain_thresh:
            pct_str = Text(f"{pct:+.2f}%", style="bold green")
            signal = Text("EXTRAORDINARY GAIN", style="bold green")
        elif pct <= loss_thresh:
            pct_str = Text(f"{pct:+.2f}%", style="bold red")
            signal = Text("EXTRAORDINARY DROP", style="bold red")
        elif pct > 3:
            pct_str = Text(f"{pct:+.2f}%", style="green")
            signal = Text("strong up", style="green")
        elif pct < -3:
            pct_str = Text(f"{pct:+.2f}%", style="red")
            signal = Text("strong down", style="red")
        else:
            pct_str = Text(f"{pct:+.2f}%", style="dim")
            signal = Text("—", style="dim")

        table.add_row(
            ticker,
            f"{price:.2f}" if price else "—",
            pct_str,
            f"{day_high:.2f}" if day_high else "—",
            f"{day_low:.2f}" if day_low else "—",
            signal,
        )

    return table

# ── Deployment reminder ───────────────────────────────────────────────────────

def build_deployment_panel(deployments: list) -> Panel:
    today = date.today()
    lines = []
    for d in deployments:
        dep_date = datetime.strptime(d["date"], "%Y-%m-%d").date()
        days_away = (dep_date - today).days
        if days_away < 0:
            prefix = "[dim]PAST[/dim]"
        elif days_away == 0:
            prefix = "[bold yellow]TODAY[/bold yellow]"
        elif days_away <= 3:
            prefix = f"[bold yellow]in {days_away}d[/bold yellow]"
        else:
            prefix = f"[dim]in {days_away}d[/dim]"
        lines.append(f"{prefix}  {d['date']}  {d['note']}")
    content = "\n".join(lines) if lines else "[dim]No upcoming deployments[/dim]"
    return Panel(content, title="Deployment Schedule", border_style="yellow")

# ── Main ──────────────────────────────────────────────────────────────────────

def render(cfg: dict) -> Layout:
    positions = cfg.get("positions", [])
    watchlist = cfg.get("watchlist", [])
    settings = cfg.get("settings", {})
    deployments = cfg.get("upcoming_deployments", [])

    threshold = settings.get("alert_threshold_pct", 5)
    gain_thresh = settings.get("scanner_gainers_pct", 8)
    loss_thresh = settings.get("scanner_losers_pct", -8)

    position_tickers = [p["ticker"] for p in positions]
    all_tickers = list(set(position_tickers + watchlist))

    quotes = fetch_quotes(all_tickers)

    portfolio_table = build_portfolio_table(positions, quotes, threshold)
    scanner_table = build_scanner_table(watchlist, quotes, gain_thresh, loss_thresh)
    deployment_panel = build_deployment_panel(deployments)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = Panel(
        f"[bold white]Investment Tracker[/bold white]  ·  [dim]{now}[/dim]  ·  "
        f"[dim]prices ~15min delayed via Yahoo Finance[/dim]",
        border_style="blue",
    )

    layout = Layout()
    layout.split_column(
        Layout(header, size=3),
        Layout(deployment_panel, size=6),
        Layout(portfolio_table, name="portfolio"),
        Layout(scanner_table, name="scanner"),
    )
    return layout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", action="store_true", help="One-shot watchlist scanner")
    parser.add_argument("--once", action="store_true", help="One-shot dashboard snapshot")
    parser.add_argument("--config", default="portfolio.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    refresh = cfg.get("settings", {}).get("refresh_seconds", 60)

    if args.scan:
        watchlist = cfg.get("watchlist", [])
        settings = cfg.get("settings", {})
        quotes = fetch_quotes(watchlist)
        table = build_scanner_table(
            watchlist, quotes,
            settings.get("scanner_gainers_pct", 8),
            settings.get("scanner_losers_pct", -8),
        )
        console.print(table)
        return

    if args.once:
        console.print(render(cfg))
        return

    # Live dashboard
    with Live(render(cfg), refresh_per_second=1, screen=True) as live:
        while True:
            time.sleep(refresh)
            try:
                cfg = load_config(args.config)
                live.update(render(cfg))
            except KeyboardInterrupt:
                break
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")


if __name__ == "__main__":
    main()
