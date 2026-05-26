"""
AI briefing and per-ticker drilldown.
Uses Gemini (free) if GOOGLE_API_KEY is set, falls back to OpenAI.
"""
from __future__ import annotations

import os
from datetime import datetime


_GEMINI_MODELS = ["gemini-2.0-flash-exp", "gemini-2.5-flash", "gemini-1.5-flash-latest", "gemini-2.0-flash"]


def _ask(prompt: str, max_tokens: int = 900) -> str:
    google_key = os.getenv("GOOGLE_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")

    if google_key:
        from google import genai
        client = genai.Client(api_key=google_key)
        for model in _GEMINI_MODELS:
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                return response.text.strip()
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ("quota", "429", "resource_exhausted", "not_found", "404", "not found", "not supported")):
                    continue  # try next model
                return f"Gemini error ({model}): {str(e)[:200]}"
        # all Gemini models exhausted, fall through to OpenAI

    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            err = str(e)
            if "insufficient_quota" in err or "429" in err:
                return "All AI quotas reached. Add billing credits to OpenAI or wait for Gemini daily reset."
            return f"OpenAI error: {err[:200]}"

    return "No AI key configured — add GOOGLE_API_KEY (free) or OPENAI_API_KEY to .env."


def generate_briefing(portfolio_snapshot: list[dict], scanner_snapshot: list[dict]) -> str:
    portfolio_lines = []
    for p in portfolio_snapshot:
        pnl = p.get("pnl_pct")
        portfolio_lines.append(
            f"- {p['ticker']} ({p['bucket']}): {p['shares']:.1f} shares, "
            f"avg cost €{p['avg_cost_eur']:.2f}, "
            + (f"P&L {pnl:+.1f}%" if pnl is not None else "no price data")
        )

    scanner_lines = [
        f"- {s['ticker']}: {s['day_pct']:+.2f}% today, price {s.get('price', '?')}"
        for s in scanner_snapshot if s.get("day_pct") is not None
    ]

    prompt = f"""You are a personal investment advisor for a retail investor based in Dublin, Ireland.
Today is {datetime.now().strftime('%A %d %B %Y')}.

Portfolio:
{chr(10).join(portfolio_lines)}

Watchlist scan:
{chr(10).join(scanner_lines)}

Investment goals:
1. Retirement DCA into diversified ETFs
2. Medium-term stock picks, 6-12 month horizon
3. High-conviction AI/tech bets (NBIS, VRT, APLD) — all 3 tranches fully deployed, targeting €15–20k exit in 6–12 months. Position locked, no further additions.

Give a concise briefing (max 250 words):
1. What's moving and why it matters to this portfolio
2. Standout watchlist moves
3. One actionable thought for the week (stay course / trim / add)
4. Key risk to watch

Be direct and specific. No generic disclaimers. Like a smart friend who knows markets."""

    return _ask(prompt, max_tokens=400)


def _fetch_fundamentals(ticker: str) -> dict:
    """Pull live fundamentals from yfinance for a ticker."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info

        def fmt_billions(v):
            if v is None: return "N/A"
            if abs(v) >= 1e9: return f"${v/1e9:.1f}B"
            if abs(v) >= 1e6: return f"${v/1e6:.0f}M"
            return f"${v:.0f}"

        def fmt_pct(v):
            return f"{v*100:.1f}%" if v is not None else "N/A"

        def fmt_x(v, suffix="x"):
            return f"{v:.1f}{suffix}" if v is not None else "N/A"

        rec_map = {1: "Strong Buy", 2: "Buy", 3: "Hold", 4: "Sell", 5: "Strong Sell"}
        rec_score = info.get("recommendationMean")
        recommendation = rec_map.get(round(rec_score) if rec_score else 0, "N/A")

        return {
            "name": info.get("shortName", ticker),
            "sector": info.get("sector", "N/A"),
            "industry": info.get("industry", "N/A"),
            "market_cap": fmt_billions(info.get("marketCap")),
            "price": info.get("currentPrice") or info.get("previousClose"),
            "currency": info.get("currency", "USD"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "52w_change": fmt_pct(info.get("52WeekChange")),
            "beta": fmt_x(info.get("beta"), ""),
            "trailing_pe": fmt_x(info.get("trailingPE")),
            "forward_pe": fmt_x(info.get("forwardPE")),
            "price_to_book": fmt_x(info.get("priceToBook")),
            "ev_ebitda": fmt_x(info.get("enterpriseToEbitda")),
            "trailing_eps": info.get("trailingEps"),
            "forward_eps": info.get("forwardEps"),
            "revenue": fmt_billions(info.get("totalRevenue")),
            "revenue_growth": fmt_pct(info.get("revenueGrowth")),
            "earnings_growth": fmt_pct(info.get("earningsGrowth")),
            "gross_margin": fmt_pct(info.get("grossMargins")),
            "operating_margin": fmt_pct(info.get("operatingMargins")),
            "profit_margin": fmt_pct(info.get("profitMargins")),
            "free_cashflow": fmt_billions(info.get("freeCashflow")),
            "total_cash": fmt_billions(info.get("totalCash")),
            "total_debt": fmt_billions(info.get("totalDebt")),
            "debt_to_equity": fmt_x(info.get("debtToEquity")),
            "current_ratio": fmt_x(info.get("currentRatio")),
            "roe": fmt_pct(info.get("returnOnEquity")),
            "roa": fmt_pct(info.get("returnOnAssets")),
            "analyst_target": info.get("targetMeanPrice"),
            "analyst_count": info.get("numberOfAnalystOpinions"),
            "recommendation": recommendation,
            "dividend_yield": fmt_pct(info.get("dividendYield")),
        }
    except Exception as e:
        return {"error": str(e)[:100]}


def generate_drilldown(ticker: str, position: dict | None, quote: dict | None) -> str:
    name = position.get("name", ticker) if position else ticker
    bucket = position.get("bucket", "watchlist") if position else "watchlist"

    # Live fundamentals from yfinance
    fundamentals = _fetch_fundamentals(ticker)
    live_name = fundamentals.get("name", name)

    # Format fundamentals block
    if "error" not in fundamentals:
        price = fundamentals.get("price")
        currency = fundamentals.get("currency", "")
        analyst_target = fundamentals.get("analyst_target")
        upside = f"{((analyst_target/price)-1)*100:+.1f}%" if price and analyst_target else "N/A"
        fundamentals_block = f"""
LIVE MARKET DATA (as of {datetime.now().strftime('%d %b %Y')}):
- Sector: {fundamentals['sector']} | Industry: {fundamentals['industry']}
- Market Cap: {fundamentals['market_cap']} | Price: {price} {currency}
- 52-week range: {fundamentals['52w_low']} – {fundamentals['52w_high']} | 52w change: {fundamentals['52w_change']}
- Beta: {fundamentals['beta']}

Valuation multiples:
- Trailing P/E: {fundamentals['trailing_pe']} | Forward P/E: {fundamentals['forward_pe']}
- Price/Book: {fundamentals['price_to_book']} | EV/EBITDA: {fundamentals['ev_ebitda']}
- Trailing EPS: {fundamentals['trailing_eps']} | Forward EPS: {fundamentals['forward_eps']}

Financials:
- Revenue: {fundamentals['revenue']} | Revenue growth YoY: {fundamentals['revenue_growth']}
- Earnings growth: {fundamentals['earnings_growth']}
- Gross margin: {fundamentals['gross_margin']} | Operating margin: {fundamentals['operating_margin']} | Net margin: {fundamentals['profit_margin']}
- Free cash flow: {fundamentals['free_cashflow']} | Cash: {fundamentals['total_cash']} | Debt: {fundamentals['total_debt']}
- Debt/Equity: {fundamentals['debt_to_equity']} | Current ratio: {fundamentals['current_ratio']}
- ROE: {fundamentals['roe']} | ROA: {fundamentals['roa']}

Analyst consensus ({fundamentals['analyst_count']} analysts):
- Recommendation: {fundamentals['recommendation']} | Mean target: {analyst_target} {currency} ({upside} upside)
- Dividend yield: {fundamentals['dividend_yield']}"""
    else:
        fundamentals_block = f"(Live data unavailable: {fundamentals['error']})"

    # Investor position context
    holding_context = ""
    if position and quote:
        shares = position.get("shares", 0)
        avg_cost = position.get("avg_cost_eur", 0)
        price_q = quote.get("price")
        currency_q = quote.get("currency", "")
        day_pct = quote.get("day_pct") or 0
        pnl_pct = position.get("pnl_pct")
        value_eur = position.get("current_value_eur")
        holding_context = f"""
YOUR POSITION:
- {shares:.0f} shares held | Avg cost: €{avg_cost:.2f}/share
- Current price: {price_q:.2f} {currency_q} ({day_pct:+.1f}% today)
- Current value: €{value_eur:,.0f} | Total P&L: {pnl_pct:+.1f}%
- Bucket: {bucket}"""
    elif quote and quote.get("price"):
        price_q = quote.get("price")
        currency_q = quote.get("currency", "")
        day_pct = quote.get("day_pct") or 0
        holding_context = f"\nNOT HELD (watchlist only). Price: {price_q:.2f} {currency_q} ({day_pct:+.1f}% today)"

    prompt = f"""You are analysing {ticker} ({live_name}) for a long-term retail investor in Dublin, Ireland.
{fundamentals_block}
{holding_context}

Using the live data above as your factual foundation, give a Warren Buffett-style analysis. Use these exact markdown headers:

## Business Model
What does the company actually do to make money? Revenue streams, customers, moat.

## Financial Health
Interpret the live numbers above: is revenue growth accelerating or slowing? Are margins expanding? Is the balance sheet strong? Quote the actual figures.

## Competitive Position
Main competitors. Is the moat widening or eroding? What keeps customers locked in?

## Growth Catalysts
The 2-3 biggest drivers of future value over the next 1-3 years.

## Key Risks
Top 3 concrete risks that could impair this thesis. Be specific.

## Valuation
Is it cheap, fair, or expensive based on the live multiples above? How does the forward P/E compare to growth rate? What does the analyst target imply?

## Verdict
One paragraph: buy, hold, or avoid for a 3-5 year horizon — and why? If held, is the current P&L a reason to trim or add?

Plain English. Reference the actual numbers. No disclaimers. Max 650 words."""

    return _ask(prompt, max_tokens=1000)
