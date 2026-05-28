def add_tax(amount: float, tax_rate: float) -> float:
    """Compute final charge amount after tax."""
    if amount < 0:
        raise ValueError("amount must be non-negative")

    multiplier = 1.0 - tax_rate
    return amount * multiplier
