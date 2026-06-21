"""
portfolio/broker.py — Broker integration via Alpaca Trading API.

Connects the portfolio manager to Alpaca for paper or live trading.
Handles account info, order placement, position management.

Usage:
    from portfolio.broker import AlpacaBroker

    broker = AlpacaBroker(paper=True)  # paper trading
    print(broker.account_summary())
    broker.market_order("SPY", 10, "buy")
    broker.close_position("SPY")
"""

import json
import os
import ssl
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

try:
    import certifi
    CA_BUNDLE = certifi.where()
except ImportError:
    CA_BUNDLE = None


# Default API endpoints
PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"


def _load_credentials() -> tuple[str, str] | None:
    """Load Alpaca API keys from ~/.alpaca/credentials.json or env vars."""
    cred_path = Path.home() / ".alpaca" / "credentials.json"

    # Check env vars first
    key_id = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if key_id and secret:
        return key_id, secret

    # Check credentials file
    if cred_path.exists():
        try:
            data = json.loads(cred_path.read_text())
            key_id = data.get("ALPACA_API_KEY") or data.get("api_key")
            secret = data.get("ALPACA_SECRET_KEY") or data.get("secret_key")
            if key_id and secret:
                return key_id, secret
        except (json.JSONDecodeError, IOError):
            pass

    return None


class AlpacaBroker:
    """Interface to Alpaca Trading API for stock/ETF execution."""

    def __init__(self, paper: bool = True):
        self.paper = paper
        self.base_url = PAPER_BASE if paper else LIVE_BASE
        creds = _load_credentials()
        if creds:
            self.api_key, self.secret_key = creds
        else:
            self.api_key = ""
            self.secret_key = ""
            print("  Warning: No Alpaca credentials found. Run 'alpaca_setup' first.")

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode() if data else None
        ctx = ssl.create_default_context(cafile=CA_BUNDLE) if CA_BUNDLE else ssl.create_default_context()
        req = Request(url, data=body, headers=self._headers(), method=method)
        try:
            resp = urlopen(req, timeout=15, context=ctx)
            result = json.loads(resp.read().decode())
            return result if isinstance(result, dict) else {"data": result}
        except HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            return {"error": f"HTTP {e.code}: {error_body[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    def account(self) -> dict:
        """Get account details — buying power, equity, status."""
        return self._request("GET", "/v2/account")

    def account_summary(self) -> str:
        """Human-readable account status."""
        acct = self.account()
        if "error" in acct:
            return f"  Alpaca error: {acct['error']}"
        if acct.get("status") != "ACTIVE" and self.paper:
            return "  Paper account not yet activated."
        label = "PAPER" if self.paper else "LIVE"
        return (
            f"  Alpaca {label} Account:\n"
            f"    Equity:       ${float(acct.get('equity', 0)):>10.2f}\n"
            f"    Buying Power: ${float(acct.get('buying_power', 0)):>10.2f}\n"
            f"    Cash:         ${float(acct.get('cash', 0)):>10.2f}\n"
            f"    Status:       {acct.get('status', 'unknown')}"
        )

    def positions(self) -> list[dict]:
        """Get all open positions."""
        result = self._request("GET", "/v2/positions")
        if isinstance(result, list):
            return result
        if "error" in result:
            print(f"  Error fetching positions: {result['error']}")
        return []

    def position(self, symbol: str) -> dict | None:
        """Get position for a specific symbol."""
        result = self._request("GET", f"/v2/positions/{symbol}")
        if "error" in result:
            return None
        return result

    def market_order(self, symbol: str, qty: float, side: str,
                     time_in_force: str = "day") -> dict:
        """Place a market order.

        Args:
            symbol: Ticker (e.g. 'SPY', 'GLD')
            qty: Number of shares
            side: 'buy' or 'sell'
            time_in_force: 'day', 'gtc', 'ioc', 'fok'
        Returns:
            Order result dict
        """
        order = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": time_in_force,
        }
        result = self._request("POST", "/v2/orders", order)
        if "id" in result:
            print(f"  Order placed: {side.upper()} {qty} {symbol} @ market "
                  f"(id: {result['id'][:8]}...)")
        return result

    def close_position(self, symbol: str) -> dict:
        """Close an open position for a symbol."""
        result = self._request("DELETE", f"/v2/positions/{symbol}")
        if isinstance(result, dict) and "id" in result:
            print(f"  Position closed: {symbol}")
        return result if isinstance(result, dict) else {"data": result}

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        result = self._request("DELETE", "/v2/orders")
        print("  All open orders cancelled.")
        return result if isinstance(result, dict) else {"data": result}
