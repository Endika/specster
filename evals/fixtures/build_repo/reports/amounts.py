def parse_amount(text: str) -> float:
    """An amount as written in a report: '12', '12.5' or '1,234.50'."""
    return float(text.replace(",", "."))
