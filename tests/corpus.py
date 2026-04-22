from __future__ import annotations

_PRODID = "-//chronos-tests//EN"


def _vcalendar(*inner_blocks: str) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{_PRODID}"]
    for block in inner_blocks:
        lines.extend(block.strip("\n").split("\n"))
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def simple_event() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
UID:simple-event-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:Simple event
ORGANIZER:mailto:alice@example.com
END:VEVENT
"""
    )


def timed_event_with_tz() -> bytes:
    return _vcalendar(
        """
BEGIN:VTIMEZONE
TZID:Europe/Madrid
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
TZNAME:CET
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
TZNAME:CEST
END:DAYLIGHT
END:VTIMEZONE
""",
        """
BEGIN:VEVENT
UID:timed-tz-1@example.com
DTSTAMP:20260422T120000Z
DTSTART;TZID=Europe/Madrid:20260501T110000
DTEND;TZID=Europe/Madrid:20260501T120000
SUMMARY:Timed event with Madrid TZ
END:VEVENT
""",
    )


def all_day_event() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
UID:all-day-1@example.com
DTSTAMP:20260422T120000Z
DTSTART;VALUE=DATE:20260501
DTEND;VALUE=DATE:20260502
SUMMARY:All-day event
END:VEVENT
"""
    )


def recurring_weekly() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
UID:weekly-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:Weekly meeting
RRULE:FREQ=WEEKLY;BYDAY=FR
END:VEVENT
"""
    )


def recurring_with_exceptions() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
UID:with-exceptions-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:Weekly meeting with exceptions
RRULE:FREQ=WEEKLY;BYDAY=FR
EXDATE:20260515T090000Z
END:VEVENT
""",
        """
BEGIN:VEVENT
UID:with-exceptions-1@example.com
RECURRENCE-ID:20260508T090000Z
DTSTAMP:20260422T120000Z
DTSTART:20260508T100000Z
DTEND:20260508T110000Z
SUMMARY:Weekly meeting (rescheduled)
END:VEVENT
""",
    )


def recurring_count() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
UID:count-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:Five-session series
RRULE:FREQ=WEEKLY;BYDAY=FR;COUNT=5
END:VEVENT
"""
    )


def recurring_until() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
UID:until-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:Weekly until June
RRULE:FREQ=WEEKLY;BYDAY=FR;UNTIL=20260626T090000Z
END:VEVENT
"""
    )


def simple_todo() -> bytes:
    return _vcalendar(
        """
BEGIN:VTODO
UID:todo-1@example.com
DTSTAMP:20260422T120000Z
DUE:20260505T170000Z
SUMMARY:File quarterly report
STATUS:NEEDS-ACTION
PRIORITY:5
END:VTODO
"""
    )


def completed_todo() -> bytes:
    return _vcalendar(
        """
BEGIN:VTODO
UID:todo-done-1@example.com
DTSTAMP:20260422T120000Z
DUE:20260422T170000Z
COMPLETED:20260421T153000Z
SUMMARY:Renew domain
STATUS:COMPLETED
PERCENT-COMPLETE:100
END:VTODO
"""
    )


def zero_duration_event() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
UID:zero-duration-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
SUMMARY:Point-in-time event (no DTEND)
END:VEVENT
"""
    )


def malformed_missing_uid() -> bytes:
    return _vcalendar(
        """
BEGIN:VEVENT
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:No UID present
END:VEVENT
"""
    )


def duplicate_uid() -> tuple[bytes, bytes]:
    first = _vcalendar(
        """
BEGIN:VEVENT
UID:duplicate-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:First copy
END:VEVENT
"""
    )
    second = _vcalendar(
        """
BEGIN:VEVENT
UID:duplicate-1@example.com
DTSTAMP:20260422T130000Z
DTSTART:20260502T090000Z
DTEND:20260502T100000Z
SUMMARY:Second copy with same UID
END:VEVENT
"""
    )
    return (first, second)


ALL_SINGLE_FIXTURES: tuple[tuple[str, bytes], ...] = (
    ("simple_event", simple_event()),
    ("timed_event_with_tz", timed_event_with_tz()),
    ("all_day_event", all_day_event()),
    ("recurring_weekly", recurring_weekly()),
    ("recurring_with_exceptions", recurring_with_exceptions()),
    ("recurring_count", recurring_count()),
    ("recurring_until", recurring_until()),
    ("zero_duration_event", zero_duration_event()),
    ("simple_todo", simple_todo()),
    ("completed_todo", completed_todo()),
    ("malformed_missing_uid", malformed_missing_uid()),
)
