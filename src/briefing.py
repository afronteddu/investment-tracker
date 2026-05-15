"""
AI briefing and per-ticker drilldown.
Uses Gemini (free) if GOOGLE_API_KEY is set, falls back to OpenAI.
"""
from __future__ import annotations

import os
from datetime import datetime


def _ask(prompt: str, max_tokens: int = 900) -> str:
    google_key = os.getenv("GOOGLE_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")

    if google_key:
        try:
            from google import genai
            client = genai.Client(api_key=google_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            err = str(e)
            if "quota" in err.lower() or "429" in err:
                return "Google API daily quota reached. Try again tomorrow or add OPENAI_API_KEY."
            return f"Gemini error: {err[:200]}"

    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            err = str(e)
            if "insufficient_quota" in err or "429" in err:
                return "OpenAI quota exceeded — add billing credits at platform.openai.com/settings/billing."
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
3. High-conviction AI/tech bets (NBIS, VRT, APLD) targeting 6-7x

Two more tranches to deploy: ~30% May 19, ~33% May 26.

Give a concise briefing (max 250 words):
1. What's moving and why it matters to this portfolio
2. Standout watchlist moves
3. One actionable thought for the week (stay course / trim / add)
4. Key risk to watch

Be direct and specific. No generic disclaimers. Like a smart friend who knows markets."""

    return _ask(prompt, max_tokens=400)


def generate_drilldown(ticker: str, position: dict | None, quote: dict | None) -> str:
    name = position.get("name", ticker) if position else ticker
    bucket = position.get("bucket", "watchlist") if position else "watchlist"

    holding_context = ""
    if position and quote:
        shares = position.get("shares", 0)
        avg_cost = position.get("avg_cost_eur", 0)
        price = quote.get("price")
        currency = quote.get("currency", "")
        day_pct = quote.get("day_pct") or 0
        pnl_pct = position.get("pnl_pct")
        value_eur = position.get("current_value_eur")
        holding_context = f"""
Investor's position:
- Shares held: {shares:.0f}
- Avg cost: €{avg_cost:.2f}/share
- Current price: {price:.2f} {currency}  ({day_pct:+.1f}% today)
- Current value: €{value_eur:,.0f}
- P&L: {pnl_pct:+.1f}%
- Bucket: {bucket}
"""
    elif quote and quote.get("price"):
        price = quote.get("price")
        currency = quote.get("currency", "")
        day_pct = quote.get("day_pct") or 0
        holding_context = f"\nWatchlist only (not held). Price: {price:.2f} {currency}, day: {day_pct:+.1f}%\n"

    prompt = f"""Analyse {ticker} ({name}) for a long-term retail investor in Dublin, Ireland.
{holding_context}
Give a Warren Buffett-style deep analysis. Use these exact markdown headers:

## Business Model
What does the company actually do to make money? Revenue streams, customers, competitive moat.

## Financial Health
Key metrics: revenue growth trend, profitability (margins, EPS), debt load, cash position, FCF. Use real numbers.

## Competitive Position
Main competitors. What is the moat (brand, network effects, switching costs, cost advantage, patents)? Widening or eroding?

## Growth Catalysts
The 2-3 biggest drivers of future value. Near-term (12 months) and long-term.

## Key Risks
Top 3 specific risks that could impair the thesis. Be concrete.

## Valuation
Cheap, fair, or expensive? Bull/bear case. What multiple are investors paying and is it justified?

## Verdict
One paragraph: buy, hold, or avoid — and why? Frame for a 3-5 year horizon.

Plain English. Specific numbers. No disclaimers. Max 600 words total."""

    return _ask(prompt, max_tokens=900)
