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
        self._last_history_refresh = None
        self._last_health_check = None

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

        # Refresh history charts every 6 hours (weekly data, no need for more)
        if self._last_history_refresh is None or (now - self._last_history_refresh).total_seconds() >= 21600:
            self._last_history_refresh = now
            asyncio.create_task(self._refresh_history())

        # Health check every 6 hours
        if self._last_health_check is None or (now - self._last_health_check).total_seconds() >= 21600:
            self._last_health_check = now
            asyncio.create_task(self._run_health_check())

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
        import logging
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._refresh_quotes_sync)
        except Exception as e:
            logging.getLogger(__name__).error("_refresh_quotes failed: %s", e, exc_info=True)

    def _refresh_quotes_sync(self):
        import json as _json
        import logging
        _log = logging.getLogger(__name__)
        _log.info("_refresh_quotes_sync starting")
        from src.quotes import fetch_quotes, day_change_pct, get_fx_rates, is_market_open_us, is_market_open_eu, currency_to_eur_rate
        from src.positions import TICKER_NAMES, compute_lifetime_stats  # noqa: F401 (used below)

        def _signal(pct):
            if pct is None: return "no data"
            if pct >= 8: return "EXTRAORDINARY GAIN"
            if pct <= -8: return "EXTRAORDINARY DROP"
            if pct >= 3: return "strong up"
            if pct <= -3: return "strong down"
            return "neutral"

        DEPLOYMENTS = self.state.get("deployments", [])

        positions = self.state.get("positions", {})
        watchlist = self.state.get("watchlist", [])
        hot_picks = set(self.state.get("hot_picks", []))
        signals = self.state.get("signals_cache", {})

        all_tickers = list(set(list(positions.keys()) + watchlist))
        _log.info("fetching quotes for %d tickers: %s", len(all_tickers), all_tickers[:5])
        quotes = fetch_quotes(all_tickers)
        fx = get_fx_rates()
        _log.info("quotes done: %d results, fx=%s", len(quotes), fx)

        self.state["quotes_cache"] = quotes
        self.state["fx_cache"] = fx

        # Lifetime stats (file read, fast)
        try:
            self.state["lifetime_cache"] = compute_lifetime_stats()
        except Exception as e:
            _log.error("compute_lifetime_stats failed: %s", e)

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
                price_eur = price * currency_to_eur_rate(currency)
                current_value = round(pos.shares * price_eur, 2)
                pnl_eur = round(current_value - pos.total_cost_eur, 2)
                if pos.total_cost_eur > 0:
                    pnl_pct = round(pnl_eur / pos.total_cost_eur * 100, 2)
            portfolio_rows.append({
                **pos.to_dict(),
                "name": TICKER_NAMES.get(ticker, pos.name),
                "price": price, "currency": currency,
                "day_pct": day_pct, "day_high": q.get("day_high"), "day_low": q.get("day_low"),
                "high_52w": q.get("high_52w"), "low_52w": q.get("low_52w"),
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
                "week_pct": q.get("week_pct"),
                "high_52w": q.get("high_52w"),
                "low_52w":  q.get("low_52w"),
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
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._refresh_hot_picks_sync)
        except Exception:
            pass

    def _refresh_hot_picks_sync(self):
        from src.quotes import fetch_quotes, day_change_pct
        from src.positions import TICKER_NAMES
        # Import canonical watchlist from api.py so HC-2 and house savings tickers are never dropped
        from src.api import WATCHLIST_BASE

        HOT_PICKS_UNIVERSE = [
            "NVDA", "AMD", "TSM", "ARM", "SMCI", "AVGO", "MRVL", "INTC", "QCOM",
            "PLTR", "AI", "SOUN", "BBAI", "UPST", "PATH", "SNOW",
            "IONQ", "RGTI", "QUBT", "QBTS",
            "META", "GOOGL", "MSFT", "AMZN", "TSLA", "AAPL",
            "VRT", "VST", "CEG", "NRG",
            "RXRX", "TMDX",
            # HC-2 picks always in universe so daily moves are scored
            "AMTM", "ONDS", "LEU", "TSSI", "BKSY", "AMSC",
        ]
        try:
            quotes = fetch_quotes(HOT_PICKS_UNIVERSE)
            scored = []
            for ticker, q in quotes.items():
                pct = day_change_pct(q)
                if pct is not None:
                    scored.append((abs(pct), ticker))
            scored.sort(reverse=True)
            hot = [t for _, t in scored[:8]]
            merged = list(dict.fromkeys(WATCHLIST_BASE + hot))
            self.state["watchlist"] = merged
            self.state["hot_picks"] = hot
        except Exception:
            pass

    async def _refresh_history(self):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._refresh_history_sync)
        except Exception:
            pass

    def _fetch_history_series(self, ticker: str, start: str, end_daily: str) -> list[dict]:
        """Fetch weekly+daily history via Yahoo cookie session. Returns list of {date, close, currency}."""
        import requests as _req
        from datetime import date, timedelta
        from src.quotes import _session, _crumb, _ensure_session, _HEADERS
        _ensure_session()

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        points_raw = []
        for interval, range_start, range_end in [
            ("1wk", start, end_daily),
            ("1d",  end_daily, tomorrow),
        ]:
            params = {
                "interval": interval,
                "period1": self._to_ts(range_start),
                "period2": self._to_ts(range_end),
            }
            if _crumb:
                params["crumb"] = _crumb
            try:
                url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
                r = _session.get(url, headers=_HEADERS, params=params, timeout=15)
                if r.status_code == 401:
                    _ensure_session()
                    params["crumb"] = _crumb
                    r = _session.get(url, headers=_HEADERS, params=params, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
                result = data["chart"]["result"][0]
                meta = result["meta"]
                currency = meta.get("currency", "?")
                timestamps = result.get("timestamp", [])
                closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                import datetime as _dt
                for ts, c in zip(timestamps, closes):
                    if c is None:
                        continue
                    date_str = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    points_raw.append({"date": date_str, "close": c, "currency": currency})
            except Exception:
                continue
        # Deduplicate by date, keep last
        seen = {}
        for p in points_raw:
            seen[p["date"]] = p
        return sorted(seen.values(), key=lambda x: x["date"])

    @staticmethod
    def _to_ts(date_str: str) -> int:
        from datetime import datetime
        return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())

    def _refresh_history_sync(self):
        from datetime import date, datetime, timedelta
        from src.quotes import get_fx_rates, currency_to_eur_rate
        from src.positions import TICKER_NAMES, compute_closed_positions

        positions = self.state.get("positions", {})
        if not positions:
            return

        fx = get_fx_rates()

        # Closed positions — for historical context only
        closed = compute_closed_positions()

        # Determine global earliest date across open + closed
        earliest = None
        for pos in positions.values():
            if pos.first_buy_date:
                d = datetime.strptime(pos.first_buy_date, "%Y-%m-%d").date()
                if earliest is None or d < earliest:
                    earliest = d
        for cp in closed:
            if cp["first_buy_date"]:
                d = datetime.strptime(cp["first_buy_date"], "%Y-%m-%d").date()
                if earliest is None or d < earliest:
                    earliest = d
        if earliest is None:
            earliest = date.today() - timedelta(days=365)
        earliest = max(earliest, date.today() - timedelta(days=365 * 3))

        daily_start = (date.today() - timedelta(days=90)).isoformat()
        result = []
        ticker_value_series: dict[str, dict[str, float]] = {}

        # ── Open positions ────────────────────────────────────────────
        for ticker, pos in positions.items():
            try:
                raw = self._fetch_history_series(ticker, earliest.isoformat(), daily_start)
                if not raw:
                    continue
                currency = raw[0]["currency"]
                buy_date = datetime.strptime(pos.first_buy_date, "%Y-%m-%d").date() if pos.first_buy_date else earliest
                points = []
                val_by_date: dict[str, float] = {}
                for p in raw:
                    row_date = datetime.strptime(p["date"], "%Y-%m-%d").date()
                    if row_date < buy_date:
                        continue
                    close = p["close"]
                    close_eur = close * currency_to_eur_rate(currency)
                    # pct anchored to avg cost — never rebased on period filter
                    pct = (close_eur - pos.avg_cost_eur) / pos.avg_cost_eur * 100 if pos.avg_cost_eur else 0
                    val = pos.shares * close_eur
                    points.append({"date": p["date"], "pct": round(pct, 2), "price": round(close, 2), "value_eur": round(val, 2)})
                    val_by_date[p["date"]] = val
                if points:
                    result.append({
                        "ticker": ticker,
                        "name": TICKER_NAMES.get(ticker, ticker),
                        "bucket": pos.bucket,
                        "avg_cost_eur": round(pos.avg_cost_eur, 2),
                        "first_buy_date": pos.first_buy_date,
                        "closed": False,
                        "points": points,
                    })
                    ticker_value_series[ticker] = val_by_date
            except Exception:
                continue

        # ── Closed positions — history only between first buy and last sell ──
        closed_value_series: dict[str, dict[str, float]] = {}
        for cp in closed:
            ticker = cp["ticker"]
            if not cp["first_buy_date"] or not cp["last_sell_date"]:
                continue
            try:
                raw = self._fetch_history_series(ticker, cp["first_buy_date"], cp["last_sell_date"])
                if not raw:
                    continue
                currency = raw[0]["currency"]
                peak_shares = cp.get("peak_shares", 0)
                buy_date = datetime.strptime(cp["first_buy_date"], "%Y-%m-%d").date()
                sell_date = datetime.strptime(cp["last_sell_date"], "%Y-%m-%d").date()
                # avg_cost_eur per share = total_cost / peak_shares
                avg_cost_per_share = cp["total_cost_eur"] / peak_shares if peak_shares else 0
                first_close_eur = None
                points = []
                val_by_date: dict[str, float] = {}
                for p in raw:
                    row_date = datetime.strptime(p["date"], "%Y-%m-%d").date()
                    if row_date < buy_date or row_date > sell_date:
                        continue
                    close = p["close"]
                    close_eur = close * currency_to_eur_rate(currency)
                    if first_close_eur is None:
                        first_close_eur = close_eur
                    # % anchored to first yahoo close in holding period — transaction
                    # cost can't be used because Yahoo adjusts historical prices after
                    # fund restructuring/consolidation, causing fake returns.
                    pct = (close_eur - first_close_eur) / first_close_eur * 100 if first_close_eur else 0
                    val = peak_shares * close_eur
                    points.append({"date": p["date"], "pct": round(pct, 2), "price": round(close, 2), "value_eur": round(val, 2)})
                    val_by_date[p["date"]] = val
                if points:
                    result.append({
                        "ticker": ticker,
                        "name": TICKER_NAMES.get(ticker, ticker),
                        "bucket": cp["bucket"],
                        "avg_cost_eur": round(avg_cost_per_share, 2),
                        "first_buy_date": cp["first_buy_date"],
                        "last_sell_date": cp["last_sell_date"],
                        "closed": True,
                        "points": points,
                    })
                    closed_value_series[ticker] = val_by_date
            except Exception:
                continue

        # Build portfolio total (open positions only, forward-filled)
        all_dates = sorted({d for vs in ticker_value_series.values() for d in vs})
        portfolio_by_date: dict[str, float] = {}
        for d in all_dates:
            portfolio_by_date[d] = 0.0
        for vs in ticker_value_series.values():
            last_val = 0.0
            for d in all_dates:
                if d in vs:
                    last_val = vs[d]
                portfolio_by_date[d] += last_val

        # Build historic portfolio total (open + closed) for the pre-exit grey line.
        # Closed tickers must NOT be forward-filled past their last_sell_date.
        closed_sell_dates = {cp["ticker"]: cp["last_sell_date"] for cp in closed if cp.get("last_sell_date")}
        all_dates_hist = sorted({d for vs in {**ticker_value_series, **closed_value_series}.values() for d in vs})
        portfolio_historic: dict[str, float] = {d: 0.0 for d in all_dates_hist}
        for ticker, vs in ticker_value_series.items():
            last_val = 0.0
            for d in all_dates_hist:
                if d in vs:
                    last_val = vs[d]
                portfolio_historic[d] += last_val
        for ticker, vs in closed_value_series.items():
            sell_date = closed_sell_dates.get(ticker)
            last_val = 0.0
            for d in all_dates_hist:
                if d in vs:
                    last_val = vs[d]
                # Stop contributing after sell date — do not forward-fill past exit
                if sell_date and d <= sell_date:
                    portfolio_historic[d] += last_val

        result.sort(key=lambda x: x["first_buy_date"] or "")
        portfolio_points = [{"date": d, "value_eur": round(v, 2)} for d, v in sorted(portfolio_by_date.items())]
        portfolio_historic_points = [{"date": d, "value_eur": round(v, 2)} for d, v in sorted(portfolio_historic.items())]
        self.state["history_cache"] = {
            "series": result,
            "portfolio": portfolio_points,
            "portfolio_historic": portfolio_historic_points,
        }

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
        for ticker in all_tickers[:20]:  # cap to avoid slow startup
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
        from src.quotes import fetch_quotes, day_change_pct, currency_to_eur_rate
        from src.briefing import generate_briefing

        positions = self.state.get("positions", {})
        watchlist = self.state.get("watchlist", [])
        signals = self.state.get("signals_cache", {})
        all_tickers = list(positions.keys()) + watchlist
        quotes = fetch_quotes(all_tickers)

        portfolio_snapshot = []
        for ticker, pos in positions.items():
            q = quotes.get(ticker, {})
            price = q.get("price")
            currency = q.get("currency", "EUR")
            pnl_pct = None
            if price and pos.avg_cost_eur:
                price_eur = price * currency_to_eur_rate(currency)
                pnl_pct = (price_eur - pos.avg_cost_eur) / pos.avg_cost_eur * 100
            sig = signals.get(ticker, {})
            portfolio_snapshot.append({
                **pos.to_dict(),
                "price": price,
                "currency": q.get("currency", "EUR"),
                "day_pct": day_change_pct(q),
                "pnl_pct": pnl_pct,
                "high_52w": q.get("high_52w"),
                "low_52w": q.get("low_52w"),
                "rsi": sig.get("rsi"),
                "rsi_signal": sig.get("rsi_signal"),
                "earnings_date": sig.get("earnings_date"),
            })

        scanner_snapshot = []
        for t in watchlist:
            q = quotes.get(t, {})
            sig = signals.get(t, {})
            scanner_snapshot.append({
                "ticker": t,
                "price": q.get("price"),
                "day_pct": day_change_pct(q),
                "week_pct": q.get("week_pct"),
                "currency": q.get("currency", "?"),
                "high_52w": q.get("high_52w"),
                "low_52w": q.get("low_52w"),
                "rsi": sig.get("rsi"),
                "rsi_signal": sig.get("rsi_signal"),
                "earnings_date": sig.get("earnings_date"),
            })

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

            # 52-week context
            high_52w = q.get("high_52w")
            low_52w = q.get("low_52w")
            w52_str = ""
            if price and high_52w and low_52w:
                pct_off_high = (price - high_52w) / high_52w * 100
                pct_off_low = (price - low_52w) / low_52w * 100
                w52_str = f"52w: {low_52w:.2f}–{high_52w:.2f} {currency}  ({pct_off_high:+.0f}% vs high, {pct_off_low:+.0f}% vs low)"

            lines = []
            if value_eur:
                lines.append(f"{name}")
                lines.append(f"Price: {price:.2f} {currency}  ({pct:+.1f}% today)")
                lines.append(f"You hold: {pos.shares:.0f} shares  ≈ €{value_eur:,.0f}")
                lines.append(f"Total P&L: {pnl_sign}€{abs(pnl_eur):,.0f} vs €{pos.total_cost_eur:,.0f} cost")
            else:
                lines.append(f"{name}\nPrice: {price} {currency}  ({pct:+.1f}% today)")
            if w52_str:
                lines.append(w52_str)
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
            high_52w = q.get("high_52w")
            low_52w = q.get("low_52w")
            w52_str = ""
            if price and high_52w and low_52w:
                pct_off_high = (price - high_52w) / high_52w * 100
                pct_off_low = (price - low_52w) / low_52w * 100
                w52_str = f"52w: {low_52w:.2f}–{high_52w:.2f} {currency}  ({pct_off_high:+.0f}% vs high, {pct_off_low:+.0f}% vs low)"

            lines = [
                f"{name}",
                f"Price: {price:.2f} {currency}  |  Extraordinary move {pct:+.1f}%",
            ]
            if w52_str:
                lines.append(w52_str)
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

    async def _run_health_check(self):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._health_check_sync)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("health check failed: %s", e)

    def _health_check_sync(self):
        from src.quotes import fetch_quotes, day_change_pct
        from src.positions import compute_positions

        issues = []
        ok = []

        # 1. Positions loaded
        positions = self.state.get("positions", {})
        if not positions:
            issues.append("❌ No positions loaded — transaction files may be missing")
        else:
            ok.append(f"✅ {len(positions)} positions loaded")

        # 2. Prices returning values
        if positions:
            tickers = list(positions.keys())
            quotes = fetch_quotes(tickers)
            missing_price = [t for t in tickers if quotes.get(t, {}).get("price") is None]
            stale_price = [t for t in tickers if quotes.get(t, {}).get("price") is not None]
            if missing_price:
                issues.append(f"❌ No price for: {', '.join(missing_price)}")
            if stale_price:
                ok.append(f"✅ Prices OK: {', '.join(stale_price)}")

        # 3. FX rates
        fx = self.state.get("fx_cache", {})
        if not fx or "USD" not in fx:
            issues.append("❌ FX rates missing (USD/EUR unavailable)")
        else:
            ok.append(f"✅ FX OK: 1 USD = {fx.get('USD', '?')} EUR⁻¹")

        # 4. History cache populated
        history = self.state.get("history_cache")
        if not history or not history.get("series"):
            issues.append("❌ History cache empty — charts may not render")
        else:
            n_series = len(history["series"])
            n_portfolio_pts = len(history.get("portfolio", []))
            ok.append(f"✅ History: {n_series} series, {n_portfolio_pts} portfolio points")

        # 5. Signals cache
        signals = self.state.get("signals_cache", {})
        missing_signals = [t for t in positions if t not in signals]
        if missing_signals:
            issues.append(f"⚠️ RSI missing for: {', '.join(missing_signals)}")
        else:
            ok.append(f"✅ Signals cached for all {len(positions)} holdings")

        # 6. WebSocket payload ready
        if not self.state.get("ws_payload_cache"):
            issues.append("❌ WebSocket payload not built — live updates won't work")
        else:
            ok.append("✅ WebSocket payload ready")

        # 7. Portfolio value sanity check
        quotes_cache = self.state.get("quotes_cache", {})
        total_value = 0.0
        for ticker, pos in positions.items():
            q = quotes_cache.get(ticker, {})
            price = q.get("price")
            currency = q.get("currency", "EUR")
            if price:
                from src.quotes import currency_to_eur_rate as _cte
                total_value += pos.shares * price * _cte(currency)
        if total_value > 0:
            ok.append(f"✅ Portfolio value: €{total_value:,.0f}")
        else:
            issues.append("❌ Portfolio value = €0 — all prices may be stale")

        # Build and send report
        now_str = now_dublin().strftime("%d/%m %H:%M")
        if issues:
            title = f"⚠️ Dashboard health check — {len(issues)} issue(s)"
            body = "\n".join(issues) + "\n\n" + "\n".join(ok)
        else:
            title = f"✅ Dashboard healthy — {now_str}"
            body = "\n".join(ok)

        notify(title, body, alert_key=f"health_{now_dublin().strftime('%Y-%m-%d-%H')}")
