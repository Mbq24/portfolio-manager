"""
portfolio/prices.py — Live price feed.

Fetches current prices using Forge's data_fetcher (yfinance), the same
source Forge's harness validates strategies against — so live prices match
the data the strategies were tested on.

Usage:
    from portfolio.prices import PriceFeed

    feed = PriceFeed()
    price = feed.get_price("SPY")       # stock/ETF
    price = feed.get_price("GLD")       # gold ETF
    prices = feed.get_prices(["SPY", "GLD", "QQQ"])
"""

import sys
import time
from pathlib import Path

FORGE_ROOT = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "VisualStudioProjects"
    / "Forge"
)
if str(FORGE_ROOT) not in sys.path:
    sys.path.insert(0, str(FORGE_ROOT))

from tradingview.data_fetcher import fetch_ohlcv  # noqa: E402


class PriceFeed:
    """Live price feed using Forge's yfinance-backed data fetcher."""

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._cache_time: float = 0
        self._cache_ttl: int = 60  # 1 minute cache (yfinance is REST)

    def _fetch(self, ticker: str) -> dict | None:
        """Fetch latest daily OHLCV for a ticker."""
        try:
            df = fetch_ohlcv(ticker, interval="1d", period="5d")
            if df is None or df.empty:
                print(f"  No data for {ticker}")
                return None
            last = df.iloc[-1]
            return {
                "ticker": ticker,
                "close": float(last["close"]),
                "open": float(last["open"]),
                "high": float(last["high"]),
                "low": float(last["low"]),
                "volume": float(last["volume"]),
                "timestamp": str(last.name),
            }
        except Exception as e:
            print(f"  Error fetching {ticker}: {e}")
            return None

    def get_price(self, ticker: str) -> float:
        """Get latest close price for a ticker. Returns 0 on failure."""
        data = self._fetch(ticker)
        if data is None:
            return 0.0
        self._cache[ticker] = data
        self._cache_time = time.time()
        return data["close"]

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Get latest close prices for multiple tickers."""
        result = {}
        for t in tickers:
            price = self.get_price(t)
            if price > 0:
                result[t] = price
            time.sleep(0.3)  # be gentle with yfinance rate limits
        return result

    def summary(self) -> str:
        """Human-readable price summary."""
        if not self._cache:
            return "  No prices cached."
        lines = []
        for ticker, data in self._cache.items():
            lines.append(f"  {ticker:12s} ${data['close']:>8.2f}  (${data['low']:>8.2f}–${data['high']:>8.2f})")
        return "\n".join(lines)


# Common ticker reference
TICKERS = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq ETF",
    "GLD": "Gold ETF",
    "C:XAUUSD": "Gold spot (forex)",
    "GC": "Gold futures",
    "DXY": "US Dollar Index",
}
