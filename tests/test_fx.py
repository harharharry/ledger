from decimal import Decimal

import pytest

from ledger.data.fx import parse_gbp_usd
from ledger.data.http import DataError


def test_parse_rate():
    payload = {"base": "GBP", "rates": {"USD": Decimal("1.2712")}}
    assert parse_gbp_usd(payload) == Decimal("1.2712")


def test_missing_rate_fails_loudly():
    with pytest.raises(DataError, match="missing USD"):
        parse_gbp_usd({"base": "GBP", "rates": {}})


def test_wrong_shape_fails_loudly():
    with pytest.raises(DataError, match="payload shape"):
        parse_gbp_usd({"error": "boom"})


def test_implausible_rate_fails_loudly():
    with pytest.raises(DataError, match="implausible"):
        parse_gbp_usd({"rates": {"USD": Decimal("12.7")}})
    with pytest.raises(DataError, match="implausible"):
        parse_gbp_usd({"rates": {"USD": Decimal("0.05")}})
