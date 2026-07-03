from decimal import Decimal

from ledger.money import gbp, qty, to_decimal


def test_gbp_rounds_to_pence():
    assert gbp("1.005") == Decimal("1.00")  # banker's rounding
    assert gbp("1.015") == Decimal("1.02")
    assert gbp("1.239") == Decimal("1.24")


def test_qty_rounds_to_eight_dp():
    assert qty("0.123456789") == Decimal("0.12345679")


def test_to_decimal_never_inherits_float_error():
    # str() conversion means 0.1 becomes Decimal('0.1'), not the float artifact
    assert to_decimal(0.1) == Decimal("0.1")
