"""Manual smoke check for the data sources. Hits the live APIs.

Usage: .venv/bin/python -m ledger.data.smoke
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import load_config
from . import coingecko, fx
from .http import DataError


def main() -> int:
    config = load_config(Path(__file__).resolve().parents[2] / "config.toml")
    failures = 0

    for asset in config.assets.values():
        try:
            series = coingecko.fetch_daily_closes(
                asset.symbol, asset.coingecko_id,
                days=100, vs_currency=asset.quote_currency.lower(),
            )
            marker = "£" if series.currency == "GBP" else "$"
            print(f"OK   {asset.symbol}: {len(series)} days, "
                  f"latest {marker}{series.latest.close:.2f} ({series.latest.date})")
        except DataError as e:
            failures += 1
            print(f"FAIL {asset.symbol}: {e}")

    try:
        rate = fx.fetch_gbp_usd()
        print(f"OK   GBPUSD: {rate}")
    except DataError as e:
        failures += 1
        print(f"FAIL GBPUSD: {e}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
