from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from chronos.domain import LocalStatus, StoredComponent, VEvent, VTodo


def build_event_ics(
    uid: str,
    summary: str,
    dtstart: datetime,
    dtend: datetime | None,
    now: datetime,
    *,
    location: str = "",
    description: str = "",
) -> bytes:
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
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


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


__all__ = ["build_event_ics", "generate_uid", "trashed_copy"]
