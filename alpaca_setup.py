#!/usr/bin/env python3
"""
alpaca_setup.py — Store Alpaca API credentials for the portfolio manager.

Usage:
    python alpaca_setup.py

You'll need your Alpaca API Key ID and Secret Key from:
    https://app.alpaca.markets/paper/dashboard/overview

Sign up for a free paper trading account if you haven't already.
"""

import json
from pathlib import Path

CRED_DIR = Path.home() / ".alpaca"


def main():
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    cred_file = CRED_DIR / "credentials.json"

    print("=" * 60)
    print("  ALPACA CREDENTIALS SETUP")
    print("=" * 60)
    print()
    print("Go to https://app.alpaca.markets/paper/dashboard/overview")
    print("Sign up for a free paper trading account, then grab your API keys.")
    print()

    api_key = input("  API Key ID: ").strip()
    secret_key = input("  Secret Key: ").strip()

    if not api_key or not secret_key:
        print("  No keys entered. Exiting.")
        return

    cred_data = {
        "ALPACA_API_KEY": api_key,
        "ALPACA_SECRET_KEY": secret_key,
    }

    cred_file.write_text(json.dumps(cred_data, indent=2))
    cred_file.chmod(0o600)  # Only owner can read
    print(f"\n  Credentials saved to {cred_file}")
    print("  You can now use the portfolio manager with Alpaca paper trading.")


if __name__ == "__main__":
    main()
