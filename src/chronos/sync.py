from __future__ import annotations

import contextlib
import hashlib
import logging
import queue
import threading
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from chronos.caldav_client import CalDAVConflictError, CalDAVError
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
from chronos.recurrence import populate_occurrences
from chronos.storage_indexing import synthetic_uid

logger = logging.getLogger(__name__)

_MASS_DELETION_RATIO = 0.2
_MASS_DELETION_MIN_BASELINE = 5

# Window the `occurrences` cache covers after each sync. Wide enough to
# include historical events the user might want to browse, narrow enough
# that infinite-RRULE masters stay bounded by `recurrence.MAX_OCCURRENCES`.
_OCCURRENCE_WINDOW_PAST = timedelta(days=365 * 30)
_OCCURRENCE_WINDOW_FUTURE = timedelta(days=365 * 5)


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
    logger.info("sync account %s", account.name)
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
    total = len(scoped)
    for index_pos, remote in enumerate(scoped, start=1):
        calendar = _resolve_calendar(account, remote)
        logger.info(
            "[%s] (%d/%d) %s", account.name, index_pos, total, calendar.calendar_name
        )
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
        except CalDAVError as exc:
            # One bad calendar (e.g. a server quirk on a single
            # resource) shouldn't take down the rest of the account's
            # sync. Surface the failure as a per-calendar error and
            # carry on.
            logger.warning(
                "calendar %s/%s failed: %s",
                account.name,
                calendar.calendar_name,
                exc,
            )
            errors.append(f"{calendar.calendar_name}: {exc}")
            continue
        logger.info(
            "[%s] (%d/%d) %s done: path=%s +%d ~%d -%d push=%d trash=%d",
            account.name,
            index_pos,
            total,
            calendar.calendar_name,
            stats.path,
            stats.added,
            stats.updated,
            stats.removed,
            stats.pushed,
            stats.deleted_remote,
        )
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
    """Reconcile one calendar.

    **Resumability invariant** (load-bearing): `set_sync_state` is the
    *last* index write before a successful return, gated on
    `_slow_path_reconcile` / `_fast_path_reconcile` having returned
    without raising. If anything raises mid-sync (KeyboardInterrupt,
    network error, server 5xx), the prior CTag stays in place — so
    the next sync re-enters the slow path and reconverges. Anything
    already ingested before the interrupt is preserved (each
    `_ingest_resource` is its own SQLite transaction); whatever was
    in flight is rolled back. Do not move `set_sync_state` earlier
    or wrap it in the same transaction as the per-resource ingests.
    """
    calendar_ref = CalendarRef(account.name, calendar.calendar_name)
    prior_state = index.get_sync_state(calendar_ref)
    server_ctag = session.get_ctag(calendar.url)
    logger.debug(
        "%s/%s ctag=%s prior=%s",
        account.name,
        calendar.calendar_name,
        server_ctag,
        prior_state.ctag if prior_state else None,
    )

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

    # Refresh the recurrence-expansion cache for this calendar so the
    # TUI's view queries return rows. Skipped on a no-op fast path
    # (nothing pushed, nothing pulled, nothing trashed) since the cache
    # is still valid then.
    if (
        stats.path == "slow"
        or stats.added
        or stats.updated
        or stats.removed
        or stats.pushed
        or stats.deleted_remote
    ):
        populate_occurrences(
            index=index,
            calendar=calendar_ref,
            window_start=now - _OCCURRENCE_WINDOW_PAST,
            window_end=now + _OCCURRENCE_WINDOW_FUTURE,
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

    # When the server returns an empty etag (CalDAV gateways that omit
    # getetag, see caldav_client._MISSING_SERVER_ETAG), we can't compare
    # — trust the local copy and skip the refetch. CTag still drives
    # slow-path entry, so missed in-place modifications on such servers
    # are bounded by CTag granularity, not by every-sync churn.
    changed_hrefs = [
        href
        for href, etag in server_map.items()
        if href in local_by_href and etag and local_by_href[href].etag != etag
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


_INGEST_PROGRESS_INTERVAL = 50

# How many hrefs go into one `calendar-multiget` REPORT. Mirrors
# `caldav_client._MULTIGET_BATCH_SIZE` deliberately: the session
# already chunks internally, but the producer thread here pre-chunks
# so it can hand each chunk to the consumer as soon as the network
# returns rather than buffering the entire response set.
_FETCH_CHUNK_SIZE = 100

# Bound on the producer/consumer queue: at most this many fetched
# chunks sit between the network worker and the ingest loop. With
# `_FETCH_CHUNK_SIZE = 100` that's ~200 in-flight resources at peak;
# enough to keep the consumer busy across one network round-trip,
# small enough that a stalled consumer doesn't pile up megabytes of
# ICS in memory.
_FETCH_PIPELINE_BUFFER = 2


# Sentinel returned to the consumer when the producer finishes
# successfully. Anything else off the queue is either an exception
# (re-raise on the consumer thread) or a chunk of fetched results.
_PRODUCER_DONE = object()


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
    """Stream chunks of `calendar-multiget` results into the local index.

    Pipelined: a daemon producer thread issues `calendar_multiget` per
    chunk and posts the results onto a bounded queue; the calling
    (consumer) thread drains the queue and runs `_ingest_resource`,
    which parses, writes the mirror file, and upserts SQLite rows.

    SQLite writes stay on the consumer thread (the only writer), so
    the existing `index.connection()` transactions and the per-
    resource atomicity guarantees from the crash-safety milestone
    carry over verbatim. The producer touches only the network
    session, which is single-threaded by virtue of being driven by
    one thread.

    Cancellation: if the consumer raises (KeyboardInterrupt, ingest
    bug, etc.) the `finally` block flips `cancel` and joins the
    producer. The producer checks `cancel` between chunks and on
    queue puts, so it returns within one outstanding network call.
    Producer-side exceptions are funnelled through the queue and
    re-raised on the consumer thread, preserving the previous
    "exception in multiget aborts the calendar's sync" semantics.
    """
    if not hrefs:
        return 0

    chunks = _chunk_hrefs(hrefs, _FETCH_CHUNK_SIZE)
    total = len(hrefs)
    total_chunks = len(chunks)

    if total_chunks > 1:
        logger.info(
            "fetching %d resources from %s (%d batches of up to %d)",
            total,
            calendar.url,
            total_chunks,
            _FETCH_CHUNK_SIZE,
        )
    if total >= _INGEST_PROGRESS_INTERVAL:
        logger.info("  ingesting %d resources locally...", total)

    chunk_queue: queue.Queue[object] = queue.Queue(maxsize=_FETCH_PIPELINE_BUFFER)
    cancel = threading.Event()

    def producer() -> None:
        fetched_so_far = 0
        try:
            for batch_index, chunk in enumerate(chunks, start=1):
                if cancel.is_set():
                    return
                results = session.calendar_multiget(calendar.url, list(chunk))
                fetched_so_far += len(chunk)
                if total_chunks > 1:
                    logger.info(
                        "  fetch batch %d/%d: %d/%d resources fetched",
                        batch_index,
                        total_chunks,
                        fetched_so_far,
                        total,
                    )
                if not _put_or_cancel(chunk_queue, results, cancel):
                    return
        except BaseException as exc:  # noqa: BLE001 — funnel to consumer
            _put_or_cancel(chunk_queue, exc, cancel)
            return
        _put_or_cancel(chunk_queue, _PRODUCER_DONE, cancel)

    producer_thread = threading.Thread(
        target=producer, name="chronos-multiget-producer", daemon=True
    )
    producer_thread.start()

    count = 0
    processed = 0
    try:
        while True:
            item = chunk_queue.get()
            if item is _PRODUCER_DONE:
                break
            if isinstance(item, BaseException):
                raise item
            chunk_results = cast(Sequence[tuple[str, str, bytes]], item)
            for href, etag, ics in chunk_results:
                processed += 1
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
                if (
                    total >= _INGEST_PROGRESS_INTERVAL
                    and processed % _INGEST_PROGRESS_INTERVAL == 0
                ):
                    logger.info("  ingest: %d/%d", processed, total)
    finally:
        cancel.set()
        # Drain the queue so a producer blocked on `put` can see the
        # cancel flag and exit promptly. We don't care about the items
        # we drop — they'd have been ingested otherwise, but we're
        # already on an error path or exiting cleanly.
        while True:
            try:
                chunk_queue.get_nowait()
            except queue.Empty:
                break
        producer_thread.join()

    if (
        total >= _INGEST_PROGRESS_INTERVAL
        and processed
        and processed % _INGEST_PROGRESS_INTERVAL
    ):
        logger.info("  ingest: %d/%d", processed, total)
    return count


def _chunk_hrefs(hrefs: Sequence[str], size: int) -> list[Sequence[str]]:
    return [hrefs[i : i + size] for i in range(0, len(hrefs), size)]


def _put_or_cancel(
    chunk_queue: queue.Queue[object], item: object, cancel: threading.Event
) -> bool:
    """Block-put `item` onto `chunk_queue`, returning early if cancelled.

    `queue.Queue.put` blocks indefinitely when the queue is full; that
    would wedge the producer if the consumer raised and stopped
    draining. Polling with a short timeout lets the producer notice
    cancellation between attempts.
    """
    while True:
        if cancel.is_set():
            return False
        try:
            chunk_queue.put(item, timeout=0.1)
            return True
        except queue.Full:
            continue


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
        except CalDAVConflictError:
            # 412 from `If-None-Match: *` means the resource already
            # exists at `target_href`. The most common cause is that
            # an earlier push succeeded server-side but its response
            # was lost (network drop, Ctrl-C between request and
            # response). Reconcile rather than burning every future
            # sync on a 412 retry loop.
            adopted = _adopt_existing_remote(
                session=session,
                calendar=calendar,
                target_href=target_href,
                local_ics=master.raw_ics,
            )
            if adopted is None:
                # Server has a *different* body at this UID, or we
                # can't read the resource for some reason. The next
                # slow-path sync will pull the remote version and
                # surface the conflict; don't overwrite blindly.
                logger.warning(
                    "push: %s/%s uid=%s already exists remotely with "
                    "different content; deferring to next slow sync",
                    account.name,
                    calendar.calendar_name,
                    uid,
                )
                continue
            target_href, new_etag = adopted
            logger.info(
                "push: %s/%s uid=%s adopted server etag (lost-response recovery)",
                account.name,
                calendar.calendar_name,
                uid,
            )
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


def _adopt_existing_remote(
    *,
    session: CalDAVSession,
    calendar: CalendarConfig,
    target_href: str,
    local_ics: bytes,
) -> tuple[str, str] | None:
    """Look up the resource at `target_href`; adopt its (href, etag)
    if its body matches `local_ics` byte-for-byte. Otherwise return
    None so the caller can defer to the next slow-path sync.
    """
    try:
        fetched = session.calendar_multiget(calendar.url, [target_href])
    except CalDAVError:
        return None
    if not fetched:
        return None
    local_hash = hashlib.sha256(local_ics).hexdigest()
    for href, etag, body in fetched:
        if hashlib.sha256(body).hexdigest() == local_hash:
            return href, etag
    return None


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
