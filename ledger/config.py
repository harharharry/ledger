"""Config loading. All tunables come from config.toml; missing keys fail loudly.

Percentage fields in the TOML are human-readable (0.40 means 0.40%, 40 means
40%). The loader converts them to Decimal fractions (0.0040 / 0.40) so
downstream code never divides by 100 again. Fraction-valued fields are named
*_rate / *_frac to keep that visible.

v1.2: crypto-only. Assets carry their own pair-level details (quote currency,
spread, target weight); venues carry fees only.
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
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    sell_regulatory_fee_rate: Decimal


@dataclass(frozen=True)
class AssetConfig:
    symbol: str
    coingecko_id: str
    venue: str
    quote_currency: str  # the trading pair's quote, e.g. 'GBP' or 'USD'
    spread_frac: Decimal  # full bid-ask spread for this pair; half charged per side
    target_weight_frac: Decimal  # share of the pot this asset aims for


@dataclass(frozen=True)
class PortfolioConfig:
    starting_capital_gbp: Decimal
    drift_threshold_pts: Decimal


@dataclass(frozen=True)
class TradingConfig:
    per_trade_cap_frac_of_asset: Decimal
    min_trade_gbp: Decimal
    max_proposals_per_day: int
    max_proposals_per_week: int


@dataclass(frozen=True)
class StrategyConfig:
    ma_days: int
    rsi_period: int
    base_trade_gbp: Decimal
    rsi_oversold: Decimal  # RSI scale 0-100, not a fraction
    rsi_overbought: Decimal
    oversold_tilt: Decimal  # plain size multipliers, not percentages
    overbought_tilt: Decimal


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
    strategy: StrategyConfig
    fx: FxConfig
    runtime: RuntimeConfig
    venues: dict[str, VenueConfig]
    assets: dict[str, AssetConfig]

    def venue(self, name: str) -> VenueConfig:
        try:
            return self.venues[name]
        except KeyError:
            raise ConfigError(f"unknown venue {name!r}; configured: {sorted(self.venues)}")

    def asset(self, symbol: str) -> AssetConfig:
        try:
            return self.assets[symbol]
        except KeyError:
            raise ConfigError(f"unknown asset {symbol!r}; configured: {sorted(self.assets)}")


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
        drift_threshold_pts=to_decimal(_require(p, "drift_threshold_pts", "portfolio")),
    )
    if portfolio.starting_capital_gbp <= 0:
        raise ConfigError("[portfolio] starting_capital_gbp must be positive")

    t = _section(data, "trading")
    trading = TradingConfig(
        per_trade_cap_frac_of_asset=_pct_to_frac(t, "per_trade_cap_pct_of_asset", "trading"),
        min_trade_gbp=to_decimal(_require(t, "min_trade_gbp", "trading")),
        max_proposals_per_day=int(_require(t, "max_proposals_per_day", "trading")),
        max_proposals_per_week=int(_require(t, "max_proposals_per_week", "trading")),
    )
    if trading.min_trade_gbp <= 0:
        raise ConfigError("[trading] min_trade_gbp must be positive")

    s = _section(data, "strategy")
    strategy = StrategyConfig(
        ma_days=int(_require(s, "ma_days", "strategy")),
        rsi_period=int(_require(s, "rsi_period", "strategy")),
        base_trade_gbp=to_decimal(_require(s, "base_trade_gbp", "strategy")),
        rsi_oversold=to_decimal(_require(s, "rsi_oversold", "strategy")),
        rsi_overbought=to_decimal(_require(s, "rsi_overbought", "strategy")),
        oversold_tilt=to_decimal(_require(s, "oversold_tilt", "strategy")),
        overbought_tilt=to_decimal(_require(s, "overbought_tilt", "strategy")),
    )
    if strategy.ma_days < 2 or strategy.rsi_period < 2:
        raise ConfigError("[strategy] ma_days and rsi_period must each be at least 2")
    if not (0 < strategy.rsi_oversold < strategy.rsi_overbought < 100):
        raise ConfigError("[strategy] need 0 < rsi_oversold < rsi_overbought < 100")
    if strategy.base_trade_gbp <= 0:
        raise ConfigError("[strategy] base_trade_gbp must be positive")
    if strategy.oversold_tilt <= 0 or strategy.overbought_tilt <= 0:
        raise ConfigError("[strategy] tilts must be positive multipliers")

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
            maker_fee_rate=_pct_to_frac(v, "maker_fee_pct", where),
            taker_fee_rate=_pct_to_frac(v, "taker_fee_pct", where),
            sell_regulatory_fee_rate=_pct_to_frac(v, "sell_regulatory_fee_pct", where),
        )
        # A fee above 5% on a mainstream venue is a config typo, not a schedule.
        for label, rate in (
            ("maker_fee_pct", venue.maker_fee_rate),
            ("taker_fee_pct", venue.taker_fee_rate),
        ):
            if rate > Decimal("0.05"):
                raise ConfigError(f"[{where}] {label} exceeds 5% — likely a typo")
        venues[name] = venue
    if not venues:
        raise ConfigError("at least one [venues.*] table is required")

    asset_tables = _section(data, "assets")
    assets: dict[str, AssetConfig] = {}
    for symbol, fields in asset_tables.items():
        where = f"assets.{symbol}"
        venue_name = str(_require(fields, "venue", where))
        if venue_name not in venues:
            raise ConfigError(f"[{where}] venue {venue_name!r} is not configured")
        spread = _pct_to_frac(fields, "spread_pct", where)
        if spread > Decimal("0.05"):
            raise ConfigError(f"[{where}] spread_pct exceeds 5% — likely a typo")
        assets[symbol] = AssetConfig(
            symbol=symbol,
            coingecko_id=str(_require(fields, "coingecko_id", where)),
            venue=venue_name,
            quote_currency=str(_require(fields, "quote_currency", where)),
            spread_frac=spread,
            target_weight_frac=_pct_to_frac(fields, "target_weight_pct", where),
        )
    if not assets:
        raise ConfigError("at least one [assets.*] table is required")
    total_weight = sum(a.target_weight_frac for a in assets.values())
    if total_weight != 1:
        raise ConfigError(
            f"[assets] target_weight_pct values must sum to 100, got "
            f"{total_weight * 100}"
        )

    return Config(
        portfolio=portfolio, trading=trading, strategy=strategy, fx=fx,
        runtime=runtime, venues=venues, assets=assets,
    )
