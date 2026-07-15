from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from chronos.domain import LocalStatus, ParsedAlarm, StoredComponent, VEvent, VTodo

_ATTENDEE_EMAIL_RE = re.compile(r"^[^@\s,;:\x00-\x1f\x7f]+@[^@\s,;:\x00-\x1f\x7f]+$")


def build_event_ics(
    uid: str,
    summary: str,
    dtstart: datetime,
    dtend: datetime | None,
    now: datetime,
    *,
    location: str = "",
    description: str = "",
    alarms: Sequence[ParsedAlarm] = (),
    attendees: Sequence[str] = (),
    organizer: str | None = None,
) -> bytes:
    normalized_attendees = normalize_attendee_emails(attendees)
    normalized_organizer = (
        normalize_attendee_email(organizer) if organizer and organizer.strip() else None
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//chronos//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_fmt_dt(now)}",
        f"DTSTART:{_fmt_dt(dtstart)}",
    ]
    if dtend is not None:
        lines.append(f"DTEND:{_fmt_dt(dtend)}")
    lines.append(f"SUMMARY:{_escape_text(summary)}")
    if location:
        lines.append(f"LOCATION:{_escape_text(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape_text(description)}")
    if normalized_attendees and normalized_organizer is not None:
        lines.append(f"ORGANIZER:mailto:{normalized_organizer}")
    for attendee in normalized_attendees:
        lines.append(
            "ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:"
            f"mailto:{attendee}"
        )
    for alarm in alarms:
        lines.append("BEGIN:VALARM")
        lines.append(f"ACTION:{alarm.action.value}")
        if isinstance(alarm.trigger_offset, timedelta):
            trigger_str = _fmt_duration(alarm.trigger_offset)
            if alarm.trigger_related == "END":
                lines.append(f"TRIGGER;RELATED=END:{trigger_str}")
            else:
                lines.append(f"TRIGGER:{trigger_str}")
        else:
            lines.append(f"TRIGGER;VALUE=DATE-TIME:{_fmt_dt(alarm.trigger_offset)}")
        if alarm.description:
            lines.append(f"DESCRIPTION:{_escape_text(alarm.description)}")
        lines.append("END:VALARM")
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def normalize_attendee_emails(values: Sequence[str]) -> tuple[str, ...]:
    """Return de-duplicated attendee email addresses safe for iCalendar output."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        email = normalize_attendee_email(value)
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(email)
    return tuple(out)


def normalize_attendee_email(value: str) -> str:
    """Normalize a user-entered attendee value to a bare email address.

    Accepts plain emails, ``mailto:`` values, and simple ``Name <email>`` input.
    The validation intentionally rejects separators and control characters
    because attendee values are serialized directly into an iCalendar property.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("attendee email cannot be blank")
    _display_name, parsed = parseaddr(raw)
    email = (parsed or raw).strip()
    if email.lower().startswith("mailto:"):
        email = email[7:].strip()
    if not _ATTENDEE_EMAIL_RE.fullmatch(email):
        raise ValueError(f"invalid attendee email: {value!r}")
    return email


def generate_uid(
    account: str, calendar: str, summary: str, start: datetime, now: datetime
) -> str:
    payload = f"{account}|{calendar}|{summary}|{start.isoformat()}|{now.isoformat()}"
    digest = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{digest[:16]}@chronos"


def trashed_copy(
    component: StoredComponent, *, trashed_at: datetime
) -> StoredComponent:
    if isinstance(component, VEvent):
        return VEvent(
            ref=component.ref,
            href=component.href,
            etag=component.etag,
            raw_ics=component.raw_ics,
            summary=component.summary,
            description=component.description,
            location=component.location,
            dtstart=component.dtstart,
            dtend=component.dtend,
            status=component.status,
            local_flags=component.local_flags,
            server_flags=component.server_flags,
            local_status=LocalStatus.TRASHED,
            trashed_at=trashed_at,
            synced_at=component.synced_at,
        )
    return VTodo(
        ref=component.ref,
        href=component.href,
        etag=component.etag,
        raw_ics=component.raw_ics,
        summary=component.summary,
        description=component.description,
        location=component.location,
        dtstart=component.dtstart,
        due=component.due,
        status=component.status,
        local_flags=component.local_flags,
        server_flags=component.server_flags,
        local_status=LocalStatus.TRASHED,
        trashed_at=trashed_at,
        synced_at=component.synced_at,
    )


def _fmt_duration(td: timedelta) -> str:
    """Format a timedelta as an iCal DURATION value (e.g. ``-PT15M``)."""
    sign = "-" if td.total_seconds() < 0 else ""
    secs = abs(int(td.total_seconds()))
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    minutes, secs = divmod(secs, 60)
    day_part = f"{days}D" if days else ""
    time_parts: list[str] = []
    if hours:
        time_parts.append(f"{hours}H")
    if minutes:
        time_parts.append(f"{minutes}M")
    if secs:
        time_parts.append(f"{secs}S")
    time_part = ("T" + "".join(time_parts)) if time_parts else ""
    body = day_part + time_part
    return f"{sign}P{body}" if body else "PT0S"


def _fmt_dt(dt: datetime) -> str:
    as_utc = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return as_utc.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


__all__ = [
    "build_event_ics",
    "generate_uid",
    "normalize_attendee_email",
    "normalize_attendee_emails",
    "trashed_copy",
]
