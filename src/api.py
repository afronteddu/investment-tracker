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

# Base watchlist — always scanned
WATCHLIST_BASE = [
    # AI infrastructure
    "NVDA", "AMD", "TSM", "ARM", "SMCI",
    # Quantum computing
    "IONQ", "RGTI", "QUBT",
    # AI software / data
    "PLTR", "SOUN", "BBAI",
    # Mag 7 / broad market
    "MSFT", "GOOGL", "META", "AMZN", "TSLA", "AAPL",
    # Indices & ETFs
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
    # ETFs
    "ARKK", "BOTZ", "AIQ",
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
            rate = fx.get(currency, 1.0)
            price_eur = price * rate
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


def _compute_lifetime_stats() -> dict:
    """Compute all-time realized P&L, total deployed, and monthly flows from raw transactions."""
    import openpyxl, glob
    from collections import defaultdict
    from datetime import datetime

    holdings = defaultdict(lambda: {'shares': 0.0, 'cost': 0.0})
    realized_pnl = 0.0
    total_deployed = 0.0
    total_returned = 0.0
    monthly = defaultdict(float)

    seen = set()
    files = sorted(glob.glob("data/transactions/*.xlsx"))
    rows = []
    for filepath in files:
        wb = openpyxl.load_workbook(filepath)
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0 or not row[0]: continue
            key = (row[0], row[2], row[6], row[15])
            if key in seen: continue
            seen.add(key)
            rows.append({'date': row[0], 'product': row[2], 'qty': float(row[6] or 0), 'total_eur': float(row[15] or 0)})

    rows.sort(key=lambda x: datetime.strptime(x['date'], '%d-%m-%Y'))

    for r in rows:
        prod = r['product']
        t = r['total_eur']
        qty = r['qty']
        month = datetime.strptime(r['date'], '%d-%m-%Y').strftime('%Y-%m')
        if t < 0:
            total_deployed += abs(t)
            monthly[month] += abs(t)
            holdings[prod]['shares'] += qty
            holdings[prod]['cost'] += abs(t)
        else:
            total_returned += t
            monthly[month] -= t
            if holdings[prod]['shares'] > 0:
                frac = abs(qty) / holdings[prod]['shares']
                realized_pnl += t - holdings[prod]['cost'] * frac
                holdings[prod]['cost'] *= (1 - frac)
                holdings[prod]['shares'] -= abs(qty)

    return {
        "total_deployed": round(total_deployed, 2),
        "total_returned": round(total_returned, 2),
        "realized_pnl": round(realized_pnl, 2),
        "monthly_flows": [{"month": m, "net": round(v, 2)} for m, v in sorted(monthly.items())],
    }


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
async def history(request: Request, period: str = "since_buy"):
    if (r := _auth_required(request)):
        return r
    import yfinance as yf
    from datetime import date, datetime, timedelta

    positions = state.get("positions", {})
    tickers = list(positions.keys())
    if not tickers:
        return {"series": [], "portfolio": []}

    fx = get_fx_rates()

    # Determine fetch period per ticker based on first buy date
    # For portfolio total we need the earliest buy across all holdings
    earliest = None
    for pos in positions.values():
        if pos.first_buy_date:
            d = datetime.strptime(pos.first_buy_date, "%Y-%m-%d").date()
            if earliest is None or d < earliest:
                earliest = d

    # Fetch weekly history since earliest buy (max 3 years to keep it fast)
    if earliest is None:
        earliest = date.today() - timedelta(days=365)
    earliest = max(earliest, date.today() - timedelta(days=365 * 3))

    result = []
    # portfolio_by_date: date → total_value_eur
    portfolio_by_date: dict[str, float] = {}

    batch = yf.Tickers(" ".join(tickers))

    for ticker in tickers:
        pos = positions[ticker]
        try:
            t_obj = batch.tickers[ticker]
            hist = t_obj.history(start=earliest.isoformat(), interval="1wk")
            if hist.empty:
                continue
            currency = getattr(t_obj.fast_info, "currency", None) or "EUR"

            # Only show points from actual buy date onwards
            buy_date = datetime.strptime(pos.first_buy_date, "%Y-%m-%d").date() if pos.first_buy_date else earliest

            points = []
            for dt, row in hist.iterrows():
                row_date = dt.date() if hasattr(dt, "date") else dt
                if row_date < buy_date:
                    continue
                close = row["Close"]
                close_eur = close * fx.get(currency, 1.0)
                pct = (close_eur - pos.avg_cost_eur) / pos.avg_cost_eur * 100 if pos.avg_cost_eur else 0
                date_str = dt.strftime("%Y-%m-%d")
                points.append({"date": date_str, "pct": round(pct, 2), "price": round(close, 2), "value_eur": round(pos.shares * close_eur, 2)})
                # Accumulate portfolio total
                portfolio_by_date[date_str] = portfolio_by_date.get(date_str, 0) + pos.shares * close_eur

            if points:
                result.append({
                    "ticker": ticker,
                    "name": TICKER_NAMES.get(ticker, ticker),
                    "bucket": pos.bucket,
                    "avg_cost_eur": round(pos.avg_cost_eur, 2),
                    "first_buy_date": pos.first_buy_date,
                    "points": points,
                })
        except Exception:
            continue

    result.sort(key=lambda x: x["first_buy_date"] or "")

    # Portfolio total line — sorted dates
    portfolio_points = [
        {"date": d, "value_eur": round(v, 2)}
        for d, v in sorted(portfolio_by_date.items())
    ]

    return {"series": result, "portfolio": portfolio_points}


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
