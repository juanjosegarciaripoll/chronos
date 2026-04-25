from __future__ import annotations

from datetime import UTC, datetime

from textual.widgets import Input


class InvalidDateError(ValueError):
    pass


def parse_date_input(value: str) -> datetime:
    """Parse a `YYYY-MM-DDTHH:MM` (or `YYYY-MM-DD`) into a UTC datetime.

    A naive datetime gets `UTC` attached. Raises `InvalidDateError`
    with a user-facing message otherwise.
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
        dt = dt.replace(tzinfo=UTC)
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
