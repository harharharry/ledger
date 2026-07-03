from decimal import Decimal
from pathlib import Path

import pytest

from ledger.config import ConfigError, load_config

REAL_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def test_real_config_loads(config):
    assert config.portfolio.starting_capital_gbp == Decimal("500.00")
    assert config.portfolio.allocation_crypto_frac == Decimal("0.6")
    assert config.trading.min_trade_gbp == Decimal("50.00")
    assert set(config.venues) == {"kraken", "coinbase", "alpaca"}


def test_pct_fields_become_fractions(config):
    kraken = config.venue("kraken")
    assert kraken.taker_fee_rate == Decimal("0.004")  # 0.40% -> 0.004
    assert config.fx.conversion_cost_rate == Decimal("0.005")


def test_missing_file_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_missing_key_fails_loudly(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[portfolio]\nstarting_capital_gbp = 500.0\n")
    with pytest.raises(ConfigError, match="allocation_crypto_pct"):
        load_config(p)


def test_allocation_must_sum_to_100(tmp_path):
    text = REAL_CONFIG.read_text().replace(
        "allocation_stocks_pct = 40", "allocation_stocks_pct = 30"
    )
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="sum to 100"):
        load_config(p)


def test_unknown_venue_fails_loudly(config):
    with pytest.raises(ConfigError, match="binance"):
        config.venue("binance")


def test_absurd_fee_rejected(tmp_path):
    text = REAL_CONFIG.read_text().replace("taker_fee_pct = 0.40", "taker_fee_pct = 40.0")
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="exceeds 5%"):
        load_config(p)
