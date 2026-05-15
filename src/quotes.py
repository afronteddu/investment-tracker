"""
Live quotes via yfinance. Cached per-process to avoid hammering Yahoo.
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


def get_fx_rates() -> dict[str, float]:
    """Return {USD: rate_to_eur, GBP: rate_to_eur}."""
    global _fx_cache_time
    now = time.time()
    if now - _fx_cache_time < FX_TTL and _fx_cache:
        return _fx_cache

    try:
        batch = yf.Tickers("EURUSD=X GBPEUR=X")
        eurusd = batch.tickers["EURUSD=X"].fast_info.last_price  # 1 EUR = X USD
        gbpeur = batch.tickers["GBPEUR=X"].fast_info.last_price  # 1 GBP = X EUR
        _fx_cache["USD"] = 1 / eurusd   # 1 USD → EUR
        _fx_cache["GBP"] = gbpeur        # 1 GBP → EUR
        _fx_cache["EUR"] = 1.0
        _fx_cache_time = now
    except Exception:
        # fallback: don't blow up, use last known or rough approximation
        _fx_cache.setdefault("USD", 0.86)
        _fx_cache.setdefault("GBP", 1.15)
        _fx_cache.setdefault("EUR", 1.0)

    return _fx_cache


def to_eur(amount: float, currency: str) -> float:
    """Convert an amount in given currency to EUR."""
    if currency == "EUR" or not currency:
        return amount
    rates = get_fx_rates()
    return amount * rates.get(currency, 1.0)


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    now = time.time()
    stale = [t for t in tickers if now - _cache_time.get(t, 0) > CACHE_TTL]

    if stale:
        batch = yf.Tickers(" ".join(stale))
        for ticker in stale:
            try:
                info = batch.tickers[ticker].fast_info
                _cache[ticker] = {
                    "price": getattr(info, "last_price", None),
                    "prev_close": getattr(info, "previous_close", None),
                    "day_high": getattr(info, "day_high", None),
                    "day_low": getattr(info, "day_low", None),
                    "currency": getattr(info, "currency", "?"),
                    "market_cap": getattr(info, "market_cap", None),
                }
                _cache_time[ticker] = now
            except Exception:
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
    """Rough check — US market hours in UTC (14:30–21:00)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return 14 <= now.hour < 21 or (now.hour == 14 and now.minute >= 30)


def is_market_open_eu() -> bool:
    """EU market hours in UTC (07:00–15:30)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return 7 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)
