from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, Select

from chronos.domain import CalendarRef, StoredComponent, VEvent
from chronos.tui.bindings import edit_bindings
from chronos.tui.widgets.date_picker import DatePicker, InvalidDateError


@dataclass(frozen=True, kw_only=True)
class EditDraft:
    """Output of `EventEditScreen` — passed to the caller's save callback."""

    target: CalendarRef
    summary: str
    dtstart: datetime
    dtend: datetime | None
    location: str
    description: str
    existing: StoredComponent | None


class EventEditScreen(Screen[None]):
    """Form for creating a new VEVENT or editing an existing one.

    `existing=None` is "new event" mode; otherwise the form pre-fills
    from the component. The caller passes the list of writable
    `CalendarRef`s, plus an `on_save(draft)` callback.
    """

    BINDINGS = edit_bindings()

    def __init__(
        self,
        *,
        calendars: tuple[CalendarRef, ...],
        existing: StoredComponent | None,
        default_calendar: CalendarRef | None,
        on_save: Callable[[EditDraft], None],
    ) -> None:
        super().__init__()
        if not calendars:
            raise ValueError("EventEditScreen needs at least one writable calendar")
        self._calendars = calendars
        self._existing = existing
        self._default_calendar = default_calendar or calendars[0]
        self._on_save = on_save
        self._error: str | None = None

    def compose(self) -> ComposeResult:
        ex = self._existing
        summary = ex.summary or "" if ex is not None else ""
        start = (
            ex.dtstart.isoformat()
            if ex is not None and ex.dtstart is not None
            else ""
        )
        end = (
            ex.dtend.isoformat()
            if isinstance(ex, VEvent) and ex.dtend is not None
            else ""
        )
        location = ex.location or "" if ex is not None else ""
        description = ex.description or "" if ex is not None else ""
        with Vertical(id="event-edit"):
            yield Label("Calendar:")
            yield Select(
                ((self._calendar_label(c), c) for c in self._calendars),
                value=self._default_calendar,
                allow_blank=False,
                id="edit-calendar",
            )
            yield Label("Summary:")
            yield Input(value=summary, id="edit-summary")
            yield Label("Start (YYYY-MM-DDTHH:MM):")
            yield DatePicker(value=start, placeholder="YYYY-MM-DDTHH:MM")
            yield Label("End (optional, YYYY-MM-DDTHH:MM):")
            yield Input(value=end, id="edit-end")
            yield Label("Location (optional):")
            yield Input(value=location, id="edit-location")
            yield Label("Description (optional):")
            yield Input(value=description, id="edit-description")
            yield Label("", id="edit-error")
        yield Footer()

    def action_save(self) -> None:
        try:
            draft = self._collect()
        except InvalidDateError as exc:
            self._show_error(str(exc))
            return
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        self._on_save(draft)

    def action_cancel(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def _collect(self) -> EditDraft:
        select = self.query_one(  # pyright: ignore[reportUnknownVariableType]
            "#edit-calendar", Select
        )
        target = cast("object", select.value)
        if not isinstance(target, CalendarRef):
            raise ValueError("calendar selection is required")
        summary_input: Input = self.query_one("#edit-summary", Input)
        summary = summary_input.value.strip()
        if not summary:
            raise ValueError("summary is required")
        date_input: DatePicker = self.query_one(DatePicker)
        dtstart = date_input.parsed()
        end_input: Input = self.query_one("#edit-end", Input)
        end_text = end_input.value.strip()
        dtend: datetime | None = None
        if end_text:
            from chronos.tui.widgets.date_picker import parse_date_input

            dtend = parse_date_input(end_text)
        location_input: Input = self.query_one("#edit-location", Input)
        description_input: Input = self.query_one("#edit-description", Input)
        return EditDraft(
            target=target,
            summary=summary,
            dtstart=dtstart,
            dtend=dtend,
            location=location_input.value.strip(),
            description=description_input.value.strip(),
            existing=self._existing,
        )

    def _show_error(self, message: str) -> None:
        self._error = message
        try:
            label: Label = self.query_one("#edit-error", Label)
        except Exception:  # noqa: BLE001 — Textual's NoMatches is private.
            return
        label.update(message)

    @staticmethod
    def _calendar_label(ref: CalendarRef) -> str:
        return f"{ref.account_name} / {ref.calendar_name}"


__all__ = ["EditDraft", "EventEditScreen"]
