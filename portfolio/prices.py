"""
portfolio/prices.py — Live price feed via Massive (Polygon.io) API.

Fetches current prices for stocks, ETFs, forex (gold), and futures.
Uses the existing API key from the VT project's .env file.

Usage:
    from portfolio.prices import PriceFeed

    feed = PriceFeed()
    price = feed.get_price("SPY")       # stock/ETF
    price = feed.get_price("C:XAUUSD")  # gold (forex)
    price = feed.get_price("GLD")       # gold ETF
    prices = feed.get_prices(["SPY", "GLD", "C:XAUUSD"])
"""

import json
import os
import ssl
import time
from pathlib import Path
from urllib.request import urlopen, Request

try:
    import certifi
    CA_BUNDLE = certifi.where()
except ImportError:
    CA_BUNDLE = None


def _load_api_key() -> str | None:
    """Read Polygon API key from VT project's .env file."""
    env_path = Path.home() / "VisualStudioProjects" / "VOLATILITY-TRADER" / ".env"
    if not env_path.exists():
        print("  Warning: VT .env not found")
        return None
    for line in env_path.read_text().splitlines():
        if line.startswith("POLYGON_API_KEY="):
            return line.split("=", 1)[1].strip()
    return None


class PriceFeed:
    """Live price feed using Massive (Polygon.io) API."""

    BASE_URL = "https://api.polygon.io/v2/aggs/ticker"

    def __init__(self):
        self.api_key = _load_api_key()
        if not self.api_key:
            print("  Warning: No Polygon API key found. Price feed disabled.")
        self._cache: dict[str, dict] = {}
        self._cache_time: float = 0
        self._cache_ttl: int = 300  # 5 minute cache

    def _fetch(self, ticker: str) -> dict | None:
        """Fetch previous day's OHLCV for a ticker."""
        if not self.api_key:
            return None
        url = f"{self.BASE_URL}/{ticker}/prev?adjusted=true&apiKey={self.api_key}"
        ctx = ssl.create_default_context(cafile=CA_BUNDLE) if CA_BUNDLE else ssl.create_default_context()
        try:
            req = Request(url, headers={"User-Agent": "portfolio-manager/1.0"})
            resp = urlopen(req, timeout=10, context=ctx)
            data = json.loads(resp.read().decode())
            if data.get("status") != "OK" or not data.get("results"):
                print(f"  No data for {ticker}: {data.get('status', 'unknown')}")
                return None
            result = data["results"][0]
            return {
                "ticker": ticker,
                "close": result.get("c", 0),
                "open": result.get("o", 0),
                "high": result.get("h", 0),
                "low": result.get("l", 0),
                "volume": result.get("v", 0),
                "vwap": result.get("vw", 0),
                "timestamp": result.get("t", 0),
            }
        except Exception as e:
            print(f"  Error fetching {ticker}: {e}")
            return None

    def get_price(self, ticker: str) -> float:
        """Get latest close price for a ticker. Returns 0 on failure."""
        data = self._fetch(ticker)
        if data is None:
            return 0.0
        # Cache result
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
            time.sleep(0.3)  # Rate limit: ~3 requests/sec on free tier
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
