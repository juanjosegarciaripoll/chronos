"""Parse the "go to date" dialog's input into a calendar date.

Kept free of Textual so the grammar is unit-testable on its own.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_RELATIVE_RE = re.compile(r"^([+-])(\d+)([dwmy])$")

GOTO_HELP = "YYYY-MM-DD · 15 · fri · +2w · -3d · +1m · today"


class GotoError(ValueError):
    pass


def parse_goto(text: str, *, viewed: date, today: date) -> date:
    """Resolve `text` to a date.

    Accepted forms (case-insensitive):

    - `2026-10-15` — that date.
    - `15` — that day of the viewed month.
    - `fri` / `friday` (any prefix of at least 2 letters) — the next such
      weekday after the viewed date.
    - `+2w`, `-3d`, `+1m`, `+1y` — relative to the viewed date.
    - `today` / `t` — today.

    Raises `GotoError` with a user-facing message on anything else.
    """
    value = text.strip().lower()
    if not value:
        raise GotoError("enter a date")
    if value in ("today", "t"):
        return today
    relative = _RELATIVE_RE.match(value)
    if relative is not None:
        sign, amount, unit = relative.groups()
        n = int(amount) * (1 if sign == "+" else -1)
        if unit == "d":
            return viewed + timedelta(days=n)
        if unit == "w":
            return viewed + timedelta(weeks=n)
        if unit == "m":
            return viewed + relativedelta(months=n)
        return viewed + relativedelta(years=n)
    if value.isdigit():
        try:
            return viewed.replace(day=int(value))
        except ValueError:
            raise GotoError(f"{viewed:%B} has no day {value}") from None
    if value.isalpha() and len(value) >= 2:
        matches = [i for i, name in enumerate(_WEEKDAYS) if name.startswith(value)]
        if len(matches) == 1:
            ahead = (matches[0] - viewed.weekday() - 1) % 7 + 1
            return viewed + timedelta(days=ahead)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise GotoError(f"could not understand {text.strip()!r}") from None


__all__ = ["GOTO_HELP", "GotoError", "parse_goto"]
