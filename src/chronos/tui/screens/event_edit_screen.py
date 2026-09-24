from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, Select

from chronos.domain import (
    AlarmAction,
    CalendarRef,
    ParsedAlarm,
    StoredComponent,
    VEvent,
)
from chronos.ical_parser import extract_alarm_triggers, extract_attendees
from chronos.mutations import normalize_attendee_emails
from chronos.tui.bindings import edit_bindings
from chronos.tui.widgets.date_picker import DatePicker, InvalidDateError

_HALF_HOUR_OPTIONS = tuple(
    (label, label)
    for hour in range(24)
    for minute in (0, 30)
    if (label := f"{hour:02d}:{minute:02d}")
)


@dataclass(frozen=True, kw_only=True)
class EditDraft:
    """Output of `EventEditScreen` — passed to the caller's save callback."""

    target: CalendarRef
    summary: str
    dtstart: datetime
    dtend: datetime | None
    location: str
    description: str
    attendees: tuple[str, ...]
    alarms: tuple[ParsedAlarm, ...]
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
        on_delete: Callable[[StoredComponent], None] | None = None,
        initial_start: datetime | None = None,
        initial_end: datetime | None = None,
    ) -> None:
        super().__init__()
        if not calendars:
            raise ValueError("EventEditScreen needs at least one writable calendar")
        self._calendars = calendars
        self._existing = existing
        self._default_calendar = default_calendar or calendars[0]
        self._on_save = on_save
        self._on_delete = on_delete
        self._initial_start = initial_start
        self._initial_end = initial_end
        self._error: str | None = None

    def compose(self) -> ComposeResult:
        ex = self._existing
        summary = ex.summary or "" if ex is not None else ""
        # Pre-fill with the system-local representation so what the user
        # sees here matches the times shown in the calendar views, and
        # so a no-op edit round-trips without shifting by their UTC offset.
        initial_start = (
            ex.dtstart
            if ex is not None and ex.dtstart is not None
            else self._initial_start
        )
        initial_end = (
            ex.dtend
            if isinstance(ex, VEvent) and ex.dtend is not None
            else self._initial_end
        )
        start_date, start_time = _local_parts(initial_start)
        end_date, end_time = _local_parts(initial_end)
        if start_date == end_date:
            end_date = ""
        location = ex.location or "" if ex is not None else ""
        description = ex.description or "" if ex is not None else ""
        attendees = ", ".join(
            extract_attendees(ex.raw_ics, ex.ref.uid) if ex is not None else ()
        )
        reminders = _alarms_to_input(
            extract_alarm_triggers(ex.raw_ics, ex.ref.uid) if ex is not None else []
        )
        with Vertical(id="event-edit"):
            yield Label(
                "Edit event" if ex is not None else "New event",
                classes="event-edit-title",
            )
            with Horizontal(classes="event-field-row"):
                yield Label("Calendar", classes="event-field-label")
                yield Select(
                    ((self._calendar_label(c), c) for c in self._calendars),
                    value=self._default_calendar,
                    allow_blank=False,
                    id="edit-calendar",
                    classes="event-field-control",
                    compact=True,
                )
            with Horizontal(classes="event-field-row"):
                yield Label("Summary", classes="event-field-label")
                yield Input(
                    value=summary,
                    id="edit-summary",
                    classes="event-field-control",
                    compact=True,
                )
            with Horizontal(classes="event-datetime-row"):
                yield Label("Starts", classes="event-field-label")
                yield DatePicker(
                    value=start_date,
                    placeholder="YYYY-MM-DD",
                    id="edit-start-date",
                    classes="event-date",
                    compact=True,
                )
                yield Select(
                    _time_options(start_time),
                    value=start_time if start_time else Select.NULL,
                    prompt="Time",
                    id="edit-start-time",
                    classes="event-time",
                    compact=True,
                )
            with Horizontal(classes="event-datetime-row"):
                yield Label("Ends", classes="event-field-label")
                yield DatePicker(
                    value=end_date,
                    placeholder="Same date",
                    id="edit-end-date",
                    classes="event-date",
                    compact=True,
                )
                yield Select(
                    _time_options(end_time),
                    value=end_time if end_time else Select.NULL,
                    prompt="Optional",
                    id="edit-end-time",
                    classes="event-time",
                    compact=True,
                )
            with Horizontal(classes="event-field-row"):
                yield Label("Location", classes="event-field-label")
                yield Input(
                    value=location,
                    id="edit-location",
                    classes="event-field-control",
                    compact=True,
                )
            with Horizontal(classes="event-field-row"):
                yield Label("Description", classes="event-field-label")
                yield Input(
                    value=description,
                    id="edit-description",
                    classes="event-field-control",
                    compact=True,
                )
            with Horizontal(classes="event-field-row"):
                yield Label("Invitees", classes="event-field-label")
                yield Input(
                    value=attendees,
                    id="edit-attendees",
                    placeholder="alice@example.com, bob@example.com",
                    classes="event-field-control",
                    compact=True,
                )
            with Horizontal(classes="event-field-row"):
                yield Label("Reminders", classes="event-field-label")
                yield Input(
                    value=reminders,
                    id="edit-reminders",
                    placeholder="Minutes before, e.g. 15, 60",
                    classes="event-field-control",
                    compact=True,
                )
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

    def action_delete(self) -> None:
        # Only meaningful when editing — a not-yet-saved event has
        # nothing to delete. Pop the form first so the delete
        # confirmation (pushed by the caller) lands on top of the
        # calendar view rather than over the edit screen.
        if self._existing is None or self._on_delete is None:
            return
        existing = self._existing
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        self._on_delete(existing)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Hide the Delete binding from the footer (and disable the key)
        # in "new event" mode, where there is nothing to delete yet.
        del parameters
        if action == "delete":
            return self._existing is not None and self._on_delete is not None
        return True

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
        start_date_input = self.query_one("#edit-start-date", DatePicker)
        start_date = _parse_date(start_date_input.value, "start date")
        start_time_select = self.query_one("#edit-start-time", Select)
        start_time = _selected_time(start_time_select, "start time")
        dtstart = datetime.combine(start_date, start_time).astimezone()

        end_date_input = self.query_one("#edit-end-date", DatePicker)
        end_date_text = end_date_input.value.strip()
        end_time_select = self.query_one("#edit-end-time", Select)
        selected_end_time = cast("object", end_time_select.value)
        dtend: datetime | None = None
        if isinstance(selected_end_time, str):
            end_date = (
                _parse_date(end_date_text, "end date") if end_date_text else start_date
            )
            dtend = datetime.combine(
                end_date, _parse_time(selected_end_time, "end time")
            ).astimezone()
            if dtend <= dtstart:
                raise ValueError("end must be after start")
        elif end_date_text:
            raise ValueError("end time is required when end date is set")
        location_input: Input = self.query_one("#edit-location", Input)
        description_input: Input = self.query_one("#edit-description", Input)
        attendees_input: Input = self.query_one("#edit-attendees", Input)
        reminders_input: Input = self.query_one("#edit-reminders", Input)
        attendees = _parse_attendees_input(attendees_input.value)
        alarms = _parse_reminder_input(reminders_input.value)
        return EditDraft(
            target=target,
            summary=summary,
            dtstart=dtstart,
            dtend=dtend,
            location=location_input.value.strip(),
            description=description_input.value.strip(),
            attendees=attendees,
            alarms=alarms,
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


def _alarms_to_input(alarms: list[ParsedAlarm]) -> str:
    """Convert a list of ParsedAlarms to a comma-separated minutes string.

    Only START-relative DISPLAY/AUDIO alarms with a negative timedelta offset
    are representable in the simple input field; others are omitted.
    """
    parts: list[str] = []
    for alarm in alarms:
        if not isinstance(alarm.trigger_offset, timedelta):
            continue
        if alarm.trigger_related != "START":
            continue
        secs = alarm.trigger_offset.total_seconds()
        if secs >= 0:
            continue
        mins = abs(int(secs)) // 60
        if mins > 0:
            parts.append(str(mins))
    return ", ".join(parts)


def _parse_reminder_input(raw: str) -> tuple[ParsedAlarm, ...]:
    """Parse a comma-separated list of minute values into ParsedAlarms.

    Each token must be a positive integer (minutes before start).
    Non-numeric tokens and values ≤ 0 are silently ignored.
    """
    alarms: list[ParsedAlarm] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            mins = int(token)
        except ValueError:
            continue
        if mins <= 0:
            continue
        alarms.append(
            ParsedAlarm(
                action=AlarmAction.DISPLAY,
                trigger_offset=timedelta(minutes=-mins),
                trigger_related="START",
                description=None,
            )
        )
    return tuple(alarms)


def _parse_attendees_input(raw: str) -> tuple[str, ...]:
    values = tuple(token.strip() for token in raw.split(",") if token.strip())
    if not values:
        return ()
    return normalize_attendee_emails(values)


def _local_parts(dt: datetime | None) -> tuple[str, str]:
    if dt is None:
        return "", ""
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _time_options(value: str) -> tuple[tuple[str, str], ...]:
    if not value or any(option == value for _, option in _HALF_HOUR_OPTIONS):
        return _HALF_HOUR_OPTIONS
    return ((value, value), *_HALF_HOUR_OPTIONS)


def _parse_date(value: str, label: str) -> date:
    text = value.strip()
    if not text:
        raise InvalidDateError(f"{label} is required")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidDateError(f"{label} must be YYYY-MM-DD") from exc


def _parse_time(value: str, label: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be HH:MM") from exc
    return parsed.replace(second=0, microsecond=0)


def _selected_time(select: Select[object], label: str) -> time:
    selected = select.value
    if not isinstance(selected, str):
        raise ValueError(f"{label} is required")
    return _parse_time(selected, label)


__all__ = ["EditDraft", "EventEditScreen"]
