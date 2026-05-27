def parse_count(raw: str) -> int:
    """Parse count values from partner feeds."""
    return int(str(raw).strip())
