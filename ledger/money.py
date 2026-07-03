"""Money and quantity precision rules.

Everything financial is a Decimal. GBP amounts round to whole pence, asset
quantities and prices to 8 decimal places (enough for BTC satoshis and Alpaca
fractional shares). Floats never enter the ledger.
"""

from decimal import ROUND_HALF_EVEN, Decimal

PENCE = Decimal("0.01")
EIGHT_DP = Decimal("0.00000001")


def to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def gbp(value: object) -> Decimal:
    """Quantize a GBP amount to whole pence (banker's rounding)."""
    return to_decimal(value).quantize(PENCE, rounding=ROUND_HALF_EVEN)


def qty(value: object) -> Decimal:
    """Quantize an asset quantity to 8 decimal places."""
    return to_decimal(value).quantize(EIGHT_DP, rounding=ROUND_HALF_EVEN)


def price(value: object) -> Decimal:
    """Quantize a price to 8 decimal places."""
    return to_decimal(value).quantize(EIGHT_DP, rounding=ROUND_HALF_EVEN)
