"""
FastAPI backend. Serves the web dashboard and exposes:
  GET  /api/portfolio     → positions + live prices
  GET  /api/scan          → watchlist scanner
  GET  /api/briefing      → latest AI briefing
  POST /api/reload        → re-parse transaction files
  WS   /ws               → push updates every 60s during market hours
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env from project root before anything else
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.positions import compute_positions, compute_lifetime_stats, TICKER_NAMES
from src.quotes import fetch_quotes, day_change_pct, is_market_open_us, is_market_open_eu, to_eur, get_fx_rates, currency_to_eur_rate
from src.scheduler import Scheduler

# Base watchlist — always scanned
WATCHLIST_BASE = [
    # AI infrastructure
    "NVDA", "AMD", "TSM", "ARM", "SMCI",
    # Quantum computing
    "IONQ", "RGTI", "QUBT",
    # AI software / data
    "PLTR", "SOUN", "BBAI",
    # Mag 7 / broad market
    "MSFT", "GOOGL", "META", "AMZN", "TSLA", "AAPL", "ASML.AS",
    # House savings CORE
    "UCG.MI", "NOVN.SW", "ENEL.MI", "AXA.PA", "IBE.MC",
    # House savings SATELLITE
    "TTE.PA", "GSK.L",
    # House savings GROWTH (sell H1 2028)
    "PYPL", "ABNB", "ROG.SW",
    # Defence / 2027 HC watch
    "LDO.MI", "RHM.DE",
]

DEPLOYMENTS = []

# Shared state — mutated by scheduler, read by API handlers
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — sync-only work, zero network calls
    state["positions"] = compute_positions()
    state["watchlist"] = list(WATCHLIST_BASE)
    state["deployments"] = DEPLOYMENTS
    state["latest_briefing"] = None
    state["hot_picks"] = []
    state["signals_cache"] = {}
    state["quotes_cache"] = {}
    state["fx_cache"] = {"USD": 0.92, "GBP": 1.17, "EUR": 1.0}  # rough fallback until scheduler fills it
    state["lifetime_cache"] = {"total_deployed": 0, "total_returned": 0, "realized_pnl": 0, "monthly_flows": []}
    state["scanner_cache"] = []
    state["ws_payload_cache"] = None
    state["history_cache"] = None
    # All network fetches happen in scheduler background tasks
    scheduler = Scheduler(state)
    task = asyncio.create_task(scheduler.run())
    yield
    task.cancel()


app = FastAPI(title="Investment Tracker", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


def _check_auth(request: Request) -> bool:
    username = os.getenv("DASHBOARD_USER", "")
    password = os.getenv("DASHBOARD_PASS", "")
    if not username or not password:
        return True  # auth disabled if not configured
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        u, _, p = decoded.partition(":")
        return secrets.compare_digest(u, username) and secrets.compare_digest(p, password)
    except Exception:
        return False


def _check_public_auth(request: Request) -> bool:
    username = os.getenv("PUBLIC_USER", "")
    password = os.getenv("PUBLIC_PASS", "")
    if not username or not password:
        return False  # public dashboard disabled if not configured
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        u, _, p = decoded.partition(":")
        return secrets.compare_digest(u, username) and secrets.compare_digest(p, password)
    except Exception:
        return False


def _auth_required(request: Request):
    if not _check_auth(request) and not _check_public_auth(request):
        return Response(
            "Unauthorized", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Portfolio"'},
        )
    return None


def _public_auth_required(request: Request):
    if not _check_public_auth(request) and not _check_auth(request):
        return Response(
            "Unauthorized", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="AlphaStack"'},
        )
    return None


def _make_ws_token() -> str:
    """Generate a short-lived HMAC token for WebSocket auth (valid 24h)."""
    secret = os.getenv("DASHBOARD_PASS", "noauth")
    ts = str(int(time.time() // 86400))  # changes daily
    sig = hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()[:16]
    return sig


def _valid_ws_token(token: str) -> bool:
    if not os.getenv("DASHBOARD_USER"):
        return True
    return secrets.compare_digest(token, _make_ws_token())


# Hot-picks universe — scanned daily to surface the biggest movers/momentum names
_HOT_PICKS_UNIVERSE = [
    # AI chips & infra
    "NVDA", "AMD", "TSM", "ARM", "SMCI", "AVGO", "MRVL", "INTC", "QCOM",
    # AI software & data
    "PLTR", "AI", "SOUN", "BBAI", "UPST", "PATH", "SNOW",
    # Quantum
    "IONQ", "RGTI", "QUBT", "QBTS",
    # High-growth tech
    "META", "GOOGL", "MSFT", "AMZN", "TSLA", "AAPL",
    # Energy / data centre power
    "VRT", "VST", "CEG", "NRG",
    # Biotech / speculative AI-adjacent
    "RXRX", "TMDX",
]


def _refresh_hot_picks():
    """Pull yesterday's top movers from the universe and merge into watchlist."""
    try:
        quotes = fetch_quotes(_HOT_PICKS_UNIVERSE)
        # Score by absolute day move
        scored = []
        for ticker, q in quotes.items():
            pct = day_change_pct(q)
            if pct is not None:
                scored.append((abs(pct), ticker))
        scored.sort(reverse=True)
        # Top 8 movers always in watchlist
        hot = [t for _, t in scored[:8]]
        merged = list(dict.fromkeys(WATCHLIST_BASE + hot))  # deduplicate, base first
        state["watchlist"] = merged
        state["hot_picks"] = hot
    except Exception:
        state["watchlist"] = list(WATCHLIST_BASE)

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass


def _build_portfolio_data() -> list[dict]:
    """Read entirely from pre-cached state — zero blocking calls."""
    positions = state.get("positions", {})
    if not positions:
        return []
    quotes = state.get("quotes_cache", {})
    signals = state.get("signals_cache", {})
    fx = state.get("fx_cache", {})

    rows = []
    for ticker, pos in positions.items():
        q = quotes.get(ticker, {})
        price = q.get("price")
        day_pct = day_change_pct(q)
        sig = signals.get(ticker, {})

        pnl_eur = None
        pnl_pct = None
        current_value = None
        currency = q.get("currency", "EUR")
        if price is not None:
            price_eur = price * currency_to_eur_rate(currency)
            current_value = round(pos.shares * price_eur, 2)
            pnl_eur = round(current_value - pos.total_cost_eur, 2)
            if pos.total_cost_eur > 0:
                pnl_pct = round(pnl_eur / pos.total_cost_eur * 100, 2)

        rows.append({
            **pos.to_dict(),
            "name": TICKER_NAMES.get(ticker, pos.name),
            "price": price,
            "currency": currency,
            "day_pct": day_pct,
            "day_high": q.get("day_high"),
            "day_low": q.get("day_low"),
            "high_52w": q.get("high_52w"),
            "low_52w": q.get("low_52w"),
            "current_value_eur": current_value,
            "pnl_eur": pnl_eur,
            "pnl_pct": pnl_pct,
            "rsi": sig.get("rsi"),
            "rsi_signal": sig.get("rsi_signal"),
            "earnings_date": sig.get("earnings_date"),
        })

    rows.sort(key=lambda x: (x["bucket"], x["ticker"]))
    return rows


def _build_scanner_data() -> list[dict]:
    """Read entirely from pre-cached state — zero blocking calls."""
    watchlist = state.get("watchlist", [])
    hot_picks = set(state.get("hot_picks", []))
    quotes = state.get("quotes_cache", {})
    signals = state.get("signals_cache", {})

    rows = []
    for ticker in watchlist:
        q = quotes.get(ticker, {})
        pct = day_change_pct(q)
        sig = signals.get(ticker, {})
        rows.append({
            "ticker": ticker,
            "name": TICKER_NAMES.get(ticker, ""),
            "price": q.get("price"),
            "day_pct": pct,
            "day_high": q.get("day_high"),
            "day_low": q.get("day_low"),
            "currency": q.get("currency", "?"),
            "signal": _signal(pct),
            "hot": ticker in hot_picks,
            "rsi": sig.get("rsi"),
            "rsi_signal": sig.get("rsi_signal"),
            "earnings_date": sig.get("earnings_date"),
            "news": sig.get("news", []),
            "week_pct": q.get("week_pct"),
            "high_52w": q.get("high_52w"),
            "low_52w":  q.get("low_52w"),
        })

    rows.sort(key=lambda x: x["day_pct"] if x["day_pct"] is not None else 0, reverse=True)
    return rows


def _signal(pct) -> str:
    if pct is None:
        return "no data"
    if pct >= 8:
        return "EXTRAORDINARY GAIN"
    if pct <= -8:
        return "EXTRAORDINARY DROP"
    if pct >= 3:
        return "strong up"
    if pct <= -3:
        return "strong down"
    return "neutral"


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if (r := _auth_required(request)):
        return r
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/public", response_class=HTMLResponse)
async def public_dashboard(request: Request):
    if (r := _public_auth_required(request)):
        return r
    return templates.TemplateResponse("public.html", {"request": request, "ws_token": _make_ws_token()})


@app.get("/property", response_class=HTMLResponse)
async def property_tracker(request: Request):
    if (r := _auth_required(request)):
        return r
    return templates.TemplateResponse("property.html", {"request": request})


@app.get("/alyssa", response_class=HTMLResponse)
async def alyssa_isa(request: Request):
    if (r := _auth_required(request)):
        return r
    return templates.TemplateResponse("alyssa.html", {"request": request})


@app.get("/api/fwrg-history")
async def fwrg_history(request: Request):
    """Proxy FWRG.AS price history from Yahoo — avoids browser CORS block."""
    if (r := _auth_required(request)):
        return r
    import time as _time
    from src.quotes import _session, _crumb, _ensure_session, _HEADERS
    loop = asyncio.get_event_loop()

    def _fetch():
        _ensure_session()
        from_ts = 1730000000  # Oct 2024 — well before first FWRG purchase
        to_ts = int(_time.time()) + 86400
        params = {"interval": "1d", "period1": from_ts, "period2": to_ts}
        if _crumb:
            params["crumb"] = _crumb
        try:
            r = _session.get(
                "https://query2.finance.yahoo.com/v8/finance/chart/FWRG.L",
                headers=_HEADERS, params=params, timeout=15
            )
            if r.status_code in (401, 403):
                _ensure_session()
                params["crumb"] = _crumb
                r = _session.get(
                    "https://query2.finance.yahoo.com/v8/finance/chart/FWRG.L",
                    headers=_HEADERS, params=params, timeout=15
                )
            if r.status_code != 200:
                return {"error": f"Yahoo returned {r.status_code}"}
            data = r.json()
            result = data["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            points = [
                {"ts": ts, "close": round(c, 4)}
                for ts, c in zip(timestamps, closes) if c is not None
            ]
            return {"points": points, "currency": result["meta"].get("currency", "GBp")}
        except Exception as e:
            return {"error": str(e)[:100]}

    result = await loop.run_in_executor(None, _fetch)
    return result




def _portfolio_payload() -> dict:
    rows = _build_portfolio_data()
    total_cost = sum(r["total_cost_eur"] for r in rows)
    total_value = sum(r["current_value_eur"] for r in rows if r["current_value_eur"])
    unrealized_pnl = total_value - total_cost
    lifetime = state.get("lifetime_cache", {"total_deployed": 0, "total_returned": 0, "realized_pnl": 0, "monthly_flows": []})
    total_pnl = lifetime["realized_pnl"] + unrealized_pnl
    total_deployed = lifetime["total_deployed"]
    return {
        "positions": rows,
        "summary": {
            "total_cost_eur": round(total_cost, 2),
            "total_value_eur": round(total_value, 2),
            "total_pnl_eur": round(total_value - total_cost, 2),
            "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        },
        "lifetime": {
            "total_deployed": total_deployed,
            "total_returned": lifetime["total_returned"],
            "realized_pnl": lifetime["realized_pnl"],
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / total_deployed * 100, 2) if total_deployed else 0,
            "monthly_flows": lifetime["monthly_flows"],
        },
        "market_status": {
            "us_open": is_market_open_us(),
            "eu_open": is_market_open_eu(),
        },
        "fx_rates": state.get("fx_cache", {}),
        "deployments": state.get("deployments", []),
    }


@app.get("/api/portfolio")
async def portfolio(request: Request):
    if (r := _auth_required(request)):
        return r
    return _portfolio_payload()


@app.get("/api/history")
async def history(request: Request):
    if (r := _auth_required(request)):
        return r
    cached = state.get("history_cache")
    if cached:
        return cached
    return {"series": [], "portfolio": [], "loading": True}


@app.get("/api/scan")
async def scan(request: Request):
    if (r := _auth_required(request)):
        return r
    return {"scanner": state.get("scanner_cache", [])}


@app.get("/api/briefing")
async def briefing(request: Request):
    if (r := _auth_required(request)):
        return r
    return {"briefing": state.get("latest_briefing") or "No briefing generated yet. First one runs at market close."}


@app.post("/api/reload")
async def reload(request: Request):
    if (r := _auth_required(request)):
        return r
    state["positions"] = compute_positions()
    return {"status": "ok", "positions_loaded": len(state["positions"])}


@app.get("/api/ws-token")
async def ws_token(request: Request):
    if (r := _auth_required(request)):
        return r
    return {"token": _make_ws_token()}


@app.get("/api/debug")
async def debug(request: Request):
    if (r := _auth_required(request)):
        return r
    quotes = state.get("quotes_cache", {})
    sample = {k: v for k, v in list(quotes.items())[:3]}
    return {
        "positions_count": len(state.get("positions", {})),
        "quotes_count": len(quotes),
        "quotes_sample": sample,
        "fx_cache": state.get("fx_cache"),
        "lifetime_cache": state.get("lifetime_cache"),
        "ws_payload_ready": state.get("ws_payload_cache") is not None,
    }


@app.post("/api/briefing/generate")
async def generate_briefing_now(request: Request):
    if (r := _auth_required(request)):
        return r
    from src.briefing import generate_briefing
    portfolio_rows = _build_portfolio_data()
    scanner_rows = _build_scanner_data()
    briefing_text = generate_briefing(portfolio_rows, scanner_rows)
    state["latest_briefing"] = briefing_text
    return {"briefing": briefing_text}


@app.get("/api/quotes")
async def quotes_batch(tickers: str, request: Request):
    """Return live prices for a comma-separated list of tickers. Used by suggestion panel."""
    if (r := _auth_required(request)):
        return r
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    # Check cache first; fetch any missing
    cached = state.get("quotes_cache", {})
    missing = [t for t in ticker_list if t not in cached]
    fresh: dict = {}
    if missing:
        try:
            loop = asyncio.get_event_loop()
            fresh = await loop.run_in_executor(None, fetch_quotes, missing)
        except Exception:
            pass
    result = {}
    for t in ticker_list:
        q = fresh.get(t) or cached.get(t) or {}
        if q.get("price") is not None:
            result[t] = {"price": q["price"], "currency": q.get("currency", ""), "day_pct": day_change_pct(q)}
    return result


@app.get("/api/drilldown/{ticker}")
async def drilldown(ticker: str, request: Request):
    if (r := _auth_required(request)):
        return r
    from src.briefing import generate_drilldown
    ticker = ticker.upper()

    # Build position context if we hold it
    portfolio_rows = _build_portfolio_data()
    position = next((r for r in portfolio_rows if r["ticker"] == ticker), None)

    # Build quote context
    quotes = fetch_quotes([ticker])
    q = quotes.get(ticker, {})
    quote_ctx = None
    if q.get("price"):
        from src.quotes import day_change_pct
        quote_ctx = {
            "price": q.get("price"),
            "currency": q.get("currency", ""),
            "day_pct": day_change_pct(q),
            "day_high": q.get("day_high"),
            "day_low": q.get("day_low"),
        }
        if position:
            position["day_pct"] = quote_ctx["day_pct"]

    analysis = generate_drilldown(ticker, position, quote_ctx)
    return {"ticker": ticker, "analysis": analysis}


# WebSocket — pushes portfolio + scanner data to connected clients
connected: list[WebSocket] = []


@app.get("/api/ai/health")
async def ai_health(request: Request):
    if (r := _auth_required(request)):
        return r
    from src.briefing import ai_health_check
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, ai_health_check)
    return result


@app.post("/api/suggest/challenge")
async def suggest_challenge(request: Request):
    if (r := _auth_required(request)):
        return r
    body = await request.json()
    ticker = body.get("ticker", "").upper()
    user_query = body.get("query", "").strip()
    suggestion_meta = body.get("suggestion", {})

    if not ticker or not user_query:
        return {"error": "ticker and query required"}

    portfolio_rows = _build_portfolio_data()

    # Live quote for context (non-blocking, from cache first)
    quotes = state.get("quotes_cache", {})
    live_quote = None
    if ticker in quotes:
        q = quotes[ticker]
        if q.get("price"):
            live_quote = {"price": q["price"], "currency": q.get("currency", ""), "day_pct": day_change_pct(q)}
    else:
        try:
            loop = asyncio.get_event_loop()
            fresh = await loop.run_in_executor(None, fetch_quotes, [ticker])
            q = fresh.get(ticker, {})
            if q.get("price"):
                live_quote = {"price": q["price"], "currency": q.get("currency", ""), "day_pct": day_change_pct(q)}
        except Exception:
            pass

    from src.briefing import generate_challenge
    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(
        None, generate_challenge, ticker, suggestion_meta, user_query, portfolio_rows, live_quote
    )
    return {"ticker": ticker, "query": user_query, "analysis": analysis}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if not _valid_ws_token(token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    connected.append(websocket)
    try:
        while True:
            import json
            # Everything reads from pre-built cache — instant, no yfinance calls here
            payload = state.get("ws_payload_cache")
            if payload:
                await websocket.send_text(payload)
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        if websocket in connected:
            connected.remove(websocket)
    except Exception:
        if websocket in connected:
            connected.remove(websocket)
