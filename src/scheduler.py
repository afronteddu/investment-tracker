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
        self._last_quotes_refresh = None

    async def run(self):
        await asyncio.sleep(5)  # let uvicorn finish startup before any fetches
        while True:
            await self._tick()
            await asyncio.sleep(30)

    async def _tick(self):
        now = now_dublin()
        date_str = now.strftime("%Y-%m-%d")

        # Refresh quotes + build all caches every 60s
        if self._last_quotes_refresh is None or (now - self._last_quotes_refresh).total_seconds() >= 60:
            self._last_quotes_refresh = now
            asyncio.create_task(self._refresh_quotes())

        # Refresh signals (RSI/earnings/news) every 30 min
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

    async def _refresh_quotes(self):
        """Fetch all quotes + FX + lifetime stats in a thread, then build portfolio/scanner/WS caches."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._refresh_quotes_sync)
        except Exception:
            pass

    def _refresh_quotes_sync(self):
        import json as _json
        from src.quotes import fetch_quotes, day_change_pct, get_fx_rates, to_eur
        from src.positions import TICKER_NAMES
        from src.api import _compute_lifetime_stats, _signal, DEPLOYMENTS, is_market_open_us, is_market_open_eu

        positions = self.state.get("positions", {})
        watchlist = self.state.get("watchlist", [])
        hot_picks = set(self.state.get("hot_picks", []))
        signals = self.state.get("signals_cache", {})

        all_tickers = list(set(list(positions.keys()) + watchlist))
        quotes = fetch_quotes(all_tickers)
        fx = get_fx_rates()

        self.state["quotes_cache"] = quotes
        self.state["fx_cache"] = fx

        # Lifetime stats (file read, fast)
        try:
            self.state["lifetime_cache"] = _compute_lifetime_stats()
        except Exception:
            pass

        # Portfolio rows
        portfolio_rows = []
        for ticker, pos in positions.items():
            q = quotes.get(ticker, {})
            price = q.get("price")
            day_pct = day_change_pct(q)
            sig = signals.get(ticker, {})
            currency = q.get("currency", "EUR")
            pnl_eur = pnl_pct = current_value = None
            if price is not None:
                price_eur = price * fx.get(currency, 1.0)
                current_value = round(pos.shares * price_eur, 2)
                pnl_eur = round(current_value - pos.total_cost_eur, 2)
                if pos.total_cost_eur > 0:
                    pnl_pct = round(pnl_eur / pos.total_cost_eur * 100, 2)
            portfolio_rows.append({
                **pos.to_dict(),
                "name": TICKER_NAMES.get(ticker, pos.name),
                "price": price, "currency": currency,
                "day_pct": day_pct, "day_high": q.get("day_high"), "day_low": q.get("day_low"),
                "current_value_eur": current_value, "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                "rsi": sig.get("rsi"), "rsi_signal": sig.get("rsi_signal"), "earnings_date": sig.get("earnings_date"),
            })
        portfolio_rows.sort(key=lambda x: (x["bucket"], x["ticker"]))

        # Scanner rows
        scanner_rows = []
        for ticker in watchlist:
            q = quotes.get(ticker, {})
            pct = day_change_pct(q)
            sig = signals.get(ticker, {})
            scanner_rows.append({
                "ticker": ticker, "name": TICKER_NAMES.get(ticker, ""),
                "price": q.get("price"), "day_pct": pct,
                "day_high": q.get("day_high"), "day_low": q.get("day_low"),
                "currency": q.get("currency", "?"), "signal": _signal(pct),
                "hot": ticker in hot_picks,
                "rsi": sig.get("rsi"), "rsi_signal": sig.get("rsi_signal"),
                "earnings_date": sig.get("earnings_date"), "news": sig.get("news", []),
            })
        scanner_rows.sort(key=lambda x: x["day_pct"] if x["day_pct"] is not None else 0, reverse=True)
        self.state["scanner_cache"] = scanner_rows

        # Pre-build WebSocket payload
        lifetime = self.state.get("lifetime_cache", {})
        total_cost = sum(r["total_cost_eur"] for r in portfolio_rows)
        total_value = sum(r["current_value_eur"] for r in portfolio_rows if r["current_value_eur"])
        unrealized_pnl = total_value - total_cost
        total_pnl = lifetime.get("realized_pnl", 0) + unrealized_pnl
        total_deployed = lifetime.get("total_deployed", 0)
        payload = {
            "portfolio": portfolio_rows,
            "scanner": scanner_rows,
            "market_status": {"us_open": is_market_open_us(), "eu_open": is_market_open_eu()},
            "fx_rates": fx,
            "deployments": self.state.get("deployments", DEPLOYMENTS),
            "lifetime": {
                "total_deployed": total_deployed,
                "total_returned": lifetime.get("total_returned", 0),
                "realized_pnl": lifetime.get("realized_pnl", 0),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round(total_pnl / total_deployed * 100, 2) if total_deployed else 0,
                "monthly_flows": lifetime.get("monthly_flows", []),
            },
        }
        self.state["ws_payload_cache"] = _json.dumps(payload)

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
        from src.quotes import day_change_pct
        from src.positions import TICKER_NAMES
        from src.signals import get_rsi, rsi_signal, days_until_earnings
        quotes = self.state.get("quotes_cache", {})

        all_tickers = list(self.state.get("positions", {}).keys()) + self.state.get("watchlist", [])
        movers = []
        for ticker in all_tickers:
            q = quotes.get(ticker, {})
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
        from src.quotes import day_change_pct, to_eur
        from src.positions import TICKER_NAMES
        from src.signals import get_rsi, rsi_signal, get_news, days_until_earnings
        positions = self.state.get("positions", {})
        watchlist = self.state.get("watchlist", [])
        threshold = float(os.getenv("ALERT_THRESHOLD_PCT", "5"))
        extraordinary = float(os.getenv("SCANNER_GAINERS_PCT", "8"))

        # Read from pre-built quotes cache — no new network calls
        portfolio_quotes = self.state.get("quotes_cache", {})

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
        watchlist_quotes = self.state.get("quotes_cache", {})
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
