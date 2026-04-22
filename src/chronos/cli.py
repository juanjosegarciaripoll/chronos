from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from chronos.config import ConfigError
from chronos.config import load as load_config
from chronos.credentials import CredentialResolutionError, DefaultCredentialsProvider
from chronos.domain import (
    AccountConfig,
    AppConfig,
    CalendarRef,
    ComponentRef,
    LocalStatus,
    ResourceRef,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.index_store import SqliteIndexRepository
from chronos.paths import default_config_path, default_index_path, user_data_dir
from chronos.protocols import (
    CalDAVSession,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from chronos.services import format_report, run_doctor
from chronos.storage import VdirMirrorRepository
from chronos.sync import sync_account

SessionFactory = Callable[[AccountConfig, str], CalDAVSession]
ContextFactory = Callable[[Path | None], "CliContext"]


@dataclass
class CliContext:
    config: AppConfig
    mirror: MirrorRepository
    index: IndexRepository
    creds: CredentialsProvider
    stdout: TextIO
    stderr: TextIO
    now: datetime
    session_factory: SessionFactory | None = None


def main(
    argv: Sequence[str] | None = None,
    *,
    context_factory: ContextFactory | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Only close the index when main() built the context itself — callers
    # that inject a factory own the lifecycle of their context.
    owns_context = context_factory is None
    factory = context_factory or _default_context_factory
    try:
        ctx = factory(args.config)
    except ConfigError as exc:
        sys.stderr.write(f"config error: {exc}\n")
        return 2
    try:
        return _dispatch(args, ctx)
    finally:
        if owns_context:
            ctx.index.close()


def _default_context_factory(config_path: Path | None) -> CliContext:
    path = config_path or default_config_path()
    config = load_config(path)
    mirror = VdirMirrorRepository(user_data_dir() / "mirror")
    index = SqliteIndexRepository(default_index_path())
    return CliContext(
        config=config,
        mirror=mirror,
        index=index,
        creds=DefaultCredentialsProvider(),
        stdout=sys.stdout,
        stderr=sys.stderr,
        now=datetime.now(UTC),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos", description="Terminal-first calendar client."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (defaults to platform user-config dir).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="Synchronise configured accounts with their servers.")

    list_p = sub.add_parser("list", help="List events and todos.")
    list_p.add_argument("--account", default=None)
    list_p.add_argument("--calendar", default=None)
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("--since", type=_parse_dt, default=None)
    list_p.add_argument("--until", type=_parse_dt, default=None)

    show_p = sub.add_parser("show", help="Show one component by UID.")
    show_p.add_argument("uid")

    add_p = sub.add_parser("add", help="Add a new VEVENT.")
    add_p.add_argument("--account", required=True)
    add_p.add_argument("--calendar", required=True)
    add_p.add_argument("--summary", required=True)
    add_p.add_argument("--start", type=_parse_dt, required=True)
    add_p.add_argument("--end", type=_parse_dt, default=None)
    add_p.add_argument("--uid", default=None)

    edit_p = sub.add_parser("edit", help="Edit an existing VEVENT (local-only in v1).")
    edit_p.add_argument("uid")
    edit_p.add_argument("--summary", default=None)
    edit_p.add_argument("--start", type=_parse_dt, default=None)
    edit_p.add_argument("--end", type=_parse_dt, default=None)

    rm_p = sub.add_parser("rm", help="Mark a component as trashed.")
    rm_p.add_argument("uid")

    sub.add_parser("doctor", help="Run diagnostics on the local state.")

    return parser


def _dispatch(args: argparse.Namespace, ctx: CliContext) -> int:
    command = str(args.command)
    if command == "sync":
        return cmd_sync(ctx)
    if command == "list":
        return cmd_list(
            ctx,
            account=args.account,
            calendar=args.calendar,
            limit=args.limit,
            since=args.since,
            until=args.until,
        )
    if command == "show":
        return cmd_show(ctx, uid=args.uid)
    if command == "add":
        return cmd_add(
            ctx,
            account_name=args.account,
            calendar_name=args.calendar,
            summary=args.summary,
            start=args.start,
            end=args.end,
            uid=args.uid,
        )
    if command == "edit":
        return cmd_edit(
            ctx,
            uid=args.uid,
            summary=args.summary,
            start=args.start,
            end=args.end,
        )
    if command == "rm":
        return cmd_rm(ctx, uid=args.uid)
    if command == "doctor":
        return cmd_doctor(ctx)
    ctx.stderr.write(f"unknown command: {command}\n")
    return 2


# Commands --------------------------------------------------------------------


def cmd_sync(ctx: CliContext) -> int:
    factory = ctx.session_factory or _default_session_factory
    fails = 0
    for account in ctx.config.accounts:
        try:
            password = ctx.creds.resolve(account.name, account.credential)
        except CredentialResolutionError as exc:
            ctx.stderr.write(f"[{account.name}] {exc}\n")
            fails += 1
            continue
        try:
            session = factory(account, password)
        except NotImplementedError as exc:
            ctx.stderr.write(f"[{account.name}] {exc}\n")
            fails += 1
            continue
        result = sync_account(
            account=account,
            session=session,
            mirror=ctx.mirror,
            index=ctx.index,
            now=ctx.now,
        )
        ctx.stdout.write(
            f"{account.name}: {result.calendars_synced} calendars "
            f"(+{result.components_added} "
            f"~{result.components_updated} "
            f"-{result.components_removed})\n"
        )
        for err in result.errors:
            ctx.stderr.write(f"[{account.name}] {err}\n")
    return 1 if fails else 0


def cmd_list(
    ctx: CliContext,
    *,
    account: str | None,
    calendar: str | None,
    limit: int,
    since: datetime | None,
    until: datetime | None,
) -> int:
    components = _collect_components(ctx, account=account, calendar=calendar)
    components = [c for c in components if c.local_status == LocalStatus.ACTIVE]
    if since is not None:
        components = [c for c in components if c.dtstart and c.dtstart >= since]
    if until is not None:
        components = [c for c in components if c.dtstart and c.dtstart < until]
    components.sort(key=_sort_key)
    for component in components[:limit]:
        ctx.stdout.write(_format_row(component) + "\n")
    return 0


def cmd_show(ctx: CliContext, *, uid: str) -> int:
    matches = _find_by_uid(ctx, uid)
    if not matches:
        ctx.stderr.write(f"not found: {uid}\n")
        return 1
    if len(matches) > 1:
        ctx.stderr.write(f"ambiguous uid {uid!r} matches multiple calendars:\n")
        for match in matches:
            ctx.stderr.write(f"  {match.ref.account_name}/{match.ref.calendar_name}\n")
        return 2
    _render_detail(matches[0], ctx.stdout)
    return 0


def cmd_add(
    ctx: CliContext,
    *,
    account_name: str,
    calendar_name: str,
    summary: str,
    start: datetime,
    end: datetime | None,
    uid: str | None,
) -> int:
    if not any(a.name == account_name for a in ctx.config.accounts):
        ctx.stderr.write(f"unknown account: {account_name}\n")
        return 2
    resolved_uid = uid or _generate_uid(
        account_name, calendar_name, summary, start, ctx.now
    )
    ics = _build_event_ics(resolved_uid, summary, start, end, ctx.now)
    ctx.mirror.write(ResourceRef(account_name, calendar_name, resolved_uid), ics)
    ref = ComponentRef(account_name, calendar_name, resolved_uid)
    component = VEvent(
        ref=ref,
        href=None,
        etag=None,
        raw_ics=ics,
        summary=summary,
        description=None,
        location=None,
        dtstart=start,
        dtend=end,
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )
    ctx.index.upsert_component(component)
    ctx.stdout.write(f"{resolved_uid}\n")
    return 0


def cmd_edit(
    ctx: CliContext,
    *,
    uid: str,
    summary: str | None,
    start: datetime | None,
    end: datetime | None,
) -> int:
    matches = _find_by_uid(ctx, uid)
    if not matches:
        ctx.stderr.write(f"not found: {uid}\n")
        return 1
    if len(matches) > 1:
        ctx.stderr.write(f"ambiguous uid {uid!r}\n")
        return 2
    current = matches[0]
    if not isinstance(current, VEvent):
        ctx.stderr.write("edit: only VEVENT is supported in v1\n")
        return 2
    new_summary = summary if summary is not None else (current.summary or "")
    new_start = start if start is not None else current.dtstart
    new_end = end if end is not None else current.dtend
    if new_start is None:
        ctx.stderr.write("edit: missing DTSTART\n")
        return 2
    new_ics = _build_event_ics(
        current.ref.uid, new_summary, new_start, new_end, ctx.now
    )
    ctx.mirror.write(current.ref.resource, new_ics)
    updated = VEvent(
        ref=current.ref,
        href=current.href,
        etag=current.etag,
        raw_ics=new_ics,
        summary=new_summary,
        description=current.description,
        location=current.location,
        dtstart=new_start,
        dtend=new_end,
        status=current.status,
        local_flags=current.local_flags,
        server_flags=current.server_flags,
        local_status=current.local_status,
        trashed_at=current.trashed_at,
        synced_at=current.synced_at,
    )
    ctx.index.upsert_component(updated)
    ctx.stdout.write(f"{current.ref.uid}\n")
    if current.href is not None:
        ctx.stderr.write(
            "warning: local-only edit; server push of edits is a v2 feature.\n"
        )
    return 0


def cmd_rm(ctx: CliContext, *, uid: str) -> int:
    matches = _find_by_uid(ctx, uid)
    if not matches:
        ctx.stderr.write(f"not found: {uid}\n")
        return 1
    for component in matches:
        trashed = _trashed_copy(component, trashed_at=ctx.now)
        ctx.index.upsert_component(trashed)
    ctx.stdout.write(f"trashed {len(matches)}\n")
    return 0


def cmd_doctor(ctx: CliContext) -> int:
    report = run_doctor(
        config=ctx.config,
        mirror=ctx.mirror,
        index=ctx.index,
        creds=ctx.creds,
    )
    ctx.stdout.write(format_report(report))
    return report.exit_code


# Helpers ---------------------------------------------------------------------


def _default_session_factory(account: AccountConfig, password: str) -> CalDAVSession:
    del account, password
    raise NotImplementedError(
        "sync: real CalDAV HTTP client is deferred. Inject a session_factory "
        "into cli.main() or use FakeCalDAVSession via the Python API."
    )


def _collect_components(
    ctx: CliContext, *, account: str | None, calendar: str | None
) -> list[StoredComponent]:
    out: list[StoredComponent] = []
    for acct in ctx.config.accounts:
        if account is not None and acct.name != account:
            continue
        for cal_name in ctx.mirror.list_calendars(acct.name):
            if calendar is not None and cal_name != calendar:
                continue
            out.extend(
                ctx.index.list_calendar_components(CalendarRef(acct.name, cal_name))
            )
    return out


def _find_by_uid(ctx: CliContext, uid: str) -> list[StoredComponent]:
    matches: list[StoredComponent] = []
    for acct in ctx.config.accounts:
        for cal_name in ctx.mirror.list_calendars(acct.name):
            ref = ComponentRef(acct.name, cal_name, uid)
            component = ctx.index.get_component(ref)
            if component is not None:
                matches.append(component)
    return matches


def _trashed_copy(
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


def _build_event_ics(
    uid: str,
    summary: str,
    dtstart: datetime,
    dtend: datetime | None,
    now: datetime,
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
    lines.extend(
        [
            f"SUMMARY:{_escape_text(summary)}",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _fmt_dt(dt: datetime) -> str:
    as_utc = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return as_utc.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape_text(value: str) -> str:
    # Minimal RFC 5545 text escaping for SUMMARY fields.
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def _generate_uid(
    account: str, calendar: str, summary: str, start: datetime, now: datetime
) -> str:
    payload = f"{account}|{calendar}|{summary}|{start.isoformat()}|{now.isoformat()}"
    digest = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{digest[:16]}@chronos"


def _parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _sort_key(component: StoredComponent) -> datetime:
    return component.dtstart or datetime.max.replace(tzinfo=UTC)


def _format_row(component: StoredComponent) -> str:
    start = component.dtstart.isoformat() if component.dtstart else "?"
    kind = "EVENT" if isinstance(component, VEvent) else "TODO "
    summary = component.summary or "(no summary)"
    return f"{start}  {kind}  {component.ref.uid:40s}  {summary}"


def _render_detail(component: StoredComponent, stdout: TextIO) -> None:
    stdout.write(f"UID: {component.ref.uid}\n")
    stdout.write(f"Account: {component.ref.account_name}\n")
    stdout.write(f"Calendar: {component.ref.calendar_name}\n")
    stdout.write(f"Kind: {'VEVENT' if isinstance(component, VEvent) else 'VTODO'}\n")
    stdout.write(f"Summary: {component.summary or ''}\n")
    if component.description:
        stdout.write(f"Description: {component.description}\n")
    if component.location:
        stdout.write(f"Location: {component.location}\n")
    if component.dtstart:
        stdout.write(f"Start: {component.dtstart.isoformat()}\n")
    if isinstance(component, VEvent) and component.dtend:
        stdout.write(f"End: {component.dtend.isoformat()}\n")
    if isinstance(component, VTodo) and component.due:
        stdout.write(f"Due: {component.due.isoformat()}\n")
    if component.status:
        stdout.write(f"Status: {component.status}\n")
    stdout.write(f"LocalStatus: {component.local_status.value}\n")
    if component.href:
        stdout.write(f"Href: {component.href}\n")
    if component.etag:
        stdout.write(f"ETag: {component.etag}\n")


__all__ = [
    "CliContext",
    "SessionFactory",
    "cmd_add",
    "cmd_doctor",
    "cmd_edit",
    "cmd_list",
    "cmd_rm",
    "cmd_show",
    "cmd_sync",
    "main",
]
