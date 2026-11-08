"""Node and element numbers as LIRA writes them: lists and ranges like "1-3 7 10-12".

Tools, services and adapters all use them, so they live outside the layers."""

import re

from lira_sapr_mcp.errors import LiraInputError


def parse_numbers(numbers: list[int] | str | None) -> list[int] | None:
    """Accept [1, 2, 5] or LIRA-style "1-3 7 10-12" (commas allowed); None when empty."""
    if numbers is None or isinstance(numbers, list):
        return numbers or None
    out: list[int] = []
    for part in re.split(r"[,\s]+", str(numbers).strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b < a or b - a > 1_000_000:
                raise LiraInputError(f"Bad range {part!r}")
            out.extend(range(a, b + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise LiraInputError(f"Bad number list {numbers!r}: use e.g. '1-3 7 10-12'")
    return out or None


def format_numbers(numbers: list[int]) -> str:
    """LIRA-style ranges of numbers, the inverse of ``parse_numbers``: "1-3 7 10-12"."""
    spans: list[str] = []
    ordered = sorted(set(numbers))
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        spans.append(str(ordered[i]) if i == j else f"{ordered[i]}-{ordered[j]}")
        i = j + 1
    return " ".join(spans)
