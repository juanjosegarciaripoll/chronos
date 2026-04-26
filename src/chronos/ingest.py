"""ICS file ingestion into the local mirror + index.

`ingest_ics_bytes` is the single entry point used by `cli.cmd_import`
and the MCP `import_ics` tool.  Each VEVENT/VTODO group (a single UID,
including any RECURRENCE-ID overrides) becomes one .ics file in the
mirror with href=NULL, so the next `chronos sync` pushes it to the
server.

Additive only: this module has no delete path.  VJOURNAL and VFREEBUSY
are rejected; callers receive `IngestError` for structural problems and
per-component failures inside `IngestReport.details`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from icalendar import Calendar as IcalCalendar

from chronos.domain import CalendarRef, ComponentRef, ResourceRef
from chronos.ical_parser import IcalParseError, parse_vcalendar
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
    """Parse *payload* and write each VEVENT/VTODO group into mirror + index.

    Groups are keyed by UID.  A VCALENDAR with a master event and its
    RECURRENCE-ID overrides all sharing the same UID is written as a
    single .ics file (matching the layout the sync engine produces).

    On collision (`on_conflict`):
    - ``"skip"``    — leave the existing component untouched.
    - ``"replace"`` — overwrite the mirror file and index row; if the
                      existing component has an href, the next sync will
                      PUT the update.
    - ``"rename"``  — assign a fresh UID and ingest as a new component.

    Raises `IngestError` for malformed input or unsupported component
    types.  Per-UID failures are recorded in ``IngestReport.details``
    and do not abort the run.
    """
    uid_to_ics = _split_by_uid(payload)
    now = datetime.now(UTC)
    imported = skipped = replaced = renamed = 0
    details: list[str] = []

    for uid, ics_bytes in uid_to_ics.items():
        comp_ref = ComponentRef(
            account_name=target.account_name,
            calendar_name=target.calendar_name,
            uid=uid,
        )
        existing = index.get_component(comp_ref)

        if existing is not None and on_conflict == "skip":
            details.append(f"{uid}: skipped (already exists)")
            skipped += 1
            continue

        effective_uid = uid
        effective_ics = ics_bytes

        if existing is not None and on_conflict == "rename":
            effective_uid = _fresh_uid()
            try:
                effective_ics = _rewrite_uid(ics_bytes, uid, effective_uid)
            except IngestError as exc:
                details.append(f"{uid}: rename failed: {exc}")
                continue
            details.append(f"{uid}: renamed to {effective_uid}")

        try:
            parsed = parse_vcalendar(effective_ics)
        except IcalParseError as exc:
            details.append(f"{uid}: parse error: {exc}")
            continue
        if not parsed:
            details.append(f"{uid}: no components after split")
            continue

        res_ref = ResourceRef(
            account_name=target.account_name,
            calendar_name=target.calendar_name,
            uid=effective_uid,
        )
        mirror.write(res_ref, effective_ics)

        with index.connection():
            for pc in parsed:
                c_ref = ComponentRef(
                    account_name=target.account_name,
                    calendar_name=target.calendar_name,
                    uid=pc.uid or effective_uid,
                    recurrence_id=pc.recurrence_id,
                )
                stored = build_stored_component(c_ref, effective_ics, pc, now)
                index.upsert_component(stored)

        if existing is not None and on_conflict == "replace":
            replaced += 1
        elif existing is not None and on_conflict == "rename":
            renamed += 1
        else:
            imported += 1

    return IngestReport(
        imported=imported,
        skipped=skipped,
        replaced=replaced,
        renamed=renamed,
        details=tuple(details),
    )


# Helpers ---------------------------------------------------------------------


def _split_by_uid(payload: bytes) -> dict[str, bytes]:
    """Parse *payload* and return a ``{uid: ics_bytes}`` map.

    VTIMEZONE components are copied into every per-UID calendar so that
    events with TZID references remain self-contained.  Raises
    `IngestError` if the payload is malformed or contains unsupported
    component types (VJOURNAL, VFREEBUSY).
    """
    try:
        cal = IcalCalendar.from_ical(payload)
    except ValueError as exc:
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

    return result


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
