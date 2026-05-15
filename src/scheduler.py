"""
Market-aware scheduler. Runs continuously, triggers jobs based on market hours.
Dublin is UTC+1 (BST) in summer.

Schedule:
- EU open  (08:05 Dublin = 07:05 UTC): EU scan + notification
- US open  (15:35 Dublin = 14:35 UTC): US scan + notification
- US close (22:05 Dublin = 21:05 UTC): EOD briefing via Claude
- Every 5min during market hours: refresh quote cache + check alerts
- Midnight: reload transaction files (picks up new exports automatically)
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta

from src.notify import send as notify

DUBLIN_TZ = timezone(timedelta(hours=1))  # BST (summer). Change to +0 in winter.


def now_dublin() -> datetime:
    return datetime.now(DUBLIN_TZ)


class Scheduler:
    def __init__(self, app_state):
        self.state = app_state
        self._last_eu_open = None
        self._last_us_open = None
        self._last_eod = None
        self._last_alert_check = None
        self._last_midnight_reload = None

    async def run(self):
        while True:
            await self._tick()
            await asyncio.sleep(30)

    async def _tick(self):
        now = now_dublin()
        date_str = now.strftime("%Y-%m-%d")

        # Midnight reload — pick up any new transaction exports dropped into data/
        if now.hour == 0 and self._last_midnight_reload != date_str:
            self._last_midnight_reload = date_str
            await self._reload_positions()

        # EU market open briefing (08:05 Dublin)
        if now.hour == 8 and now.minute >= 5 and self._last_eu_open != date_str and now.weekday() < 5:
            self._last_eu_open = date_str
            await self._run_scan(market="EU")

        # US market open briefing (15:35 Dublin)
        if now.hour == 15 and now.minute >= 35 and self._last_us_open != date_str and now.weekday() < 5:
            self._last_us_open = date_str
            await self._run_scan(market="US")

        # EOD AI briefing (22:05 Dublin)
        if now.hour == 22 and now.minute >= 5 and self._last_eod != date_str and now.weekday() < 5:
            self._last_eod = date_str
            await self._run_eod_briefing()

        # During market hours: refresh every 5 min and check alerts
        if self._should_refresh(now):
            last = self._last_alert_check
            if last is None or (now - last).total_seconds() >= 300:
                self._last_alert_check = now
                await self._check_alerts()

    def _should_refresh(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        eu_open = (7 <= now.hour < 15) or (now.hour == 15 and now.minute <= 30)
        us_open = (15 <= now.hour < 22) or (now.hour == 14 and now.minute >= 30)
        return eu_open or us_open

    async def _reload_positions(self):
        import importlib
        from src import positions as pos_module
        importlib.reload(pos_module)
        self.state["positions"] = pos_module.compute_positions()

    async def _run_scan(self, market: str):
        from src.quotes import fetch_quotes, day_change_pct
        from src.positions import TICKER_NAMES
        tickers = list(self.state.get("positions", {}).keys()) + self.state.get("watchlist", [])
        quotes = fetch_quotes(tickers)

        movers = []
        for ticker, q in quotes.items():
            pct = day_change_pct(q)
            if pct is not None and abs(pct) >= 3:
                name = TICKER_NAMES.get(ticker, ticker)
                price = q.get("price")
                currency = q.get("currency", "")
                movers.append((pct, ticker, name, price, currency))

        movers.sort(key=lambda x: abs(x[0]), reverse=True)

        if movers:
            lines = []
            for pct, ticker, name, price, currency in movers[:6]:
                arrow = "🟢" if pct >= 0 else "🔴"
                lines.append(f"{arrow} {ticker} ({name}): {pct:+.1f}% @ {price:.2f} {currency}")
            notify(
                f"📊 {market} Open — {len(movers)} mover{'s' if len(movers)>1 else ''}",
                "\n".join(lines),
            )
        else:
            notify(f"📊 {market} Open", "No significant moves (all within ±3%).")

    async def _run_eod_briefing(self):
        from src.quotes import fetch_quotes, day_change_pct
        from src.briefing import generate_briefing

        positions = self.state.get("positions", {})
        watchlist = self.state.get("watchlist", [])
        all_tickers = list(positions.keys()) + watchlist
        quotes = fetch_quotes(all_tickers)

        portfolio_snapshot = []
        for ticker, pos in positions.items():
            q = quotes.get(ticker, {})
            price = q.get("price")
            pnl_pct = None
            if price and pos.avg_cost_eur:
                pnl_pct = (price - pos.avg_cost_eur) / pos.avg_cost_eur * 100
            portfolio_snapshot.append({
                **pos.to_dict(),
                "price": price,
                "day_pct": day_change_pct(q),
                "pnl_pct": pnl_pct,
            })

        scanner_snapshot = [
            {"ticker": t, "price": quotes.get(t, {}).get("price"), "day_pct": day_change_pct(quotes.get(t, {}))}
            for t in watchlist
        ]

        briefing = generate_briefing(portfolio_snapshot, scanner_snapshot)
        self.state["latest_briefing"] = briefing
        notify("Investment Tracker — EOD Briefing", "Your daily briefing is ready. Open the dashboard.")

    async def _check_alerts(self):
        from src.quotes import fetch_quotes, day_change_pct, to_eur
        from src.positions import TICKER_NAMES
        positions = self.state.get("positions", {})
        watchlist = self.state.get("watchlist", [])
        threshold = float(os.getenv("ALERT_THRESHOLD_PCT", "5"))
        extraordinary = float(os.getenv("SCANNER_GAINERS_PCT", "8"))

        # Portfolio holdings — alert at threshold (default 5%)
        portfolio_tickers = list(positions.keys())
        portfolio_quotes = fetch_quotes(portfolio_tickers)

        for ticker, pos in positions.items():
            q = portfolio_quotes.get(ticker, {})
            pct = day_change_pct(q)
            if pct is None or abs(pct) < threshold:
                continue
            direction = "UP" if pct > 0 else "DOWN"
            price = q.get("price")
            currency = q.get("currency", "")
            name = TICKER_NAMES.get(ticker, ticker)
            price_eur = to_eur(price, currency) if price else None
            value_eur = round(pos.shares * price_eur, 0) if price_eur else None
            pnl_eur = round(value_eur - pos.total_cost_eur, 0) if value_eur else None
            pnl_sign = "+" if pnl_eur and pnl_eur >= 0 else ""
            emoji = "🚀" if pct >= extraordinary else ("📈" if pct > 0 else ("💥" if pct <= -extraordinary else "📉"))
            msg = (
                f"{name}\n"
                f"Price: {price:.2f} {currency}  ({pct:+.1f}% today)\n"
                f"You hold: {pos.shares:.0f} shares  ≈ €{value_eur:,.0f}\n"
                f"Total P&L: {pnl_sign}€{abs(pnl_eur):,.0f} vs €{pos.total_cost_eur:,.0f} cost"
            ) if value_eur else f"{name}\nPrice: {price} {currency}  ({pct:+.1f}% today)"
            notify(
                f"{emoji} {ticker} {direction} {abs(pct):.1f}%",
                msg,
                alert_key=f"{ticker}_{direction}_{int(abs(pct))}",
            )

        # Watchlist — only alert on extraordinary moves (default ±8%)
        watchlist_quotes = fetch_quotes(watchlist)
        for ticker in watchlist:
            q = watchlist_quotes.get(ticker, {})
            pct = day_change_pct(q)
            if pct is None or abs(pct) < extraordinary:
                continue
            direction = "UP" if pct > 0 else "DOWN"
            price = q.get("price")
            currency = q.get("currency", "")
            name = TICKER_NAMES.get(ticker, ticker)
            emoji = "🚀" if pct > 0 else "💥"
            notify(
                f"{emoji} WATCHLIST: {ticker} {direction} {abs(pct):.1f}%",
                f"{name}\nPrice: {price:.2f} {currency}  |  Extraordinary move",
                alert_key=f"watch_{ticker}_{direction}_{int(abs(pct))}",
            )
