from __future__ import annotations

from datetime import datetime

from textual.widgets import Input


class InvalidDateError(ValueError):
    pass


def parse_date_input(value: str) -> datetime:
    """Parse a `YYYY-MM-DDTHH:MM` (or `YYYY-MM-DD`) into a tz-aware datetime.

    A naive datetime is interpreted in the system local timezone — so
    "2026-04-26T11:00" typed by a user in Madrid means 11:00 Madrid
    local, matching what the calendar views display. Raises
    `InvalidDateError` with a user-facing message on malformed input.
    """
    text = value.strip()
    if not text:
        raise InvalidDateError("date is required")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidDateError(
            f"could not parse {text!r} (expected YYYY-MM-DD or YYYY-MM-DDTHH:MM)"
        ) from exc
    if dt.tzinfo is None:
        # astimezone() on a naive datetime presumes it is in the system
        # local timezone, then attaches that tzinfo. Storage downstream
        # converts to UTC; display converts back via astimezone().
        dt = dt.astimezone()
    return dt


class DatePicker(Input):
    """Input field accepting a date or datetime in ISO format.

    The widget itself is a thin Input. Validation lives in
    `parse_date_input`, callable from anywhere.
    """

    def __init__(self, value: str = "", placeholder: str = "YYYY-MM-DD HH:MM") -> None:
        super().__init__(value=value, placeholder=placeholder)

    def parsed(self) -> datetime:
        return parse_date_input(self.value)


__all__ = ["DatePicker", "InvalidDateError", "parse_date_input"]
