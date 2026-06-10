"""
Live quotes via Yahoo Finance v8 API with cookie+crumb auth.
Bypasses yfinance entirely — works from datacenter IPs where curl_cffi is unavailable.
"""
from __future__ import annotations

import time
import json
import threading
from typing import Optional

import requests

_cache: dict[str, dict] = {}
_cache_time: dict[str, float] = {}
_fx_cache: dict[str, float] = {}
_fx_cache_time: float = 0
CACHE_TTL = 60
FX_TTL = 300

# Yahoo cookie+crumb state (refreshed when expired)
_crumb: str = ""
_session = requests.Session()
_session_lock = threading.Lock()
_session_init_time: float = 0
SESSION_TTL = 3600  # re-authenticate every hour

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def _init_session():
    """Get Yahoo cookie and crumb. Must be called before any quote fetch."""
    global _crumb, _session_init_time
    try:
        # Step 1: hit Yahoo Finance to get cookie
        r = _session.get("https://fc.yahoo.com", headers=_HEADERS, timeout=10, allow_redirects=True)
        # Step 2: get crumb
        r2 = _session.get(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            headers={**_HEADERS, "Accept": "text/plain"},
            timeout=10,
        )
        if r2.status_code == 200 and r2.text and r2.text != "":
            _crumb = r2.text.strip()
            _session_init_time = time.time()
            return True
    except Exception:
        pass
    return False


def _ensure_session():
    with _session_lock:
        if not _crumb or time.time() - _session_init_time > SESSION_TTL:
            _init_session()


def _yahoo_quote(ticker: str) -> dict:
    _ensure_session()
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": "1d", "range": "5d"}
    if _crumb:
        params["crumb"] = _crumb
    try:
        r = _session.get(url, headers=_HEADERS, params=params, timeout=10)
        if r.status_code == 401 or r.status_code == 403:
            # Session expired — re-auth and retry once
            with _session_lock:
                _init_session()
            params["crumb"] = _crumb
            r = _session.get(url, headers=_HEADERS, params=params, timeout=10)
        if r.status_code != 200:
            return {}
        data = r.json()
        result = data["chart"]["result"][0]
        meta = result["meta"]
        quotes_data = result.get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in (quotes_data.get("close") or []) if c is not None]
        highs  = [h for h in (quotes_data.get("high")  or []) if h is not None]
        lows   = [l for l in (quotes_data.get("low")   or []) if l is not None]
        if not closes:
            # fallback to meta regularMarketPrice
            price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
            if not price:
                return {}
            return {
                "price": price,
                "prev_close": meta.get("chartPreviousClose") or price,
                "day_high": None, "day_low": None,
                "currency": meta.get("currency", "?"),
                "market_cap": None,
                "high_52w": meta.get("fiftyTwoWeekHigh"),
                "low_52w":  meta.get("fiftyTwoWeekLow"),
                "week_pct": None,
            }
        return {
            "price": closes[-1],
            "prev_close": closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose", closes[-1]),
            "day_high": highs[-1] if highs else None,
            "day_low":  lows[-1]  if lows  else None,
            "currency": meta.get("currency", "?"),
            "market_cap": None,
            "high_52w": meta.get("fiftyTwoWeekHigh"),
            "low_52w":  meta.get("fiftyTwoWeekLow"),
            "week_pct": round((closes[-1] - closes[0]) / closes[0] * 100, 2) if len(closes) >= 2 and closes[0] else None,
        }
    except Exception:
        return {}


def _fetch_batch(tickers: list[str]) -> dict[str, dict]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_yahoo_quote, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                results[ticker] = fut.result()
            except Exception:
                results[ticker] = {}
    return results


def get_fx_rates() -> dict[str, float]:
    global _fx_cache_time
    now = time.time()
    if now - _fx_cache_time < FX_TTL and _fx_cache:
        return dict(_fx_cache)

    for pair, key, invert in [("EURUSD=X", "USD", True), ("GBPEUR=X", "GBP", False), ("CHFEUR=X", "CHF", False), ("DKKEUR=X", "DKK", False)]:
        q = _yahoo_quote(pair)
        price = q.get("price")
        if price:
            _fx_cache[key] = (1 / price) if invert else price

    _fx_cache.setdefault("USD", 0.86)
    _fx_cache.setdefault("GBP", 1.15)
    _fx_cache.setdefault("CHF", 1.05)
    _fx_cache.setdefault("DKK", 0.134)
    _fx_cache["EUR"] = 1.0
    _fx_cache_time = now
    return dict(_fx_cache)


def currency_to_eur_rate(currency: str) -> float:
    """Return the multiplier to convert 1 unit of currency → EUR.
    Handles Yahoo's GBp (pence) quirk for LSE-listed securities."""
    if not currency or currency == "EUR":
        return 1.0
    if currency == "GBp":
        return get_fx_rates().get("GBP", 1.15) / 100
    return get_fx_rates().get(currency, 1.0)


def to_eur(amount: float, currency: str) -> float:
    return amount * currency_to_eur_rate(currency)


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    now = time.time()
    stale = [t for t in tickers if now - _cache_time.get(t, 0) > CACHE_TTL]

    if stale:
        fetched = _fetch_batch(stale)
        for ticker in stale:
            result = fetched.get(ticker) or {}
            _cache[ticker] = result if result.get("price") else {
                "price": None, "prev_close": None,
                "day_high": None, "day_low": None,
                "currency": "?", "market_cap": None,
                "high_52w": None, "low_52w": None, "week_pct": None,
            }
            _cache_time[ticker] = now

    return {t: _cache.get(t, {}) for t in tickers}


def day_change_pct(q: dict) -> Optional[float]:
    p, pc = q.get("price"), q.get("prev_close")
    if p and pc and pc != 0:
        return round((p - pc) / pc * 100, 2)
    return None


def is_market_open_us() -> bool:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return 14 <= now.hour < 21 or (now.hour == 14 and now.minute >= 30)


def is_market_open_eu() -> bool:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return 7 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)
