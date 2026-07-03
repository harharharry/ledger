"""Config loading. All tunables come from config.toml; missing keys fail loudly.

Percentage fields in the TOML are human-readable (0.40 means 0.40%). The loader
converts them to Decimal fractions (0.0040) so downstream code never divides by
100 again. Fraction-valued fields are named *_rate / *_frac to keep that visible.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .money import to_decimal


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class VenueConfig:
    name: str
    quote_currency: str
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    spread_frac: Decimal  # full bid-ask spread as a fraction; half is charged per side
    sell_regulatory_fee_rate: Decimal


@dataclass(frozen=True)
class PortfolioConfig:
    starting_capital_gbp: Decimal
    allocation_crypto_frac: Decimal
    allocation_stocks_frac: Decimal
    drift_threshold_pts: Decimal


@dataclass(frozen=True)
class TradingConfig:
    per_trade_cap_frac_of_sleeve: Decimal
    min_trade_gbp: Decimal
    max_proposals_per_day: int
    max_proposals_per_week: int


@dataclass(frozen=True)
class FxConfig:
    conversion_cost_rate: Decimal  # one-way cost as a fraction of GBP notional


@dataclass(frozen=True)
class RuntimeConfig:
    db_path: str
    kill_switch_path: str


@dataclass(frozen=True)
class Config:
    portfolio: PortfolioConfig
    trading: TradingConfig
    fx: FxConfig
    runtime: RuntimeConfig
    venues: dict[str, VenueConfig]

    def venue(self, name: str) -> VenueConfig:
        try:
            return self.venues[name]
        except KeyError:
            raise ConfigError(f"unknown venue {name!r}; configured: {sorted(self.venues)}")


def _require(section: dict, key: str, where: str) -> object:
    if key not in section:
        raise ConfigError(f"missing required config key [{where}] {key}")
    return section[key]


def _section(data: dict, name: str) -> dict:
    value = _require(data, name, "top level")
    if not isinstance(value, dict):
        raise ConfigError(f"config section [{name}] must be a table")
    return value


def _pct_to_frac(section: dict, key: str, where: str) -> Decimal:
    value = to_decimal(_require(section, key, where)) / 100
    if not (0 <= value <= 1):
        raise ConfigError(f"[{where}] {key} out of range: must be between 0 and 100 percent")
    return value


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = tomllib.loads(path.read_text())

    p = _section(data, "portfolio")
    portfolio = PortfolioConfig(
        starting_capital_gbp=to_decimal(_require(p, "starting_capital_gbp", "portfolio")),
        allocation_crypto_frac=_pct_to_frac(p, "allocation_crypto_pct", "portfolio"),
        allocation_stocks_frac=_pct_to_frac(p, "allocation_stocks_pct", "portfolio"),
        drift_threshold_pts=to_decimal(_require(p, "drift_threshold_pts", "portfolio")),
    )
    if portfolio.allocation_crypto_frac + portfolio.allocation_stocks_frac != 1:
        raise ConfigError("[portfolio] allocation percentages must sum to 100")
    if portfolio.starting_capital_gbp <= 0:
        raise ConfigError("[portfolio] starting_capital_gbp must be positive")

    t = _section(data, "trading")
    trading = TradingConfig(
        per_trade_cap_frac_of_sleeve=_pct_to_frac(t, "per_trade_cap_pct_of_sleeve", "trading"),
        min_trade_gbp=to_decimal(_require(t, "min_trade_gbp", "trading")),
        max_proposals_per_day=int(_require(t, "max_proposals_per_day", "trading")),
        max_proposals_per_week=int(_require(t, "max_proposals_per_week", "trading")),
    )
    if trading.min_trade_gbp <= 0:
        raise ConfigError("[trading] min_trade_gbp must be positive")

    f = _section(data, "fx")
    fx = FxConfig(conversion_cost_rate=_pct_to_frac(f, "conversion_cost_pct", "fx"))

    r = _section(data, "runtime")
    runtime = RuntimeConfig(
        db_path=str(_require(r, "db_path", "runtime")),
        kill_switch_path=str(_require(r, "kill_switch_path", "runtime")),
    )

    venue_tables = _section(data, "venues")
    venues: dict[str, VenueConfig] = {}
    for name, v in venue_tables.items():
        where = f"venues.{name}"
        venue = VenueConfig(
            name=name,
            quote_currency=str(_require(v, "quote_currency", where)),
            maker_fee_rate=_pct_to_frac(v, "maker_fee_pct", where),
            taker_fee_rate=_pct_to_frac(v, "taker_fee_pct", where),
            spread_frac=_pct_to_frac(v, "spread_pct", where),
            sell_regulatory_fee_rate=_pct_to_frac(v, "sell_regulatory_fee_pct", where),
        )
        # A fee or spread above 5% on a mainstream venue is a config typo, not a schedule.
        for label, rate in (
            ("maker_fee_pct", venue.maker_fee_rate),
            ("taker_fee_pct", venue.taker_fee_rate),
            ("spread_pct", venue.spread_frac),
        ):
            if rate > Decimal("0.05"):
                raise ConfigError(f"[{where}] {label} exceeds 5% — likely a typo")
        venues[name] = venue
    if not venues:
        raise ConfigError("at least one [venues.*] table is required")

    return Config(portfolio=portfolio, trading=trading, fx=fx, runtime=runtime, venues=venues)
