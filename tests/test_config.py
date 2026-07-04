from decimal import Decimal
from pathlib import Path

import pytest

from ledger.config import ConfigError, load_config

REAL_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def test_real_config_loads(config):
    assert config.portfolio.starting_capital_gbp == Decimal("500.00")
    assert config.trading.min_trade_gbp == Decimal("50.00")
    assert set(config.assets) == {"BTC", "ETH", "SOL", "SUI", "HYPE"}
    assert set(config.venues) == {"kraken", "coinbase"}


def test_asset_fields(config):
    btc = config.asset("BTC")
    assert btc.coingecko_id == "bitcoin"
    assert btc.venue == "kraken"
    assert btc.quote_currency == "GBP"
    assert btc.target_weight_frac == Decimal("0.4")
    hype = config.asset("HYPE")
    assert hype.quote_currency == "USD"  # Kraken pair is USD-quoted
    assert hype.spread_frac == Decimal("0.003")  # 0.30% -> 0.003


def test_pct_fields_become_fractions(config):
    kraken = config.venue("kraken")
    assert kraken.taker_fee_rate == Decimal("0.004")  # 0.40% -> 0.004
    assert config.fx.conversion_cost_rate == Decimal("0.005")
    assert config.trading.per_trade_cap_frac_of_asset == Decimal("0.2")


def test_weights_sum_to_one(config):
    assert sum(a.target_weight_frac for a in config.assets.values()) == 1


def test_missing_file_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_missing_key_fails_loudly(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[portfolio]\ndrift_threshold_pts = 10\n")
    with pytest.raises(ConfigError, match="starting_capital_gbp"):
        load_config(p)


def test_weights_must_sum_to_100(tmp_path):
    text = REAL_CONFIG.read_text().replace("target_weight_pct = 40", "target_weight_pct = 45")
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="sum to 100"):
        load_config(p)


def test_asset_venue_must_exist(tmp_path):
    text = REAL_CONFIG.read_text().replace('venue = "kraken"', 'venue = "binance"', 1)
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="binance"):
        load_config(p)


def test_unknown_asset_fails_loudly(config):
    with pytest.raises(ConfigError, match="DOGE"):
        config.asset("DOGE")


def test_absurd_fee_rejected(tmp_path):
    text = REAL_CONFIG.read_text().replace("taker_fee_pct = 0.40", "taker_fee_pct = 40.0")
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="exceeds 5%"):
        load_config(p)


def test_absurd_spread_rejected(tmp_path):
    text = REAL_CONFIG.read_text().replace("spread_pct = 0.40", "spread_pct = 40.0")
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="spread_pct exceeds"):
        load_config(p)
