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
        self._last_signals_refresh = None

    async def run(self):
        await asyncio.sleep(5)  # let uvicorn finish startup before any fetches
        while True:
            await self._tick()
            await asyncio.sleep(30)

    async def _tick(self):
        now = now_dublin()
        date_str = now.strftime("%Y-%m-%d")

        # Refresh signals cache every 30 min (non-blocking background task)
        if self._last_signals_refresh is None or (now - self._last_signals_refresh).total_seconds() >= 1800:
            self._last_signals_refresh = now
            asyncio.create_task(self._refresh_signals())

        # Midnight reload — pick up new exports + refresh hot picks universe
        if now.hour == 0 and self._last_midnight_reload != date_str:
            self._last_midnight_reload = date_str
            await self._reload_positions()
            await self._refresh_hot_picks()

        # Also refresh hot picks at 08:00 (fresh data for EU open)
        if now.hour == 8 and now.minute < 5 and self._last_midnight_reload == date_str:
            await self._refresh_hot_picks()

        # EU market open briefing (08:05 Dublin)
        if now.hour == 8 and now.minute >= 5 and self._last_eu_open != date_str and now.weekday() < 5:
            self._last_eu_open = date_str
            asyncio.create_task(self._run_scan(market="EU"))

        # US market open briefing (15:35 Dublin)
        if now.hour == 15 and now.minute >= 35 and self._last_us_open != date_str and now.weekday() < 5:
            self._last_us_open = date_str
            asyncio.create_task(self._run_scan(market="US"))

        # EOD AI briefing (22:05 Dublin)
        if now.hour == 22 and now.minute >= 5 and self._last_eod != date_str and now.weekday() < 5:
            self._last_eod = date_str
            asyncio.create_task(self._run_eod_briefing())

        # During market hours: refresh every 5 min and check alerts
        if self._should_refresh(now):
            last = self._last_alert_check
            if last is None or (now - last).total_seconds() >= 300:
                self._last_alert_check = now
                asyncio.create_task(self._check_alerts())

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

    async def _refresh_hot_picks(self):
        from src.api import _refresh_hot_picks
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _refresh_hot_picks)
        except Exception:
            pass

    async def _refresh_signals(self):
        """Fetch RSI/earnings/news per ticker, one at a time in a thread, yielding between each."""
        from src.signals import get_signals
        loop = asyncio.get_event_loop()
        positions = self.state.get("positions", {})
        watchlist = self.state.get("watchlist", [])
        all_tickers = list(set(list(positions.keys()) + watchlist))
        cache = dict(self.state.get("signals_cache", {}))  # keep stale data while refreshing
        for ticker in all_tickers:
            try:
                result = await loop.run_in_executor(None, get_signals, ticker)
                cache[ticker] = result
            except Exception:
                pass
            await asyncio.sleep(0.1)  # yield between tickers, don't hammer Yahoo
        self.state["signals_cache"] = cache

    async def _run_scan(self, market: str):
        from src.quotes import fetch_quotes, day_change_pct
        from src.positions import TICKER_NAMES
        from src.signals import get_rsi, rsi_signal, days_until_earnings
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

        # Earnings warnings for holdings (within 7 days)
        earn_warnings = []
        for ticker in self.state.get("positions", {}):
            d = days_until_earnings(ticker)
            if d is not None and 0 <= d <= 7:
                earn_warnings.append(f"⚠️ {ticker} earnings in {d}d")

        # RSI extremes across all tickers
        rsi_alerts = []
        for ticker in tickers[:20]:  # cap to avoid slow startup
            rsi = get_rsi(ticker)
            sig = rsi_signal(rsi)
            if sig and sig != "neutral":
                rsi_alerts.append(f"RSI {ticker}: {rsi} ({sig})")

        lines = []
        if movers:
            for pct, ticker, name, price, currency in movers[:6]:
                arrow = "🟢" if pct >= 0 else "🔴"
                lines.append(f"{arrow} {ticker} ({name}): {pct:+.1f}% @ {price:.2f} {currency}")
        else:
            lines.append("No significant moves (all within ±3%).")

        if earn_warnings:
            lines.append("\n" + "\n".join(earn_warnings))
        if rsi_alerts:
            lines.append("\n📡 " + " | ".join(rsi_alerts[:4]))

        notify(
            f"📊 {market} Open{' — ' + str(len(movers)) + ' movers' if movers else ''}",
            "\n".join(lines),
        )

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
        from src.signals import get_rsi, rsi_signal, get_news, days_until_earnings
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

            # RSI context
            rsi = get_rsi(ticker)
            rsi_str = f"RSI: {rsi} ({rsi_signal(rsi)})" if rsi else ""

            # Upcoming earnings
            earn_days = days_until_earnings(ticker)
            earn_str = f"⚠️ Earnings in {earn_days}d" if earn_days is not None and earn_days <= 7 else ""

            # Recent news
            news = get_news(ticker, max_items=2)
            news_str = "\n".join(f"• {n['title']}" for n in news) if news else ""

            lines = []
            if value_eur:
                lines.append(f"{name}")
                lines.append(f"Price: {price:.2f} {currency}  ({pct:+.1f}% today)")
                lines.append(f"You hold: {pos.shares:.0f} shares  ≈ €{value_eur:,.0f}")
                lines.append(f"Total P&L: {pnl_sign}€{abs(pnl_eur):,.0f} vs €{pos.total_cost_eur:,.0f} cost")
            else:
                lines.append(f"{name}\nPrice: {price} {currency}  ({pct:+.1f}% today)")
            if rsi_str:
                lines.append(rsi_str)
            if earn_str:
                lines.append(earn_str)
            if news_str:
                lines.append(f"\n📰 News:\n{news_str}")

            notify(
                f"{emoji} {ticker} {direction} {abs(pct):.1f}%",
                "\n".join(lines),
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

            rsi = get_rsi(ticker)
            rsi_str = f"RSI: {rsi} ({rsi_signal(rsi)})" if rsi else ""
            earn_days = days_until_earnings(ticker)
            earn_str = f"⚠️ Earnings in {earn_days}d" if earn_days is not None and earn_days <= 7 else ""
            news = get_news(ticker, max_items=2)
            news_str = "\n".join(f"• {n['title']}" for n in news) if news else ""

            lines = [
                f"{name}",
                f"Price: {price:.2f} {currency}  |  Extraordinary move {pct:+.1f}%",
            ]
            if rsi_str:
                lines.append(rsi_str)
            if earn_str:
                lines.append(earn_str)
            if news_str:
                lines.append(f"\n📰 News:\n{news_str}")

            notify(
                f"{emoji} WATCHLIST: {ticker} {direction} {abs(pct):.1f}%",
                "\n".join(lines),
                alert_key=f"watch_{ticker}_{direction}_{int(abs(pct))}",
            )
