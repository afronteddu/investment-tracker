"""
Parse DeGiro transaction exports → compute current positions with avg cost basis.
Drop new exports into data/transactions/ and this recomputes automatically.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from datetime import datetime

import openpyxl

# ISIN → Yahoo Finance ticker mapping
ISIN_TO_TICKER = {
    "US0381692070": "APLD",       # Applied Digital Corp
    "US92537N1081": "VRT",        # Vertiv Holdings
    "NL0009805522": "NBIS",       # Nebius Group (ex-Yandex)
    "US80004C2008": "SNDK",       # SanDisk Corp
    "US67066G1040": "NVDA",       # Nvidia
    "NL0010273215": "ASML.AS",    # ASML (Euronext Amsterdam)
    "US0846707026": "BRK-B",      # Berkshire Hathaway B
    "IE00B4ND3602": "IGLN.L",     # iShares Physical Gold ETC (LSE, USD)
    "IE00B4NCWG09": "ISLN.L",     # iShares Physical Silver ETC (LSE, USD)
    "IE00B4L5Y983": "IWDA.AS",    # iShares Core MSCI World
    "IE00B3XXRP09": "VUSA.AS",    # Vanguard S&P 500
    "IE00B4K48X80": "IMAE.AS",    # iShares Core MSCI Europe
    "LU2009202107": "AEME.PA",    # Amundi MSCI Emerging ex-China
    "IE00B4WXJJ64": "IEAG.AS",    # iShares Core EUR Govt Bond
}

TICKER_NAMES = {
    "APLD": "Applied Digital Corp",
    "VRT": "Vertiv Holdings",
    "NBIS": "Nebius Group",
    "SNDK": "SanDisk Corp",
    "NVDA": "Nvidia",
    "ASML.AS": "ASML Holding NV",
    "BRK-B": "Berkshire Hathaway B",
    "IGLN.L": "iShares Physical Gold ETC",
    "ISLN.L": "iShares Physical Silver ETC",
    "IWDA.AS": "iShares MSCI World",
    "VUSA.AS": "Vanguard S&P 500",
    "IMAE.AS": "iShares MSCI Europe",
    "AEME.PA": "Amundi MSCI EM ex-China",
    "IEAG.AS": "iShares EUR Govt Bond",
    # Watchlist
    "SMCI": "Super Micro Computer",
    "AMD": "Advanced Micro Devices",
    "PLTR": "Palantir Technologies",
    "IONQ": "IonQ (Quantum Computing)",
    "RGTI": "Rigetti Computing",
    "QUBT": "Quantum Computing Inc",
    "TSM": "Taiwan Semiconductor",
    "ARM": "Arm Holdings",
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "VWRL.AS": "Vanguard FTSE All-World",
}

BUCKET_MAP = {
    "APLD": "high_conviction",
    "VRT": "high_conviction",
    "NBIS": "high_conviction",
    "SNDK": "high_conviction",
    "NVDA": "growth",
    "ASML.AS": "growth",
    "BRK-B": "retirement",
    "IGLN.L": "retirement",
    "ISLN.L": "retirement",
    "IWDA.AS": "retirement",
    "VUSA.AS": "retirement",
    "IMAE.AS": "retirement",
    "AEME.PA": "retirement",
    "IEAG.AS": "retirement",
}


@dataclass
class Position:
    ticker: str
    name: str
    isin: str
    shares: float = 0.0
    total_cost_eur: float = 0.0   # total EUR spent (including fees)
    buy_count: int = 0

    @property
    def avg_cost_eur(self) -> float:
        return self.total_cost_eur / self.shares if self.shares > 0 else 0.0

    @property
    def bucket(self) -> str:
        return BUCKET_MAP.get(self.ticker, "growth")

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "isin": self.isin,
            "shares": round(self.shares, 4),
            "avg_cost_eur": round(self.avg_cost_eur, 4),
            "total_cost_eur": round(self.total_cost_eur, 2),
            "buy_count": self.buy_count,
            "bucket": self.bucket,
        }


def load_transactions(data_dir: str = "data/transactions") -> list[dict]:
    """Load and merge all transaction xlsx files, deduplicate by order ID."""
    rows = []
    seen_orders = set()

    pattern = os.path.join(data_dir, "*.xlsx")
    files = sorted(glob.glob(pattern))

    for filepath in files:
        wb = openpyxl.load_workbook(filepath)
        ws = wb.active
        headers = None
        for row in ws.iter_rows(values_only=True):
            if headers is None:
                headers = row
                continue
            if not row[0]:
                continue
            # Columns: Data, Ora, Prodotto, ISIN, Borsa rif, Borsa, Quantità,
            #          Quotazione, currency, Valore locale, currency, Valore EUR,
            #          Tasso cambio, AutoFX, Costi EUR, Totale EUR, ID Ordine, ...
            order_id = row[16] if len(row) > 16 else None

            # Deduplicate: prefer order_id when present, otherwise use content key
            dedup_key = order_id if order_id else (row[0], row[2], row[6], row[15])
            if dedup_key in seen_orders:
                continue
            seen_orders.add(dedup_key)

            rows.append({
                "date": row[0],
                "time": row[1],
                "product": row[2],
                "isin": row[3],
                "quantity": row[6],
                "price": row[7],
                "price_currency": row[8],
                "value_local": row[9],
                "value_eur": row[11],
                "fx_rate": row[12],
                "autofx_cost": row[13],
                "transaction_cost_eur": row[14],
                "total_eur": row[15],
                "order_id": order_id,
            })

    return rows


def compute_positions(data_dir: str = "data/transactions") -> dict[str, Position]:
    transactions = load_transactions(data_dir)

    # Must process chronologically so sells never arrive before their buys
    def parse_date(tx):
        try:
            return datetime.strptime(tx["date"], "%d-%m-%Y")
        except Exception:
            return datetime.min

    transactions.sort(key=parse_date)

    positions: dict[str, Position] = {}

    for tx in transactions:
        isin = tx.get("isin")
        qty = tx.get("quantity")
        total_eur = tx.get("total_eur")

        if not isin or not qty or not total_eur:
            continue

        # Skip sells (positive total = money in = sell)
        # Buys have negative total_eur (money out)
        if total_eur > 0:
            # This is a sell — reduce position
            ticker = ISIN_TO_TICKER.get(isin, isin)
            if ticker in positions and positions[ticker].shares > 0:
                sell_frac = abs(qty) / positions[ticker].shares
                positions[ticker].total_cost_eur *= (1 - sell_frac)
                positions[ticker].shares -= abs(qty)
            continue

        ticker = ISIN_TO_TICKER.get(isin, isin)
        product = tx.get("product", ticker)

        if ticker not in positions:
            positions[ticker] = Position(ticker=ticker, name=product, isin=isin)

        positions[ticker].shares += qty
        positions[ticker].total_cost_eur += abs(total_eur)
        positions[ticker].buy_count += 1

    # Drop positions with 0 shares (fully sold)
    return {k: v for k, v in positions.items() if v.shares > 0.001}


def add_new_export(src_path: str, data_dir: str = "data/transactions"):
    """Copy a new DeGiro export into the data directory."""
    import shutil
    filename = os.path.basename(src_path)
    dest = os.path.join(data_dir, filename)
    shutil.copy2(src_path, dest)
    return dest
