"""
Live quotes via yfinance. Cached per-process to avoid hammering Yahoo.
Uses history() as primary source — more resilient to datacenter IP blocks
than fast_info which frequently returns None in cloud environments.
"""
from __future__ import annotations

import time
from typing import Optional

import yfinance as yf

_cache: dict[str, dict] = {}
_cache_time: dict[str, float] = {}
_fx_cache: dict[str, float] = {}
_fx_cache_time: float = 0
CACHE_TTL = 60  # seconds
FX_TTL = 300    # refresh FX rates every 5 min


def _fetch_via_history(ticker: str) -> dict:
    """Fetch latest price + prev_close using 5d history. Works in cloud environments."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", interval="1d", auto_adjust=True)
        if hist.empty:
            return {}
        closes = hist["Close"].dropna()
        if len(closes) < 1:
            return {}
        price = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else price
        # day high/low from most recent row
        day_high = float(hist["High"].iloc[-1]) if "High" in hist.columns else None
        day_low = float(hist["Low"].iloc[-1]) if "Low" in hist.columns else None
        # currency from fast_info (lightweight, usually works)
        try:
            currency = t.fast_info.currency or "?"
        except Exception:
            currency = "?"
        return {
            "price": price,
            "prev_close": prev_close,
            "day_high": day_high,
            "day_low": day_low,
            "currency": currency,
            "market_cap": None,
        }
    except Exception:
        return {}


def get_fx_rates() -> dict[str, float]:
    """Return {USD: rate_to_eur, GBP: rate_to_eur}."""
    global _fx_cache_time
    now = time.time()
    if now - _fx_cache_time < FX_TTL and _fx_cache:
        return dict(_fx_cache)

    for pair, key, invert in [("EURUSD=X", "USD", True), ("GBPEUR=X", "GBP", False)]:
        try:
            t = yf.Ticker(pair)
            hist = t.history(period="2d", interval="1d", auto_adjust=True)
            if not hist.empty:
                val = float(hist["Close"].dropna().iloc[-1])
                _fx_cache[key] = (1 / val) if invert else val
        except Exception:
            pass

    _fx_cache.setdefault("USD", 0.86)
    _fx_cache.setdefault("GBP", 1.15)
    _fx_cache["EUR"] = 1.0
    _fx_cache_time = now
    return dict(_fx_cache)


def to_eur(amount: float, currency: str) -> float:
    if currency == "EUR" or not currency:
        return amount
    rates = get_fx_rates()
    return amount * rates.get(currency, 1.0)


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    now = time.time()
    stale = [t for t in tickers if now - _cache_time.get(t, 0) > CACHE_TTL]

    for ticker in stale:
        result = _fetch_via_history(ticker)
        if result:
            _cache[ticker] = result
        else:
            _cache[ticker] = {
                "price": None, "prev_close": None,
                "day_high": None, "day_low": None,
                "currency": "?", "market_cap": None,
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
