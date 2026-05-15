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

from src.positions import compute_positions, TICKER_NAMES
from src.quotes import fetch_quotes, day_change_pct, is_market_open_us, is_market_open_eu, to_eur, get_fx_rates
from src.scheduler import Scheduler

WATCHLIST = [
    "SMCI", "NVDA", "AMD", "PLTR", "IONQ", "RGTI", "QUBT", "TSM", "ARM",
    "SPY", "QQQ", "VWRL.AS", "ASML.AS",
]

DEPLOYMENTS = [
    {"date": "2026-05-19", "note": "Deploy 2nd tranche ~30% of 6-12mo bet"},
    {"date": "2026-05-26", "note": "Deploy 3rd tranche ~33% of 6-12mo bet"},
]

# Shared state — mutated by scheduler, read by API handlers
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    state["positions"] = compute_positions()
    state["watchlist"] = WATCHLIST
    state["deployments"] = DEPLOYMENTS
    state["latest_briefing"] = None
    scheduler = Scheduler(state)
    task = asyncio.create_task(scheduler.run())
    yield
    # Shutdown
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


def _auth_required(request: Request):
    if not _check_auth(request):
        return Response(
            "Unauthorized", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Portfolio"'},
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

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass


def _build_portfolio_data() -> list[dict]:
    positions = state.get("positions", {})
    if not positions:
        return []

    tickers = list(positions.keys())
    quotes = fetch_quotes(tickers)

    rows = []
    for ticker, pos in positions.items():
        q = quotes.get(ticker, {})
        price = q.get("price")
        day_pct = day_change_pct(q)

        pnl_eur = None
        pnl_pct = None
        current_value = None
        currency = q.get("currency", "EUR")
        if price is not None:
            # Convert current price to EUR before comparing against EUR cost basis
            price_eur = to_eur(price, currency)
            current_value = round(pos.shares * price_eur, 2)
            pnl_eur = round(current_value - pos.total_cost_eur, 2)
            if pos.total_cost_eur > 0:
                pnl_pct = round(pnl_eur / pos.total_cost_eur * 100, 2)

        rows.append({
            **pos.to_dict(),
            "name": TICKER_NAMES.get(ticker, pos.name),
            "price": price,
            "currency": q.get("currency", "?"),
            "day_pct": day_pct,
            "day_high": q.get("day_high"),
            "day_low": q.get("day_low"),
            "current_value_eur": current_value,
            "pnl_eur": pnl_eur,
            "pnl_pct": pnl_pct,
        })

    rows.sort(key=lambda x: (x["bucket"], x["ticker"]))
    return rows


def _build_scanner_data() -> list[dict]:
    watchlist = state.get("watchlist", [])
    quotes = fetch_quotes(watchlist)

    rows = []
    for ticker in watchlist:
        q = quotes.get(ticker, {})
        pct = day_change_pct(q)
        rows.append({
            "ticker": ticker,
            "price": q.get("price"),
            "day_pct": pct,
            "day_high": q.get("day_high"),
            "day_low": q.get("day_low"),
            "currency": q.get("currency", "?"),
            "signal": _signal(pct),
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


@app.get("/api/portfolio")
async def portfolio(request: Request):
    if (r := _auth_required(request)):
        return r
    rows = _build_portfolio_data()
    total_cost = sum(r["total_cost_eur"] for r in rows)
    total_value = sum(r["current_value_eur"] for r in rows if r["current_value_eur"])
    return {
        "positions": rows,
        "summary": {
            "total_cost_eur": round(total_cost, 2),
            "total_value_eur": round(total_value, 2),
            "total_pnl_eur": round(total_value - total_cost, 2),
            "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        },
        "market_status": {
            "us_open": is_market_open_us(),
            "eu_open": is_market_open_eu(),
        },
        "fx_rates": get_fx_rates(),
        "deployments": state.get("deployments", []),
    }


@app.get("/api/scan")
async def scan(request: Request):
    if (r := _auth_required(request)):
        return r
    return {"scanner": _build_scanner_data()}


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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Browsers can't send Authorization headers on WS upgrade, so we use a short-lived HMAC token
    token = websocket.query_params.get("token", "")
    if not _valid_ws_token(token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    connected.append(websocket)
    try:
        while True:
            import json
            payload = {
                "portfolio": _build_portfolio_data(),
                "scanner": _build_scanner_data(),
                "market_status": {
                    "us_open": is_market_open_us(),
                    "eu_open": is_market_open_eu(),
                },
                "fx_rates": get_fx_rates(),
                "deployments": state.get("deployments", []),
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        connected.remove(websocket)
    except Exception:
        if websocket in connected:
            connected.remove(websocket)
