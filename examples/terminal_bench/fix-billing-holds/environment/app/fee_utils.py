"""
Fee calculation utilities for the billing platform.

Clients are assigned one of three fee calculation types via the fee_type column
in the clients table:

  - percentage : fee is a percentage of the hold amount
                 See legacy_fee_config.json for platform fee_rate conventions
  - tiered     : fee depends on amount brackets defined in TIER_SCHEDULE
                 fee_rate in holds is 0 (unused)
  - flat_rate  : fee is a fixed dollar amount per transaction
                 fee_rate stores the flat fee dollar amount directly
"""

# Tier schedule for 'tiered' fee type clients.
# Each entry: (max_amount_inclusive, rate)
TIER_SCHEDULE = [
    (100.0,        0.03),   # amounts up to $100: 3%
    (1000.0,       0.05),   # amounts up to $1000: 5%
    (float("inf"), 0.08),   # amounts above $1000: 8%
]


def calculate_fee_percentage(amount: float, fee_rate: float) -> float:
    """
    Calculate fee for 'percentage' type clients.

    fee_rate follows the platform convention defined in legacy_fee_config.json
    (percentage integer format, e.g. 5 = 5%), so must be divided by 100.

    Args:
        amount:   Hold amount in currency units.
        fee_rate: Fee rate value from the holds table.

    Returns:
        Fee charged, rounded to 2 decimal places.
    """
    return round(amount * fee_rate / 100, 2)


def calculate_fee_tiered(amount: float) -> float:
    """
    Calculate fee for 'tiered' type clients using TIER_SCHEDULE.

    Args:
        amount: Hold amount in currency units.

    Returns:
        Fee charged, rounded to 2 decimal places.
    """
    for max_amount, rate in TIER_SCHEDULE:
        if amount <= max_amount:
            return round(amount * rate, 2)
    return round(amount * 0.08, 2)


def calculate_fee_flat_rate(fee_rate: float) -> float:
    """
    Calculate fee for 'flat_rate' type clients.

    fee_rate stores the flat fee dollar amount directly (e.g. 25.0 = $25 flat fee).

    Args:
        fee_rate: The flat fee amount in currency units.

    Returns:
        Flat fee, rounded to 2 decimal places.
    """
    return round(fee_rate, 2)


def calculate_fee(amount: float, fee_type: str, fee_rate: float) -> float:
    """
    Calculate the platform fee for a given amount, fee_type, and fee_rate.

    Args:
        amount:   The hold amount (in currency units).
        fee_type: Client's fee calculation type ('percentage', 'tiered', 'flat_rate').
        fee_rate: The fee_rate from the holds table; semantics depend on fee_type.

    Returns:
        The fee to be charged (in currency units), rounded to 2 decimal places.
    """
    if fee_type == "percentage":
        return calculate_fee_percentage(amount, fee_rate)
    elif fee_type == "tiered":
        return calculate_fee_tiered(amount)
    elif fee_type == "flat_rate":
        return calculate_fee_flat_rate(fee_rate)
    else:
        raise ValueError(f"Unknown fee_type: {fee_type!r}")
