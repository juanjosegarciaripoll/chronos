from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import cast

from dateutil.rrule import rrule, rruleset, rrulestr
from icalendar import Calendar

from chronos.domain import (
    CalendarRef,
    Occurrence,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.protocols import IndexRepository

logger = logging.getLogger(__name__)

MAX_OCCURRENCES = 10_000

_POPULATE_PROGRESS_INTERVAL = 50


class RecurrenceExpansionError(ValueError):
    pass


def expand(
    *,
    master: StoredComponent,
    overrides: Sequence[StoredComponent] = (),
    window_start: datetime,
    window_end: datetime,
    max_occurrences: int = MAX_OCCURRENCES,
) -> list[Occurrence]:
    """Expand a master component into concrete occurrences in a window.

    Pure function: identical inputs always produce identical output.
    For non-recurring components, returns one occurrence when the
    master's anchor falls inside the window, else an empty list.
    """
    if window_end <= window_start:
        return []

    anchor = _anchor(master)
    if anchor is None:
        return []
    anchor = _to_utc(anchor)

    duration = _master_duration(master)
    rrule_str, rdates, exdates = _extract_rules(master.raw_ics, master.ref.uid)

    if rrule_str is None and not rdates:
        return _single_occurrence(master, anchor, duration, window_start, window_end)

    ruleset = _build_ruleset(anchor, rrule_str, rdates, exdates)
    raw_occurrences = ruleset.between(
        _to_utc(window_start), _to_utc(window_end), inc=True
    )

    in_window = [dt for dt in raw_occurrences if window_start <= dt < window_end]
    if len(in_window) > max_occurrences:
        raise RecurrenceExpansionError(
            f"{len(in_window)} occurrences exceeds max_occurrences="
            f"{max_occurrences} for uid={master.ref.uid}"
        )

    override_map = _index_overrides(overrides)
    out: list[Occurrence] = []
    for occ_dt in in_window:
        override = override_map.get(occ_dt)
        if override is None:
            out.append(
                Occurrence(
                    ref=master.ref,
                    start=occ_dt,
                    end=occ_dt + duration if duration else None,
                    recurrence_id=None,
                    is_override=False,
                )
            )
            continue
        override_anchor = _anchor(override)
        if override_anchor is None:
            continue
        override_start = _to_utc(override_anchor)
        override_end = _end_time(override, duration)
        out.append(
            Occurrence(
                ref=override.ref,
                start=override_start,
                end=override_end,
                recurrence_id=override.ref.recurrence_id,
                is_override=True,
            )
        )
    return out


def populate_occurrences(
    *,
    index: IndexRepository,
    calendar: CalendarRef,
    window_start: datetime,
    window_end: datetime,
    max_occurrences: int = MAX_OCCURRENCES,
) -> int:
    """Rebuild the `occurrences` cache for one calendar + window.

    Returns the number of occurrence rows written. Masters whose RRULE
    expands to more than `max_occurrences` instances inside the window
    are skipped (their cache row is left empty); they remain in the
    `components` table for diagnostics.
    """
    components = index.list_calendar_components(calendar)
    masters = [c for c in components if c.ref.recurrence_id is None]
    overrides_by_uid: dict[str, list[StoredComponent]] = {}
    for component in components:
        if component.ref.recurrence_id is not None:
            overrides_by_uid.setdefault(component.ref.uid, []).append(component)

    total = 0
    n_masters = len(masters)
    # Recurrence expansion + per-master SQLite write is silent
    # otherwise; for a calendar with thousands of recurring masters
    # this phase can dominate sync wall-time, so emit a heartbeat.
    if n_masters >= _POPULATE_PROGRESS_INTERVAL:
        logger.info(
            "  expanding occurrences for %d master(s) in %s/%s...",
            n_masters,
            calendar.account_name,
            calendar.calendar_name,
        )
    with index.connection():
        for processed, master in enumerate(masters, start=1):
            try:
                occurrences = expand(
                    master=master,
                    overrides=tuple(overrides_by_uid.get(master.ref.uid, ())),
                    window_start=window_start,
                    window_end=window_end,
                    max_occurrences=max_occurrences,
                )
            except RecurrenceExpansionError:
                # One master's runaway RRULE shouldn't take the whole
                # population down. Skip it; everything else stays cached.
                continue
            index.set_occurrences(master.ref, occurrences)
            total += len(occurrences)
            if (
                n_masters >= _POPULATE_PROGRESS_INTERVAL
                and processed % _POPULATE_PROGRESS_INTERVAL == 0
            ):
                logger.info(
                    "  expand: %d/%d masters, %d occurrences so far",
                    processed,
                    n_masters,
                    total,
                )
    if (
        n_masters >= _POPULATE_PROGRESS_INTERVAL
        and n_masters % _POPULATE_PROGRESS_INTERVAL
    ):
        logger.info(
            "  expand: %d/%d masters, %d occurrences",
            n_masters,
            n_masters,
            total,
        )
    return total


def _anchor(component: StoredComponent) -> datetime | None:
    if isinstance(component, VEvent):
        return component.dtstart
    if component.dtstart is not None:
        return component.dtstart
    return component.due


def _master_duration(component: StoredComponent) -> timedelta | None:
    anchor = _anchor(component)
    if anchor is None:
        return None
    end = component.dtend if isinstance(component, VEvent) else component.due
    if end is None:
        return None
    return _to_utc(end) - _to_utc(anchor)


def _end_time(
    component: StoredComponent, fallback_duration: timedelta | None
) -> datetime | None:
    if isinstance(component, VEvent) and component.dtend is not None:
        return _to_utc(component.dtend)
    if isinstance(component, VTodo) and component.due is not None:
        return _to_utc(component.due)
    anchor = _anchor(component)
    if anchor is None or fallback_duration is None:
        return None
    return _to_utc(anchor) + fallback_duration


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _single_occurrence(
    master: StoredComponent,
    anchor: datetime,
    duration: timedelta | None,
    window_start: datetime,
    window_end: datetime,
) -> list[Occurrence]:
    if not (window_start <= anchor < window_end):
        return []
    end = _end_time(master, duration)
    return [
        Occurrence(
            ref=master.ref,
            start=anchor,
            end=end,
            recurrence_id=None,
            is_override=False,
        )
    ]


def _build_ruleset(
    anchor: datetime,
    rrule_str: str | None,
    rdates: Sequence[datetime],
    exdates: Sequence[datetime],
) -> rruleset:
    ruleset = rruleset()
    if rrule_str is not None:
        parsed = rrulestr(f"RRULE:{rrule_str}", dtstart=anchor)
        if isinstance(parsed, rrule):
            ruleset.rrule(parsed)
    for rdate in rdates:
        ruleset.rdate(rdate)
    for exdate in exdates:
        ruleset.exdate(exdate)
    return ruleset


def _index_overrides(
    overrides: Sequence[StoredComponent],
) -> dict[datetime, StoredComponent]:
    mapping: dict[datetime, StoredComponent] = {}
    for override in overrides:
        rid = override.ref.recurrence_id
        if rid is None:
            continue
        try:
            mapping[datetime.fromisoformat(rid).astimezone(UTC)] = override
        except ValueError:
            continue
    return mapping


def _extract_rules(
    raw_ics: bytes, uid: str
) -> tuple[str | None, list[datetime], list[datetime]]:
    try:
        cal = Calendar.from_ical(raw_ics)
    except ValueError:
        return None, [], []
    for sub in cal.walk():  # pyright: ignore[reportUnknownMemberType]
        name = _component_name(sub)
        if name not in ("VEVENT", "VTODO"):
            continue
        if _component_get_str(sub, "UID") != uid:
            continue
        if _component_has(sub, "RECURRENCE-ID"):
            continue
        return (
            _extract_rrule(sub),
            _extract_date_list(sub, "RDATE"),
            _extract_date_list(sub, "EXDATE"),
        )
    return None, [], []


def _extract_rrule(component: object) -> str | None:
    rrule_val = _component_get(component, "RRULE")
    if rrule_val is None:
        return None
    values: list[object] = (
        cast(list[object], rrule_val) if isinstance(rrule_val, list) else [rrule_val]
    )
    for value in values:
        to_ical = getattr(value, "to_ical", None)
        if to_ical is None:
            continue
        raw = to_ical()
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        if isinstance(raw, str):
            return raw
    return None


def _extract_date_list(component: object, key: str) -> list[datetime]:
    value = _component_get(component, key)
    if value is None:
        return []
    items: list[object] = (
        cast(list[object], value) if isinstance(value, list) else [value]
    )
    collected: list[datetime] = []
    for item in items:
        dts = getattr(item, "dts", None)
        if dts is None:
            continue
        for entry in cast(list[object], dts):
            dt = getattr(entry, "dt", None)
            if isinstance(dt, datetime):
                collected.append(_to_utc(dt))
            elif isinstance(dt, date):
                collected.append(datetime(dt.year, dt.month, dt.day, tzinfo=UTC))
    return collected


def _component_name(component: object) -> str:
    return str(getattr(component, "name", ""))


def _component_get(component: object, key: str) -> object:
    getter = getattr(component, "get", None)
    if getter is None:
        return None
    return getter(key)


def _component_get_str(component: object, key: str) -> str | None:
    value = _component_get(component, key)
    if value is None:
        return None
    return str(value)


def _component_has(component: object, key: str) -> bool:
    contains = getattr(component, "__contains__", None)
    if contains is None:
        return False
    return bool(contains(key))


__all__ = [
    "MAX_OCCURRENCES",
    "RecurrenceExpansionError",
    "expand",
    "populate_occurrences",
]
