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
        day = p.get("day_pct")
        rsi = p.get("rsi")
        rsi_sig = p.get("rsi_signal", "")
        earn = p.get("earnings_date")
        high_52w = p.get("high_52w")
        low_52w = p.get("low_52w")
        price = p.get("price")
        currency = p.get("currency", "")
        extras = []
        if day is not None:
            extras.append(f"{day:+.1f}% today")
        if rsi is not None:
            extras.append(f"RSI {rsi} ({rsi_sig})" if rsi_sig and rsi_sig != "neutral" else f"RSI {rsi}")
        if earn:
            extras.append(f"earnings {earn}")
        if price and high_52w and low_52w:
            pct_off_high = (price - high_52w) / high_52w * 100
            pct_off_low = (price - low_52w) / low_52w * 100
            extras.append(f"52w {low_52w:.2f}–{high_52w:.2f} {currency} ({pct_off_high:+.0f}% vs high / {pct_off_low:+.0f}% vs low)")
        year_ret = p.get("year_return")
        if year_ret is not None:
            extras.append(f"52w return {year_ret:+.1f}%")
        extra_str = "  [" + ", ".join(extras) + "]" if extras else ""
        portfolio_lines.append(
            f"- {p['ticker']} ({p['bucket']}): {p['shares']:.1f} shares, "
            f"avg cost €{p['avg_cost_eur']:.2f}, "
            + (f"P&L {pnl:+.1f}%" if pnl is not None else "no price data")
            + extra_str
        )

    scanner_lines = []
    for s in scanner_snapshot:
        if s.get("day_pct") is None:
            continue
        price = s.get("price")
        currency = s.get("currency", "")
        high_52w = s.get("high_52w")
        low_52w = s.get("low_52w")
        rsi = s.get("rsi")
        rsi_sig = s.get("rsi_signal", "")
        earn = s.get("earnings_date")
        extras = []
        if rsi is not None:
            extras.append(f"RSI {rsi} ({rsi_sig})" if rsi_sig and rsi_sig != "neutral" else f"RSI {rsi}")
        if earn:
            extras.append(f"earnings {earn}")
        if price and high_52w and low_52w:
            pct_off_high = (price - high_52w) / high_52w * 100
            pct_off_low = (price - low_52w) / low_52w * 100
            extras.append(f"52w {low_52w:.2f}–{high_52w:.2f} {currency} ({pct_off_high:+.0f}% vs high / {pct_off_low:+.0f}% vs low)")
        year_ret = s.get("year_return")
        if year_ret is not None:
            extras.append(f"52w return {year_ret:+.1f}%")
        week_pct = s.get("week_pct")
        extra_str = "  [" + ", ".join(extras) + "]" if extras else ""
        week_str = f" / {week_pct:+.1f}% 5d" if week_pct is not None else ""
        price_str = f"{price:.2f}" if price is not None else "N/A"
        scanner_lines.append(f"- {s['ticker']}: {s['day_pct']:+.2f}% today{week_str}, price {price_str} {s.get('currency','')}{extra_str}")

    prompt = f"""You are a personal investment advisor for a retail investor based in Dublin, Ireland.
Today is {datetime.now().strftime('%A %d %B %Y')}.

Portfolio:
{chr(10).join(portfolio_lines)}

Watchlist scan:
{chr(10).join(scanner_lines)}

Investment goals and bucket structure:
1. RETIREMENT — DCA into diversified ETFs (IWDA, VUSA, IMAE, AEME, IEAG, BRK-B). Long-term hold, no active trading.
2. GROWTH — Medium-term stock picks (NVDA, ASML, SNDK), 6-12 month horizon.
3. HIGH CONVICTION (HC-1) — AI/tech bets (NBIS, VRT, APLD). All tranches fully deployed. Target €15–20k exit in 6–12 months. Position locked, no further additions.
4. HIGH CONVICTION (HC-2) — NEW orthogonal bucket, €2,000 total. Deployment in progress (NOT correlated to HC-1 AI capex theme). Three picks from adversarial research (Jun 2026):
   - AMTM (Amentum Holdings, govt services/nuclear): €900 target. Deploy €600 now at ~$20.25, hold €300 for post Aug 4 earnings. At 52w low. Forward P/E 7.99x, FCF yield 8.9%. Hard stop: $14.
   - ONDS (Ondas Holdings, defence drones/autonomous systems): €650 target. Deploy €325 if price ≤$8.50, hold €325 for Q2 earnings beat or pullback to $5.50. Earnings ~Aug 11-12. Hard stop: revenue miss >15%.
   - LEU (Centrus Energy, HALEU nuclear monopoly): €450 target. GTC limit order at $152 only — do NOT deploy at market ($165). Earnings Aug 4. Hard stop once filled: $120.
   - TSSI (TSS Inc) was excluded despite highest EV because it is a 4th AI-infra name correlated to HC-1.
   IF any HC-2 tickers appear in the watchlist scan with significant moves, flag them specifically.
5. HIGH CONVICTION (HC-3) — Space/defence/lunar bucket, deployed 30 Jun – 1 Jul 2026. Orthogonal to HC-1 (AI infra) and HC-2 (govt services/nuclear/drones-non-space):
   - RCAT (Red Cat Holdings, military drones): 67 shares. Small-cap, high-beta defence drone play.
   - LUNR (Intuitive Machines, NASA lunar lander): 11 shares. Binary catalyst around lunar missions.
   - BKSY (BlackSky Technology, satellite ISR): 32 shares. Real-time satellite imagery, defence/intel customer base.
   - SERV (Serve Robotics, autonomous delivery): 83 shares @ €480.63 cost basis, bought 01-07-2026. NVDA-backed sidewalk delivery robots.
   HC-3 is speculative — treat as venture-style: size is small, earnings volatility is expected. Flag significant moves (±10%) or thesis-changing news.
6. HOUSE SAVINGS — Dublin property purchase target ~2028 (First Home Scheme eligible, cap €500k).
   - CORE (low-vol dividend): UCG.MI, ENEL.MI, NOVN.SW, AXA.PA, IBE.MC
   - SATELLITE (income): TTE.PA, GSK.L
   - GROWTH (upside): PYPL, ABNB, ROG.SW
   These positions must be liquid and capital-preserved by H1 2028. Advise if any show momentum to trim early.

Irish tax rules (CRITICAL — affects any add/trim advice):
- Stocks: 33% CGT on gains. Trim signals matter here.
- ETFs/ETCs: 41% exit tax + deemed disposal every 8 years. No new ETF positions.
- No loss harvesting offset against ETF gains.
- HC-2 picks (AMTM, ONDS, LEU, TSSI) and HC-3 picks (RCAT, LUNR, BKSY, SERV) are all NYSE/NASDAQ stocks — 33% CGT applies, no deemed disposal.

Give a concise briefing (max 300 words):
1. What's moving and why it matters to this portfolio
2. Standout watchlist moves — call out AMTM, ONDS, LEU specifically if in scan; also flag any HC-3 name (RCAT, LUNR, BKSY, SERV) moving ±10% or with news
3. One actionable thought for the week — specify bucket (stay course / trim / add / deploy HC-2 tranche / hold HC-3), flag CGT if trimming
4. Key risk to watch — include HC-2 deployment risk if any entry conditions are approaching, and HC-3 binary catalysts (lunar mission outcomes, defence contract awards)

Be direct and specific. No generic disclaimers. Like a smart friend who knows markets and Irish tax."""

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


def ai_health_check() -> dict:
    """Lightweight probe — returns which providers are live."""
    google_key = os.getenv("GOOGLE_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")
    result = {"gemini": False, "openai": False, "active": None}
    if google_key:
        try:
            from google import genai
            client = genai.Client(api_key=google_key)
            resp = client.models.generate_content(model="gemini-2.0-flash", contents="Reply OK")
            if resp.text:
                result["gemini"] = True
                result["active"] = "gemini"
        except Exception:
            pass
    if openai_key and not result["active"]:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini", max_tokens=5,
                messages=[{"role": "user", "content": "Reply OK"}],
            )
            if resp.choices[0].message.content:
                result["openai"] = True
                result["active"] = "openai"
        except Exception:
            pass
    return result


def generate_challenge(
    ticker: str,
    suggestion: dict,
    user_query: str,
    portfolio_snapshot: list[dict],
    live_quote: dict | None,
) -> str:
    """AI response to a user challenge on a suggested addition."""
    positions_text = "\n".join(
        f"- {p['ticker']} ({p['bucket']}): {p['shares']:.1f}sh @ avg €{p['avg_cost_eur']:.2f}, "
        f"P&L {p.get('pnl_pct', 0):+.1f}%, value €{p.get('current_value_eur', 0):,.0f}"
        for p in portfolio_snapshot
    )

    total_value = sum(p.get("current_value_eur", 0) or 0 for p in portfolio_snapshot)
    total_cost = sum(p.get("total_cost_eur", 0) or 0 for p in portfolio_snapshot)
    total_pnl_pct = (total_value - total_cost) / total_cost * 100 if total_cost else 0

    price_str = ""
    if live_quote and live_quote.get("price"):
        day_pct = live_quote.get("day_pct") or 0
        price_str = f"Live: {live_quote['price']:.2f} {live_quote.get('currency','')} ({day_pct:+.1f}% today)"

    prompt = f"""You are a rigorous investment analyst with a contrarian mindset.
Today is {datetime.now().strftime('%A %d %B %Y')}. Investor is in Dublin, Ireland.

PORTFOLIO CONTEXT (total €{total_value:,.0f} | P&L {total_pnl_pct:+.1f}%):
{positions_text}

SUGGESTED ADDITION BEING CHALLENGED:
Ticker: {ticker}
Sector: {suggestion.get('sector','')} | Geography: {suggestion.get('geo','')} | Currency: {suggestion.get('ccy','')}
Priority rationale: {suggestion.get('why','')}
Buy guide: {suggestion.get('buyGuide','')}
What it diversifies: {suggestion.get('diversifies','')}
Known bear case: {suggestion.get('bearCase','')}
Conviction: {suggestion.get('conviction','')}
{price_str}

USER'S CHALLENGE / QUESTION:
"{user_query}"

Respond using EXACTLY these four markdown sections with no intro text before the first header:

## Bull Case
One paragraph: why the original thesis holds. Quote at least one specific number (valuation, growth rate, market share). Be concise.

## Contrarian Take
Play devil's advocate hard. What is the strongest reason NOT to buy this? What would a short-seller say? What assumption in the thesis is most likely to be wrong? Be specific and direct.

## Sources & Assumptions to Verify
List 3 specific claims in the thesis that need verification before buying. For each, say what to check and where (e.g. company earnings release, analyst consensus, regulatory filing). No vague advice.

## Portfolio Fit Verdict
Given this specific portfolio (positions, buckets, concentrations above), should the investor add this position now, wait for a better entry, or skip it? Consider Irish CGT rules (33% on stocks, never ETFs). One clear recommendation with a specific condition if applicable.

Max 400 words total. No disclaimers. Be direct."""

    return _ask(prompt, max_tokens=600)


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

    # ── HELD POSITION ─────────────────────────────────────────────────────
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
One paragraph: buy, hold, or avoid for a 3-5 year horizon — and why? Is the current P&L a reason to trim or add? Irish tax note: 33% CGT on any sale.

Plain English. Reference the actual numbers. No disclaimers. Max 650 words."""

        return _ask(prompt, max_tokens=1000)

    # ── WATCHLIST TICKER (not held) ────────────────────────────────────────
    # Build price context for prompt
    price_context = ""
    if quote and quote.get("price"):
        price_q = quote.get("price")
        currency_q = quote.get("currency", "")
        day_pct = quote.get("day_pct") or 0
        price_context = f"\nCurrent price: {price_q:.2f} {currency_q} ({day_pct:+.1f}% today) — NOT HELD"

    # Portfolio context summary (what the investor already owns)
    portfolio_context = """
INVESTOR'S EXISTING PORTFOLIO (for fit assessment):
Positions: NVDA (AI GPU, Growth), ASML.AS (EUV monopoly, Growth), VRT (AI data centre power, HC-1),
APLD (AI data centre infra, HC-1), NBIS (GPU cloud/AI inference, HC-1), SNDK (storage, Growth),
BRK-B (conglomerate, Retirement), IWDA/VUSA/IMAE/AEME/IEAG (index ETFs, Retirement).
Watchlist buckets: QUALITY (long-run compounders), GROWTH (AI-cycle timed), DEFENCE (NATO structural),
HOUSE (capital preservation 2028-2029, Dublin FTB purchase ~€500k), MOONSHOT (speculative, <5% allocation).
Irish tax: 33% CGT on stocks (stocks only — never ETFs due to 41% exit tax + deemed disposal)."""

    prompt = f"""You are a senior equity analyst briefing a retail investor in Dublin, Ireland on a watchlist stock they are considering buying.
Today is {datetime.now().strftime('%d %b %Y')}.

TICKER: {ticker} ({live_name})
{fundamentals_block}
{price_context}
{portfolio_context}

This stock is on the investor's watchlist but NOT yet purchased. Give a rigorous, specific investment case. Use these EXACT markdown headers — every section is mandatory:

## Why This Stock
Why was {ticker} selected for this watchlist? What structural thesis — competitive moat, macro tailwind, sector position, or valuation asymmetry — justifies monitoring it? Be specific: reference actual business facts, not generalities.

## Portfolio Fit
How does {ticker} fit this specific portfolio? What does it add that isn't already covered (sector, geography, currency, risk profile)? Name the existing position it most correlates with and explain whether the correlation is acceptable.

## Entry Zone
Where is the right entry price? Give a specific price range or RSI/technical condition. Is the current price good, extended, or cheap? Reference the 52W range and forward P/E vs growth rate. Where would you set a limit order today?

## Exit Strategy
What is the exit trigger? Give: (1) a price target (bear / base / bull case), (2) a time horizon, (3) a non-price exit trigger (e.g. thesis broken if X happens). For HOUSE bucket: must be liquid and exit by H1 2029.

## Return Scenarios
Be quantitative. For each case state the expected price and % return:
- **Bear case** (30% probability): what goes wrong, what price
- **Base case** (50% probability): core thesis plays out, what price in 12-24 months
- **Bull case** (20% probability): upside surprise, what price
Expected value = (0.3 × bear) + (0.5 × base) + (0.2 × bull). State it.

## Conviction & Invalidation
Conviction rating: LOW / MEDIUM / HIGH — and why.
What single development would immediately invalidate this thesis? Be precise (e.g. "if revenue growth drops below X%", "if this contract is lost", "if this regulatory ruling fails").

## Verdict
One clear paragraph: buy now / wait for better entry / avoid. Give the specific entry condition if waiting. No disclaimers. Irish CGT note: 33% on any gain at sale.

Plain English. Quote actual numbers from the live data. Max 750 words total."""

    return _ask(prompt, max_tokens=1200)
