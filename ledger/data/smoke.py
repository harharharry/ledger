"""Manual smoke check for all three data sources. Hits the live APIs.

Usage: .venv/bin/python -m ledger.data.smoke
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import load_config
from . import alpaca, coingecko, fx
from .alpaca import MissingCredentialsError
from .http import DataError


def main() -> int:
    config = load_config(Path(__file__).resolve().parents[2] / "config.toml")
    failures = 0

    for asset in config.sleeve_assets("crypto"):
        try:
            series = coingecko.fetch_daily_closes(asset.symbol, asset.coingecko_id, days=100)
            print(f"OK   {asset.symbol}: {len(series)} days, "
                  f"latest £{series.latest.close:.2f} ({series.latest.date})")
        except DataError as e:
            failures += 1
            print(f"FAIL {asset.symbol}: {e}")

    try:
        rate = fx.fetch_gbp_usd()
        print(f"OK   GBPUSD: {rate}")
    except DataError as e:
        failures += 1
        print(f"FAIL GBPUSD: {e}")

    for asset in config.sleeve_assets("stocks"):
        try:
            series = alpaca.fetch_daily_closes(asset.symbol, days=100)
            print(f"OK   {asset.symbol}: {len(series)} days, "
                  f"latest ${series.latest.close:.2f} ({series.latest.date})")
        except MissingCredentialsError as e:
            failures += 1
            print(f"SKIP {asset.symbol}: {e}")
        except DataError as e:
            failures += 1
            print(f"FAIL {asset.symbol}: {e}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
