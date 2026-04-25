from __future__ import annotations

import contextlib
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from chronos.domain import (
    AccountConfig,
    CalendarConfig,
    CalendarRef,
    ComponentKind,
    ComponentRef,
    LocalStatus,
    RemoteCalendar,
    ResourceRef,
    StoredComponent,
    SyncResult,
    SyncState,
    VEvent,
    VTodo,
)
from chronos.ical_parser import IcalParseError, ParsedComponent, parse_vcalendar
from chronos.protocols import CalDAVSession, IndexRepository, MirrorRepository
from chronos.storage_indexing import synthetic_uid

_MASS_DELETION_RATIO = 0.2
_MASS_DELETION_MIN_BASELINE = 5


class SyncError(Exception):
    pass


class SyncHaltError(SyncError):
    """Raised when the engine refuses to proceed without user confirmation."""


@dataclass(frozen=True, kw_only=True)
class CalendarSyncStats:
    calendar: CalendarRef
    path: Literal["fast", "slow"]
    added: int = 0
    updated: int = 0
    removed: int = 0
    pushed: int = 0
    deleted_remote: int = 0
    errors: tuple[str, ...] = ()


def sync_account(
    *,
    account: AccountConfig,
    session: CalDAVSession,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime | None = None,
) -> SyncResult:
    clock = now or datetime.now(UTC)
    principal_url = session.discover_principal()
    remote_calendars = session.list_calendars(principal_url)
    scoped = _scoped_calendars(remote_calendars, account)

    per_calendar_stats: list[CalendarSyncStats] = []
    errors: list[str] = []
    if remote_calendars and not scoped:
        # Distinguishes the silent "your include / exclude regex matched
        # nothing" case from "the server has no calendars at all". Without
        # this the user sees a 0-calendar success and is stumped.
        names = ", ".join(sorted(c.name for c in remote_calendars))
        include_patterns = [p.pattern for p in account.include]
        exclude_patterns = [p.pattern for p in account.exclude]
        errors.append(
            f"server has {len(remote_calendars)} calendar(s) "
            f"({names}) but none matched include={include_patterns} / "
            f"exclude={exclude_patterns}"
        )
    for remote in scoped:
        calendar = _resolve_calendar(account, remote)
        try:
            stats = _sync_calendar(
                account=account,
                calendar=calendar,
                session=session,
                mirror=mirror,
                index=index,
                now=clock,
            )
        except SyncHaltError as exc:
            errors.append(f"{calendar.calendar_name}: {exc}")
            continue
        per_calendar_stats.append(stats)
        errors.extend(stats.errors)

    return SyncResult(
        account_name=account.name,
        calendars_synced=len(per_calendar_stats),
        components_added=sum(s.added for s in per_calendar_stats),
        components_updated=sum(s.updated for s in per_calendar_stats),
        components_removed=sum(s.removed for s in per_calendar_stats),
        errors=tuple(errors),
    )


def _sync_calendar(
    *,
    account: AccountConfig,
    calendar: CalendarConfig,
    session: CalDAVSession,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime,
) -> CalendarSyncStats:
    calendar_ref = CalendarRef(account.name, calendar.calendar_name)
    prior_state = index.get_sync_state(calendar_ref)
    server_ctag = session.get_ctag(calendar.url)

    if _ctag_matches(prior_state, server_ctag):
        stats = _fast_path_reconcile(
            account=account,
            calendar=calendar,
            session=session,
            mirror=mirror,
            index=index,
            now=now,
        )
    else:
        stats = _slow_path_reconcile(
            account=account,
            calendar=calendar,
            session=session,
            mirror=mirror,
            index=index,
            now=now,
        )

    # CTag can have advanced because of our own pushes; re-read after mutations.
    final_ctag = (
        session.get_ctag(calendar.url)
        if stats.path == "slow" or (stats.pushed or stats.deleted_remote)
        else server_ctag
    )

    index.set_sync_state(
        SyncState(
            calendar=calendar_ref,
            ctag=final_ctag,
            sync_token=None,
            synced_at=now,
        )
    )
    return stats


def _ctag_matches(prior_state: SyncState | None, server_ctag: str | None) -> bool:
    return (
        prior_state is not None
        and prior_state.ctag is not None
        and server_ctag is not None
        and prior_state.ctag == server_ctag
    )


def _fast_path_reconcile(
    *,
    account: AccountConfig,
    calendar: CalendarConfig,
    session: CalDAVSession,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime,
) -> CalendarSyncStats:
    calendar_ref = CalendarRef(account.name, calendar.calendar_name)
    if calendar.read_only:
        return CalendarSyncStats(calendar=calendar_ref, path="fast")
    deleted = _push_trashed(
        account=account,
        calendar=calendar,
        session=session,
        mirror=mirror,
        index=index,
    )
    pushed = _push_pending(
        account=account,
        calendar=calendar,
        session=session,
        index=index,
        now=now,
    )
    return CalendarSyncStats(
        calendar=calendar_ref,
        path="fast",
        pushed=pushed,
        deleted_remote=deleted,
    )


def _slow_path_reconcile(
    *,
    account: AccountConfig,
    calendar: CalendarConfig,
    session: CalDAVSession,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime,
) -> CalendarSyncStats:
    calendar_ref = CalendarRef(account.name, calendar.calendar_name)
    server_resources = session.calendar_query(calendar.url)
    server_map: dict[str, str] = dict(server_resources)

    local_components = index.list_calendar_components(calendar_ref)
    local_by_href: dict[str, StoredComponent] = {}
    for component in local_components:
        if component.href is not None:
            local_by_href.setdefault(component.href, component)

    removed_hrefs = set(local_by_href) - set(server_map)
    _guard_mass_deletion(
        calendar_ref=calendar_ref,
        baseline=len(local_by_href),
        removed=len(removed_hrefs),
    )

    errors: list[str] = []
    removed = _apply_server_deletions(
        removed_hrefs=removed_hrefs,
        local_by_href=local_by_href,
        local_components=local_components,
        mirror=mirror,
        index=index,
    )

    new_hrefs = [h for h in server_map if h not in local_by_href]
    added = _fetch_and_ingest(
        account=account,
        calendar=calendar,
        session=session,
        mirror=mirror,
        index=index,
        hrefs=new_hrefs,
        now=now,
        errors=errors,
    )

    changed_hrefs = [
        href
        for href, etag in server_map.items()
        if href in local_by_href and local_by_href[href].etag != etag
    ]
    updated = _fetch_and_ingest(
        account=account,
        calendar=calendar,
        session=session,
        mirror=mirror,
        index=index,
        hrefs=changed_hrefs,
        now=now,
        errors=errors,
    )

    pushed = 0
    deleted_remote = 0
    if not calendar.read_only:
        deleted_remote = _push_trashed(
            account=account,
            calendar=calendar,
            session=session,
            mirror=mirror,
            index=index,
        )
        pushed = _push_pending(
            account=account,
            calendar=calendar,
            session=session,
            index=index,
            now=now,
        )

    return CalendarSyncStats(
        calendar=calendar_ref,
        path="slow",
        added=added,
        updated=updated,
        removed=removed,
        pushed=pushed,
        deleted_remote=deleted_remote,
        errors=tuple(errors),
    )


def _guard_mass_deletion(
    *, calendar_ref: CalendarRef, baseline: int, removed: int
) -> None:
    if baseline <= _MASS_DELETION_MIN_BASELINE:
        return
    if removed > _MASS_DELETION_RATIO * baseline:
        raise SyncHaltError(
            f"{calendar_ref.calendar_name}: {removed}/{baseline} resources "
            f"missing from server (>{int(_MASS_DELETION_RATIO * 100)}%). "
            "Refusing to delete locally without confirmation."
        )


def _apply_server_deletions(
    *,
    removed_hrefs: set[str],
    local_by_href: dict[str, StoredComponent],
    local_components: Sequence[StoredComponent],
    mirror: MirrorRepository,
    index: IndexRepository,
) -> int:
    if not removed_hrefs:
        return 0
    removed = 0
    seen_resources: set[ResourceRef] = set()
    for href in removed_hrefs:
        anchor = local_by_href[href]
        resource = anchor.ref.resource
        # Delete every index row whose href matches (master + overrides share href).
        for component in local_components:
            if component.href == href:
                index.delete_component(component.ref)
                removed += 1
        if resource not in seen_resources:
            with contextlib.suppress(FileNotFoundError):
                mirror.delete(resource)
            seen_resources.add(resource)
    return removed


def _fetch_and_ingest(
    *,
    account: AccountConfig,
    calendar: CalendarConfig,
    session: CalDAVSession,
    mirror: MirrorRepository,
    index: IndexRepository,
    hrefs: Sequence[str],
    now: datetime,
    errors: list[str],
) -> int:
    if not hrefs:
        return 0
    fetched = session.calendar_multiget(calendar.url, list(hrefs))
    count = 0
    for href, etag, ics in fetched:
        try:
            count += _ingest_resource(
                account=account,
                calendar=calendar,
                href=href,
                etag=etag,
                ics=ics,
                mirror=mirror,
                index=index,
                now=now,
            )
        except IcalParseError as exc:
            errors.append(f"{href}: {exc}")
    return count


def _ingest_resource(
    *,
    account: AccountConfig,
    calendar: CalendarConfig,
    href: str,
    etag: str,
    ics: bytes,
    mirror: MirrorRepository,
    index: IndexRepository,
    now: datetime,
) -> int:
    parsed = parse_vcalendar(ics)
    if not parsed:
        return 0
    resource_uid = _primary_uid(account, calendar, ics, parsed)
    resource_ref = ResourceRef(
        account_name=account.name,
        calendar_name=calendar.calendar_name,
        uid=resource_uid,
    )
    mirror.write(resource_ref, ics)
    count = 0
    with index.connection():
        for component in parsed:
            comp_ref = ComponentRef(
                account_name=account.name,
                calendar_name=calendar.calendar_name,
                uid=component.uid or resource_uid,
                recurrence_id=component.recurrence_id,
            )
            index.upsert_component(
                _build_stored(comp_ref, ics, component, href, etag, now)
            )
            count += 1
    return count


def _primary_uid(
    account: AccountConfig,
    calendar: CalendarConfig,
    ics: bytes,
    parsed: list[ParsedComponent],
) -> str:
    for component in parsed:
        if component.uid is not None:
            return component.uid
    return synthetic_uid(account.name, calendar.calendar_name, ics)


def _build_stored(
    ref: ComponentRef,
    raw_ics: bytes,
    parsed: ParsedComponent,
    href: str,
    etag: str,
    synced_at: datetime,
) -> StoredComponent:
    if parsed.kind == ComponentKind.VEVENT:
        return VEvent(
            ref=ref,
            href=href,
            etag=etag,
            raw_ics=raw_ics,
            summary=parsed.summary,
            description=parsed.description,
            location=parsed.location,
            dtstart=parsed.dtstart,
            dtend=parsed.dtend,
            status=parsed.status,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=synced_at,
        )
    return VTodo(
        ref=ref,
        href=href,
        etag=etag,
        raw_ics=raw_ics,
        summary=parsed.summary,
        description=parsed.description,
        location=parsed.location,
        dtstart=parsed.dtstart,
        due=parsed.due,
        status=parsed.status,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=synced_at,
    )


def _push_trashed(
    *,
    account: AccountConfig,
    calendar: CalendarConfig,
    session: CalDAVSession,
    mirror: MirrorRepository,
    index: IndexRepository,
) -> int:
    calendar_ref = CalendarRef(account.name, calendar.calendar_name)
    trashed = [
        c
        for c in index.list_calendar_components(calendar_ref)
        if c.local_status == LocalStatus.TRASHED
    ]
    hrefs_done: set[str] = set()
    purge_local_only: list[ComponentRef] = []
    deleted_remote = 0
    for component in trashed:
        if component.href is None:
            purge_local_only.append(component.ref)
            continue
        if component.href in hrefs_done:
            continue
        etag = component.etag
        if etag is None:
            # Locally trashed without server identity: purge locally only.
            purge_local_only.append(component.ref)
            continue
        try:
            session.delete(component.href, etag)
        except Exception:  # noqa: BLE001 — any server error means retry next sync
            continue
        hrefs_done.add(component.href)
        deleted_remote += 1

    # Purge local rows + mirror files for everything we either deleted on the
    # server or that was never synced in the first place.
    with index.connection():
        for ref in purge_local_only:
            index.delete_component(ref)
            with contextlib.suppress(FileNotFoundError):
                mirror.delete(ref.resource)
        for component in trashed:
            if component.href in hrefs_done:
                index.delete_component(component.ref)
        # Mirror files keyed by UID; dedupe per resource.
        resources_cleared: set[ResourceRef] = set()
        for component in trashed:
            if component.href in hrefs_done:
                if component.ref.resource in resources_cleared:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    mirror.delete(component.ref.resource)
                resources_cleared.add(component.ref.resource)

    return deleted_remote


def _push_pending(
    *,
    account: AccountConfig,
    calendar: CalendarConfig,
    session: CalDAVSession,
    index: IndexRepository,
    now: datetime,
) -> int:
    calendar_ref = CalendarRef(account.name, calendar.calendar_name)
    pending = index.list_pending_pushes(calendar_ref)
    if not pending:
        return 0

    by_uid: dict[str, list[StoredComponent]] = {}
    for component in pending:
        by_uid.setdefault(component.ref.uid, []).append(component)

    pushed = 0
    for uid, rows in by_uid.items():
        master = next((r for r in rows if r.ref.recurrence_id is None), rows[0])
        if master.raw_ics == b"":
            continue
        target_href = _compute_href(calendar.url, uid)
        try:
            new_etag = session.put(target_href, master.raw_ics, etag=None)
        except Exception:  # noqa: BLE001 — treat any PUT failure as retry next sync
            continue
        with index.connection():
            for row in rows:
                updated = _with_server_metadata(
                    row, href=target_href, etag=new_etag, synced_at=now
                )
                index.upsert_component(updated)
        pushed += 1
    return pushed


def _with_server_metadata(
    component: StoredComponent,
    *,
    href: str,
    etag: str,
    synced_at: datetime,
) -> StoredComponent:
    if isinstance(component, VEvent):
        return VEvent(
            ref=component.ref,
            href=href,
            etag=etag,
            raw_ics=component.raw_ics,
            summary=component.summary,
            description=component.description,
            location=component.location,
            dtstart=component.dtstart,
            dtend=component.dtend,
            status=component.status,
            local_flags=component.local_flags,
            server_flags=component.server_flags,
            local_status=component.local_status,
            trashed_at=component.trashed_at,
            synced_at=synced_at,
        )
    return VTodo(
        ref=component.ref,
        href=href,
        etag=etag,
        raw_ics=component.raw_ics,
        summary=component.summary,
        description=component.description,
        location=component.location,
        dtstart=component.dtstart,
        due=component.due,
        status=component.status,
        local_flags=component.local_flags,
        server_flags=component.server_flags,
        local_status=component.local_status,
        trashed_at=component.trashed_at,
        synced_at=synced_at,
    )


def _compute_href(calendar_url: str, uid: str) -> str:
    safe = urllib.parse.quote(uid, safe="")
    base = calendar_url.rstrip("/")
    return f"{base}/{safe}.ics"


def _scoped_calendars(
    remote_calendars: Sequence[RemoteCalendar], account: AccountConfig
) -> tuple[RemoteCalendar, ...]:
    scoped: list[RemoteCalendar] = []
    for calendar in remote_calendars:
        if not any(p.fullmatch(calendar.name) for p in account.include):
            continue
        if any(p.fullmatch(calendar.name) for p in account.exclude):
            continue
        scoped.append(calendar)
    return tuple(scoped)


def _resolve_calendar(account: AccountConfig, remote: RemoteCalendar) -> CalendarConfig:
    read_only = any(p.fullmatch(remote.name) for p in account.read_only)
    return CalendarConfig(
        account_name=account.name,
        calendar_name=remote.name,
        url=remote.url,
        read_only=read_only,
        supported_components=remote.supported_components,
    )


__all__ = [
    "CalendarSyncStats",
    "SyncError",
    "SyncHaltError",
    "sync_account",
]
