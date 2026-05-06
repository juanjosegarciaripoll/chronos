from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from icalendar import Calendar

from chronos.domain import ComponentKind


class IcalParseError(ValueError):
    pass


@dataclass(frozen=True, kw_only=True)
class ParsedComponent:
    kind: ComponentKind
    uid: str | None
    recurrence_id: str | None
    summary: str | None
    description: str | None
    location: str | None
    dtstart: datetime | None
    dtend: datetime | None
    due: datetime | None
    status: str | None


def parse_vcalendar(raw: bytes) -> list[ParsedComponent]:
    try:
        cal = Calendar.from_ical(raw)
    except ValueError as exc:
        raise IcalParseError(f"invalid iCalendar data: {exc}") from exc
    out: list[ParsedComponent] = []
    for sub in cal.walk("VEVENT"):  # pyright: ignore[reportUnknownMemberType]
        out.append(_project(sub, ComponentKind.VEVENT))
    for sub in cal.walk("VTODO"):  # pyright: ignore[reportUnknownMemberType]
        out.append(_project(sub, ComponentKind.VTODO))
    return out


def _project(component: object, kind: ComponentKind) -> ParsedComponent:
    return ParsedComponent(
        kind=kind,
        uid=_get_str(component, "UID"),
        recurrence_id=_get_recurrence_id(component),
        summary=_get_str(component, "SUMMARY"),
        description=_get_str(component, "DESCRIPTION"),
        location=_get_str(component, "LOCATION"),
        dtstart=_get_datetime(component, "DTSTART"),
        dtend=_get_datetime(component, "DTEND"),
        due=_get_datetime(component, "DUE"),
        status=_get_str(component, "STATUS"),
    )


def _get_str(component: object, key: str) -> str | None:
    value = _call_get(component, key)
    if value is None:
        return None
    return str(value)


def _get_datetime(component: object, key: str) -> datetime | None:
    value = _decoded(component, key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def _get_recurrence_id(component: object) -> str | None:
    dt = _get_datetime(component, "RECURRENCE-ID")
    if dt is None:
        return None
    return dt.isoformat()


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)


def _call_get(component: object, key: str) -> object:
    getter = getattr(component, "get", None)
    if getter is None:
        return None
    return getter(key)


def _decoded(component: object, key: str) -> object:
    contains = getattr(component, "__contains__", None)
    if contains is None or not contains(key):
        return None
    decoder = getattr(component, "decoded", None)
    if decoder is None:
        return None
    try:
        return decoder(key)
    except (KeyError, ValueError):
        return None
