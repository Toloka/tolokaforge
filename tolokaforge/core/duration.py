"""Duration-string parser for CLI flags accepting spans (e.g. ``--time-limit``).

Accepts compound units — ``30m``, ``2h``, ``1h30m``, ``90s``, ``1d12h`` — and
returns seconds as a float. Fractional units are accepted (``1.5h`` → 5400.0).
Bare numbers, unknown units, negative values, and whitespace-broken tokens
all raise :class:`ValueError` naming the offending input; the CLI wraps this
in ``click.BadParameter``.
"""

from __future__ import annotations

import re

__all__ = ["parse_duration"]

_UNITS: dict[str, float] = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)([a-zA-Z])")


def parse_duration(spec: str) -> float:
    """Return the number of seconds represented by ``spec``.

    Accepts a run of ``<number><unit>`` tokens whose units come from
    ``{s, m, h, d}``. Compound (``1h30m``) and fractional (``1.5h``) forms
    are supported. Bare numbers (no unit), empty input, unknown units, and
    negative values raise :class:`ValueError` with a message naming the
    offending input.
    """
    if not spec or not spec.strip():
        raise ValueError(f"empty duration spec: {spec!r}")

    if spec.lstrip().startswith("-"):
        raise ValueError(f"negative duration not allowed: {spec!r}")

    text = spec.strip()
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            remainder = text[pos:]
            if remainder.strip("0123456789.") == "":
                raise ValueError(
                    f"bare number without unit in duration {spec!r}: "
                    f"got {remainder!r}, expected a unit suffix in "
                    f"{sorted(_UNITS)}"
                )
            raise ValueError(f"unparseable token in duration {spec!r}: {remainder!r}")
        tokens.append((match.group(1), match.group(2).lower()))
        pos = match.end()

    total = 0.0
    for value_str, unit in tokens:
        if unit not in _UNITS:
            raise ValueError(
                f"unknown unit {unit!r} in duration {spec!r}: expected one of {sorted(_UNITS)}"
            )
        total += float(value_str) * _UNITS[unit]
    return total
