import re
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[Decimal]:
    found = set()
    for raw in _NUMBER.findall(text):
        try:
            found.add(Decimal(raw.replace(",", "")).normalize())
        except InvalidOperation:
            pass
    return found


def invented_numbers(report: str, facts: str) -> list[str]:
    """Numbers in the report that don't appear in the facts. Small counts and years are ignored."""
    made_up = _numbers(report) - _numbers(facts)
    return sorted(format(n, "f") for n in made_up if n >= 100 and not 2000 <= n <= 2100)
