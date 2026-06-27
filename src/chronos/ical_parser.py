from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from icalendar import Calendar

from chronos.domain import AlarmAction, ComponentKind, ParsedAlarm


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
    sequence: int | None


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


def parse_method(raw: bytes) -> str | None:
    """Return the upper-cased VCALENDAR ``METHOD`` (iTIP) or ``None``.

    ``METHOD:REQUEST``/``CANCEL``/``REPLY``/``PUBLISH`` distinguishes an
    iTIP scheduling message from a plain calendar export.  ``None`` means
    no method was declared (treat as a plain ``PUBLISH``-style import).
    """
    try:
        cal = Calendar.from_ical(raw)
    except ValueError as exc:
        raise IcalParseError(f"invalid iCalendar data: {exc}") from exc
    method = _call_get(cal, "METHOD")
    if method is None:
        return None
    return str(method).strip().upper() or None


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
        sequence=_get_int(component, "SEQUENCE"),
    )


def _get_int(component: object, key: str) -> int | None:
    value = _call_get(component, key)
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


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


def parse_recurrence_id(component: object) -> str | None:
    """Return a component's RECURRENCE-ID as a UTC ISO string, or ``None``.

    Public wrapper over the internal projection so callers handling raw
    icalendar subcomponents (e.g. ingest's instance-cancel path) compare
    RECURRENCE-IDs in the same normalized form the index stores.
    """
    return _get_recurrence_id(component)


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


_SUPPORTED_ALARM_ACTIONS = frozenset({"DISPLAY", "AUDIO"})


def extract_alarm_triggers(raw_ics: bytes, uid: str) -> list[ParsedAlarm]:
    """Return all DISPLAY/AUDIO VALARM triggers for the master component with ``uid``.

    Only examines the master (no RECURRENCE-ID) — alarms are defined on the
    master and inherited by every recurrence instance.  Returns ``[]`` on any
    parse error or when no supported alarms exist.
    """
    try:
        cal = Calendar.from_ical(raw_ics)
    except ValueError:
        return []
    out: list[ParsedAlarm] = []
    for sub in cal.walk():  # pyright: ignore[reportUnknownMemberType]
        name = _component_name(sub)
        if name not in ("VEVENT", "VTODO"):
            continue
        if _component_get_str(sub, "UID") != uid:
            continue
        if _component_has(sub, "RECURRENCE-ID"):
            continue
        for valarm in sub.walk("VALARM"):  # pyright: ignore[reportUnknownMemberType]
            parsed = _parse_valarm(valarm)
            if parsed is not None:
                out.append(parsed)
        break  # found our master; stop scanning
    return out


def _parse_valarm(valarm: object) -> ParsedAlarm | None:
    action_raw = str(_call_get(valarm, "ACTION") or "DISPLAY").upper()
    if action_raw not in _SUPPORTED_ALARM_ACTIONS:
        return None
    action = AlarmAction(action_raw)

    trigger_val = _decoded(valarm, "TRIGGER")
    if trigger_val is None:
        return None

    if isinstance(trigger_val, timedelta):
        raw_prop = _call_get(valarm, "TRIGGER")
        params = getattr(raw_prop, "params", {})
        related = str(params.get("RELATED", "START")).upper()
        return ParsedAlarm(
            action=action,
            trigger_offset=trigger_val,
            trigger_related=related,
            description=_get_str(valarm, "DESCRIPTION"),
        )
    if isinstance(trigger_val, datetime):
        utc_trigger = (
            trigger_val.astimezone(UTC)
            if trigger_val.tzinfo
            else trigger_val.replace(tzinfo=UTC)
        )
        return ParsedAlarm(
            action=action,
            trigger_offset=utc_trigger,
            trigger_related="START",
            description=_get_str(valarm, "DESCRIPTION"),
        )
    return None


def _component_name(component: object) -> str:
    return str(getattr(component, "name", ""))


def _component_get_str(component: object, key: str) -> str | None:
    value = _call_get(component, key)
    if value is None:
        return None
    return str(value)


def _component_has(component: object, key: str) -> bool:
    contains = getattr(component, "__contains__", None)
    if contains is None:
        return False
    return bool(contains(key))
