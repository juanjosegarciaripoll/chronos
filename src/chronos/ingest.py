"""ICS file ingestion into the local mirror + index.

`ingest_ics_bytes` is the single entry point used by `cli.cmd_import`
and the MCP `import_ics` tool.  Each VEVENT/VTODO group (a single UID,
including any RECURRENCE-ID overrides) becomes one .ics file in the
mirror with href=NULL, so the next `chronos sync` pushes it to the
server.

iTIP-aware: the VCALENDAR ``METHOD`` is honored so that scheduling
messages (e.g. e-mail invitations) work the way a user expects:

- ``METHOD:CANCEL`` trashes the matching event (whole UID) or, when a
  ``RECURRENCE-ID`` is present, cancels just those instances by adding an
  ``EXDATE`` to the master and dropping the override.
- ``METHOD:REQUEST`` / ``PUBLISH`` (or no method) adds a new event, or
  updates an existing one in place when the incoming ``SEQUENCE`` is
  newer — regardless of ``on_conflict``, since dropping a genuine update
  is the bug this guards against.  Same-or-older SEQUENCE collisions
  fall back to ``on_conflict``.
- ``METHOD:REPLY`` (attendee RSVP) is out of scope and skipped.

Updates and cancellations of already-synced events (href set) are
flagged so the next `chronos sync` propagates them to the server: an
If-Match PUT for updates (``LOCAL_FLAG_DIRTY``), a DELETE for
cancellations (``LocalStatus.TRASHED``).  VJOURNAL and VFREEBUSY are
rejected; callers receive `IngestError` for structural problems and
per-component failures inside `IngestReport.details`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from icalendar import Calendar as IcalCalendar

from chronos.domain import (
    LOCAL_FLAG_DIRTY,
    CalendarRef,
    ComponentRef,
    ResourceRef,
    StoredComponent,
)
from chronos.ical_parser import (
    IcalParseError,
    ParsedComponent,
    parse_method,
    parse_recurrence_id,
    parse_vcalendar,
)
from chronos.mutations import trashed_copy
from chronos.protocols import IndexRepository, MirrorRepository
from chronos.storage_indexing import build_stored_component

# icalendar ships without type stubs; interactions with its API use
# `Any` annotations or targeted `type: ignore` to satisfy mypy strict.
_IC = IcalCalendar

OnConflict = Literal["skip", "replace", "rename"]

_UNSUPPORTED_TYPES = frozenset({"VJOURNAL", "VFREEBUSY"})


class IngestError(ValueError):
    pass


@dataclass(frozen=True, kw_only=True)
class IngestReport:
    imported: int
    updated: int
    cancelled: int
    skipped: int
    replaced: int
    renamed: int
    details: tuple[str, ...]


def ingest_ics_bytes(
    payload: bytes,
    *,
    target: CalendarRef,
    mirror: MirrorRepository,
    index: IndexRepository,
    on_conflict: OnConflict = "skip",
) -> IngestReport:
    """Parse *payload* and apply each VEVENT/VTODO group to mirror + index.

    Groups are keyed by UID.  A VCALENDAR with a master event and its
    RECURRENCE-ID overrides all sharing the same UID is one .ics file
    (matching the layout the sync engine produces).

    Behavior depends on the VCALENDAR ``METHOD`` and, for updates, on
    ``SEQUENCE`` (see the module docstring).  ``on_conflict`` governs
    only same-or-older-SEQUENCE collisions of an add/update message:

    - ``"skip"``    — leave the existing component untouched.
    - ``"replace"`` — overwrite the mirror file and index row; an
                      already-synced component is queued for an If-Match
                      PUT on the next sync.
    - ``"rename"``  — assign a fresh UID and ingest as a new component.

    Raises `IngestError` for malformed input or unsupported component
    types.  Per-UID failures are recorded in ``IngestReport.details``
    and do not abort the run.
    """
    method, uid_to_ics = _split_by_uid(payload)
    now = datetime.now(UTC)

    counts = {
        "imported": 0,
        "updated": 0,
        "cancelled": 0,
        "skipped": 0,
        "replaced": 0,
        "renamed": 0,
    }
    details: list[str] = []

    for uid, ics_bytes in uid_to_ics.items():
        # A CANCEL/REQUEST references an existing event by its (globally
        # unique) UID, so locate it wherever it lives rather than only in
        # the selected `target` calendar — otherwise a cancellation
        # imported into the "wrong" calendar would silently do nothing.
        effective_target, existing_rows = _resolve_existing(index, uid, target)

        try:
            parsed = parse_vcalendar(ics_bytes)
        except IcalParseError as exc:
            details.append(f"{uid}: parse error: {exc}")
            continue
        if not parsed:
            details.append(f"{uid}: no components after split")
            continue

        if method == "CANCEL":
            _apply_cancellation(
                uid=uid,
                parsed=parsed,
                existing_rows=existing_rows,
                target=effective_target,
                mirror=mirror,
                index=index,
                now=now,
                counts=counts,
                details=details,
            )
            continue

        if method == "REPLY":
            details.append(f"{uid}: METHOD:REPLY (attendee RSVP) is not supported")
            counts["skipped"] += 1
            continue

        _apply_add_or_update(
            uid=uid,
            ics_bytes=ics_bytes,
            parsed=parsed,
            existing_rows=existing_rows,
            target=effective_target,
            mirror=mirror,
            index=index,
            now=now,
            on_conflict=on_conflict,
            counts=counts,
            details=details,
        )

    return IngestReport(
        imported=counts["imported"],
        updated=counts["updated"],
        cancelled=counts["cancelled"],
        skipped=counts["skipped"],
        replaced=counts["replaced"],
        renamed=counts["renamed"],
        details=tuple(details),
    )


# Add / update -----------------------------------------------------------------


def _apply_add_or_update(
    *,
    uid: str,
    ics_bytes: bytes,
    parsed: list[ParsedComponent],
    existing_rows: list[StoredComponent],
    target: CalendarRef,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime,
    on_conflict: OnConflict,
    counts: dict[str, int],
    details: list[str],
) -> None:
    existing_master = _master_of(existing_rows)

    if existing_master is None:
        _store_resource(
            uid=uid,
            ics=ics_bytes,
            parsed=parsed,
            existing_rows=[],
            target=target,
            mirror=mirror,
            index=index,
            now=now,
        )
        counts["imported"] += 1
        return

    is_newer = _master_seq(parsed) > _stored_seq(existing_master)

    if not is_newer:
        if on_conflict == "skip":
            details.append(f"{uid}: skipped (already exists)")
            counts["skipped"] += 1
            return
        if on_conflict == "rename":
            new_uid = _fresh_uid()
            try:
                new_ics = _rewrite_uid(ics_bytes, uid, new_uid)
                renamed_parsed = parse_vcalendar(new_ics)
            except (IngestError, IcalParseError) as exc:
                details.append(f"{uid}: rename failed: {exc}")
                return
            _store_resource(
                uid=new_uid,
                ics=new_ics,
                parsed=renamed_parsed,
                existing_rows=[],
                target=target,
                mirror=mirror,
                index=index,
                now=now,
            )
            details.append(f"{uid}: renamed to {new_uid}")
            counts["renamed"] += 1
            return
        # on_conflict == "replace": forced overwrite.
        _store_resource(
            uid=uid,
            ics=ics_bytes,
            parsed=parsed,
            existing_rows=existing_rows,
            target=target,
            mirror=mirror,
            index=index,
            now=now,
        )
        counts["replaced"] += 1
        return

    # Genuine update — incoming SEQUENCE is newer.
    _store_resource(
        uid=uid,
        ics=ics_bytes,
        parsed=parsed,
        existing_rows=existing_rows,
        target=target,
        mirror=mirror,
        index=index,
        now=now,
    )
    counts["updated"] += 1


def _store_resource(
    *,
    uid: str,
    ics: bytes,
    parsed: list[ParsedComponent],
    existing_rows: list[StoredComponent],
    target: CalendarRef,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime,
) -> None:
    """Write *ics* to the mirror and (re)build the index rows for *uid*.

    When *existing_rows* describes an already-synced resource (master has
    an href), the new rows inherit its href/etag and gain
    ``LOCAL_FLAG_DIRTY`` so the next sync issues an If-Match PUT.  Stale
    rows for the UID are deleted first so dropped RECURRENCE-ID overrides
    don't linger.
    """
    res_ref = ResourceRef(target.account_name, target.calendar_name, uid)
    mirror.write(res_ref, ics)
    master_existing = _master_of(existing_rows)
    synced = master_existing is not None and master_existing.href is not None

    with index.connection():
        for old in existing_rows:
            index.delete_component(old.ref)
        for pc in parsed:
            c_ref = ComponentRef(
                account_name=target.account_name,
                calendar_name=target.calendar_name,
                uid=pc.uid or uid,
                recurrence_id=pc.recurrence_id,
            )
            stored = build_stored_component(c_ref, ics, pc, now)
            if synced:
                assert master_existing is not None
                stored = dataclasses.replace(
                    stored,
                    href=master_existing.href,
                    etag=master_existing.etag,
                    local_flags=stored.local_flags | {LOCAL_FLAG_DIRTY},
                )
            index.upsert_component(stored)


# Cancellation -----------------------------------------------------------------


def _apply_cancellation(
    *,
    uid: str,
    parsed: list[ParsedComponent],
    existing_rows: list[StoredComponent],
    target: CalendarRef,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime,
    counts: dict[str, int],
    details: list[str],
) -> None:
    cancelled_rids = {pc.recurrence_id for pc in parsed if pc.recurrence_id}

    if not existing_rows:
        details.append(f"{uid}: cancellation ignored (no matching event)")
        counts["skipped"] += 1
        return

    if cancelled_rids:
        _cancel_instances(
            uid=uid,
            cancelled_rids=cancelled_rids,
            existing_rows=existing_rows,
            target=target,
            mirror=mirror,
            index=index,
            counts=counts,
            details=details,
        )
        return

    # Whole-event cancellation.
    res_ref = ResourceRef(target.account_name, target.calendar_name, uid)
    master = _master_of(existing_rows)
    synced = master is not None and master.href is not None
    with index.connection():
        for row in existing_rows:
            if synced:
                index.upsert_component(trashed_copy(row, trashed_at=now))
            else:
                index.delete_component(row.ref)
    if not synced:
        # Never reached the server, so there is nothing to DELETE remotely;
        # drop the mirror file directly.  (Synced events keep their file
        # until `_push_trashed` confirms the server DELETE.)
        with contextlib.suppress(FileNotFoundError):
            mirror.delete(res_ref)
    counts["cancelled"] += 1
    details.append(f"{uid}: cancelled in {target.calendar_name}")


def _cancel_instances(
    *,
    uid: str,
    cancelled_rids: set[str],
    existing_rows: list[StoredComponent],
    target: CalendarRef,
    mirror: MirrorRepository,
    index: IndexRepository,
    counts: dict[str, int],
    details: list[str],
) -> None:
    master = _master_of(existing_rows)
    if master is None or master.ref.recurrence_id is not None:
        # No master to anchor an EXDATE on: just drop matching overrides.
        removed = [r for r in existing_rows if r.ref.recurrence_id in cancelled_rids]
        if not removed:
            details.append(f"{uid}: cancellation ignored (no matching instance)")
            counts["skipped"] += 1
            return
        with index.connection():
            for row in removed:
                index.delete_component(row.ref)
        counts["cancelled"] += 1
        details.append(f"{uid}: cancelled {len(removed)} instance(s)")
        return

    try:
        new_ics = _exclude_instances(master.raw_ics, uid, cancelled_rids)
    except IngestError as exc:
        details.append(f"{uid}: instance cancellation failed: {exc}")
        return

    res_ref = ResourceRef(target.account_name, target.calendar_name, uid)
    mirror.write(res_ref, new_ics)
    synced = master.href is not None
    with index.connection():
        for row in existing_rows:
            if row.ref.recurrence_id in cancelled_rids:
                index.delete_component(row.ref)
                continue
            flags = row.local_flags | {LOCAL_FLAG_DIRTY} if synced else row.local_flags
            index.upsert_component(
                dataclasses.replace(row, raw_ics=new_ics, local_flags=flags)
            )
    counts["cancelled"] += 1
    details.append(f"{uid}: cancelled {len(cancelled_rids)} instance(s)")


def _exclude_instances(raw: bytes, uid: str, rids: set[str]) -> bytes:
    """Return *raw* with each *rids* occurrence excluded from the *uid* master.

    Adds an EXDATE to the master for every cancelled RECURRENCE-ID and
    drops any override subcomponent for those instances.  Other
    components (the master, surviving overrides, VTIMEZONEs) are copied
    through unchanged.
    """
    try:
        cal = IcalCalendar.from_ical(raw)
    except ValueError as exc:
        raise IngestError(f"cannot edit resource: {exc}") from exc

    new_cal: Any = _IC()  # type: ignore[no-untyped-call]
    new_cal.add("prodid", "-//chronos//EN")
    new_cal.add("version", "2.0")

    for sub in cal.subcomponents:
        name = getattr(sub, "name", "")
        if name not in ("VEVENT", "VTODO"):
            new_cal.add_component(sub)
            continue
        sub_uid = str(sub.get("UID") or "").strip()  # type: ignore[no-untyped-call]
        rid = parse_recurrence_id(sub)
        if sub_uid == uid and rid is None:
            for rid_iso in sorted(rids):
                try:
                    sub.add("EXDATE", datetime.fromisoformat(rid_iso))  # pyright: ignore[reportUnknownMemberType]
                except ValueError:
                    continue
            new_cal.add_component(sub)
        elif sub_uid == uid and rid in rids:
            continue  # drop the cancelled override
        else:
            new_cal.add_component(sub)

    return bytes(new_cal.to_ical())


# Helpers ---------------------------------------------------------------------


def _resolve_existing(
    index: IndexRepository, uid: str, target: CalendarRef
) -> tuple[CalendarRef, list[StoredComponent]]:
    """Find the calendar an existing *uid* lives in and its component rows.

    UIDs are globally unique, so an iTIP CANCEL/REQUEST applies to the
    calendar that already holds the event regardless of the user-selected
    *target*.  Returns ``(target, [])`` for a brand-new UID (an add goes
    to the selected calendar).  In the rare case the UID exists in more
    than one calendar, the selected *target* wins if it is one of them.
    """
    rows = list(index.list_components_by_uid(uid))
    master = _master_of(rows)
    if master is None:
        return target, []
    calendars = {r.ref.calendar for r in rows}
    chosen = target if target in calendars else master.ref.calendar
    same = [r for r in rows if r.ref.calendar == chosen]
    return chosen, same


def _master_of(rows: list[StoredComponent]) -> StoredComponent | None:
    if not rows:
        return None
    return next((r for r in rows if r.ref.recurrence_id is None), rows[0])


def _master_seq(parsed: list[ParsedComponent]) -> int:
    master = next((p for p in parsed if p.recurrence_id is None), None)
    if master is None and parsed:
        master = parsed[0]
    return master.sequence or 0 if master is not None else 0


def _stored_seq(component: StoredComponent | None) -> int:
    if component is None or not component.raw_ics:
        return 0
    try:
        parsed = parse_vcalendar(component.raw_ics)
    except IcalParseError:
        return 0
    return _master_seq(parsed)


def _split_by_uid(payload: bytes) -> tuple[str | None, dict[str, bytes]]:
    """Parse *payload* into ``(method, {uid: ics_bytes})``.

    ``method`` is the upper-cased VCALENDAR ``METHOD`` (or ``None``).
    VTIMEZONE components are copied into every per-UID calendar so that
    events with TZID references remain self-contained.  Raises
    `IngestError` if the payload is malformed or contains unsupported
    component types (VJOURNAL, VFREEBUSY).
    """
    try:
        cal = IcalCalendar.from_ical(payload)
    except ValueError as exc:
        raise IngestError(f"invalid iCalendar data: {exc}") from exc

    try:
        method = parse_method(payload)
    except IcalParseError as exc:
        raise IngestError(f"invalid iCalendar data: {exc}") from exc

    # icalendar ships with minimal stubs; use Any for all component objects.
    vtimezones: list[Any] = []
    groups: dict[str, list[Any]] = {}

    for item in cal.subcomponents:
        name: str = getattr(item, "name", "")
        if name == "VTIMEZONE":
            vtimezones.append(item)
        elif name in _UNSUPPORTED_TYPES:
            raise IngestError(
                f"{name} components are not supported (see ai/SPECIFICATIONS.md §4)"
            )
        elif name in ("VEVENT", "VTODO"):
            uid = str(item.get("UID") or "").strip()  # type: ignore[no-untyped-call]
            if not uid:
                uid = _fresh_uid()
                item.add("UID", uid)  # pyright: ignore[reportUnknownMemberType]
            groups.setdefault(uid, []).append(item)

    if not groups:
        raise IngestError("no VEVENT or VTODO components found in payload")

    result: dict[str, bytes] = {}
    for uid, components in groups.items():
        new_cal: Any = _IC()  # type: ignore[no-untyped-call]
        new_cal.add("prodid", "-//chronos//EN")
        new_cal.add("version", "2.0")
        for tz in vtimezones:
            new_cal.add_component(tz)
        for comp in components:
            new_cal.add_component(comp)
        result[uid] = bytes(new_cal.to_ical())

    return method, result


def _rewrite_uid(ics_bytes: bytes, old_uid: str, new_uid: str) -> bytes:
    """Return *ics_bytes* with every occurrence of *old_uid* replaced by *new_uid*."""
    try:
        cal = IcalCalendar.from_ical(ics_bytes)
    except ValueError as exc:
        raise IngestError(f"cannot rewrite UID: {exc}") from exc
    for sub in cal.walk():  # pyright: ignore[reportUnknownMemberType]
        uid_val: str = str(sub.get("UID") or "").strip()  # type: ignore[no-untyped-call]
        if sub.name in ("VEVENT", "VTODO") and uid_val == old_uid:
            del sub["UID"]
            sub.add("UID", new_uid)  # pyright: ignore[reportUnknownMemberType]
    return bytes(cal.to_ical())


def _fresh_uid() -> str:
    return f"{uuid.uuid4().hex[:16]}@chronos"


__all__ = [
    "IngestError",
    "IngestReport",
    "OnConflict",
    "ingest_ics_bytes",
]
