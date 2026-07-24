"""
Technical signals: RSI (14-day), upcoming earnings date, recent news, 52W return.
All via yfinance — no extra API keys required.
Cached aggressively to avoid rate limits.
"""
from __future__ import annotations

import math
import time
from typing import Optional


def _finite_or_none(v):
    """Coerce NaN/inf to None. Python's json module happily emits `NaN` which
    then crashes browsers' JSON.parse() on the WebSocket payload."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


_rsi_cache: dict[str, tuple[float | None, float]] = {}
_news_cache: dict[str, tuple[list, float]] = {}
_earnings_cache: dict[str, tuple[str | None, float]] = {}
_year_cache: dict[str, tuple[float | None, float]] = {}

RSI_TTL = 900           # 15 min
NEWS_TTL = 1800         # 30 min
EARNINGS_TTL = 21600    # 6 hours
YEAR_TTL = 21600        # 6 hours (52W return — slow-moving)

# Minimum weekly bars required to compute a valid 52W return.
# 1y weekly = ~52 bars; require 40 to tolerate gaps/new listings.
YEAR_MIN_BARS = 40


def _fetch_history_via_session(ticker: str, period_days: int, interval: str) -> list[float]:
    """Fetch closing prices using the shared cookie+crumb session from quotes.py.
    Falls back to yfinance if the session fetch fails (e.g. local dev)."""
    from src.quotes import _session, _crumb, _ensure_session, _HEADERS
    import datetime as _dt
    _ensure_session()
    end_ts = int(time.time()) + 86400
    start_ts = int(time.time()) - period_days * 86400
    params = {"interval": interval, "period1": start_ts, "period2": end_ts}
    if _crumb:
        params["crumb"] = _crumb
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        r = _session.get(url, headers=_HEADERS, params=params, timeout=15)
        if r.status_code in (401, 403):
            from src.quotes import _init_session
            _init_session()
            params["crumb"] = _crumb
            r = _session.get(url, headers=_HEADERS, params=params, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        return [c for c in closes if c is not None]
    except Exception:
        return []


def get_rsi(ticker: str) -> Optional[float]:
    now = time.time()
    cached = _rsi_cache.get(ticker)
    if cached and now - cached[1] < RSI_TTL:
        return cached[0]
    result = None
    try:
        closes = _fetch_history_via_session(ticker, period_days=95, interval="1d")
        if len(closes) >= 15:
            import pandas as _pd
            s = _pd.Series(closes)
            delta = s.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss
            raw = float((100 - (100 / (1 + rs))).iloc[-1])
            f = _finite_or_none(raw)
            result = round(f, 1) if f is not None else None
    except Exception:
        pass
    _rsi_cache[ticker] = (result, now)
    return result


def rsi_signal(rsi: Optional[float]) -> str:
    if rsi is None or (isinstance(rsi, float) and not math.isfinite(rsi)):
        return ""
    if rsi >= 75:
        return "strongly overbought"
    if rsi >= 70:
        return "overbought"
    if rsi <= 25:
        return "strongly oversold"
    if rsi <= 30:
        return "oversold"
    return "neutral"


def get_earnings_date(ticker: str) -> Optional[str]:
    now = time.time()
    cached = _earnings_cache.get(ticker)
    if cached and now - cached[1] < EARNINGS_TTL:
        return cached[0]
    result = None
    try:
        import yfinance as yf
        from datetime import date
        today = date.today()
        t = yf.Ticker(ticker)
        # yfinance returns earnings_dates DESCENDING (newest→oldest). Iterating and
        # breaking on the first ts >= today picks the FURTHEST future date, not the
        # nearest — which then makes the 7-day earnings alert fire on the wrong quarter.
        # Collect all future dates, then take the minimum.
        ed = t.earnings_dates
        if ed is not None and not ed.empty:
            future = []
            for ts in ed.index:
                try:
                    d = ts.date()
                    if d >= today:
                        future.append((d, ts))
                except Exception:
                    continue
            if future:
                future.sort(key=lambda x: x[0])
                result = future[0][1].strftime("%d %b %Y")
    except Exception:
        pass
    _earnings_cache[ticker] = (result, now)
    return result


def get_news(ticker: str, max_items: int = 4) -> list[dict]:
    now = time.time()
    cached = _news_cache.get(ticker)
    if cached and now - cached[1] < NEWS_TTL:
        return cached[0]
    result = []
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).news or []
        for item in raw[:max_items]:
            title = item.get("title", "")
            publisher = item.get("publisher", "")
            link = item.get("link", "") or item.get("url", "")
            if title:
                result.append({"title": title, "publisher": publisher, "link": link})
    except Exception:
        pass
    _news_cache[ticker] = (result, now)
    return result


def get_year_return(ticker: str) -> Optional[float]:
    """52-week price return in %. Positive = stock up over past year."""
    now = time.time()
    cached = _year_cache.get(ticker)
    if cached and now - cached[1] < YEAR_TTL:
        return cached[0]
    result = None
    try:
        closes = _fetch_history_via_session(ticker, period_days=370, interval="1wk")
        if len(closes) >= YEAR_MIN_BARS:
            first_close = closes[0]
            last_close = closes[-1]
            if first_close and first_close > 0:
                raw = (last_close - first_close) / first_close * 100
                f = _finite_or_none(raw)
                result = round(f, 1) if f is not None else None
    except Exception:
        pass
    _year_cache[ticker] = (result, now)
    return result


def get_signals(ticker: str) -> dict:
    rsi = get_rsi(ticker)
    return {
        "rsi": rsi,
        "rsi_signal": rsi_signal(rsi),
        "earnings_date": get_earnings_date(ticker),
        "news": get_news(ticker, max_items=4),
        "year_return": get_year_return(ticker),
    }


def days_until_earnings(ticker: str) -> Optional[int]:
    """Return number of days until next earnings, or None."""
    date_str = get_earnings_date(ticker)
    if not date_str:
        return None
    try:
        from datetime import date, datetime
        d = datetime.strptime(date_str, "%d %b %Y").date()
        return (d - date.today()).days
    except Exception:
        return None
